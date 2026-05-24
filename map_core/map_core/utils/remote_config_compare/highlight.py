"""
终端字符级差异高亮。

用法:
    from map_core.utils.remote_config_compare.highlight import highlight_inline_diff, format_diff_hunk

    left_hl, right_hl = highlight_inline_diff(left_str, right_str)
    # 长字符串用 hunk 模式:
    left_hl, right_hl = format_diff_hunk(left_str, right_str)
"""
from __future__ import annotations

from difflib import SequenceMatcher

# ANSI 颜色码
_RED = "\033[91m"      # 亮红色: 标记 left 中删除/变更的字符
_GREEN = "\033[92m"    # 亮绿色: 标记 right 中新增/变更的字符
_RESET = "\033[0m"     # 重置颜色

# 可调参数
_CONTEXT = 30           # 每个差异块前后保留的上下文字符数
_LONG_THRESHOLD = 100   # 超过此长度自动使用 hunk 模式(截断输出)
_MAX_HUNK_LEN = 200     # hunk 超过此长度时进一步截断到第一个差异处


def highlight_inline_diff(left: str, right: str) -> tuple[str, str]:
    """
    对两个字符串的每个差异字符进行高亮。

    使用 SequenceMatcher 的 opcodes 从左到右遍历:
    - equal(相同): 原样输出
    - delete(仅 left 有): 红色高亮
    - insert(仅 right 有): 绿色高亮
    - replace(两边都有但不同): left 红色, right 绿色

    适合短字符串, 可以看到完整文本。

    Args:
        left:  "旧"字符串
        right: "新"字符串

    Returns:
        (高亮后的 left, 高亮后的 right)
    """
    if not isinstance(left, str) or not isinstance(right, str):
        return repr(left), repr(right)

    sm = SequenceMatcher(None, left, right)
    left_parts: list[str] = []
    right_parts: list[str] = []

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            left_parts.append(left[i1:i2])
            right_parts.append(right[j1:j2])
        elif tag == "delete":
            left_parts.append(f"{_RED}{left[i1:i2]}{_RESET}")
        elif tag == "insert":
            right_parts.append(f"{_GREEN}{right[j1:j2]}{_RESET}")
        elif tag == "replace":
            left_parts.append(f"{_RED}{left[i1:i2]}{_RESET}")
            right_parts.append(f"{_GREEN}{right[j1:j2]}{_RESET}")

    return "".join(left_parts), "".join(right_parts)


def format_diff_hunk(left: str, right: str) -> tuple[str, str]:
    """
    只显示变更区域及少量上下文, 类似 git diff 的 hunk。

    对于很长的相同部分, 会折叠成 "..." + 末尾 _CONTEXT 个字符,
    让你聚焦于实际改动的地方。

    如果结果仍超过 _MAX_HUNK_LEN, 会进一步截断到第一个差异处
    (调用 _trim_to_first_diff)。

    适合长字符串(如 prompt), 避免完整输出刷满终端。

    Args:
        left:  "旧"字符串
        right: "新"字符串

    Returns:
        (截断高亮后的 left, 截断高亮后的 right)
    """
    if not isinstance(left, str) or not isinstance(right, str):
        return repr(left), repr(right)

    sm = SequenceMatcher(None, left, right)
    left_hunks: list[str] = []
    right_hunks: list[str] = []

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            ctx = left[i1:i2]
            # 折叠长的相同部分, 只保留尾部上下文
            if len(ctx) > _CONTEXT * 2:
                ctx = "..." + ctx[-_CONTEXT:]
            left_hunks.append(ctx)
            right_hunks.append(ctx)
        elif tag == "delete":
            left_hunks.append(f"{_RED}{left[i1:i2]}{_RESET}")
        elif tag == "insert":
            right_hunks.append(f"{_GREEN}{right[j1:j2]}{_RESET}")
        elif tag == "replace":
            left_hunks.append(f"{_RED}{left[i1:i2]}{_RESET}")
            right_hunks.append(f"{_GREEN}{right[j1:j2]}{_RESET}")

    left_out = "".join(left_hunks)
    right_out = "".join(right_hunks)

    # 仍然太长? 截断到第一个带颜色的差异处
    if len(left_out) > _MAX_HUNK_LEN or len(right_out) > _MAX_HUNK_LEN:
        left_out = _trim_to_first_diff(left_out)
        right_out = _trim_to_first_diff(right_out)

    return left_out, right_out


def _trim_to_first_diff(text: str) -> str:
    """
    将高亮字符串截断到第一个变色处及其上下文。

    扫描第一个红色或绿色 ANSI 序列, 保留前面 _CONTEXT 个字符
    和后面最多 _CONTEXT * 4 个字符。更早的内容用 "..." 代替。

    这是最后的兜底保护, 防止差异极大的长字符串刷爆终端。

    Args:
        text: 可能包含 ANSI 颜色码的字符串

    Returns:
        聚焦于第一个可见变更的缩短版本
    """
    red_pos = text.find(_RED)
    green_pos = text.find(_GREEN)
    candidates = [p for p in (red_pos, green_pos) if p != -1]
    if not candidates:
        return text[:_MAX_HUNK_LEN]
    first = min(candidates)
    start = max(0, first - _CONTEXT)
    prefix = "..." if start > 0 else ""
    return prefix + text[start : start + _CONTEXT * 4]


def auto_highlight(left: str, right: str) -> tuple[str, str]:
    """
    自动选择最佳高亮策略。

    - 短字符串 (<= _LONG_THRESHOLD 字符): 使用 highlight_inline_diff,
      完整显示每个变更。
    - 长字符串 (> _LONG_THRESHOLD 字符): 使用 format_diff_hunk,
      避免刷满终端。

    非字符串输入会回退到 repr()。

    Args:
        left:  "旧"值
        right: "新"值

    Returns:
        (高亮后的 left, 高亮后的 right)
    """
    if isinstance(left, str) and isinstance(right, str) and len(left) > _LONG_THRESHOLD:
        return format_diff_hunk(left, right)
    return highlight_inline_diff(left, right)
