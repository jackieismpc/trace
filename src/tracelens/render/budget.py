"""``--max-tokens`` 预算收紧（方案 §5.5、§四）。

这条是从业界直接借鉴来的：Claude Code、OpenCode、OpenHands 都用粗粒度 token
估算对着阈值做决策（4 字符/token 就够用），是三家共同的工程判断。

给定预算，渲染前估算骨架体积，超预算则**按既定次序逐级增压**：

    1. 先收紧截断阈值（预览留得更短）
    2. 再折叠同类兄弟——但**渐进**收紧：先只折 ≥20 的大组、再 ≥8、≥3、≥2，
       避免上一版从「完全不折」一步跳到「全折」造成的预算过冲；
       折叠时保留一个代表节点，让读者看到被折内容的真实样例
    3. 最后再压缩展示深度

直到达标，或触及**保底集**（根节点 + ERROR 节点到根的整条路径）——保底集不可
再剪。这让「骨架一定装得进指定预算」成为可承诺的性质，Agent harness 可以放心
把它接进固定大小的上下文槽位。

与剪枝阶段的区别：剪枝改变的是「骨架包含哪些节点」，受五条不变量约束；
本模块是**展示层**的进一步压缩，会在骨架末尾如实记录压到了第几级。
"""

from __future__ import annotations

from collections.abc import Callable

from ..model import Skeleton, SpanKind, SpanNode, Status, TruncationMark
from ..tokens import DEFAULT_CHARS_PER_TOKEN, count_tokens
from .tree import MARK_ELIDED

__all__ = ["fit_to_budget", "PRESSURE_LEVELS"]

# 增压阶梯。每一级都是「在上一级基础上再加一道」，次序即方案 §5.5 规定的次序：
# 先收紧截断阈值 → 再降低展示详细度 → 再折叠同类兄弟 → 最后压缩展示深度。
#
# 每一项是 (预览字符数, 展示详细度, 折叠最小兄弟组大小, 展示深度上限)。
#   - 折叠最小兄弟组大小为 None 表示不折叠；为整数 N 表示只折叠
#     「同一父下、同类型、≥N 个」的兄弟组，小于 N 的组保持可见。
#   - 折叠时保留组内第一个成员作为代表节点（真实样例），占位节点只统计其余成员。
#   - 展示深度上限为 None 表示不限。
#
# 折叠档位刻意渐进（20 → 8 → 3 → 2）：上一版从「完全不折」直接跳到
# 「全折」，在 411 span 的 Trace 上从 >8000 token 一步落到 239 token，
# 只用了预算的 ~6%。渐进收紧让预算大时保留更多可见节点。
_LADDER: list[tuple[int, int, int | None, int | None]] = [
    (120, 2, None, None),
    (60, 2, None, None),
    (24, 2, None, None),
    (24, 1, None, None),
    (24, 0, None, None),
    (24, 0, 20, None),
    (24, 0, 8, None),
    (24, 0, 3, None),
    (24, 0, 2, None),
    (24, 0, 2, 3),
    (24, 0, 2, 2),
    (24, 0, 2, 1),
]

PRESSURE_LEVELS = len(_LADDER)
"""可用的增压级数（0 表示原样）。"""


def _clone(node: SpanNode) -> SpanNode:
    fresh = SpanNode(meta=node.meta)
    fresh.elided_depth = node.elided_depth
    fresh.truncated_fields = list(node.truncated_fields)
    fresh.collapsed = node.collapsed
    fresh.collapsed_count = node.collapsed_count
    fresh.collapsed_kind = node.collapsed_kind
    fresh.collapsed_all_ok = node.collapsed_all_ok
    fresh.children = [_clone(c) for c in node.children]
    return fresh


def _clone_skeleton(skeleton: Skeleton) -> Skeleton:
    return Skeleton(
        trace_id=skeleton.trace_id,
        roots=[_clone(r) for r in skeleton.roots],
        original_span_count=skeleton.original_span_count,
        kept_span_count=skeleton.kept_span_count,
        status=skeleton.status,
        duration_ns=skeleton.duration_ns,
        source_file=skeleton.source_file,
        source_size=skeleton.source_size,
        notes=list(skeleton.notes),
    )


def _protected(node: SpanNode) -> bool:
    """本节点或其任一后代是 ERROR——即它落在保底集里。"""
    if not node.collapsed and node.meta.status is Status.ERROR:
        return True
    return any(_protected(c) for c in node.children)


def _subtree_size(node: SpanNode) -> int:
    """子树里代表的原始节点数（占位节点按其汇总数计）。"""
    if node.collapsed:
        return node.collapsed_count
    return 1 + sum(_subtree_size(c) for c in node.children)


def _placeholder(kind: SpanKind, count: int, all_ok: bool) -> SpanNode:
    from ..model import ByteRange, KindSource, SpanMeta

    meta = SpanMeta(
        span_id="",
        parent_id=None,
        trace_id="",
        name=f"elided {count} {kind.value} spans",
        kind=kind,
        kind_source=KindSource.EXPLICIT,
        status=Status.OK if all_ok else Status.ERROR,
        raw_range=ByteRange(0, 0),
    )
    node = SpanNode(meta=meta)
    node.collapsed = True
    node.collapsed_count = count
    node.collapsed_kind = kind
    node.collapsed_all_ok = all_ok
    return node


def _shorten_previews(node: SpanNode, max_chars: int) -> None:
    """把已有的截断预览进一步收紧。

    只动预览文本与 ``kept_chars``，``original_chars``、摘要与 ``expand_hint``
    一概不动——取回路径必须始终有效。
    """
    from ..prune.truncate import truncate_text

    new_marks: list[TruncationMark] = []
    for m in node.truncated_fields:
        new_marks.append(
            TruncationMark(
                span_id=m.span_id,
                field_path=m.field_path,
                original_chars=m.original_chars,
                kept_chars=min(m.kept_chars, max_chars),
                strategy=m.strategy,
                digest=m.digest,
                expand_hint=m.expand_hint,
                preview=truncate_text(m.preview, max_chars, m.strategy),
            )
        )
    node.truncated_fields = new_marks
    for c in node.children:
        _shorten_previews(c, max_chars)


def _collapse_siblings(node: SpanNode, min_group: int) -> None:
    """把同一父节点下、同类型、且不在保底集里的兄弟合并成占位节点。

    ``min_group`` 为折叠阈值：只有组内兄弟数 ≥ min_group 才折，
    小于阈值的组保持可见——这是预算收紧的渐进档位。

    折叠时保留组内第一个成员作为代表节点（连同其子树渲染，提供真实样例），
    占位节点只统计其余成员，``elided N`` 的 N 与代表节点一起还原组内全部节点。
    """
    for c in node.children:
        _collapse_siblings(c, min_group)

    kept: list[SpanNode] = []
    groups: dict[SpanKind, list[SpanNode]] = {}
    for child in node.children:
        if child.collapsed or _protected(child):
            kept.append(child)
            continue
        groups.setdefault(child.meta.kind, []).append(child)

    for kind, members in groups.items():
        if len(members) < min_group:
            kept.extend(members)
            continue
        rep, *rest = members
        kept.append(rep)
        rest_total = sum(_subtree_size(m) for m in rest)
        if rest_total > 0:
            kept.append(_placeholder(kind, rest_total, all_ok=True))
    node.children = kept


def _limit_depth(node: SpanNode, depth: int, limit: int) -> None:
    """超过展示深度的子树折叠成占位节点，保底集除外。"""
    if depth >= limit:
        survivors: list[SpanNode] = []
        folded: dict[SpanKind, int] = {}
        for child in node.children:
            if _protected(child):
                survivors.append(child)
                _limit_depth(child, depth + 1, limit)
            else:
                kind = child.collapsed_kind if child.collapsed else child.meta.kind
                folded[kind] = folded.get(kind, 0) + _subtree_size(child)
        for kind, count in folded.items():
            survivors.append(_placeholder(kind, count, all_ok=True))
        node.children = survivors
        return
    for child in node.children:
        _limit_depth(child, depth + 1, limit)


def _apply_pressure(skeleton: Skeleton, level: int) -> Skeleton:
    """产出第 ``level`` 级压力下的骨架副本（``level`` 从 1 起）。"""
    out = _clone_skeleton(skeleton)
    if level <= 0:
        return out

    preview_chars, detail, min_group, depth_limit = _LADDER[min(level, len(_LADDER)) - 1]
    out.detail = detail
    for r in out.roots:
        _shorten_previews(r, preview_chars)
    if min_group is not None:
        for r in out.roots:
            _collapse_siblings(r, min_group)
    if depth_limit is not None:
        for r in out.roots:
            _limit_depth(r, 0, depth_limit)
    return out


def fit_to_budget(
    skeleton: Skeleton,
    renderer: Callable[[Skeleton], str],
    max_tokens: int | None,
    exact: bool = False,
    chars_per_token: float = DEFAULT_CHARS_PER_TOKEN,
) -> tuple[Skeleton, str]:
    """把骨架压进 token 预算，返回 ``(最终骨架, 渲染文本)``。

    :param renderer: 渲染函数，预算是按**渲染后的文本**算的
    :param max_tokens: 预算；None 表示不限
    :param exact: 用 tiktoken 精确计数（需 ``[tokens]`` extra）
    :param chars_per_token: 估算系数；`exact=True` 时不生效（tiktoken 精确计数）

    达不到预算时不会假装成功：返回压到最紧的版本，并在骨架说明里写清
    「已压到最紧仍超预算」——保底集不可再剪是硬约束。
    """
    text = renderer(skeleton)
    est = count_tokens(text, exact=exact, chars_per_token=chars_per_token)
    if max_tokens is None:
        skeleton.notes.append(f"token 估算：{est.tokens}（方法：{est.method}）")
        return skeleton, renderer(skeleton)

    if est.tokens <= max_tokens:
        skeleton.notes.append(f"token 估算：{est.tokens} / 预算 {max_tokens}（方法：{est.method}）")
        return skeleton, renderer(skeleton)

    last = skeleton
    last_est = est
    for level in range(1, PRESSURE_LEVELS + 1):
        candidate = _apply_pressure(skeleton, level)
        text = renderer(candidate)
        cur = count_tokens(text, exact=exact, chars_per_token=chars_per_token)
        last, last_est = candidate, cur
        if cur.tokens <= max_tokens:
            candidate.notes.append(
                f"token 估算：{cur.tokens} / 预算 {max_tokens}（方法：{cur.method}）；"
                f"为达标已增压到第 {level} 级"
                f"（{MARK_ELIDED} 标记处有节点被折叠，可用 expand 取回原文）"
            )
            return candidate, renderer(candidate)

    last.notes.append(
        f"token 估算：{last_est.tokens} / 预算 {max_tokens}（方法：{last_est.method}）；"
        "已压到最紧仍超预算——保底集（根节点 + ERROR 路径）不可再剪。"
    )
    return last, renderer(last)
