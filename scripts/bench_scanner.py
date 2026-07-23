"""扫描器吞吐微基准（方案 1.2 的验收手段）。

验收线：扫描吞吐 ≥ 200 MB/s。若不达标，按方案 R1' 启动兜底评估。

用法：
    uv run python scripts/bench_scanner.py [目标体积MB]
"""

from __future__ import annotations

import mmap
import sys
import tempfile
import time
from pathlib import Path

from tracelens.ingest.scanner import iter_object_ranges
from tracelens.ingest.sniff import sniff
from tracelens.testkit import SynthConfig, build_mlflow_trace


def make_file(target_mb: int, path: Path) -> int:
    """合成一个约 target_mb 大小的 trace 文件，返回实际字节数。"""
    # 先用小样本估算单 span 体积，再反推需要的 span 数
    probe = build_mlflow_trace(SynthConfig(span_count=100, seed=1))
    per_span = len(probe) / 100
    span_count = max(200, int(target_mb * 1024 * 1024 / per_span))
    data = build_mlflow_trace(SynthConfig(span_count=span_count, seed=1))
    path.write_bytes(data)
    return len(data)


def main() -> int:
    target_mb = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "bench.json"
        size = make_file(target_mb, path)
        print(f"合成文件：{size / 1024 / 1024:.1f} MB")

        with path.open("rb") as fh:
            mm = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
            try:
                sniffed = sniff(mm)
                t0 = time.perf_counter()
                count = 0
                for array_start in sniffed.span_array_offsets:
                    for _ in iter_object_ranges(mm, array_start):
                        count += 1
                elapsed = time.perf_counter() - t0
            finally:
                mm.close()

    mbps = size / 1024 / 1024 / elapsed
    print(f"扫描 {count} 个 span，用时 {elapsed:.3f}s，吞吐 {mbps:.1f} MB/s")
    ok = mbps >= 200
    print("验收：" + ("通过（≥ 200 MB/s）" if ok else "未达标（< 200 MB/s）"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
