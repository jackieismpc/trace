"""截断：三种策略与自描述截断标记（方案 §5.4、附录 A7、B18）。

**截断只发生在骨架视图里，原文件从不被修改。**

策略跟着信息分布走：

* ``head``      —— prompt 的系统指令在开头，保头部（默认）
* ``tail``      —— 错误栈的根因在结尾，保尾部
* ``head_tail`` —— 工具输出常常开头是格式说明、结尾是结论，两头都保

UTF-8 处理：先 ``decode("utf-8")`` 到 str 再按字符数截断——在 str 层操作天然
避开「把多字节字符劈成两半」的问题，这是 Python 相对按字节切片的一个便利面。
但码点仍不等于用户感知字符：``👨‍👩‍👧`` 由多个码点经 ZWJ 拼成，按码点截断可能
把家庭切散；组合字符（``é`` = ``e`` + U+0301）同理。默认按码点（零依赖、
覆盖绝大多数场景），``--strict-grapheme`` 时用 ``regex`` 的 ``\\X`` 按 grapheme 截。
"""

from __future__ import annotations

import hashlib
from typing import Any

from ..ingest.common import PayloadField
from ..model import SpanNode, TruncationMark

__all__ = ["ELLIPSIS", "truncate_text", "digest_bytes", "expand_hint", "apply_truncation"]

ELLIPSIS = "…"
"""截断处的省略标记，占 1 个字符。"""


def digest_bytes(raw: bytes) -> str:
    """原文摘要，用于「取回的内容确实是当初被截断的那一段」的校验。

    用标准库 `hashlib.blake2b`（RFC 7693）而不是 blake3：零第三方依赖、比
    SHA-256 快，完整性校验强度绰绰有余。为一个非瓶颈环节增加一个带原生扩展的
    二进制依赖，不符合依赖最小化原则（附录 B14）。
    """
    return "blake2b:" + hashlib.blake2b(raw, digest_size=32).hexdigest()[:16]


def expand_hint(span_id: str, field_path: str) -> str:
    """生成可直接执行的取回命令。

    把「怎么取回」的知识直接写进给 LLM 的数据里，Agent 不需要额外的
    system prompt 教它怎么展开——这是 progressive disclosure 的层间导航链接。
    """
    short = span_id[:6] if len(span_id) > 6 else span_id
    return f"tracelens expand --span-id {short} --field '{field_path}'"


def _graphemes(text: str) -> list[str]:
    """按 grapheme cluster 切分；`regex` 不可用时退回按码点切。"""
    try:
        import regex  # type: ignore[import-untyped]
    except ImportError:
        return list(text)
    clusters: list[str] = regex.findall(r"\X", text)
    return clusters


def truncate_text(
    text: str,
    max_chars: int,
    strategy: str = "head",
    strict_grapheme: bool = False,
) -> str:
    """按策略截断文本；未超长则原样返回。

    :param max_chars: 保留的字符数（不含省略号）
    :param strategy: ``head`` / ``tail`` / ``head_tail``
    :param strict_grapheme: 按 grapheme cluster 而不是码点计数与切分
    """
    units: list[str] | str = _graphemes(text) if strict_grapheme else text
    if len(units) <= max_chars:
        return text

    def take(seq: list[str] | str, lo: int, hi: int | None) -> str:
        part = seq[lo:hi]
        return "".join(part) if isinstance(part, list) else part

    if strategy == "tail":
        return ELLIPSIS + take(units, len(units) - max_chars, None)
    if strategy == "head_tail":
        head_n = max_chars // 2
        tail_n = max_chars - head_n
        return take(units, 0, head_n) + ELLIPSIS + take(units, len(units) - tail_n, None)
    # 默认 head
    return take(units, 0, max_chars) + ELLIPSIS


def _text_len(text: str, strict_grapheme: bool) -> int:
    return len(_graphemes(text)) if strict_grapheme else len(text)


def apply_truncation(
    node: SpanNode,
    fields: list[PayloadField],
    buf: Any,
    max_chars: int,
    strategy: str = "head",
    field_globs: list[str] | None = None,
    strict_grapheme: bool = False,
) -> list[TruncationMark]:
    """对一个节点的大体积字段执行截断，产出自描述标记并挂到节点上。

    :param fields: 由适配器给出的字段定位信息（路径 + 字节区间）
    :param buf: 原文件缓冲区；只读取被选中的字段，其余字段一个字节都不碰
    :param field_globs: 字段路径通配；为空表示所有字段都参与
    """
    from fnmatch import fnmatchcase

    marks: list[TruncationMark] = []
    for f in fields:
        if field_globs and not any(fnmatchcase(f.path, g) for g in field_globs):
            continue
        raw = bytes(buf[f.start : f.end])
        text = raw.decode("utf-8", errors="replace")
        original_chars = _text_len(text, strict_grapheme)
        if original_chars <= max_chars:
            continue
        kept = truncate_text(text, max_chars, strategy, strict_grapheme)
        mark = TruncationMark(
            span_id=node.meta.span_id,
            field_path=f.path,
            original_chars=original_chars,
            kept_chars=max_chars,
            strategy=strategy,
            digest=digest_bytes(raw),
            expand_hint=expand_hint(node.meta.span_id, f.path),
            preview=kept,
        )
        marks.append(mark)

    node.truncated_fields.extend(marks)
    return marks
