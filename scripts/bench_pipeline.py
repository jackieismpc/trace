"""全流程性能基准（方案 §六、3.6）。

三条验收指标（按 Python 重新校定）：

    扫描吞吐        ≥ 200 MB/s
    全流程用时      1 GB Trace ≤ 120 秒（扫描 + 元数据 + 剪枝 + 渲染 + 建索引）
    堆内存峰值      ≤ max(256 MB, 2 × 最大单 span 解析产物)

第三条是本路线要验证的**核心性质**：mmap 只读映射 + 逐 span 解析用后即弃，
使堆占用与「最大单个 span」成正比，而不是与文件大小成正比。

用法：
    uv run python scripts/bench_pipeline.py [目标体积MB]   # 默认 200 MB

1 GB 全量跑一次约需数分钟与 1 GB 磁盘，CI 里跑缩小版即可；正式验收数据用
`--size 1024` 单独跑并记入验证报告。
"""

from __future__ import annotations

import argparse
import tempfile
import time
import tracemalloc
from pathlib import Path

from tracelens.index.reader import build_index
from tracelens.ingest.reader import TraceReader
from tracelens.prune.engine import prune
from tracelens.prune.rules import DEFAULT_RULESET
from tracelens.render import render_tree
from tracelens.testkit import SynthConfig, build_mlflow_trace

MB = 1024 * 1024


def make_file(target_mb: int, path: Path) -> int:
    probe = build_mlflow_trace(SynthConfig(span_count=100, seed=1))
    per_span = len(probe) / 100
    span_count = max(200, int(target_mb * MB / per_span))
    path.write_bytes(build_mlflow_trace(SynthConfig(span_count=span_count, seed=1)))
    return path.stat().st_size


def run(path: Path, trace_memory: bool = False) -> dict[str, float]:
    """跑一遍全流程，返回各阶段耗时与（可选的）堆峰值。

    计时与测内存分两趟跑：``tracemalloc`` 会给每次内存分配加钩子，
    开着它测出来的耗时会失真到数倍——同一趟里既测时间又测内存，
    两个数字都不可信。
    """
    stats: dict[str, float] = {}
    if trace_memory:
        tracemalloc.start()
    t_all = time.perf_counter()

    with TraceReader(path) as reader:
        t0 = time.perf_counter()
        ranges = sum(1 for _ in reader.iter_span_ranges())
        stats["scan_s"] = time.perf_counter() - t0

        t0 = time.perf_counter()
        doc = reader.read()
        stats["parse_s"] = time.perf_counter() - t0

        t0 = time.perf_counter()
        skeleton = prune(
            doc,
            DEFAULT_RULESET,
            buf=reader.buf,
            payload_fields_fn=reader.payload_fields,
            source_file=str(path),
        )
        stats["prune_s"] = time.perf_counter() - t0

        t0 = time.perf_counter()
        text = render_tree(skeleton)
        stats["render_s"] = time.perf_counter() - t0

        t0 = time.perf_counter()
        index = build_index(doc.spans, reader.buf)
        stats["index_s"] = time.perf_counter() - t0

    stats["total_s"] = time.perf_counter() - t_all
    if trace_memory:
        _cur, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        stats["peak_heap_mb"] = peak / MB
    stats["spans"] = float(ranges)
    stats["skeleton_bytes"] = float(len(text.encode("utf-8")))
    stats["index_bytes"] = float(len(index))
    stats["max_span_bytes"] = float(max(s.payload_bytes for s in doc.spans))
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="tracelens 全流程性能基准")
    parser.add_argument("size", nargs="?", type=int, default=200, help="合成文件目标体积（MB）")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "bench.json"
        size = make_file(args.size, path)
        print(f"合成文件：{size / MB:.1f} MB")
        stats = run(path)  # 第一趟：纯计时
        mem = run(path, trace_memory=True)  # 第二趟：只取堆峰值
        stats["peak_heap_mb"] = mem["peak_heap_mb"]

    scan_mbps = size / MB / stats["scan_s"]
    # 全流程用时按 1 GB 线性外推，便于与 120 秒的验收线直接对比
    projected_1gb = stats["total_s"] * (1024 * MB / size)
    heap_limit_mb = max(256.0, 2 * stats["max_span_bytes"] / MB)

    print(f"span 数        ：{int(stats['spans'])}")
    print(f"扫描           ：{stats['scan_s']:.2f}s（{scan_mbps:.0f} MB/s）")
    print(f"元数据解析     ：{stats['parse_s']:.2f}s")
    print(f"剪枝           ：{stats['prune_s']:.2f}s")
    print(f"渲染           ：{stats['render_s']:.2f}s")
    print(f"建索引         ：{stats['index_s']:.2f}s")
    print(f"全流程         ：{stats['total_s']:.2f}s（按 1 GB 线性外推 {projected_1gb:.0f}s）")
    print(f"堆内存峰值     ：{stats['peak_heap_mb']:.1f} MB（上限 {heap_limit_mb:.0f} MB）")
    print(f"最大单 span    ：{stats['max_span_bytes'] / 1024:.1f} KB")
    print(
        f"骨架体积       ：{stats['skeleton_bytes'] / 1024:.1f} KB，"
        f"占原文件 {stats['skeleton_bytes'] / size:.3%}"
    )
    print(
        f"索引体积       ：{stats['index_bytes'] / 1024:.1f} KB，"
        f"占原文件 {stats['index_bytes'] / size:.3%}"
    )

    checks = [
        ("扫描吞吐 ≥ 200 MB/s", scan_mbps >= 200),
        ("1 GB 全流程 ≤ 120s（外推）", projected_1gb <= 120),
        ("堆峰值达标", stats["peak_heap_mb"] <= heap_limit_mb),
    ]
    print()
    ok = True
    for label, passed in checks:
        print(f"  [{'通过' if passed else '未达标'}] {label}")
        ok = ok and passed
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
