"""盲测用 Trace 生成器（真实 Agent 测试）。

与 make_demo_fixture.py 的区别：这里的根因是**运行时用 os.urandom 熵随机
选定**的——原型、具体参数、触发轮次、span_id 全部随机。生成器把答案密封写入
一个单独的文件（out/blind_answer_key.SEALED.json），生成者（Claude）在被测
子 Agent 给出诊断之前不打开它，以此构成一次真正的盲测。

stdout 只打印**不泄露 bug 内容**的统计量：span 数、文件体积、根因 span 的
字节偏移、以及它相对给定窗口预算落在窗口内还是窗口外。

所有 4 个原型共享同一种“静默失败”结构：内层工具如实报 ERROR，外层封装层把
错误吞掉并标成 OK，错误字符串被当作正常数据进入后续每一轮 prompt，最终答案
基于被污染的上下文编造而成。骨架上的结构信号（父/兄 OK、子 ERROR）因此保持
一致，而“错在哪、错的具体内容、如何传播”才是被测 Agent 要盲查出来的东西。

用法：
    python scripts/blind_bug_fixture.py out/blind_trace.json out/blind_answer_key.SEALED.json
"""

from __future__ import annotations

import json
import os
import random
import sys
from pathlib import Path

ROUNDS = 24
TOOLS_PER_ROUND = 15
_TRACE_ID = "9d1e4c8b2a7f4e0b8c3d5a6f1e2b7c40"

# 4 个根因原型。每个给出：内层工具名、外层封装名、错误码、错误串模板、
# 一组候选“错值”与其对应“对值”，以及修复说明模板。
_ARCHETYPES = [
    {
        "key": "sql_wrong_table",
        "inner_tool": "sql_query",
        "wrapper_tool": "tool_executor",
        "code": "42P01",
        "candidates": [
            ("revenue_q3", "revenue_2025q3"),
            ("orders_2024", "orders_fact_2024"),
            ("user_events", "user_events_v2"),
            ("sales_apac", "sales_apac_2025"),
        ],
        "err_tmpl": 'relation "{wrong}" does not exist',
        "inner_inputs": lambda wrong: {
            "sql": f"SELECT line, SUM(amount) FROM {wrong} GROUP BY line"
        },
        "fix_tmpl": '表名写错：应为 "{right}"，实际写成 "{wrong}"。',
    },
    {
        "key": "http_404_endpoint",
        "inner_tool": "http_get",
        "wrapper_tool": "api_router",
        "code": "HTTP_404",
        "candidates": [
            ("/api/v1/revenue", "/api/v2/revenue"),
            ("/metrics/q3", "/metrics/2025q3"),
            ("/reports/segment", "/reports/segments"),
            ("/finance/summary", "/finance/summaries"),
        ],
        "err_tmpl": '404 Not Found: endpoint "{wrong}" has been removed',
        "inner_inputs": lambda wrong: {"method": "GET", "url": f"https://api.internal{wrong}"},
        "fix_tmpl": 'API 路径过时：应为 "{right}"，实际请求的是 "{wrong}"。',
    },
    {
        "key": "auth_401_token",
        "inner_tool": "fetch_metrics",
        "wrapper_tool": "service_call",
        "code": "HTTP_401",
        "candidates": [
            ("svc-token-legacy", "svc-token-2025"),
            ("readonly-key", "analytics-key"),
            ("dashboard-cred", "dashboard-cred-rotated"),
            ("bi-service-old", "bi-service-new"),
        ],
        "err_tmpl": '401 Unauthorized: credential "{wrong}" expired',
        "inner_inputs": lambda wrong: {"resource": "revenue_metrics", "credential": wrong},
        "fix_tmpl": '用了过期凭据 "{wrong}"，应改用 "{right}"。',
    },
    {
        "key": "missing_config_key",
        "inner_tool": "load_dataset",
        "wrapper_tool": "config_loader",
        "code": "KeyError",
        "candidates": [
            ("REVENUE_DB_URL", "REVENUE_DB_URL_2025"),
            ("WAREHOUSE_DSN", "WAREHOUSE_DSN_PROD"),
            ("METRICS_BUCKET", "METRICS_BUCKET_V2"),
            ("FINANCE_PATH", "FINANCE_PATH_CURRENT"),
        ],
        "err_tmpl": "KeyError: config key '{wrong}' is not set",
        "inner_inputs": lambda wrong: {"dataset": "q3_revenue", "config_key": wrong},
        "fix_tmpl": "配置键名过时：应读 '{right}'，实际读的是 '{wrong}'。",
    },
]


class _Builder:
    def __init__(self, seed: int) -> None:
        self.rng = random.Random(seed)
        self.spans: list[dict[str, object]] = []
        self.t = 1_760_000_000_000_000_000
        self.history: list[str] = []

    def _next_id(self) -> str:
        return f"{self.rng.getrandbits(64):016x}"

    def add(self, name, span_type, parent, inputs, outputs, duration_ms,
            status="OK", status_message="") -> str:
        span_id = self._next_id()
        start = self.t
        self.t += duration_ms * 1_000_000
        self.spans.append({
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
        })
        return span_id


def _web_page(rng: random.Random, topic: str) -> str:
    sentences = [
        f"关于{topic}的公开资料显示，行业整体保持增长态势。",
        "分析师普遍认为该趋势将在下一季度延续，但存在一定不确定性。",
        "Revenue growth in the enterprise segment has outpaced the broader market.",
        "需要注意的是，不同口径的统计结果之间存在差异，引用时应注明来源。",
        "The report does not disclose segment-level figures for the period in question.",
    ]
    return " ".join(rng.choice(sentences) for _ in range(120))


def build(entropy: int) -> tuple[dict, dict]:
    rng = random.Random(entropy)
    arch = rng.choice(_ARCHETYPES)
    wrong, right = rng.choice(arch["candidates"])
    bug_round = rng.randint(8, 20)
    err_msg = arch["err_tmpl"].format(wrong=wrong)

    b = _Builder(seed=entropy ^ 0x5DEECE66D)
    root = b.add("research_agent", "AGENT", None,
                 {"question": "2025 年 Q3 的营收是多少？按业务线拆分。"},
                 {"answer": "（见最终轮输出）"}, 48_200)

    b.history.append("[system] 你是一个数据分析助手。必须严格依据工具返回的数据回答，"
                     "不得编造。若工具返回错误，必须如实说明错误，不得把错误信息当作数据继续推理。")
    b.history.append("[user] 2025 年 Q3 的营收是多少？按业务线拆分。")

    root_cause_span = ""
    wrapper_span = ""
    for round_no in range(1, ROUNDS + 1):
        turn = b.add(f"turn_{round_no}", "CHAIN", root,
                     {"round": round_no}, {"round": round_no}, 3_500)
        prompt = "\n".join(b.history)
        completion = (f"第 {round_no} 轮：我需要先查询营收数据，再按业务线拆分。"
                      f"计划调用工具并整理结果。")
        model = b.add("gpt-4o", "LLM", turn,
                      {"messages": [{"role": "user", "content": prompt}],
                       "model": "gpt-4o", "temperature": 0},
                      {"content": completion, "usage": {"input_tokens": len(prompt) // 4}},
                      2_100)
        b.history.append(f"[assistant#{model[:6]}] {completion}")

        for i in range(TOOLS_PER_ROUND):
            if round_no == bug_round and i == 0:
                inner = b.add(arch["inner_tool"], "TOOL", turn,
                              arch["inner_inputs"](wrong),
                              {"error": err_msg, "code": arch["code"]},
                              200, status="ERROR", status_message=err_msg)
                b.add(arch["wrapper_tool"], "TOOL", turn,
                      {"tool": arch["inner_tool"], "child_span": inner},
                      {"result": err_msg}, 210, status="OK")
                b.history.append(f"[tool:{arch['inner_tool']}] {err_msg}")
                root_cause_span = inner
                wrapper_span = b.spans[-1]["context"]["span_id"]  # type: ignore[index]
                continue
            topic = rng.choice(["企业级业务", "云服务", "订阅收入", "硬件出货"])
            page = _web_page(rng, topic)
            b.add("web_search", "TOOL", turn,
                  {"query": f"2025 Q3 {topic} 营收"},
                  {"results": [{"url": f"https://example.com/{i}", "content": page}]}, 340)
            if i == 0:
                b.history.append(f"[tool:web_search] {page[:200]}…")

    b.add("final_answer", "LLM", root,
          {"messages": [{"role": "user", "content": "\n".join(b.history)}]},
          {"content": "2025 年 Q3 营收数据暂时无法从数据库获取，"
                      "根据公开资料推算，企业级业务约占总营收的 45%。"}, 4_800)

    doc = {
        "info": {"request_id": "tr-blind-0001", "trace_id": _TRACE_ID,
                 "execution_time_ms": 48_200},
        "data": {"spans": b.spans},
    }
    key = {
        "archetype": arch["key"],
        "inner_tool": arch["inner_tool"],
        "wrapper_tool": arch["wrapper_tool"],
        "root_cause_span_id": root_cause_span,
        "wrapper_span_id": wrapper_span,
        "bug_round": bug_round,
        "error_code": arch["code"],
        "error_message": err_msg,
        "wrong_value": wrong,
        "right_value": right,
        "fix": arch["fix_tmpl"].format(wrong=wrong, right=right),
        "propagation": "内层工具报 ERROR → 外层封装标 OK 并把错误串当结果 → "
                       "错误串进入后续每轮 prompt → 最终答案基于被污染上下文编造。",
    }
    return doc, key


def main() -> int:
    trace_out = Path(sys.argv[1] if len(sys.argv) > 1 else "out/blind_trace.json")
    key_out = Path(sys.argv[2] if len(sys.argv) > 2 else "out/blind_answer_key.SEALED.json")
    entropy = int.from_bytes(os.urandom(8), "big")

    doc, key = build(entropy)
    trace_out.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(doc, ensure_ascii=False, indent=2).encode("utf-8")
    trace_out.write_bytes(payload)
    key_out.write_text(json.dumps(key, ensure_ascii=False, indent=2), encoding="utf-8")

    # 计算根因 span 的字节偏移（只暴露位置，不暴露内容）
    rc_id = key["root_cause_span_id"].encode()
    offset = payload.find(rc_id)
    n_spans = len(doc["data"]["spans"])  # type: ignore[index]

    WINDOW_TOKENS = 128_000
    window_bytes = WINDOW_TOKENS * 4  # 粗略 4 字节/token（与验证报告一致口径）
    inside = "窗口内" if offset <= window_bytes else "窗口外"

    print(f"span 总数            : {n_spans}")
    print(f"文件体积            : {len(payload):,} 字节 ({len(payload)/1024/1024:.2f} MB)")
    print(f"根因 span 字节偏移   : {offset:,}")
    print(f"128K 窗口字节预算    : {window_bytes:,}")
    print(f"根因相对窗口        : {inside}")
    print(f"密封答案已写入      : {key_out}（生成者在评分前不打开）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
