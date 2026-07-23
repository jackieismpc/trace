"""JSON 渲染——给程序消费（方案 §5.5）。

与 tree 形态的分工：tree 给 LLM 看（token 效率优先），json 给程序消费
（schema 稳定、可 diff、可作为 CI 的比对基线）。

schema 稳定性由快照测试（syrupy）保证：某次重构让输出多了一个字段或改了字段名，
功能测试可能全绿，快照会立刻标红。
"""

from __future__ import annotations

import json
from typing import Any

from ..model import Skeleton, SpanNode

__all__ = ["skeleton_to_dict", "render_json"]

SCHEMA_VERSION = 1


def _node_to_dict(node: SpanNode, detail: int = 2) -> dict[str, Any]:
    if node.collapsed:
        return {
            "elided": node.collapsed_count,
            "kind": node.collapsed_kind.value,
            "all_ok": node.collapsed_all_ok,
        }

    meta = node.meta
    out: dict[str, Any] = {
        "span_id": meta.span_id,
        "name": meta.name,
        "kind": meta.kind.value,
        "kind_source": meta.kind_source.value,
        "status": meta.status.value,
        "duration_ns": meta.duration_ns,
        "input_bytes": meta.input_bytes,
        "output_bytes": meta.output_bytes,
    }
    if meta.status_message:
        out["status_message"] = meta.status_message
    if node.elided_depth:
        out["elided_depth"] = node.elided_depth
    if node.truncated_fields:
        # 每个被截断的字段输出「保留下来的内容 + __truncated__ 标记」，
        # 与方案 §5.4 的示例同构（那里标记与内容也是并列的）。
        out["truncated_fields"] = [
            {
                "field": m.field_path,
                **({"preview": m.preview} if detail >= 2 else {}),
                "__truncated__": m.to_dict(),
            }
            for m in node.truncated_fields
        ]
    if node.children:
        out["children"] = [_node_to_dict(c, detail) for c in node.children]
    return out


def skeleton_to_dict(skeleton: Skeleton) -> dict[str, Any]:
    """骨架 → 稳定 schema 的字典。"""
    return {
        "schema_version": SCHEMA_VERSION,
        "trace_id": skeleton.trace_id,
        "status": skeleton.status.value,
        "duration_ns": skeleton.duration_ns,
        "original_span_count": skeleton.original_span_count,
        "kept_span_count": skeleton.kept_span_count,
        "source_file": skeleton.source_file,
        "source_size": skeleton.source_size,
        "notes": list(skeleton.notes),
        "roots": [_node_to_dict(r, skeleton.detail) for r in skeleton.roots],
    }


def render_json(skeleton: Skeleton) -> str:
    """渲染成 JSON 文本。

    ``ensure_ascii=False``：骨架是给人和模型读的，把中文写成 ``\\uXXXX``
    既浪费 token 又难读。注意这与「原始 Payload 的字节承诺」无关——
    骨架本来就是新生成的视图，承诺只约束 expand 的输出通路。
    """
    return json.dumps(skeleton_to_dict(skeleton), ensure_ascii=False, indent=2)
