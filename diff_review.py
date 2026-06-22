"""
Phase 1 T2: Diff审查机制
基于MiniCode file-review.ts的buildUnifiedDiff + permissions.ensureEdit

MiniCode流程 (file-review.ts 70行):
1. edit-file.ts调用buildUnifiedDiff(path, oldContent, newContent)
2. 生成unified diff格式
3. permissions.ensureEdit(path, diff) → 检查路径白名单+编辑审批
4. 审批通过才writeFile

GA适配:
- 在file_write/file_patch前插入diff生成
- 可选审批模式: 自动/人工确认
- 支持多个文件批量审查
"""

import difflib
import os
from dataclasses import dataclass
from typing import List, Optional, Literal


@dataclass
class FileDiff:
    """单个文件的diff信息"""
    path: str
    action: Literal['create', 'modify', 'delete']
    old_content: Optional[str]
    new_content: Optional[str]
    unified_diff: str
    lines_added: int
    lines_removed: int
    hunks: int


@dataclass
class DiffReviewResult:
    """审查结果"""
    approved: bool
    reason: str = ""
    diffs: List[FileDiff] = None


def build_unified_diff(
    path: str,
    old_content: Optional[str],
    new_content: Optional[str],
    context_lines: int = 3,
) -> FileDiff:
    """
    生成unified diff（对应MiniCode file-review.ts的buildUnifiedDiff）
    
    Args:
        path: 文件路径
        old_content: 原始内容（None表示新建）
        new_content: 新内容（None表示删除）
        context_lines: 上下文行数
    
    Returns:
        FileDiff对象
    """
    if old_content is None and new_content is None:
        raise ValueError("old_content和new_content不能同时为None")
    
    # 确定操作类型
    if old_content is None:
        action = 'create'
        old_lines = []
        new_lines = (new_content or '').splitlines(keepends=True)
    elif new_content is None:
        action = 'delete'
        old_lines = old_content.splitlines(keepends=True)
        new_lines = []
    else:
        action = 'modify'
        old_lines = old_content.splitlines(keepends=True)
        new_lines = new_content.splitlines(keepends=True)
    
    # 生成unified diff
    diff_lines = list(difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile=f'a/{path}',
        tofile=f'b/{path}',
        n=context_lines,
    ))
    
    unified_diff = ''.join(diff_lines)
    
    # 统计
    lines_added = sum(1 for line in diff_lines if line.startswith('+') and not line.startswith('+++'))
    lines_removed = sum(1 for line in diff_lines if line.startswith('-') and not line.startswith('---'))
    hunks = sum(1 for line in diff_lines if line.startswith('@@'))
    
    return FileDiff(
        path=path,
        action=action,
        old_content=old_content,
        new_content=new_content,
        unified_diff=unified_diff,
        lines_added=lines_added,
        lines_removed=lines_removed,
        hunks=hunks,
    )


def format_diff_summary(diffs: List[FileDiff]) -> str:
    """格式化diff摘要，用于日志或用户确认"""
    lines = []
    for diff in diffs:
        action_emoji = {'create': '🆕', 'modify': '📝', 'delete': '🗑️'}
        emoji = action_emoji.get(diff.action, '❓')
        lines.append(f"{emoji} {diff.path} (+{diff.lines_added}/-{diff.lines_removed})")
    return '\n'.join(lines)


def format_diff_detail(diffs: List[FileDiff], max_lines: int = 50) -> str:
    """格式化diff详情，用于完整审查"""
    parts = []
    for diff in diffs:
        parts.append(f"{'='*60}")
        parts.append(f"File: {diff.path} ({diff.action})")
        parts.append(f"+{diff.lines_added} -{diff.lines_removed} ({diff.hunks} hunks)")
        parts.append(f"{'='*60}")
        if diff.unified_diff:
            # 限制输出行数
            diff_lines = diff.unified_diff.splitlines()
            if len(diff_lines) > max_lines:
                parts.extend(diff_lines[:max_lines])
                parts.append(f"... ({len(diff_lines) - max_lines} more lines)")
            else:
                parts.append(diff.unified_diff)
        parts.append('')
    return '\n'.join(parts)


class DiffReviewer:
    """
    Diff审查器
    
    审查模式:
    - 'auto': 自动批准所有diff
    - 'confirm': 需要用户确认（TUI模式）
    - 'log_only': 只记录日志不阻断
    - 'dry_run': 只生成diff不执行
    
    用法:
        reviewer = DiffReviewer(mode='auto')
        
        # 在file_write前:
        diff = build_unified_diff(path, old_content, new_content)
        result = reviewer.review([diff])
        if result.approved:
            # 执行写入
            write_file(path, new_content)
    """
    
    def __init__(
        self,
        mode: Literal['auto', 'confirm', 'log_only', 'dry_run'] = 'auto',
        max_diff_lines: int = 1000,
        confirm_fn: Optional[callable] = None,
    ):
        self.mode = mode
        self.max_diff_lines = max_diff_lines
        self.confirm_fn = confirm_fn  # 自定义确认函数
        self.history: List[FileDiff] = []
    
    def review(self, diffs: List[FileDiff]) -> DiffReviewResult:
        """
        审查diff列表
        
        Returns:
            DiffReviewResult: approved=True表示允许执行
        """
        # 记录历史
        self.history.extend(diffs)
        
        # 检查diff大小
        for diff in diffs:
            total_lines = diff.lines_added + diff.lines_removed
            if total_lines > self.max_diff_lines:
                return DiffReviewResult(
                    approved=False,
                    reason=f"Diff过大: {diff.path} ({total_lines} lines > {self.max_diff_lines})",
                    diffs=diffs,
                )
        
        # 根据模式决定
        if self.mode == 'auto':
            return DiffReviewResult(approved=True, diffs=diffs)
        
        elif self.mode == 'dry_run':
            return DiffReviewResult(
                approved=False,
                reason="dry_run模式，仅生成diff不执行",
                diffs=diffs,
            )
        
        elif self.mode == 'log_only':
            return DiffReviewResult(approved=True, diffs=diffs)
        
        elif self.mode == 'confirm':
            if self.confirm_fn:
                approved = self.confirm_fn(diffs)
                return DiffReviewResult(
                    approved=approved,
                    reason="用户确认" if approved else "用户拒绝",
                    diffs=diffs,
                )
            else:
                # 无确认函数，默认通过
                return DiffReviewResult(approved=True, diffs=diffs)
        
        return DiffReviewResult(approved=False, reason=f"未知审查模式: {self.mode}", diffs=diffs)


# ─── 集成示例 ───
"""
在GA的file_write/file_patch工具中集成:

from diff_review import build_unified_diff, DiffReviewer

reviewer = DiffReviewer(mode='auto')  # 或 'confirm' 需要用户确认

# file_write工具:
def do_file_write(self, args, response):
    path = args['path']
    new_content = args['content']
    
    # 读取原内容
    try:
        with open(path, 'r') as f:
            old_content = f.read()
    except FileNotFoundError:
        old_content = None
    
    # 生成diff
    diff = build_unified_diff(path, old_content, new_content)
    
    # 审查
    result = reviewer.review([diff])
    if not result.approved:
        yield f"❌ 写入被拒绝: {result.reason}"
        return StepOutcome(None, next_prompt=result.reason)
    
    # 执行写入
    with open(path, 'w') as f:
        f.write(new_content)
    
    yield f"✅ 写入成功: {path} (+{diff.lines_added}/-{diff.lines_removed})"
    return StepOutcome({'path': path, 'lines_added': diff.lines_added})
"""
