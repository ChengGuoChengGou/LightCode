import json, re, os, time, logging
from dataclasses import dataclass
from typing import Any, Optional
try: from plugins.hooks import trigger as _hook
except ImportError: _hook = lambda *a, **k: None
try: from compact import ContextCompactor, snip_compact, estimate_tokens
except ImportError: ContextCompactor = None; snip_compact = None; estimate_tokens = None
try: from session_manager import SessionManager
except ImportError: SessionManager = None

logger = logging.getLogger(__name__)

# ─── T1: Agent循环恢复机制 (from MiniCode agent-loop.ts) ───
MAX_EMPTY_RETRIES = 2
BACKOFF_BASE = 1.0
BACKOFF_MAX = 30.0
RECOVERY_PROMPT = "Your previous response was empty or invalid. Please provide a valid response with tool calls or a final answer."
CONTEXT_OVERFLOW_PROMPT = "The conversation context is too long. Please summarize what you've done so far and continue from the key points."
@dataclass
class StepOutcome:
    data: Any
    next_prompt: Optional[str] = None
    should_exit: bool = False
def try_call_generator(func, *args, **kwargs):
    ret = func(*args, **kwargs)
    if hasattr(ret, '__iter__') and not isinstance(ret, (str, bytes, dict, list)): ret = yield from ret
    return ret

class BaseHandler:
    def turn_end_callback(self, response, tool_calls, tool_results, turn, next_prompt, exit_reason): return next_prompt
    def dispatch(self, tool_name, args, response, index=0, tool_num=1):
        method_name = f"do_{tool_name}"
        if hasattr(self, method_name):
            args['_index'] = index; args['_tool_num'] = tool_num
            _hook('tool_before', locals())
            ret = yield from try_call_generator(getattr(self, method_name), args, response)
            _hook('tool_after', locals())
            return ret
        elif tool_name == 'bad_json': return StepOutcome(None, next_prompt=args.get('msg', 'bad_json'), should_exit=False)
        else:
            yield f"未知工具: {tool_name}\n"
            return StepOutcome(None, next_prompt=f"未知工具 {tool_name}", should_exit=False)

def json_default(o): return list(o) if isinstance(o, set) else str(o)
def exhaust(g):
    try: 
        while True: next(g)
    except StopIteration as e: return e.value

def get_pretty_json(data):
    if isinstance(data, dict) and "script" in data:
        data = data.copy(); data["script"] = data["script"].replace("; ", ";\n  ")
    return json.dumps(data, indent=2, ensure_ascii=False).replace('\\n', '\n')

def agent_runner_loop(client, system_prompt, user_input, handler, tools_schema, 
                      max_turns=40, verbose=True, initial_user_content=None, yield_info=False):
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": initial_user_content if initial_user_content is not None else user_input}
    ]
    turn = 0;  handler.max_turns = max_turns
    empty_retry_count = 0  # T1: 空响应重试计数器
    rate_limit_backoff = BACKOFF_BASE  # T1: 限速退避计数器
    # T9: Session JSONL持久化 (from MiniCode session.ts)
    session_mgr = None
    if SessionManager:
        try: session_mgr = SessionManager(cwd=handler.cwd if hasattr(handler, 'cwd') else '.')
        except Exception: pass
    if session_mgr:
        session_mgr.add_message('user', user_input)
    _hook('agent_before', locals())
    while turn < handler.max_turns:
        turn += 1; turnstr = f'LLM Running (Turn {turn}) ...'
        if handler.parent.task_dir: turnstr = f'Turn {turn} ...'
        if verbose: turnstr = f'**{turnstr}**'
        if yield_info: yield {'turn': turn}
        yield f"\n\n{turnstr}\n\n"
        if turn%10 == 0: client.last_tools = ''  # 每10轮重置一次工具描述
        _hook('turn_before', locals())
        _hook('llm_before', locals())

        # ─── T1-Layer4: 限速恢复 (exponential backoff) ───
        try:
            response_gen = client.chat(messages=messages, tools=tools_schema)
        except Exception as e:
            err_msg = str(e).lower()
            if 'rate' in err_msg or '429' in err_msg or 'limit' in err_msg:
                wait = min(rate_limit_backoff, BACKOFF_MAX)
                logger.warning(f"Turn {turn}: 限速，等待{wait:.1f}s后重试")
                yield f"⏳ API限速，{wait:.1f}秒后重试...\n"
                time.sleep(wait)
                rate_limit_backoff *= 2
                turn -= 1  # 不消耗turn
                continue
            else:
                raise  # 非限速错误，向上抛出

        if verbose:
            response = yield from response_gen
            yield '\n\n'
        else:
            response = exhaust(response_gen)
            cleaned = _clean_content(response.content)
            if cleaned: yield cleaned + '\n'
        _hook('llm_after', locals())
        if session_mgr:
            session_mgr.add_message('assistant', response.content or '', {'tool_calls': [tc['tool_name'] for tc in tool_calls]} if tool_calls else {})

        # ─── T1-Layer1: 空响应恢复 ───
        if not response.tool_calls and not (response.content or '').strip():
            empty_retry_count += 1
            if empty_retry_count <= MAX_EMPTY_RETRIES:
                logger.warning(f"Turn {turn}: 空响应(第{empty_retry_count}次)，注入recovery_prompt重试")
                messages.append({"role": "assistant", "content": ""})
                messages.append({"role": "user", "content": RECOVERY_PROMPT})
                turn -= 1  # 不消耗turn
                continue
            else:
                logger.warning(f"Turn {turn}: 空响应超过{MAX_EMPTY_RETRIES}次，跳过")
                empty_retry_count = 0
        else:
            empty_retry_count = 0  # 正重置

        # ─── T1-Layer2: thinking截断恢复 ───
        if response.content and '<thinking>' in response.content and '</thinking>' not in response.content:
            logger.warning(f"Turn {turn}: thinking标签未闭合，截断并重试")
            trimmed = re.sub(r'<thinking>[\s\S]*$', '', response.content)
            messages.append({"role": "assistant", "content": trimmed})
            messages.append({"role": "user", "content": "Your thinking was truncated. Please continue from where you left off with a complete response."})
            turn -= 1
            continue

        # ─── T4+T8: Context Compactor with boundary markers (from MiniCode compact.ts) ───
        try:
            total_tokens = sum(estimate_tokens(m.get('content', '')) for m in messages) if estimate_tokens else sum(len(json.dumps(m.get('content', ''), ensure_ascii=False)) // 3 for m in messages)
            if total_tokens > 100000:
                logger.warning(f"Turn {turn}: 上下文过大(~{total_tokens}tokens)，执行snip_compact")
                if snip_compact:
                    before_count = len(messages)
                    messages = snip_compact(messages, max_context_tokens=100000)
                    after_count = len(messages)
                    # T8: Insert compact_boundary marker into history
                    compact_marker = {
                        "role": "system",
                        "content": f"[compact_boundary] 上下文已压缩: {before_count}→{after_count}条消息, ~{total_tokens}→压缩后tokens"
                    }
                    # Insert marker after system message (index 0) to track compression point
                    if len(messages) > 1:
                        messages.insert(1, compact_marker)
                    yield f"🗜️ 上下文已压缩 (~{total_tokens}→压缩后) tokens\n"
                else:
                    messages.append({"role": "user", "content": CONTEXT_OVERFLOW_PROMPT})
                turn -= 1
                continue
        except Exception as e:
            logger.warning(f"Compact failed: {e}")

        rate_limit_backoff = BACKOFF_BASE  # 成功调用后重置退避

        if not response.tool_calls: tool_calls = [{'tool_name': 'no_tool', 'args': {}}]
        else: tool_calls = [{'tool_name': tc.function.name, 'args': json.loads(tc.function.arguments), 'id': tc.id}
                          for tc in response.tool_calls]
       
        tool_results = []; next_prompts = set(); exit_reason = {}
        for ii, tc in enumerate(tool_calls):
            tool_name, args, tid = tc['tool_name'], tc['args'], tc.get('id', '')
            if session_mgr and tool_name != 'no_tool':
                session_mgr.add_tool_call(tool_name, args)
            if tool_name == 'no_tool': pass
            else: 
                if verbose: yield f"🛠️ Tool: `{tool_name}`  📥 args:\n````text\n{get_pretty_json(args)}\n````\n"
                else: yield f"🛠️ {tool_name}({_compact_tool_args(tool_name, args)})\n\n\n"
            handler.current_turn = turn
            gen = handler.dispatch(tool_name, args, response, index=ii, tool_num=len(tool_calls))
            try:
                v = next(gen)
                def proxy(): yield v; return (yield from gen)
                if verbose: yield '`````\n'
                outcome = (yield from proxy()) if verbose else exhaust(proxy())
                if verbose: yield '`````\n'
            except StopIteration as e: outcome = e.value
            
            if outcome.should_exit: 
                exit_reason = {'result': 'EXITED', 'data': outcome.data}; break
            if not outcome.next_prompt: 
                exit_reason = {'result': 'CURRENT_TASK_DONE', 'data': outcome.data}; break
            if outcome.next_prompt.startswith('未知工具'): client.last_tools = ''
            if outcome.data is not None and tool_name != 'no_tool': 
                datastr = json.dumps(outcome.data, ensure_ascii=False, default=json_default) if type(outcome.data) in [dict, list] else str(outcome.data) 
                tool_results.append({'tool_use_id': tid, 'content': datastr})
            next_prompts.add(outcome.next_prompt)
        if len(next_prompts) == 0 or exit_reason:
            if len(handler._done_hooks) == 0 or exit_reason.get('result', '') == 'EXITED': break
            next_prompts.add(handler._done_hooks.pop(0))
        next_prompt = handler.turn_end_callback(response, tool_calls, tool_results, turn, '\n'.join(next_prompts), exit_reason)
        _hook('turn_after', locals())
        messages = [{"role": "user", "content": next_prompt, "tool_results": tool_results}]   # just new message, history is kept in *Session

    # ─── T1-Layer5: 超限存档 (checkpoint on max_turns) ───
    result = exit_reason or {'result': 'MAX_TURNS_EXCEEDED'}
    if result.get('result') == 'MAX_TURNS_EXCEEDED':
        logger.warning(f"达到最大轮次({handler.max_turns})，生成checkpoint")
        checkpoint = {
            'turn': turn,
            'last_messages': messages[-3:] if len(messages) > 3 else messages,
            'reason': 'MAX_TURNS_EXCEEDED',
            'timestamp': time.time(),
        }
        try:
            cp_path = os.path.join(getattr(handler.parent, 'task_dir', '.') or '.', 'checkpoint.json')
            with open(cp_path, 'w') as f:
                json.dump(checkpoint, f, ensure_ascii=False, indent=2, default=json_default)
            yield f"\n⚠️ 达到最大轮次，checkpoint已保存: {cp_path}\n"
        except Exception as e:
            logger.error(f"Checkpoint保存失败: {e}")

    if exit_reason: handler.turn_end_callback(response, tool_calls, tool_results, turn, '', exit_reason)
    _hook('agent_after', locals())
    if session_mgr: session_mgr.finish()
    return result

def _clean_content(text):
    if not text: return ''
    def _shrink_code(m):
        lines = m.group(0).split('\n')
        lang = lines[0].replace('```','').strip()
        body = [l for l in lines[1:-1] if l.strip()]
        if len(body) <= 6: return m.group(0)
        preview = '\n'.join(body[:5])
        return f'```{lang}\n{preview}\n  ... ({len(body)} lines)\n```'
    text = re.sub(r'```[\s\S]*?```', _shrink_code, text)
    for p in [r'<file_content>[\s\S]*?</file_content>', r'<tool_(?:use|call)>[\s\S]*?</tool_(?:use|call)>', r'(\r?\n){3,}']:
        text = re.sub(p, '\n\n' if '\\n' in p else '', text)
    return text.strip()

def _compact_tool_args(name, args):
    a = {k: v for k, v in args.items() if k != '_index'}
    for k in ('path',): 
        if k in a: a[k] = os.path.basename(a[k])
    if name == 'update_working_checkpoint': s = a.get('key_info', ''); return (s[:60]+'...') if len(s)>60 else s
    if name == 'ask_user':
        q = str(a.get('question', ''))
        cs = a.get('candidates') or []
        if cs: q += '\ncandidates:\n' + '\n'.join(f'- {c}' for c in cs)
        return q
    s = json.dumps(a, ensure_ascii=False); return (s[:120]+'...') if len(s)>120 else s
