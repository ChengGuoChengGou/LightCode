"""
CodePilot T3: 权限系统
基于MiniCode permissions.ts PermissionManager

功能:
- 路径白名单/黑名单 (deny/allow)
- 命令白名单/黑名单
- 文件编辑模式控制 (allowed_edit_patterns)
- 持久化: permissions.json
"""

import json
import os
from dataclasses import dataclass, field, asdict
from typing import List, Optional, Literal
from pathlib import Path
from fnmatch import fnmatch
import logging

logger = logging.getLogger(__name__)


@dataclass
class PermissionConfig:
    """权限配置"""
    allowed_read_dirs: List[str] = field(default_factory=list)
    allowed_write_dirs: List[str] = field(default_factory=list)
    denied_paths: List[str] = field(default_factory=list)
    allowed_commands: List[str] = field(default_factory=list)
    denied_commands: List[str] = field(default_factory=list)
    allowed_edit_patterns: List[str] = field(default_factory=lambda: [
        '*.py', '*.js', '*.ts', '*.json', '*.md', '*.txt', '*.yaml', '*.yml', '*.toml'
    ])
    denied_edit_patterns: List[str] = field(default_factory=lambda: [
        '*.exe', '*.dll', '*.so', '*.dylib', '*.bin'
    ])
    allow_write_to_cwd: bool = True
    allow_home_dir: bool = False
    allow_temp_dir: bool = True


class PermissionManager:
    """权限管理器"""
    
    def __init__(self, cwd: str = '.', config: Optional[PermissionConfig] = None, config_path: Optional[str] = None):
        self.cwd = os.path.abspath(cwd)
        self.config = config or PermissionConfig()
        self.config_path = config_path
        self._init_defaults()
        if config_path and os.path.exists(config_path):
            self._load_config(config_path)
    
    def _init_defaults(self):
        if self.config.allow_write_to_cwd:
            if self.cwd not in self.config.allowed_write_dirs:
                self.config.allowed_write_dirs.append(self.cwd)
        if self.config.allow_temp_dir:
            temp_dir = os.path.abspath(os.path.join(self.cwd, 'temp'))
            if temp_dir not in self.config.allowed_write_dirs:
                self.config.allowed_write_dirs.append(temp_dir)
    
    def _load_config(self, path: str):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            for key, value in data.items():
                if hasattr(self.config, key):
                    setattr(self.config, key, value)
            logger.info(f"已加载权限配置: {path}")
        except Exception as e:
            logger.warning(f"加载权限配置失败: {e}")
    
    def save_config(self, path: Optional[str] = None):
        path = path or self.config_path
        if not path:
            return
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(asdict(self.config), f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"保存权限配置失败: {e}")
    
    def _normalize_path(self, path: str) -> str:
        return os.path.abspath(os.path.expanduser(path))
    
    def _is_in_directory(self, path: str, directory: str) -> bool:
        try:
            return os.path.commonpath([path, directory]) == directory
        except ValueError:
            return False
    
    def _match_pattern(self, path: str, pattern: str) -> bool:
        return fnmatch(os.path.basename(path), pattern)
    
    def ensure_path_access(self, path: str, mode: Literal['read', 'write', 'list', 'search']) -> str:
        normalized = self._normalize_path(path)
        for denied in self.config.denied_paths:
            if self._is_in_directory(normalized, self._normalize_path(denied)):
                raise PermissionError(f"路径在黑名单中: {path}")
        if mode in ('read', 'list', 'search'):
            return normalized
        if mode == 'write':
            for allowed in self.config.allowed_write_dirs:
                if self._is_in_directory(normalized, self._normalize_path(allowed)):
                    return normalized
            for allowed in self.config.allowed_read_dirs:
                if self._is_in_directory(normalized, self._normalize_path(allowed)):
                    raise PermissionError(f"路径只允许读取: {path}")
            raise PermissionError(f"路径不在允许写入的目录中: {path}")
        raise ValueError(f"未知访问模式: {mode}")
    
    def ensure_command_access(self, command: str) -> None:
        cmd_name = command.split()[0] if command else ''
        for denied in self.config.denied_commands:
            if cmd_name == denied or command.startswith(denied):
                raise PermissionError(f"命令被禁止: {command}")
        if self.config.allowed_commands:
            for allowed in self.config.allowed_commands:
                if cmd_name == allowed or command.startswith(allowed):
                    return
            raise PermissionError(f"命令不在白名单中: {command}")
    
    def ensure_edit(self, path: str, diff: Optional[str] = None) -> None:
        self.ensure_path_access(path, 'write')
        basename = os.path.basename(path)
        for pattern in self.config.denied_edit_patterns:
            if self._match_pattern(basename, pattern):
                raise PermissionError(f"文件类型不允许编辑: {path}")
        if self.config.allowed_edit_patterns:
            for pattern in self.config.allowed_edit_patterns:
                if self._match_pattern(basename, pattern):
                    return
            raise PermissionError(f"文件类型不在允许编辑列表: {path}")
