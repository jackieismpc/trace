"""拓扑重建：剪枝里唯一容易出错、也唯一必须形式化的部分（方案 §5.3、附录 A5）。

**「拓扑完整」不等于「一个节点都不能删」。** 可以删，但不能出现孤儿节点：
删掉中间节点 B 时，B 的子节点必须重挂到最近的存活祖先上，并留下「此处折叠了
几层」的痕迹。否则骨架会让人误以为调用链是扁平的——一个会撒谎的骨架比没有骨架
更糟。

算法四步：

1. 按规则给每个 span 求出动作（Keep / Drop / CollapseSubtree / Truncate）；
2. 对每个**显式** Keep 节点、以及受硬保护的节点，把到根的祖先链强制保留；
3. 对 Drop 节点，把其存活后代重挂到最近存活祖先，并在边上记 ``elided_depth``；
4. 把归属于同一存活父节点的被删节点按类型合并成占位节点。

五条不变量（由 `tests/test_topology.py` 用 hypothesis 钉死）：

① 输出是合法森林（无环、单亲、根可达）——防结构性损坏；
② 祖先关系在存活节点间单调保持——**核心**，保证 Agent 在骨架上读到的因果关系
   在真实执行中一定成立；
③ 存活节点的相对深度顺序不变——防层级错乱；
④ 无孤儿节点；
⑤ 守恒律：存活节点数 + 各占位节点汇总数 = 原节点数——防节点静默蒸发。

关于⑤的一处收紧：方案原文写的是「``elided_depth`` 之和加存活节点数等于原节点
数」。当一个被删节点有多条存活分支时，它会被多个子节点的 ``elided_depth``
各计一次，那条等式并不成立。这里把守恒律实现为严格形式：**每个被删节点恰好
归属于一个占位节点**（它的最近存活祖先下、按类型分组的那个），``elided_depth``
退回其本义——「这条边上折叠了几层」，只用于展示。
"""

from __future__ import annotations

from collections import defaultdict

from ..model import ByteRange, KindSource, SpanKind, SpanMeta, SpanNode, Status

__all__ = ["build_forest", "compute_depths", "rebuild"]


def build_forest(spans: list[SpanMeta]) -> tuple[list[SpanNode], dict[str, SpanNode]]:
    """由扁平的 span 列表重建原始调用森林。

    两种脏数据在真实 Trace 里都出现过，这里都按「降级为根」处理而不是报错：
    * ``parent_id`` 指向一个不存在的 span（父 span 被采样丢弃）；
    * 父子指针成环（埋点 bug）。

    :return: ``(根节点列表, span_id → 节点)``
    """
    nodes: dict[str, SpanNode] = {}
    for meta in spans:
        # 同 id 重复出现时后者覆盖前者，保持与 json 对象重复 key 的语义一致
        nodes[meta.span_id] = SpanNode(meta=meta)

    roots: list[SpanNode] = []
    for meta in spans:
        node = nodes[meta.span_id]
        parent_id = meta.parent_id
        if parent_id and parent_id in nodes and parent_id != meta.span_id:
            nodes[parent_id].children.append(node)
        else:
            roots.append(node)

    # 环检测：从根出发可达的才算合法，其余按出现顺序补成根
    reachable: set[str] = set()
    stack = list(roots)
    while stack:
        cur = stack.pop()
        if cur.meta.span_id in reachable:
            continue
        reachable.add(cur.meta.span_id)
        stack.extend(cur.children)

    for meta in spans:
        if meta.span_id not in reachable:
            node = nodes[meta.span_id]
            parent_id = meta.parent_id
            if parent_id and parent_id in nodes:
                # 断开成环的那条边，把节点提升为根
                siblings = nodes[parent_id].children
                if node in siblings:
                    siblings.remove(node)
            roots.append(node)
            stack = [node]
            while stack:
                cur = stack.pop()
                if cur.meta.span_id in reachable:
                    continue
                reachable.add(cur.meta.span_id)
                stack.extend(cur.children)

    return roots, nodes


def compute_depths(roots: list[SpanNode]) -> dict[str, int]:
    """计算每个节点在原始森林中的深度（根为 0）。"""
    depths: dict[str, int] = {}
    stack: list[tuple[SpanNode, int]] = [(r, 0) for r in reversed(roots)]
    while stack:
        node, d = stack.pop()
        depths[node.meta.span_id] = d
        for child in reversed(node.children):
            stack.append((child, d + 1))
    return depths


def _preorder(roots: list[SpanNode]) -> list[SpanNode]:
    out: list[SpanNode] = []
    stack = list(reversed(roots))
    while stack:
        node = stack.pop()
        out.append(node)
        stack.extend(reversed(node.children))
    return out


def _placeholder(kind: SpanKind, count: int, all_ok: bool, parent_id: str | None) -> SpanNode:
    """造一个「此处折叠了 N 个同类节点」的占位节点。"""
    meta = SpanMeta(
        span_id="",
        parent_id=parent_id,
        trace_id="",
        name=f"elided {count} similar {kind.value} spans",
        kind=kind,
        # 占位节点的类型来自被折叠成员的共同类型，不是推断出来的
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


def rebuild(
    roots: list[SpanNode],
    dropped: set[str],
) -> list[SpanNode]:
    """按被删集合重建森林，返回新的根节点列表。

    输入的 ``roots`` 不会被修改：本函数产出的是一片**新**的节点树，
    原始森林仍可用于统计与交叉验证。

    :param roots: 原始森林
    :param dropped: 被删除的 span_id 集合（根节点必须不在其中，由引擎的硬保护保证）
    """
    parent_of: dict[str, str | None] = {}
    order: list[SpanNode] = _preorder(roots)
    for node in order:
        for child in node.children:
            parent_of[child.meta.span_id] = node.meta.span_id
    for r in roots:
        parent_of.setdefault(r.meta.span_id, None)

    # 每个节点找最近的存活祖先，同时数出中间隔了几层
    nearest_alive: dict[str, str | None] = {}
    elided_between: dict[str, int] = {}
    for node in order:
        sid = node.meta.span_id
        depth = 0
        cur = parent_of.get(sid)
        while cur is not None and cur in dropped:
            depth += 1
            cur = parent_of.get(cur)
        nearest_alive[sid] = cur
        elided_between[sid] = depth

    # 新节点：只为存活节点建
    new_nodes: dict[str, SpanNode] = {}
    for node in order:
        sid = node.meta.span_id
        if sid in dropped:
            continue
        fresh = SpanNode(meta=node.meta)
        fresh.elided_depth = elided_between[sid]
        fresh.truncated_fields = list(node.truncated_fields)
        new_nodes[sid] = fresh

    new_roots: list[SpanNode] = []
    for node in order:
        sid = node.meta.span_id
        if sid in dropped:
            continue
        parent_sid = nearest_alive[sid]
        if parent_sid is None:
            new_roots.append(new_nodes[sid])
        else:
            new_nodes[parent_sid].children.append(new_nodes[sid])

    # 被删节点按「最近存活祖先 × 类型」分组，合并成占位节点。
    # 每个被删节点恰好进一个组——这正是守恒律⑤成立的原因。
    groups: dict[tuple[str | None, SpanKind], list[SpanNode]] = defaultdict(list)
    for node in order:
        sid = node.meta.span_id
        if sid not in dropped:
            continue
        groups[(nearest_alive[sid], node.meta.kind)].append(node)

    for (parent_sid, kind), members in sorted(
        groups.items(), key=lambda kv: (kv[0][0] or "", kv[0][1].value)
    ):
        all_ok = all(m.meta.status is not Status.ERROR for m in members)
        holder = _placeholder(kind, len(members), all_ok, parent_sid)
        if parent_sid is None:
            new_roots.append(holder)
        else:
            new_nodes[parent_sid].children.append(holder)

    return new_roots
