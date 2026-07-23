"""自验实验组：Agent 用 tracelens 定位根因的完整闭环（方案 4.1）。

这个脚本**不调用任何 LLM**。它把一个 Agent 拿到骨架后会走的推理路径逐步复现出来，
每一步都打印「这一步实际消耗了多少数据」——因为要论证的命题是数据量层面的：

    128K 窗口下，原始 Trace 只能塞进 4%，而且 Agent 无法选择塞哪 4%；
    用了这个工具之后，Agent 拿到 100% 的拓扑加上自己选定的关键细节，只占 5% 的窗口。

不调 LLM 是刻意的：如果结论依赖某个模型某次运行的表现，它就不可复现，
也说不清是工具起了作用还是模型碰巧猜对了。这里论证的是**信息可达性**——
根因所需的证据是否落在 Agent 能拿到的那部分数据里。模型能力那一维由对照组
（`demo_control.py`）从反面补上。

用法：
    uv run python scripts/make_demo_fixture.py out/demo_trace.json
    uv run python scripts/demo_investigate.py
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from tracelens.index.reader import TraceIndex, write_index
from tracelens.ingest.reader import TraceReader
from tracelens.model import Status
from tracelens.prune.engine import prune
from tracelens.prune.rules import load_ruleset
from tracelens.render import render_tree
from tracelens.tokens import count_tokens

WINDOW_TOKENS = 128_000
"""对标的上下文窗口大小。"""


def _step(n: int, title: str) -> None:
    print(f"\n{'=' * 72}\n步骤 {n}：{title}\n{'=' * 72}")


def main() -> int:
    parser = argparse.ArgumentParser(description="tracelens 自验实验组")
    parser.add_argument("--input", default="out/demo_trace.json")
    parser.add_argument("--config", default="examples/demo_rules.toml")
    parser.add_argument("--index", default="out/demo.idx")
    args = parser.parse_args()

    src = Path(args.input)
    if not src.is_file():
        print(f"找不到 {src}，请先运行：uv run python scripts/make_demo_fixture.py {src}")
        return 1

    file_size = src.stat().st_size
    # 累积「Agent 实际读进上下文的文本」，最后统一计 token——
    # 用字符数乘系数会低估中文，这里直接对真实文本计数
    consumed: list[str] = []

    # ---- 步骤 1：生成骨架 --------------------------------------------------
    _step(1, "生成骨架与索引")
    ruleset = load_ruleset(args.config)
    with TraceReader(src) as reader:
        doc = reader.read()
        skeleton = prune(
            doc,
            ruleset,
            buf=reader.buf,
            payload_fields_fn=reader.payload_fields,
            source_file=str(src),
        )
        write_index(args.index, doc.spans, reader.buf)
        skeleton_text = render_tree(skeleton)

    skeleton_tokens = count_tokens(skeleton_text).tokens
    consumed.append(skeleton_text)
    print(f"原始 Trace     ：{file_size:,} 字节，{doc.span_count} 个 span")
    print(f"骨架           ：{len(skeleton_text):,} 字符，约 {skeleton_tokens:,} token")
    print(f"骨架占原文件   ：{len(skeleton_text.encode('utf-8')) / file_size:.2%}")
    print(f"骨架占 128K 窗口：{skeleton_tokens / WINDOW_TOKENS:.1%}")
    print(
        f"拓扑完整性     ：{doc.span_count} 个 span 全部有交代"
        f"（{skeleton.kept_span_count} 个存活节点 + 占位节点汇总）"
    )

    # ---- 步骤 2：在骨架上定位可疑节点 --------------------------------------
    _step(2, "Agent 读骨架，定位唯一的 ERROR 节点")
    error_nodes = [
        n for n in skeleton.all_nodes() if not n.collapsed and n.meta.status is Status.ERROR
    ]
    if not error_nodes:
        print("骨架里没有 ERROR 节点，demo 数据可能不对")
        return 1
    target = error_nodes[0]
    print(f"命中：{target.meta.span_id[:6]} {target.meta.kind.value} {target.meta.name}")
    print(f"错误信息：{target.meta.status_message}")

    # 关键观察：父节点状态是 OK ——错误被吞掉了
    by_id = {s.span_id: s for s in doc.spans}
    parent = by_id.get(target.meta.parent_id or "")
    siblings = [s for s in doc.spans if s.parent_id == (parent.span_id if parent else None)]
    swallower = next(
        (s for s in siblings if s.name == "tool_executor" and s.status is Status.OK), None
    )
    if swallower is not None:
        print(
            f"\n可疑点：同层的 {swallower.span_id[:6]} {swallower.name} 状态是 "
            f"{swallower.status.value}——下游出错、上游却报成功，"
            "说明错误在工具封装层被吞掉了。"
        )

    # ---- 步骤 3：expand 取证 ----------------------------------------------
    _step(3, "expand 取回出错 span 的完整 Payload")
    index = TraceIndex.load(args.index)
    with TraceReader(src) as reader:
        raw = index.expand(reader.buf, target.meta.span_id)
    consumed.append(raw.decode("utf-8"))
    print(f"取回 {len(raw):,} 字节：")
    print(json.dumps(json.loads(raw)["attributes"], ensure_ascii=False, indent=2)[:400])

    # ---- 步骤 4：验证错误是否流入下游 prompt -------------------------------
    _step(4, "expand 下游 MODEL span，确认错误字符串被当作数据进了 prompt")
    error_text = target.meta.status_message
    downstream = [
        s for s in doc.spans if s.kind.value == "MODEL" and s.start_ns > target.meta.start_ns
    ]
    hit = None
    with TraceReader(src) as reader:
        for span in downstream:
            fields = {f.path: f for f in reader.payload_fields(span)}
            key = '$.attributes["mlflow.spanInputs"]'
            if key not in fields:
                continue
            f = fields[key]
            # MLflow 把 attribute 值再包了一层 JSON 字符串：
            # 外层 loads 得到 JSON 文本，内层 loads 才是真正的 inputs 对象。
            # 这是「解析元数据、用后即弃」的只读路径，不影响 expand 的字节承诺。
            payload = json.loads(json.loads(reader.slice(f.start, f.end).decode("utf-8")))
            content = payload["messages"][0]["content"]
            if error_text in content:
                hit = (span, len(content))
                # Agent 只需要看命中处上下文的一小段，不必把整个 prompt 读进来
                pos = content.index(error_text)
                consumed.append(content[max(0, pos - 200) : pos + 200])
                break

    if hit is None:
        print("下游 prompt 里没有找到该错误字符串——与预期不符")
        return 1
    span, length = hit
    print(f"命中：{span.span_id[:6]} {span.name}，prompt 共 {length:,} 字符")
    print(f"其中包含原样的错误串：{error_text!r}")
    print("→ 也就是说，数据库错误没有被当成错误处理，而是作为「工具返回的数据」")
    print("  进入了后续每一轮的上下文，模型据此编出了一个看起来合理的答案。")

    # ---- 步骤 5：结论 ------------------------------------------------------
    _step(5, "根因")
    print(f'根因：SQL 查询用了不存在的表 "{"revenue_q3"}"，数据库返回 42P01；')
    print(
        f"      工具封装层 {swallower.span_id[:6] if swallower else '(未命中)'} "
        "把错误吞掉并标成 OK，"
    )
    print("      错误字符串随后作为正常数据进入了下游 prompt。")
    print("修复：① 修正表名；② 工具封装层不得吞掉子 span 的错误状态。")

    used_tokens = count_tokens("\n".join(consumed)).tokens
    full_tokens = count_tokens(src.read_text(encoding="utf-8")).tokens
    print(
        f"\n全过程消耗上下文：约 {used_tokens:,} token"
        f"（占 128K 窗口 {used_tokens / WINDOW_TOKENS:.1%}）"
    )
    print(
        f"原始 Trace 整体 ：约 {full_tokens:,} token"
        f"，128K 窗口只装得下其中 {WINDOW_TOKENS / full_tokens:.1%}，"
        "而且无法选择装哪一部分。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
