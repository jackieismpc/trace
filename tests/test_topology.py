"""拓扑不变量测试：剪枝正确性的形式化定义（方案 §5.3、附录 A5）。

五条不变量：
① 输出是合法森林（无环、单亲、根可达）
② 祖先关系在存活节点间单调保持  ← 核心
③ 存活节点的相对深度顺序不变
④ 无孤儿节点
⑤ 守恒律：存活节点数 + 各占位节点汇总数 = 原节点数

用 hypothesis 随机生成「span 森林 × 规则集」的组合来钉死它们——手写用例很难
想到「兄弟中第 1、3、5 个被折叠、中间夹一个 ERROR」这种组合。
"""

from __future__ import annotations

import random

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tracelens.model import ByteRange, KindSource, SpanKind, SpanMeta, SpanNode, Status, TraceDoc
from tracelens.prune.engine import prune
from tracelens.prune.rules import Action, Match, Rule, RuleSet, TruncateParams
from tracelens.prune.topology import build_forest, compute_depths, rebuild

_KINDS = [SpanKind.AGENT, SpanKind.MODEL, SpanKind.TOOL, SpanKind.CHAIN]


# ---- 随机 Trace 与随机规则集 ---------------------------------------------


def _make_span(i: int, parent: int | None, kind: SpanKind, status: Status, size: int) -> SpanMeta:
    return SpanMeta(
        span_id=f"{i:04x}",
        parent_id=None if parent is None else f"{parent:04x}",
        trace_id="t",
        name=f"{kind.value.lower()}_{i}",
        kind=kind,
        kind_source=KindSource.EXPLICIT,
        status=status,
        start_ns=1000 + i,
        end_ns=1000 + i + size,
        raw_range=ByteRange(i * 100, i * 100 + size),
    )


@st.composite
def random_trace(draw: st.DrawFn) -> TraceDoc:
    """随机 span 森林：可控节点数、深度、类型分布与错误注入。"""
    n = draw(st.integers(min_value=1, max_value=25))
    seed = draw(st.integers(min_value=0, max_value=10_000))
    rng = random.Random(seed)
    spans: list[SpanMeta] = [_make_span(0, None, SpanKind.AGENT, Status.OK, 50)]
    for i in range(1, n):
        parent = rng.randrange(0, i)
        kind = rng.choice(_KINDS)
        status = Status.ERROR if rng.random() < 0.15 else Status.OK
        spans.append(_make_span(i, parent, kind, status, rng.randint(10, 5000)))
    return TraceDoc(trace_id="t", spans=spans, source_format="mlflow", file_size=n * 100)


@st.composite
def random_ruleset(draw: st.DrawFn) -> RuleSet:
    """随机规则集：动作、匹配条件与优先级都随机。"""
    count = draw(st.integers(min_value=0, max_value=4))
    rules: list[Rule] = []
    for _ in range(count):
        action = draw(st.sampled_from(list(Action)))
        rules.append(
            Rule(
                match=Match(
                    kind=draw(st.one_of(st.none(), st.sampled_from(_KINDS))),
                    status=draw(st.one_of(st.none(), st.sampled_from([Status.OK, Status.ERROR]))),
                    min_bytes=draw(st.one_of(st.none(), st.integers(min_value=0, max_value=3000))),
                    max_depth=draw(st.one_of(st.none(), st.integers(min_value=0, max_value=5))),
                ),
                action=action,
                params=TruncateParams() if action is Action.TRUNCATE else None,
                priority=draw(st.integers(min_value=0, max_value=100)),
            )
        )
    return RuleSet(rules=rules)


# ---- 不变量断言 -----------------------------------------------------------


def _collect(roots: list[SpanNode]) -> tuple[list[SpanNode], dict[str, int]]:
    """遍历输出森林，返回全部节点与存活节点的输出深度。"""
    nodes: list[SpanNode] = []
    depths: dict[str, int] = {}
    stack: list[tuple[SpanNode, int]] = [(r, 0) for r in roots]
    seen: set[int] = set()
    while stack:
        node, d = stack.pop()
        assert id(node) not in seen, "不变量①违反：出现环或节点被多个父节点共享"
        seen.add(id(node))
        nodes.append(node)
        if not node.collapsed:
            depths[node.meta.span_id] = d
        for child in node.children:
            stack.append((child, d + 1))
    return nodes, depths


def _ancestors(spans: list[SpanMeta]) -> dict[str, set[str]]:
    by_id = {s.span_id: s for s in spans}
    out: dict[str, set[str]] = {}
    for s in spans:
        chain: set[str] = set()
        cur = s.parent_id
        while cur is not None and cur in by_id and cur not in chain:
            chain.add(cur)
            cur = by_id[cur].parent_id
        out[s.span_id] = chain
    return out


def assert_invariants(doc: TraceDoc, roots: list[SpanNode]) -> None:
    """一次性校验全部五条不变量。"""
    nodes, out_depths = _collect(roots)
    kept_ids = set(out_depths)

    # ④ 无孤儿：输出里的每个存活节点都能在原始数据里找到
    original_ids = {s.span_id for s in doc.spans}
    assert kept_ids <= original_ids, "不变量④违反：出现原始数据里没有的节点"

    # ⑤ 守恒律：存活节点数 + 占位节点汇总数 = 原节点数
    collapsed_total = sum(n.collapsed_count for n in nodes if n.collapsed)
    assert len(kept_ids) + collapsed_total == len(doc.spans), (
        f"不变量⑤违反：存活 {len(kept_ids)} + 折叠 {collapsed_total} != 原 {len(doc.spans)}"
    )

    # ② 祖先关系单调保持：输出中的祖先关系必须是原始祖先关系的子集，且方向一致
    orig_anc = _ancestors(doc.spans)
    parent_map: dict[str, str] = {}
    stack: list[tuple[SpanNode, str | None]] = [(r, None) for r in roots]
    while stack:
        node, parent = stack.pop()
        if not node.collapsed and parent is not None:
            parent_map[node.meta.span_id] = parent
        next_parent = parent if node.collapsed else node.meta.span_id
        for child in node.children:
            stack.append((child, next_parent))

    for sid in kept_ids:
        cur = parent_map.get(sid)
        chain: list[str] = []
        while cur is not None:
            chain.append(cur)
            cur = parent_map.get(cur)
        for anc in chain:
            assert anc in orig_anc[sid], (
                f"不变量②违反：输出中 {anc} 是 {sid} 的祖先，但原始数据里不是"
            )
        # ③ 深度顺序：祖先的输出深度必须严格小于后代
        for anc in chain:
            assert out_depths[anc] < out_depths[sid], "不变量③违反：深度顺序错乱"

    # ① 根可达（_collect 已断言无环与单亲），并且原始根一定还在
    for r in doc.spans:
        if r.parent_id is None:
            assert r.span_id in kept_ids, "硬保护违反：根节点被剪掉了"


# ---- 属性测试 -------------------------------------------------------------


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(doc=random_trace(), ruleset=random_ruleset())
def test_invariants_hold_for_any_rules(doc: TraceDoc, ruleset: RuleSet) -> None:
    skeleton = prune(doc, ruleset)
    assert_invariants(doc, skeleton.roots)


@settings(max_examples=100, deadline=None)
@given(doc=random_trace())
def test_error_paths_are_never_pruned(doc: TraceDoc) -> None:
    """硬保护：ERROR 节点到根的整条路径永不剪除，用户配置也关不掉。"""
    drop_everything = RuleSet(rules=[Rule(match=Match(), action=Action.DROP)])
    skeleton = prune(doc, drop_everything)
    _nodes, out_depths = _collect(skeleton.roots)

    by_id = {s.span_id: s for s in doc.spans}
    for span in doc.spans:
        if span.status is not Status.ERROR:
            continue
        cur: str | None = span.span_id
        while cur is not None:
            assert cur in out_depths, f"ERROR 路径上的 {cur} 被剪掉了"
            cur = by_id[cur].parent_id
    assert_invariants(doc, skeleton.roots)


# ---- 手写用例：算法的具体行为 ---------------------------------------------


def _doc(spans: list[SpanMeta]) -> TraceDoc:
    return TraceDoc(trace_id="t", spans=spans, source_format="mlflow", file_size=1000)


def test_reattach_records_elided_depth() -> None:
    """A → B → C，规则删 B：C 必须重挂到 A 下并记 elided_depth=1。

    朴素做法会输出互不相连的 A 和 C，读者会以为 C 是顶层调用；
    正确做法保住「A 间接导致 C」这条因果。
    """
    doc = _doc(
        [
            _make_span(0, None, SpanKind.AGENT, Status.OK, 10),
            _make_span(1, 0, SpanKind.CHAIN, Status.OK, 10),
            _make_span(2, 1, SpanKind.TOOL, Status.OK, 10),
        ]
    )
    ruleset = RuleSet(rules=[Rule(match=Match(kind=SpanKind.CHAIN), action=Action.DROP)])
    skeleton = prune(doc, ruleset)

    root = skeleton.roots[0]
    assert root.meta.span_id == "0000"
    kept_children = [c for c in root.children if not c.collapsed]
    assert len(kept_children) == 1
    assert kept_children[0].meta.span_id == "0002"
    assert kept_children[0].elided_depth == 1
    assert_invariants(doc, skeleton.roots)


def test_dropped_siblings_merge_into_placeholder() -> None:
    """同一存活父节点下、同类型的被删兄弟合并成一个占位节点。"""
    spans = [_make_span(0, None, SpanKind.AGENT, Status.OK, 10)]
    spans += [_make_span(i, 0, SpanKind.TOOL, Status.OK, 10) for i in range(1, 6)]
    doc = _doc(spans)
    ruleset = RuleSet(rules=[Rule(match=Match(kind=SpanKind.TOOL), action=Action.DROP)])
    skeleton = prune(doc, ruleset)

    root = skeleton.roots[0]
    holders = [c for c in root.children if c.collapsed]
    assert len(holders) == 1
    assert holders[0].collapsed_count == 5
    assert holders[0].collapsed_kind is SpanKind.TOOL
    assert holders[0].collapsed_all_ok is True
    assert skeleton.kept_span_count == 1
    assert_invariants(doc, skeleton.roots)


def test_explicit_keep_protects_ancestor_chain() -> None:
    """显式 Keep 的节点，其到根的整条祖先链被强制保留。"""
    doc = _doc(
        [
            _make_span(0, None, SpanKind.AGENT, Status.OK, 10),
            _make_span(1, 0, SpanKind.CHAIN, Status.OK, 10),
            _make_span(2, 1, SpanKind.TOOL, Status.OK, 10),
        ]
    )
    ruleset = RuleSet(
        rules=[
            Rule(match=Match(kind=SpanKind.TOOL), action=Action.KEEP, priority=10),
            Rule(match=Match(), action=Action.DROP, priority=1),
        ]
    )
    skeleton = prune(doc, ruleset)
    _nodes, depths = _collect(skeleton.roots)
    assert depths == {"0000": 0, "0001": 1, "0002": 2}


def test_collapse_subtree_keeps_node_drops_descendants() -> None:
    doc = _doc(
        [
            _make_span(0, None, SpanKind.AGENT, Status.OK, 10),
            _make_span(1, 0, SpanKind.CHAIN, Status.OK, 10),
            _make_span(2, 1, SpanKind.TOOL, Status.OK, 10),
            _make_span(3, 1, SpanKind.TOOL, Status.OK, 10),
        ]
    )
    ruleset = RuleSet(
        rules=[Rule(match=Match(kind=SpanKind.CHAIN), action=Action.COLLAPSE_SUBTREE)]
    )
    skeleton = prune(doc, ruleset)
    chain = skeleton.roots[0].children[0]
    assert chain.meta.span_id == "0001"
    assert [c.collapsed for c in chain.children] == [True]
    assert chain.children[0].collapsed_count == 2
    assert_invariants(doc, skeleton.roots)


def test_orphan_parent_becomes_root() -> None:
    """父节点缺失（采样丢弃）时降级为根，而不是报错。"""
    spans = [
        _make_span(1, 99, SpanKind.TOOL, Status.OK, 10),
        _make_span(2, 1, SpanKind.TOOL, Status.OK, 10),
    ]
    roots, _nodes = build_forest(spans)
    assert [r.meta.span_id for r in roots] == ["0001"]


def test_cycle_is_broken() -> None:
    """父子指针成环时断边提升为根，不能死循环。"""
    a = _make_span(1, 2, SpanKind.TOOL, Status.OK, 10)
    b = _make_span(2, 1, SpanKind.TOOL, Status.OK, 10)
    roots, _nodes = build_forest([a, b])
    depths = compute_depths(roots)
    assert set(depths) == {"0001", "0002"}


def test_longer_cycle_does_not_blow_up() -> None:
    """三节点环：断边提升为根，且不触发逐字段 __eq__ 在 children 上的无限递归。

    节点按身份比较（SpanNode eq=False），成环也不会让 `node in siblings`
    递归到栈溢出——这条用例把这个保证钉死。
    """
    spans = [
        _make_span(1, 3, SpanKind.TOOL, Status.OK, 10),
        _make_span(2, 1, SpanKind.TOOL, Status.OK, 10),
        _make_span(3, 2, SpanKind.TOOL, Status.OK, 10),
    ]
    roots, _nodes = build_forest(spans)
    depths = compute_depths(roots)
    assert set(depths) == {"0001", "0002", "0003"}


def test_span_node_uses_identity_equality() -> None:
    """字段值相同的两个节点不相等——身份比较是拓扑定位的正确语义。"""
    m = _make_span(1, None, SpanKind.TOOL, Status.OK, 10)
    n1, n2 = SpanNode(meta=m), SpanNode(meta=m)
    assert n1 != n2
    assert n1 == n1
    assert n1 in [n1] and n1 not in [n2]


def test_rebuild_does_not_mutate_input() -> None:
    """rebuild 产出新树，原始森林保持可用（供交叉验证）。"""
    spans = [
        _make_span(0, None, SpanKind.AGENT, Status.OK, 10),
        _make_span(1, 0, SpanKind.TOOL, Status.OK, 10),
    ]
    roots, _nodes = build_forest(spans)
    before = len(roots[0].children)
    rebuild(roots, {"0001"})
    assert len(roots[0].children) == before
