"""
Phase 2 T5: Session JSONL持久化
基于MiniCode session.ts 708行的SessionManager

MiniCode实现:
- JSONL格式逐行写入（非JSON单文件）
- UUID事件标识
- 事件类型: message/tool_call/compact_boundary/snip_boundary/error
- compact_boundary标记压缩点
- snip_boundary标记截断点
- 支持replay和断点恢复

GA适配:
- 保留JSONL格式（比JSON单文件更安全，崩溃不丢数据）
- 简化事件类型为GA需要的几种
- 与现有session JSON兼容
"""

import json
import os
import uuid
import time
import logging
from dataclasses import dataclass, asdict, field
from typing import List, Dict, Any, Optional, Literal, Iterator
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class SessionEvent:
    """Session事件"""
    id: str
    timestamp: float
    type: Literal[
        'message',           # 对话消息
        'tool_call',         # 工具调用
        'tool_result',       # 工具结果
        'compact_boundary',  # 压缩边界
        'snip_boundary',     # 截断边界
        'error',             # 错误
        'checkpoint',        # 检查点
        'turn_start',        # 轮次开始
        'turn_end',          # 轮次结束
    ]
    data: Dict[str, Any]
    metadata: Dict[str, Any] = field(default_factory=dict)


class SessionManager:
    """
    Session管理器 - JSONL格式
    
    对应MiniCode session.ts的SessionManager
    
    用法:
        sm = SessionManager(session_dir='sessions')
        sm.start('my-session-001')
        
        sm.add_message({'role': 'user', 'content': 'hello'})
        sm.add_tool_call('file_read', {'path': 'test.py'})
        sm.add_tool_result('file_read', 'content...')
        sm.add_compact_boundary(tokens_before=50000, tokens_after=30000)
        
        sm.finish()
    """
    
    def __init__(
        self,
        session_dir: str = 'sessions',
        max_events_per_file: int = 10000,
    ):
        self.session_dir = session_dir
        self.max_events_per_file = max_events_per_file
        self.session_id: Optional[str] = None
        self.file_path: Optional[str] = None
        self.event_count: int = 0
        self._file = None
    
    def start(self, session_id: Optional[str] = None) -> str:
        """
        开始新session
        
        Args:
            session_id: 可选的session ID，不提供则自动生成
        
        Returns:
            session_id
        """
        self.session_id = session_id or f"session_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
        
        # 创建session目录
        os.makedirs(self.session_dir, exist_ok=True)
        
        self.file_path = os.path.join(self.session_dir, f"{self.session_id}.jsonl")
        self.event_count = 0
        
        # 打开文件
        self._file = open(self.file_path, 'w', encoding='utf-8')
        
        # 写入session元数据
        self._write_event(SessionEvent(
            id=uuid.uuid4().hex,
            timestamp=time.time(),
            type='checkpoint',
            data={'action': 'session_start', 'session_id': self.session_id},
        ))
        
        logger.info(f"Session started: {self.session_id}")
        return self.session_id
    
    def finish(self):
        """结束session"""
        if self._file:
            self._write_event(SessionEvent(
                id=uuid.uuid4().hex,
                timestamp=time.time(),
                type='checkpoint',
                data={'action': 'session_end', 'event_count': self.event_count},
            ))
            self._file.close()
            self._file = None
            logger.info(f"Session finished: {self.session_id} ({self.event_count} events)")
    
    def _write_event(self, event: SessionEvent):
        """写入单个事件"""
        if not self._file:
            return
        
        line = json.dumps(asdict(event), ensure_ascii=False) + '\n'
        self._file.write(line)
        self._file.flush()
        self.event_count += 1
    
    def add_message(self, message: Dict[str, Any]):
        """添加对话消息"""
        self._write_event(SessionEvent(
            id=uuid.uuid4().hex,
            timestamp=time.time(),
            type='message',
            data=message,
        ))
    
    def add_tool_call(self, tool_name: str, arguments: Dict[str, Any], call_id: Optional[str] = None):
        """添加工具调用"""
        self._write_event(SessionEvent(
            id=uuid.uuid4().hex,
            timestamp=time.time(),
            type='tool_call',
            data={
                'tool_name': tool_name,
                'arguments': arguments,
                'call_id': call_id or uuid.uuid4().hex[:8],
            },
        ))
    
    def add_tool_result(self, tool_name: str, result: Any, call_id: Optional[str] = None):
        """添加工具结果"""
        self._write_event(SessionEvent(
            id=uuid.uuid4().hex,
            timestamp=time.time(),
            type='tool_result',
            data={
                'tool_name': tool_name,
                'result': str(result)[:5000],  # 截断过长结果
                'call_id': call_id,
            },
        ))
    
    def add_compact_boundary(self, tokens_before: int, tokens_after: int, summary: Optional[str] = None):
        """
        添加压缩边界标记
        
        对应MiniCode的compact_boundary事件
        """
        self._write_event(SessionEvent(
            id=uuid.uuid4().hex,
            timestamp=time.time(),
            type='compact_boundary',
            data={
                'tokens_before': tokens_before,
                'tokens_after': tokens_after,
                'summary': summary,
            },
        ))
    
    def add_snip_boundary(self, reason: str, truncated_at: int):
        """
        添加截断边界标记
        
        对应MiniCode的snip_boundary事件
        """
        self._write_event(SessionEvent(
            id=uuid.uuid4().hex,
            timestamp=time.time(),
            type='snip_boundary',
            data={
                'reason': reason,
                'truncated_at': truncated_at,
            },
        ))
    
    def add_error(self, error_type: str, message: str, details: Optional[Dict] = None):
        """添加错误事件"""
        self._write_event(SessionEvent(
            id=uuid.uuid4().hex,
            timestamp=time.time(),
            type='error',
            data={
                'error_type': error_type,
                'message': message,
                'details': details or {},
            },
        ))
    
    def add_turn_start(self, turn_number: int):
        """标记轮次开始"""
        self._write_event(SessionEvent(
            id=uuid.uuid4().hex,
            timestamp=time.time(),
            type='turn_start',
            data={'turn_number': turn_number},
        ))
    
    def add_turn_end(self, turn_number: int, duration_ms: int):
        """标记轮次结束"""
        self._write_event(SessionEvent(
            id=uuid.uuid4().hex,
            timestamp=time.time(),
            type='turn_end',
            data={'turn_number': turn_number, 'duration_ms': duration_ms},
        ))


class SessionReader:
    """
    Session读取器 - 用于回放和分析
    
    用法:
        reader = SessionReader('sessions/session_xxx.jsonl')
        for event in reader.iter_events():
            print(event.type, event.data)
        
        # 获取统计
        stats = reader.get_stats()
    """
    
    def __init__(self, file_path: str):
        self.file_path = file_path
    
    def iter_events(self) -> Iterator[SessionEvent]:
        """迭代所有事件"""
        with open(self.file_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    yield SessionEvent(**data)
                except json.JSONDecodeError as e:
                    logger.warning(f"跳过无效行: {e}")
    
    def get_stats(self) -> Dict[str, Any]:
        """获取session统计"""
        event_counts = {}
        first_ts = None
        last_ts = None
        
        for event in self.iter_events():
            event_counts[event.type] = event_counts.get(event.type, 0) + 1
            if first_ts is None:
                first_ts = event.timestamp
            last_ts = event.timestamp
        
        return {
            'file': self.file_path,
            'total_events': sum(event_counts.values()),
            'event_counts': event_counts,
            'duration_seconds': (last_ts - first_ts) if first_ts and last_ts else 0,
            'first_event': first_ts,
            'last_event': last_ts,
        }
    
    def get_messages(self) -> List[Dict[str, Any]]:
        """提取所有对话消息（用于replay）"""
        messages = []
        for event in self.iter_events():
            if event.type == 'message':
                messages.append(event.data)
        return messages
    
    def get_last_n_events(self, n: int = 10) -> List[SessionEvent]:
        """获取最后N个事件"""
        events = list(self.iter_events())
        return events[-n:]


def list_sessions(session_dir: str = 'sessions') -> List[Dict[str, Any]]:
    """列出所有session"""
    if not os.path.exists(session_dir):
        return []
    
    sessions = []
    for f in os.listdir(session_dir):
        if f.endswith('.jsonl'):
            path = os.path.join(session_dir, f)
            reader = SessionReader(path)
            stats = reader.get_stats()
            sessions.append({
                'session_id': f.replace('.jsonl', ''),
                'path': path,
                'events': stats['total_events'],
                'duration': stats['duration_seconds'],
                'modified': os.path.getmtime(path),
            })
    
    return sorted(sessions, key=lambda x: x['modified'], reverse=True)


# ─── 集成示例 ───
"""
在GA的agent_loop.py中集成:

from session_manager import SessionManager

# 在agent循环开始时:
session_mgr = SessionManager(session_dir='sessions')
session_id = session_mgr.start()

# 在每轮开始:
session_mgr.add_turn_start(turn)

# 在LLM响应后:
session_mgr.add_message({'role': 'assistant', 'content': response})

# 在工具调用时:
session_mgr.add_tool_call('file_read', {'path': 'test.py'})
session_mgr.add_tool_result('file_read', file_content)

# 在compact时:
session_mgr.add_compact_boundary(tokens_before=50000, tokens_after=30000)

# 在出错时:
session_mgr.add_error('rate_limit', 'API rate limited', {'retry_after': 60})

# 在agent循环结束时:
session_mgr.finish()
"""
