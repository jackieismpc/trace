"""Markdown 渲染——给人看，可直接贴进 PR（方案 §5.5）。"""

from __future__ import annotations

from ..model import KindSource, Skeleton, SpanNode, Status
from .tree import MARK_HEURISTIC, MARK_TRUNCATED, format_bytes, format_duration

__all__ = ["render_md"]


def _rows(node: SpanNode, depth: int, out: list[str]) -> None:
    indent = "&nbsp;" * (depth * 4)
    if node.collapsed:
        ok = "全部 OK" if node.collapsed_all_ok else "含 ERROR"
        out.append(
            f"| {indent}⋯ | — | {node.collapsed_kind.value} | "
            f"折叠 {node.collapsed_count} 个同类节点（{ok}） | — | — | — |"
        )
        return

    meta = node.meta
    flags: list[str] = []
    if node.truncated_fields:
        flags.append(MARK_TRUNCATED)
    if meta.kind_source is KindSource.HEURISTIC:
        flags.append(MARK_HEURISTIC)
    if node.elided_depth:
        flags.append(f"折叠 {node.elided_depth} 层")

    status = "❌ ERROR" if meta.status is Status.ERROR else meta.status.value
    out.append(
        f"| {indent}`{meta.span_id[:8]}` | {meta.kind.value} | {meta.kind_source.value} | "
        f"{meta.name} | {status} | {format_duration(meta.duration_ns)} | "
        f"{' '.join(flags) or '—'} |"
    )
    for child in node.children:
        _rows(child, depth + 1, out)


def render_md(skeleton: Skeleton) -> str:
    """渲染成 Markdown 报告。"""
    out: list[str] = []
    out.append(f"# Trace 骨架 `{skeleton.trace_id[:12] or '(无 trace_id)'}`")
    out.append("")
    out.append(f"- 整体状态：**{skeleton.status.value}**")
    out.append(f"- 总耗时：{format_duration(skeleton.duration_ns)}")
    out.append(
        f"- span 数：{skeleton.original_span_count} → **{skeleton.kept_span_count}** 个存活节点"
    )
    if skeleton.source_file:
        out.append(f"- 来源文件：`{skeleton.source_file}`（{format_bytes(skeleton.source_size)}）")
    out.append("")

    out.append("## 调用树")
    out.append("")
    out.append("| span | 类型 | 类型来源 | 名称 | 状态 | 耗时 | 标记 |")
    out.append("| --- | --- | --- | --- | --- | --- | --- |")
    for root in skeleton.roots:
        _rows(root, 0, out)
    out.append("")

    errors = [
        n
        for n in skeleton.all_nodes()
        if not n.collapsed and n.meta.status is Status.ERROR and n.meta.status_message
    ]
    if errors:
        out.append("## 错误节点")
        out.append("")
        for n in errors:
            out.append(f"### `{n.meta.span_id[:8]}` {n.meta.name}")
            out.append("")
            out.append("```")
            out.append(n.meta.status_message)
            out.append("```")
            out.append("")

    truncated = [n for n in skeleton.all_nodes() if n.truncated_fields]
    if truncated:
        out.append("## 被截断的字段")
        out.append("")
        out.append("每一条都可以原样取回——截断只发生在这份视图里，原文件从未被修改。")
        out.append("")
        for n in truncated:
            for m in n.truncated_fields:
                out.append(
                    f"- `{m.span_id[:8]}` `{m.field_path}`："
                    f"{m.original_chars} → {m.kept_chars} 字符（{m.strategy}）"
                )
                if m.preview:
                    out.append(f"  > {m.preview}")
                out.append(f"  ```bash\n  {m.expand_hint}\n  ```")
        out.append("")

    if skeleton.notes:
        out.append("## 说明")
        out.append("")
        for note in skeleton.notes:
            out.append(f"- {note}")
        out.append("")

    return "\n".join(out)
