"""
Phase 1 T1: Agent循环恢复机制
基于MiniCode agent-loop.ts的5层恢复策略，移植到GA的agent_loop.py

MiniCode原文5层恢复:
1. 空响应恢复 → 发送recovery_prompt让模型重试
2. thinking截断恢复 → trim thinking blocks + 重试
3. 上下文坍缩恢复 → 调用compact压缩后重试
4. 限速恢复 → exponential backoff自动重试
5. 超限存档 → max_turns到达时保存checkpoint

GA适配:
- GA用OpenAI兼容API，无thinking blocks → 跳过thinking截断
- GA无compact机制 → Phase 2实现，此处预留接口
- GA已有session JSON → 基于此做checkpoint
"""

import time
import logging
from dataclasses import dataclass, field
from typing import Any, Optional, Callable

logger = logging.getLogger(__name__)

# ─── 配置常量 ───
MAX_RETRIES = 3              # 单轮最大重试次数
BACKOFF_BASE = 1.0           # 限速退避基数(秒)
BACKOFF_MAX = 30.0           # 限速退避上限(秒)
MAX_EMPTY_RETRIES = 2        # 空响应最大重试
MAX_CONSECUTIVE_FAILURES = 5 # 连续失败上限，超过则禁用恢复

RECOVERY_PROMPT = (
    "Your previous response was empty or invalid. "
    "Please provide a valid response with tool calls or a final answer."
)

CONTEXT_OVERFLOW_PROMPT = (
    "The conversation context is too long. "
    "Please summarize what you've done so far and continue from the key points."
)


@dataclass
class RecoveryState:
    """恢复状态追踪"""
    empty_retries: int = 0
    rate_limit_retries: int = 0
    consecutive_failures: int = 0
    context_overflow_count: int = 0
    disabled: bool = False
    last_error: Optional[str] = None


@dataclass  
class RecoveryResult:
    """恢复操作结果"""
    action: str  # 'retry' | 'skip' | 'abort' | 'compact' | 'checkpoint'
    message: Optional[str] = None
    delay: float = 0


class AgentRecovery:
    """
    Agent循环恢复管理器
    
    用法:
        recovery = AgentRecovery()
        # 在agent循环中:
        response = llm_call()
        result = recovery.handle_response(response, turn, max_turns)
        if result.action == 'retry':
            # 插入recovery_prompt后重试
            messages.append({"role": "user", "content": result.message})
            continue
        elif result.action == 'abort':
            break
    """
    
    def __init__(
        self,
        compact_fn: Optional[Callable] = None,
        checkpoint_fn: Optional[Callable[[int, Any], None]] = None,
        max_retries: int = MAX_RETRIES,
        verbose: bool = True,
    ):
        self.state = RecoveryState()
        self.compact_fn = compact_fn  # Phase 2: compact回调
        self.checkpoint_fn = checkpoint_fn
        self.max_retries = max_retries
        self.verbose = verbose
    
    def handle_response(self, response: Any, turn: int, max_turns: int) -> RecoveryResult:
        """
        处理LLM响应，决定恢复策略
        
        返回RecoveryResult:
        - action='retry': 重试，message是recovery prompt
        - action='continue': 正常继续
        - action='compact': 需要压缩上下文后重试
        - action='checkpoint': 保存存档后退出
        - action='abort': 终止循环
        """
        if self.state.disabled:
            return RecoveryResult(action='continue')
        
        # 检查空响应
        if self._is_empty_response(response):
            return self._handle_empty_response(turn)
        
        # 检查上下文溢出
        if self._is_context_overflow(response):
            return self._handle_context_overflow(turn)
        
        # 检查限速
        if self._is_rate_limited(response):
            return self._handle_rate_limit(turn)
        
        # 检查是否接近turn上限
        if turn >= max_turns - 1:
            return self._handle_max_turns(turn)
        
        # 成功，重置连续失败计数
        self.state.consecutive_failures = 0
        return RecoveryResult(action='continue')
    
    def _is_empty_response(self, response: Any) -> bool:
        """检测空响应（MiniCode: empty_response_recovery）"""
        if response is None:
            return True
        if hasattr(response, 'content') and hasattr(response, 'tool_calls'):
            # OpenAI ChatCompletion格式
            if not response.content and not response.tool_calls:
                return True
        return False
    
    def _is_context_overflow(self, response: Any) -> bool:
        """检测上下文溢出"""
        if hasattr(response, 'error'):
            err = str(response.error).lower()
            if any(kw in err for kw in ['context_length', 'context window', 'token limit', 'too long']):
                return True
        # 检查content中的错误消息
        if hasattr(response, 'content') and response.content:
            content_lower = str(response.content).lower()
            if 'context_length_exceeded' in content_lower:
                return True
        return False
    
    def _is_rate_limited(self, response: Any) -> bool:
        """检测限速（MiniCode: rate_limit_recovery）"""
        if hasattr(response, 'error'):
            err = str(response.error).lower()
            if any(kw in err for kw in ['rate_limit', '429', 'too many requests']):
                return True
        return False
    
    def _handle_empty_response(self, turn: int) -> RecoveryResult:
        """空响应恢复（MiniCode第101-112行）"""
        self.state.empty_retries += 1
        self.state.consecutive_failures += 1
        
        if self.state.empty_retries >= MAX_EMPTY_RETRIES:
            logger.warning(f"Turn {turn}: 空响应重试上限({MAX_EMPTY_RETRIES})，终止")
            self._check_disable()
            return RecoveryResult(action='abort', message='empty_response_limit')
        
        if self.verbose:
            logger.info(f"Turn {turn}: 空响应，插入recovery prompt重试 ({self.state.empty_retries}/{MAX_EMPTY_RETRIES})")
        
        return RecoveryResult(
            action='retry',
            message=RECOVERY_PROMPT,
        )
    
    def _handle_context_overflow(self, turn: int) -> RecoveryResult:
        """上下文溢出恢复（MiniCode第115-125行）"""
        self.state.context_overflow_count += 1
        self.state.consecutive_failures += 1
        
        if self.compact_fn:
            # Phase 2: 有compact机制，调用压缩
            if self.verbose:
                logger.info(f"Turn {turn}: 上下文溢出，调用compact压缩")
            try:
                self.compact_fn()
                return RecoveryResult(action='compact', message=CONTEXT_OVERFLOW_PROMPT)
            except Exception as e:
                logger.error(f"Compact失败: {e}")
        
        # 无compact机制，插入摘要prompt让模型自行总结
        if self.verbose:
            logger.info(f"Turn {turn}: 上下文溢出，请求模型自行总结继续")
        
        return RecoveryResult(
            action='retry',
            message=CONTEXT_OVERFLOW_PROMPT,
        )
    
    def _handle_rate_limit(self, turn: int) -> RecoveryResult:
        """限速恢复（MiniCode第126-135行，exponential backoff）"""
        self.state.rate_limit_retries += 1
        self.state.consecutive_failures += 1
        
        if self.state.rate_limit_retries >= self.max_retries:
            logger.warning(f"Turn {turn}: 限速重试上限({self.max_retries})，终止")
            self._check_disable()
            return RecoveryResult(action='abort', message='rate_limit_retries_exceeded')
        
        # exponential backoff
        delay = min(
            BACKOFF_BASE * (2 ** (self.state.rate_limit_retries - 1)),
            BACKOFF_MAX,
        )
        
        if self.verbose:
            logger.info(f"Turn {turn}: 限速，等待{delay:.1f}s后重试 ({self.state.rate_limit_retries}/{self.max_retries})")
        
        time.sleep(delay)
        return RecoveryResult(action='retry', delay=delay)
    
    def _handle_max_turns(self, turn: int) -> RecoveryResult:
        """超限存档（MiniCode第136-150行）"""
        if self.verbose:
            logger.info(f"Turn {turn}: 接近max_turns，保存checkpoint")
        
        if self.checkpoint_fn:
            try:
                self.checkpoint_fn(turn, None)
            except Exception as e:
                logger.error(f"Checkpoint保存失败: {e}")
        
        return RecoveryResult(action='checkpoint', message='max_turns_approaching')
    
    def _check_disable(self):
        """连续失败次数超限则禁用恢复（MiniCode: MAX_CONSECUTIVE_FAILURES）"""
        if self.state.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            self.state.disabled = True
            logger.warning(f"连续失败{MAX_CONSECUTIVE_FAILURES}次，禁用恢复机制")
    
    def reset(self):
        """重置恢复状态（新一轮对话时调用）"""
        self.state = RecoveryState()


# ─── 集成示例 ───
"""
在 agent_loop.py 的 agent_runner_loop 中集成:

from recovery import AgentRecovery

def agent_runner_loop(client, system_prompt, user_input, handler, tools_schema, 
                      max_turns=40, verbose=True, initial_user_content=None, yield_info=False):
    recovery = AgentRecovery(verbose=verbose)
    messages = [...]
    turn = 0
    
    while turn < handler.max_turns:
        turn += 1
        response = client.chat(messages=messages, tools=tools_schema)
        
        # ─── 新增: 恢复检查 ───
        recovery_result = recovery.handle_response(response, turn, handler.max_turns)
        if recovery_result.action == 'retry':
            messages.append({"role": "user", "content": recovery_result.message})
            continue
        elif recovery_result.action == 'compact':
            # Phase 2: compact后继续
            continue
        elif recovery_result.action in ('abort', 'checkpoint'):
            break
        # ─── 恢复检查结束 ───
        
        # ... 原有tool_calls处理逻辑 ...
"""
