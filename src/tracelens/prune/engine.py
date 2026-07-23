"""规则求解与剪枝编排（方案 2.2）。

求解语义：规则按 ``priority`` 降序排序，**首匹配生效**；同优先级按声明顺序。
未命中任何规则的 span 取默认动作 Keep，但那是「隐式保留」——只有被规则**显式**
判为 Keep 的节点，才会强制保留它到根的整条祖先链。这条区分很重要：
如果所有隐式保留的节点都强制保留祖先，那么 Drop 将永远不会发生重挂，
``elided_depth`` 也就永远是 0。

**硬保护写死在引擎里，不在默认规则集里**——用户配置无法关掉它：

* 根节点永不剪除；
* 任一 ERROR 节点到根的整条路径永不剪除。

这两条是第四节业界共识（OpenHands 的 keep_first、Claude Code 的压缩豁免记忆）
在本方案的落地：「有些东西永远不能被裁掉」。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..model import Skeleton, SpanMeta, SpanNode, Status, TraceDoc
from .rules import Action, Rule, RuleSet
from .topology import build_forest, compute_depths, rebuild
from .truncate import apply_truncation

__all__ = ["Decision", "resolve_actions", "prune"]


@dataclass(slots=True, frozen=True)
class Decision:
    """单个 span 的求解结果。"""

    action: Action
    rule: Rule | None
    """命中的规则；None 表示没有规则命中（隐式 Keep）。"""

    @property
    def explicit(self) -> bool:
        """是否由规则显式判定——决定要不要强制保留祖先链。"""
        return self.rule is not None and self.action is Action.KEEP


def resolve_actions(
    spans: list[SpanMeta],
    depths: dict[str, int],
    ruleset: RuleSet,
) -> dict[str, Decision]:
    """对每个 span 求出动作。首匹配语义。"""
    ordered = ruleset.ordered()
    out: dict[str, Decision] = {}
    for span in spans:
        depth = depths.get(span.span_id, 0)
        hit: Rule | None = None
        for rule in ordered:
            if rule.match.matches(span, depth):
                hit = rule
                break
        out[span.span_id] = Decision(
            action=hit.action if hit is not None else Action.KEEP,
            rule=hit,
        )
    return out


def _protected_ids(
    roots: list[SpanNode],
    decisions: dict[str, Decision],
) -> set[str]:
    """算出受保护、必须存活的节点集合（含其到根的整条路径）。"""
    parent_of: dict[str, str | None] = {}
    stack: list[tuple[SpanNode, str | None]] = [(r, None) for r in roots]
    all_nodes: list[SpanNode] = []
    while stack:
        node, parent = stack.pop()
        parent_of[node.meta.span_id] = parent
        all_nodes.append(node)
        for child in node.children:
            stack.append((child, node.meta.span_id))

    seeds: set[str] = {r.meta.span_id for r in roots}
    for node in all_nodes:
        sid = node.meta.span_id
        # 硬保护：ERROR 节点；以及规则显式 Keep 的节点
        if node.meta.status is Status.ERROR or decisions[sid].explicit:
            seeds.add(sid)

    protected: set[str] = set()
    for sid in seeds:
        cur: str | None = sid
        while cur is not None and cur not in protected:
            protected.add(cur)
            cur = parent_of.get(cur)
    return protected


def _descendants(node: SpanNode) -> list[SpanNode]:
    out: list[SpanNode] = []
    stack = list(node.children)
    while stack:
        cur = stack.pop()
        out.append(cur)
        stack.extend(cur.children)
    return out


def prune(
    doc: TraceDoc,
    ruleset: RuleSet,
    buf: Any = None,
    payload_fields_fn: Any = None,
    strict_grapheme: bool = False,
    source_file: str = "",
) -> Skeleton:
    """执行完整的剪枝流程，产出骨架。

    :param doc: ingest 阶段的解析结果
    :param ruleset: 规则集
    :param buf: 原文件缓冲区；给 None 则跳过截断（只做拓扑剪枝）
    :param payload_fields_fn: ``(SpanMeta) -> list[PayloadField]``，通常是
        `TraceReader.payload_fields`
    :param strict_grapheme: 按 grapheme cluster 截断
    """
    roots, nodes = build_forest(doc.spans)
    depths = compute_depths(roots)
    decisions = resolve_actions(doc.spans, depths, ruleset)
    protected = _protected_ids(roots, decisions)

    # 第一步：显式 Drop 与 CollapseSubtree 的后代
    dropped: set[str] = set()
    for span in doc.spans:
        decision = decisions[span.span_id]
        if decision.action is Action.DROP:
            dropped.add(span.span_id)
        elif decision.action is Action.COLLAPSE_SUBTREE:
            for desc in _descendants(nodes[span.span_id]):
                dropped.add(desc.meta.span_id)

    # 第二步：受保护节点（根 / ERROR 路径 / 显式 Keep 的祖先链）一律拉回
    dropped -= protected

    # 第三步：截断——只对存活且动作为 Truncate 的节点做，且只读被选中的字段
    if buf is not None and payload_fields_fn is not None:
        for span in doc.spans:
            if span.span_id in dropped:
                continue
            decision = decisions[span.span_id]
            if decision.action is not Action.TRUNCATE or decision.rule is None:
                continue
            params = decision.rule.effective_params()
            apply_truncation(
                node=nodes[span.span_id],
                fields=payload_fields_fn(span),
                buf=buf,
                max_chars=params.max_chars,
                strategy=params.strategy,
                field_globs=params.field_globs,
                strict_grapheme=strict_grapheme,
            )

    # 第四步：重建拓扑（重挂 + 占位合并）
    new_roots = rebuild(roots, dropped)

    kept = len(doc.spans) - len(dropped)
    duration = max((s.duration_ns for s in doc.spans), default=0)
    return Skeleton(
        trace_id=doc.trace_id,
        roots=new_roots,
        original_span_count=len(doc.spans),
        kept_span_count=kept,
        status=doc.status(),
        duration_ns=duration,
        source_file=source_file,
        source_size=doc.file_size,
    )
