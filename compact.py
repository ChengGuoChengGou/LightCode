"""
CodePilot T4: Context Compactor
基于MiniCode compact.ts 的Python移植

功能:
- 从对话尾部扫描，保留最近消息，压缩旧消息为摘要
- 用LLM生成结构化摘要(Primary Request/Key Decisions/Files Modified/Errors/Current State/Pending Tasks)
- 自动/手动触发上下文压缩

常量来自MiniCode constants.ts + prompt.ts
"""

import re
import json
import logging
from dataclasses import dataclass
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# === Constants (from MiniCode constants.ts) ===

THRESHOLDS = {
    'MICROCOMPACT_UTILIZATION': 0.50,
    'AUTOCOMPACT_UTILIZATION': 0.85,
    'BLOCKED_UTILIZATION': 0.95,
}

SNIP_COMPACT_THRESHOLD = 0.70
SNIP_TARGET_USAGE = 0.60
SNIP_MIN_MESSAGES_TO_REMOVE = 6
SNIP_KEEP_RECENT_MESSAGES = 12
SNIP_MIN_TOKENS_TO_FREE = 2000

CONTEXT_COLLAPSE_UTILIZATION = 0.75
CONTEXT_COLLAPSE_TARGET_USAGE = 0.65
CONTEXT_COLLAPSE_KEEP_RECENT_MESSAGES = 12
CONTEXT_COLLAPSE_MIN_TOKENS_TO_SAVE = 2000
CONTEXT_COLLAPSE_MAX_SPANS_PER_PASS = 2
CONTEXT_COLLAPSE_MAX_FAILURES = 3

RETENTION = {
    'KEEP_RECENT_TOOL_RESULTS': 3,
    'MIN_KEEP_MESSAGES': 6,
    'MIN_KEEP_TOKENS': 10000,
    'MAX_KEEP_TOKENS': 40000,
}

LIMITS = {
    'MAX_AUTOCOMPACT_FAILURES': 3,
    'SUMMARY_MAX_OUTPUT_TOKENS': 4096,
    'PTL_MAX_RETRIES': 2,
    'MIN_EFFECTIVE_INPUT_FOR_AUTOCOMPACT': 20000,
}


# === Token Estimation ===

def estimate_tokens(messages: List[dict]) -> int:
    """粗略估算消息列表的token数 (≈1 token per 4 chars)"""
    total_chars = 0
    for msg in messages:
        content = msg.get('content', '')
        if isinstance(content, str):
            total_chars += len(content)
        role = msg.get('role', '')
        total_chars += len(role) + 10  # overhead per message
    return max(1, total_chars // 4)


def estimate_single_tokens(msg: dict) -> int:
    """估算单条消息token数"""
    content = msg.get('content', '')
    if isinstance(content, str):
        return max(1, len(content) // 4 + 10)
    return 10


# === Message Grouping (API rounds) ===

def group_messages_by_api_round(messages: List[dict]) -> List[List[dict]]:
    """按API轮次分组消息(assistant_tool_call + tool_result 同组)"""
    groups = []
    i = 0
    while i < len(messages):
        group = []
        cursor = i

        # assistant_thinking
        if cursor < len(messages) and messages[cursor].get('role') == 'assistant_thinking':
            group.append(messages[cursor])
            cursor += 1

        # assistant_tool_call
        while cursor < len(messages) and messages[cursor].get('role') == 'assistant_tool_call':
            group.append(messages[cursor])
            cursor += 1

        # tool_result
        while cursor < len(messages) and messages[cursor].get('role') == 'tool_result':
            group.append(messages[cursor])
            cursor += 1

        if any(m.get('role') in ('assistant_tool_call', 'tool_result') for m in group):
            groups.append(group)
            i = cursor
        else:
            groups.append([messages[i]])
            i += 1

    return groups


def align_boundary_to_api_round(messages: List[dict], boundary: int) -> int:
    """将边界对齐到API轮次"""
    start = 0
    for group in group_messages_by_api_round(messages):
        end = start + len(group)
        if boundary > start and boundary < end:
            return start
        start = end
    return boundary


# === Retention Boundary ===

def find_retention_boundary(messages: List[dict]) -> int:
    """从尾部扫描，确定保留边界"""
    token_sum = 0
    boundary = len(messages)

    for i in range(len(messages) - 1, 0, -1):
        msg_tokens = estimate_single_tokens(messages[i])

        if token_sum + msg_tokens > RETENTION['MAX_KEEP_TOKENS']:
            break

        token_sum += msg_tokens
        boundary = i

    # 确保至少保留 MIN_KEEP_MESSAGES
    min_boundary = max(1, len(messages) - RETENTION['MIN_KEEP_MESSAGES'])
    boundary = min(boundary, min_boundary)

    # 如果几乎保留了全部，强制保留 MIN_KEEP_MESSAGES
    if boundary <= 1 and len(messages) > RETENTION['MIN_KEEP_MESSAGES'] + 1:
        boundary = max(1, len(messages) - RETENTION['MIN_KEEP_MESSAGES'])

    return align_boundary_to_api_round(messages, boundary)


# === Summary Prompt (from MiniCode prompt.ts) ===

def build_compact_summary_prompt(conversation_text: str) -> str:
    """构建摘要提示词"""
    return f"""You are summarizing a conversation for context compression.
Produce a structured summary in <summary> tags.

Sections:
1. Primary Request — What the user asked for
2. Key Decisions — Important choices made
3. Files Modified — Which files were changed and why
4. Errors Encountered — Problems hit and how they were resolved
5. Current State — Where things stand right now
6. Pending Tasks — What still needs to be done

Rules:
- Be concise but preserve actionable details (file paths, command outputs, error messages)
- Use <analysis> tags as scratchpad, then <summary> tags for final output
- The summary will replace all messages before the recent tail

Conversation to summarize:

{conversation_text}"""


def parse_summary_from_response(response: str) -> Optional[str]:
    """从LLM响应中解析摘要"""
    match = re.search(r'<summary>([\s\S]*?)</summary>', response)
    if match and match.group(1).strip():
        return match.group(1).strip()

    match = re.search(r'<analysis>([\s\S]*?)</analysis>', response)
    if not match:
        trimmed = response.strip()
        if trimmed:
            return trimmed

    return None


# === Message Serialization ===

def messages_to_text(messages: List[dict]) -> str:
    """将消息列表转为文本供摘要使用"""
    parts = []
    for msg in messages:
        role = msg.get('role', '')
        content = msg.get('content', '')

        if role == 'user':
            parts.append(f"[User]: {content}")
        elif role in ('assistant', 'assistant_progress'):
            parts.append(f"[Assistant]: {content}")
        elif role == 'assistant_thinking':
            parts.append('[Assistant Thinking]: preserved provider reasoning block')
        elif role == 'assistant_tool_call':
            tool_name = msg.get('toolName', 'unknown')
            tool_input = json.dumps(msg.get('input', {}), ensure_ascii=False)
            parts.append(f"[Tool Call: {tool_name}]: {tool_input}")
        elif role == 'tool_result':
            tool_name = msg.get('toolName', 'unknown')
            is_error = msg.get('isError', False)
            trunc_content = content[:500] + '... (truncated)' if len(content) > 500 else content
            parts.append(f"[Tool Result: {tool_name}{' ERROR' if is_error else ''}]: {trunc_content}")
        elif role == 'context_summary':
            parts.append(f"[Previous Summary]: {content}")

    return '\n\n'.join(parts)


# === Main Compact Function ===

@dataclass
class CompressionResult:
    """压缩结果"""
    messages: List[dict]
    summary_content: str
    removed_count: int
    tokens_before: int
    tokens_after: int


def compact_conversation(
    messages: List[dict],
    llm_call_fn=None,
) -> Optional[CompressionResult]:
    """
    压缩对话历史
    
    Args:
        messages: 消息列表 (每个dict至少有role和content)
        llm_call_fn: LLM调用函数 fn(prompt: str) -> str
                    如果为None，返回None(无法压缩)
    
    Returns:
        CompressionResult 或 None(无需/无法压缩)
    """
    if len(messages) <= 2:
        return None

    tokens_before = estimate_tokens(messages)

    system_messages = [m for m in messages if m.get('role') == 'system']
    non_system_messages = [m for m in messages if m.get('role') != 'system']

    if len(non_system_messages) <= RETENTION['MIN_KEEP_MESSAGES']:
        return None

    boundary = find_retention_boundary(messages)
    messages_to_compress = messages[1:boundary]
    messages_to_keep = messages[boundary:]

    if not messages_to_compress:
        return None

    # 生成摘要
    if llm_call_fn is None:
        logger.warning("No LLM call function provided, cannot generate summary")
        return None

    conversation_text = messages_to_text(messages_to_compress)
    summary_prompt = build_compact_summary_prompt(conversation_text)

    try:
        response = llm_call_fn(summary_prompt)
        if not response or not response.strip():
            return None

        summary_content = parse_summary_from_response(response)
        if not summary_content:
            return None

        # 构建摘要消息
        summary_message = {
            'role': 'context_summary',
            'content': summary_content,
            'compressed_count': len(messages_to_compress),
        }

        new_messages = system_messages + [summary_message] + messages_to_keep
        tokens_after = estimate_tokens(new_messages)

        logger.info(f"Compact: {len(messages_to_compress)} msgs → summary, "
                    f"tokens {tokens_before} → {tokens_after}")

        return CompressionResult(
            messages=new_messages,
            summary_content=summary_content,
            removed_count=len(messages_to_compress),
            tokens_before=tokens_before,
            tokens_after=tokens_after,
        )

    except Exception as e:
        logger.error(f"Compact failed: {e}")
        return None


# === Auto-compact Check ===

def should_auto_compact(messages: List[dict], max_context_tokens: int = 128000) -> Tuple[bool, float]:
    """
    检查是否应该自动触发压缩
    
    Returns:
        (should_compact, utilization_ratio)
    """
    tokens = estimate_tokens(messages)
    utilization = tokens / max_context_tokens if max_context_tokens > 0 else 0

    if utilization >= THRESHOLDS['BLOCKED_UTILIZATION']:
        logger.warning(f"Context blocked! Utilization: {utilization:.1%}")
        return True, utilization

    if utilization >= THRESHOLDS['AUTOCOMPACT_UTILIZATION']:
        logger.info(f"Auto-compact triggered. Utilization: {utilization:.1%}")
        return True, utilization

    return False, utilization


# === Snip Compact (for very long tool results) ===

def snip_compact(messages: List[dict], max_context_tokens: int = 128000) -> Optional[List[dict]]:
    """
    Snip compact: 截断过长的tool_result内容
    
    当context占用>70%时，截断早期tool_result到500字符
    """
    tokens = estimate_tokens(messages)
    utilization = tokens / max_context_tokens if max_context_tokens > 0 else 0

    if utilization < SNIP_COMPACT_THRESHOLD:
        return None

    target_tokens = int(max_context_tokens * SNIP_TARGET_USAGE)
    tokens_to_free = tokens - target_tokens

    if tokens_to_free < SNIP_MIN_TOKENS_TO_FREE:
        return None

    new_messages = list(messages)
    freed = 0

    # 从前面开始截断tool_result (保留最近的)
    keep_recent = SNIP_KEEP_RECENT_MESSAGES
    for i in range(len(new_messages) - keep_recent):
        msg = new_messages[i]
        if msg.get('role') == 'tool_result':
            content = msg.get('content', '')
            if len(content) > 500:
                old_tokens = estimate_single_tokens(msg)
                new_messages[i] = {**msg, 'content': content[:500] + '\n... (snipped)'}
                new_tokens = estimate_single_tokens(new_messages[i])
                freed += old_tokens - new_tokens

                if freed >= tokens_to_free:
                    break

    if freed < SNIP_MIN_TOKENS_TO_FREE:
        return None

    logger.info(f"Snip compact: freed ~{freed} tokens")
    return new_messages


# === Integration Helper ===

class ContextCompactor:
    """上下文压缩器，集成到agent循环中"""
    
    def __init__(self, llm_call_fn=None, max_context_tokens=128000):
        self.llm_call_fn = llm_call_fn
        self.max_context_tokens = max_context_tokens
        self.compact_failures = 0
    
    def maybe_compact(self, messages: List[dict]) -> Optional[List[dict]]:
        """
        检查并可能压缩消息列表
        
        Returns:
            压缩后的消息列表，或None(无需压缩)
        """
        should, utilization = should_auto_compact(messages, self.max_context_tokens)
        
        if not should:
            return None
        
        if self.compact_failures >= LIMITS['MAX_AUTOCOMPACT_FAILURES']:
            logger.warning("Max compact failures reached, trying snip instead")
            return snip_compact(messages, self.max_context_tokens)
        
        result = compact_conversation(messages, self.llm_call_fn)
        if result:
            self.compact_failures = 0
            return result.messages
        else:
            self.compact_failures += 1
            # fallback to snip
            return snip_compact(messages, self.max_context_tokens)
    
    def manual_compact(self, messages: List[dict]) -> Optional[CompressionResult]:
        """手动触发压缩"""
        return compact_conversation(messages, self.llm_call_fn)
