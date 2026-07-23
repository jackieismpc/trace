"""生成自验用的 demo Trace（方案 4.1）。

复现一个**故意植入 bug** 的 LangGraph 风格 Agent 执行：

    bug：SQL 表名写错（``revenue_q3`` 实际叫 ``revenue_2025q3``），
         数据库返回错误 → 工具封装层把错误吞掉、状态标成 OK →
         错误字符串被当作正常数据进入后续每一轮的 prompt。

这个 bug 的形态是刻意选的：它**不会让整个执行崩掉**，最终答案看起来也像模像样，
只是内容是错的。真实世界里最难查的就是这一类——有征兆但征兆被掩埋，
而掩埋它的正是「上下文太长塞不进去」。

体积分布也刻意贴近真实：每轮 prompt 携带全量历史，随轮次近似平方级膨胀，
少数 MODEL span 贡献了绝大部分字节，而拓扑信息占比不到 1%。

用法：
    uv run python scripts/make_demo_fixture.py out/demo_trace.json
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

# 表名写错——整个 demo 的根因
WRONG_TABLE = "revenue_q3"
RIGHT_TABLE = "revenue_2025q3"
DB_ERROR = f'relation "{WRONG_TABLE}" does not exist'

ROUNDS = 24
"""Agent 的推理轮数。"""

TOOLS_PER_ROUND = 15
"""每轮的工具调用数（含检索与若干次重试）。"""

BUG_ROUND = 9
"""在第几轮触发那个被吞掉的 SQL 错误。

放在中段而不是开头是刻意的：前几轮的 prompt 还不够大，错误 span 会落在 128K
窗口以内，对照组就演示不出「证据根本进不了窗口」这件事。放在第 9 轮时，
它的字节偏移已经远超窗口截断点。"""

_TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"


class _Builder:
    """按执行顺序累积 span，同时维护递增的对话历史。"""

    def __init__(self, seed: int = 20260723) -> None:
        self.rng = random.Random(seed)
        self.spans: list[dict[str, object]] = []
        self.t = 1_760_000_000_000_000_000
        self.counter = 0
        self.history: list[str] = []

    def _next_id(self) -> str:
        # 真实的 span_id 是随机的 16 位十六进制；顺序编号会让短前缀全都撞在一起，
        # 演示不出「照抄 6 位就能展开」的体验
        self.counter += 1
        return f"{self.rng.getrandbits(64):016x}"

    def add(
        self,
        name: str,
        span_type: str,
        parent: str | None,
        inputs: object,
        outputs: object,
        duration_ms: int,
        status: str = "OK",
        status_message: str = "",
    ) -> str:
        span_id = self._next_id()
        start = self.t
        self.t += duration_ms * 1_000_000
        self.spans.append(
            {
                "name": name,
                "context": {"trace_id": _TRACE_ID, "span_id": span_id},
                "parent_id": parent,
                "start_time": start,
                "end_time": start + duration_ms * 1_000_000,
                "status_code": status,
                "status_message": status_message,
                "attributes": {
                    "mlflow.spanType": json.dumps(span_type),
                    "mlflow.spanInputs": json.dumps(inputs, ensure_ascii=False),
                    "mlflow.spanOutputs": json.dumps(outputs, ensure_ascii=False),
                },
            }
        )
        return span_id


def _web_page(rng: random.Random, topic: str) -> str:
    """造一段网页正文——真实 Trace 里工具返回的大块无结构文本。"""
    sentences = [
        f"关于{topic}的公开资料显示，行业整体保持增长态势。",
        "分析师普遍认为该趋势将在下一季度延续，但存在一定不确定性。",
        "Revenue growth in the enterprise segment has outpaced the broader market.",
        "需要注意的是，不同口径的统计结果之间存在差异，引用时应注明来源。",
        "The report does not disclose segment-level figures for the period in question.",
    ]
    return " ".join(rng.choice(sentences) for _ in range(120))


def build() -> dict[str, object]:
    b = _Builder()
    root = b.add(
        name="research_agent",
        span_type="AGENT",
        parent=None,
        inputs={"question": "2025 年 Q3 的营收是多少？按业务线拆分。"},
        outputs={"answer": "（见最终轮输出）"},
        duration_ms=48_200,
    )

    system_prompt = (
        "你是一个数据分析助手。必须严格依据工具返回的数据回答，不得编造。"
        "若工具返回错误，必须如实说明错误，不得把错误信息当作数据继续推理。"
    )
    b.history.append(f"[system] {system_prompt}")
    b.history.append("[user] 2025 年 Q3 的营收是多少？按业务线拆分。")

    for round_no in range(1, ROUNDS + 1):
        turn = b.add(
            name=f"turn_{round_no}",
            span_type="CHAIN",
            parent=root,
            inputs={"round": round_no},
            outputs={"round": round_no},
            duration_ms=3_500,
        )

        # 每轮的模型调用都携带**全量历史**——体积随轮次近似平方级增长
        prompt = "\n".join(b.history)
        completion = (
            f"第 {round_no} 轮：我需要先查询营收数据，再按业务线拆分。"
            f"计划调用 sql_query 与 web_search。"
        )
        model = b.add(
            name="gpt-4o",
            span_type="LLM",
            parent=turn,
            inputs={
                "messages": [{"role": "user", "content": prompt}],
                "model": "gpt-4o",
                "temperature": 0,
            },
            outputs={"content": completion, "usage": {"input_tokens": len(prompt) // 4}},
            duration_ms=2_100,
        )
        b.history.append(f"[assistant#{model[:6]}] {completion}")

        for i in range(TOOLS_PER_ROUND):
            if round_no == BUG_ROUND and i == 0:
                # ---- 植入的 bug ----------------------------------------
                # 内层 sql_query 如实报 ERROR
                sql = b.add(
                    name="sql_query",
                    span_type="TOOL",
                    parent=turn,
                    inputs={"sql": f"SELECT line, SUM(amount) FROM {WRONG_TABLE} GROUP BY line"},
                    outputs={"error": DB_ERROR, "code": "42P01"},
                    duration_ms=200,
                    status="ERROR",
                    status_message=DB_ERROR,
                )
                # 外层封装层把错误吞掉，状态标成 OK——这就是「有征兆但被掩埋」
                b.add(
                    name="tool_executor",
                    span_type="TOOL",
                    parent=turn,
                    inputs={"tool": "sql_query", "child_span": sql},
                    outputs={"result": DB_ERROR},  # 错误字符串被当作正常返回值
                    duration_ms=210,
                    status="OK",
                )
                # 并且进入后续每一轮的 prompt
                b.history.append(f"[tool:sql_query] {DB_ERROR}")
                continue

            topic = b.rng.choice(["企业级业务", "云服务", "订阅收入", "硬件出货"])
            page = _web_page(b.rng, topic)
            b.add(
                name="web_search",
                span_type="TOOL",
                parent=turn,
                inputs={"query": f"2025 Q3 {topic} 营收"},
                outputs={"results": [{"url": f"https://example.com/{i}", "content": page}]},
                duration_ms=340,
            )
            if i == 0:
                b.history.append(f"[tool:web_search] {page[:200]}…")

    # 最终答案：基于被吞掉的错误信息编出来的，看起来像模像样
    b.add(
        name="final_answer",
        span_type="LLM",
        parent=root,
        inputs={"messages": [{"role": "user", "content": "\n".join(b.history)}]},
        outputs={
            "content": (
                "2025 年 Q3 营收数据暂时无法从数据库获取（relation 相关限制），"
                "根据公开资料推算，企业级业务约占总营收的 45%。"
            )
        },
        duration_ms=4_800,
    )

    return {
        "info": {
            "request_id": "tr-demo-0001",
            "trace_id": _TRACE_ID,
            "execution_time_ms": 48_200,
            "note": f"植入的 bug：表名应为 {RIGHT_TABLE}，实际写成 {WRONG_TABLE}",
        },
        "data": {"spans": b.spans},
    }


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "out/demo_trace.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    doc = build()
    payload = json.dumps(doc, ensure_ascii=False, indent=2).encode("utf-8")
    out.write_bytes(payload)
    spans = doc["data"]["spans"]  # type: ignore[index]
    print(f"已生成 {out}：{len(spans)} 个 span，{len(payload) / 1024 / 1024:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
