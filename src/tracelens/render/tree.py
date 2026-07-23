"""树形文本渲染——默认形态（方案 §5.5、附录 A6）。

**骨架的最终读者是 LLM，不是人**，所以序列化按 token 效率优化而不是可读性优化。
同样的信息，缩进树形文本比 JSON 省 40%~60% 的 token：JSON 的引号、花括号、
每个对象里重复出现的 key 名，对 LLM 都是纯开销；缩进树用相对位置表达父子关系，
用单 token 符号承载元信息。

三个符号各占 1 token 但信息量很大：

    ✂  该节点有字段被截断，可 expand 取回
    ⚠  该节点的类型是启发式猜的，不要当事实用
    ⋯  此处折叠了若干同类节点
"""

from __future__ import annotations

from ..model import KindSource, Skeleton, SpanNode, Status

__all__ = ["render_tree", "format_duration", "format_bytes"]

MARK_TRUNCATED = "✂"
MARK_HEURISTIC = "⚠"
MARK_ELIDED = "⋯"


def format_duration(ns: int) -> str:
    """纳秒 → 人和模型都好读的短形式。"""
    if ns <= 0:
        return "-"
    seconds = ns / 1e9
    if seconds < 0.001:
        return f"{ns / 1000:.0f}us"
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    return f"{seconds / 60:.1f}m"


def format_bytes(n: int) -> str:
    """字节数 → 紧凑形式（省 token）。"""
    if n < 1024:
        return str(n)
    if n < 1024 * 1024:
        return f"{n / 1024:.1f}K"
    return f"{n / 1024 / 1024:.1f}M"


def _short(span_id: str, width: int = 6) -> str:
    return span_id[:width] if span_id else "-" * width


def _node_line(node: SpanNode) -> str:
    """渲染一个节点自身的信息（不含树枝前缀）。"""
    if node.collapsed:
        ok = "all OK" if node.collapsed_all_ok else "含 ERROR"
        return (
            f"{MARK_ELIDED} elided {node.collapsed_count} similar "
            f"{node.collapsed_kind.value} spans ({ok})"
        )

    meta = node.meta
    parts = [
        _short(meta.span_id),
        f"{meta.kind.value:<9}",
        meta.name,
    ]
    line = " ".join(parts)
    line = f"{line:<48}"
    line += f"{meta.status.value:>5} {format_duration(meta.duration_ns):>7}"
    if meta.input_bytes or meta.output_bytes:
        line += f"  in={format_bytes(meta.input_bytes):<6} out={format_bytes(meta.output_bytes):<6}"

    flags: list[str] = []
    if node.truncated_fields:
        flags.append(MARK_TRUNCATED)
    if meta.kind_source is KindSource.HEURISTIC:
        flags.append(MARK_HEURISTIC)
    if node.elided_depth:
        flags.append(f"{MARK_ELIDED}{node.elided_depth}层")
    if flags:
        line += "  " + " ".join(flags)
    return line.rstrip()


def _render_node(node: SpanNode, prefix: str, is_last: bool, out: list[str], detail: int) -> None:
    connector = "└─ " if is_last else "├─ "
    out.append(prefix + connector + _node_line(node))
    child_prefix = prefix + ("   " if is_last else "│  ")

    # ERROR 节点把错误信息首行直接摊在骨架上——这是排查的第一落点，
    # 值得为它多花几个 token，省掉一次 expand 往返。
    if not node.collapsed and node.meta.status is Status.ERROR and node.meta.status_message:
        first_line = node.meta.status_message.splitlines()[0]
        out.append(child_prefix + f"└─ error: {first_line}")

    # detail=0 时截断细节全部收起，节点行的 ✂ 仍在，取回方式见图例
    if detail >= 1:
        for mark in node.truncated_fields:
            out.append(
                child_prefix
                + f"   {MARK_TRUNCATED} {mark.field_path} "
                + f"{mark.original_chars}→{mark.kept_chars} chars, {mark.strategy}"
            )
            if detail >= 2:
                # 保留下来的内容本身才是截断的产物——不展示它，截断就退化成了删除
                out.append(child_prefix + f"     {mark.preview}")
                out.append(child_prefix + f"     expand: {mark.expand_hint}")

    for i, child in enumerate(node.children):
        _render_node(child, child_prefix, i == len(node.children) - 1, out, detail)


def render_tree(skeleton: Skeleton) -> str:
    """把骨架渲染成缩进树形文本。"""
    out: list[str] = []
    header = (
        f"trace {_short(skeleton.trace_id, 8)}  status={skeleton.status.value}  "
        f"spans={skeleton.original_span_count}→{skeleton.kept_span_count}  "
        f"dur={format_duration(skeleton.duration_ns)}"
    )
    out.append(header)

    for i, root in enumerate(skeleton.roots):
        _render_node(root, "", i == len(skeleton.roots) - 1, out, skeleton.detail)

    out.append("")
    out.append(
        f"图例：{MARK_TRUNCATED} 字段被截断（可 expand 取回）  "
        f"{MARK_HEURISTIC} 类型为启发式推断，不要当事实用  "
        f"{MARK_ELIDED} 此处折叠了同类节点"
    )
    if skeleton.detail < 2:
        out.append(
            "取回方式：tracelens expand --span-id <前 6 位 id> [--field <字段路径>]"
            "（为压进 token 预算，本骨架已收起截断细节）"
        )
    if skeleton.source_file:
        out.append(f"来源：{skeleton.source_file}（{format_bytes(skeleton.source_size)}）")
    for note in skeleton.notes:
        out.append(note)
    return "\n".join(out)
