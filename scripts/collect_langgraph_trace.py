"""可选：用真实 LangGraph Agent 采集一份 MLflow Trace（方案 4.1、1.6）。

**这个脚本不在 CI 里跑，也不是任何测试的依赖。** 它需要额外的第三方依赖与一个
可用的模型 API key，属于「想拿真实数据验证适配器时再用」的工具。

验证报告用的是 `make_demo_fixture.py` 生成的离线 fixture——那份数据是确定性的、
可复现的，报告里的每个数字都能被任何人在任何机器上重跑出来。真实采集的 Trace
反而做不到这一点（模型输出不确定、轮数不固定）。两者分工明确：
离线 fixture 负责**可复现的量化结论**，本脚本负责**验证真实字段名**（方案 R2）。

准备：
    uv pip install mlflow langgraph langchain-openai
    export OPENAI_API_KEY=...      # 或按 LangChain 的约定配置其他供应商

运行：
    uv run python scripts/collect_langgraph_trace.py out/real_trace.json

产出的文件可直接喂给 tracelens：
    uv run tracelens inspect --input out/real_trace.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# 与 demo fixture 保持同一个植入 bug：表名写错，且工具层把错误吞掉
WRONG_TABLE = "revenue_q3"
DB_ERROR = f'relation "{WRONG_TABLE}" does not exist'


def _require(module: str) -> object:
    try:
        return __import__(module)
    except ImportError:  # pragma: no cover - 仅在缺依赖时触发
        print(
            f"缺少依赖 {module}。请先执行：\n    uv pip install mlflow langgraph langchain-openai",
            file=sys.stderr,
        )
        raise SystemExit(1) from None


def main() -> int:  # pragma: no cover - 需要外部依赖与网络，不纳入测试
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "out/real_trace.json")
    out.parent.mkdir(parents=True, exist_ok=True)

    mlflow = _require("mlflow")
    _require("langgraph")

    mlflow.set_experiment("tracelens-demo")
    mlflow.langchain.autolog()

    from langchain_core.tools import tool
    from langchain_openai import ChatOpenAI
    from langgraph.prebuilt import create_react_agent

    @tool
    def sql_query(sql: str) -> str:
        """执行 SQL 查询。注意：这里刻意保留了「吞掉错误」的行为。"""
        if WRONG_TABLE in sql:
            # 真实世界里最常见的反模式：捕获异常后把错误信息当成普通字符串返回，
            # 调用方无从区分「查到了这个内容」和「查询失败了」
            return DB_ERROR
        return json.dumps({"lines": {"enterprise": 1234, "cloud": 5678}}, ensure_ascii=False)

    @tool
    def web_search(query: str) -> str:
        """检索公开资料（返回一大段无结构正文，模拟真实工具输出的体积）。"""
        return "公开资料显示行业整体保持增长态势。" * 200

    agent = create_react_agent(
        ChatOpenAI(model="gpt-4o", temperature=0),
        [sql_query, web_search],
    )
    agent.invoke(
        {
            "messages": [
                (
                    "user",
                    f"用 SQL 查询 {WRONG_TABLE} 表统计 2025 年 Q3 各业务线营收，"
                    "并结合公开资料交叉验证，多查几轮确保准确。",
                )
            ]
        }
    )

    trace = mlflow.get_last_active_trace()
    if trace is None:
        print("没有采集到 Trace，请确认 mlflow.langchain.autolog() 已生效", file=sys.stderr)
        return 1

    out.write_text(trace.to_json(), encoding="utf-8")
    print(f"已写出 {out}（{out.stat().st_size:,} 字节）")
    print("接下来可以运行：")
    print(f"    uv run tracelens inspect --input {out}")
    print(f"    uv run tracelens skeleton --input {out} --config examples/rules.toml")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
