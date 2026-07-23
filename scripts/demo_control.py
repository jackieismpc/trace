"""自验对照组：不用工具，直接把原始 Trace 的前 128K token 喂给模型（方案 4.2）。

没有对照组就说不清是工具起了作用，还是模型本身就能猜出来。两组之间唯一的变量
是「是否使用工具」——Trace、任务、窗口大小全部固定。

这里同样不调用 LLM，因为对照组的失败可以**在数据层面直接判定**：如果根因所在的
span 根本没进窗口，那么无论用什么模型、怎么提示，它都不可能基于证据给出正确结论。
证据不可达是比模型能力更硬的约束。

用法：
    uv run python scripts/demo_control.py
"""

from __future__ import annotations

import argparse
from pathlib import Path

from tracelens.ingest.reader import TraceReader
from tracelens.model import Status
from tracelens.tokens import count_tokens, estimate_tokens

WINDOW_TOKENS = 128_000


def _prefix_bytes_for_budget(text: str, budget_tokens: int) -> int:
    """二分出「前多少字节的文本」刚好等于给定的 token 预算。"""
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if estimate_tokens(text[:mid]) <= budget_tokens:
            lo = mid
        else:
            hi = mid - 1
    return len(text[:lo].encode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="tracelens 自验对照组")
    parser.add_argument("--input", default="out/demo_trace.json")
    parser.add_argument("--window", type=int, default=WINDOW_TOKENS)
    args = parser.parse_args()

    src = Path(args.input)
    if not src.is_file():
        print(f"找不到 {src}，请先运行：uv run python scripts/make_demo_fixture.py {src}")
        return 1

    text = src.read_text(encoding="utf-8")
    total_tokens = count_tokens(text).tokens
    cutoff = _prefix_bytes_for_budget(text, args.window)
    file_size = len(text.encode("utf-8"))

    print("对照组设置：不使用 tracelens，把原始 Trace 从头截断到窗口上限直接喂给模型")
    print(f"  原始 Trace   ：{file_size:,} 字节，约 {total_tokens:,} token")
    print(f"  窗口         ：{args.window:,} token")
    print(f"  实际能装下   ：前 {cutoff:,} 字节，占全文 {cutoff / file_size:.1%}")

    with TraceReader(src) as reader:
        doc = reader.read()

    in_window = [s for s in doc.spans if s.raw_range.end <= cutoff]
    errors = [s for s in doc.spans if s.status is Status.ERROR]
    errors_in_window = [s for s in errors if s.raw_range.end <= cutoff]

    print(
        f"\n窗口内的 span ：{len(in_window)} / {doc.span_count}"
        f"（{len(in_window) / doc.span_count:.1%}）"
    )
    print(f"ERROR 节点     ：共 {len(errors)} 个，其中落在窗口内的 {len(errors_in_window)} 个")

    for s in errors:
        inside = s.raw_range.end <= cutoff
        where = "窗口内" if inside else "窗口外"
        print(
            f"  {s.span_id[:6]} {s.name}：字节偏移 {s.raw_range.start:,}"
            f"（窗口截断在 {cutoff:,}）→ {where}"
        )

    print()
    if errors_in_window:
        print("对照组**能**看到 ERROR 节点——这份 demo 数据不足以体现差异，需要调大轮数。")
        return 1

    print("对照组结论：失败，且失败模式可预判。")
    print("  前 128K token 几乎全被前几轮的巨型 prompt 占满，出错的 sql_query span")
    print("  根本进不了窗口。模型看不到唯一的错误证据，只能基于半截上下文猜测——")
    print("  这个失败本身反向演示了任务动机：不是模型不够强，是证据没送到它面前。")
    print()
    print("实验组（见 demo_investigate.py）：100% 拓扑 + 自选细节，约占窗口 3%，")
    print("成功定位到根因。两组唯一的变量就是是否使用本工具。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
