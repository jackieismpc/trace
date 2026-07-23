"""自验脚本的集成测试。

自验报告里的每一个数字都来自这三个脚本，所以它们不能悄悄坏掉——
脚本一旦跑不通，报告就成了无法复现的宣称。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SCRIPTS = ROOT / "scripts"


def _run(script: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [sys.executable, str(SCRIPTS / script), *args],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )


@pytest.fixture(scope="module")
def demo_trace(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("demo") / "demo_trace.json"
    result = _run("make_demo_fixture.py", str(out))
    assert result.returncode == 0, result.stderr
    return out


def test_fixture_has_the_planted_bug(demo_trace: Path) -> None:
    """demo 数据必须真的带那个被吞掉的错误，否则整个自验没有意义。"""
    from tracelens.ingest.reader import TraceReader
    from tracelens.model import Status

    with TraceReader(demo_trace) as reader:
        doc = reader.read()

    assert doc.span_count == 411
    errors = [s for s in doc.spans if s.status is Status.ERROR]
    assert len(errors) == 1
    assert errors[0].name == "sql_query"
    assert 'relation "revenue_q3" does not exist' in errors[0].status_message

    # 吞掉错误的那个封装层状态是 OK——「有征兆但被掩埋」
    swallower = [s for s in doc.spans if s.name == "tool_executor"]
    assert len(swallower) == 1
    assert swallower[0].status is Status.OK


@pytest.mark.slow
def test_investigate_reaches_root_cause(demo_trace: Path, tmp_path: Path) -> None:
    result = _run(
        "demo_investigate.py",
        "--input",
        str(demo_trace),
        "--index",
        str(tmp_path / "demo.idx"),
    )
    assert result.returncode == 0, result.stderr
    assert "根因" in result.stdout
    assert "工具封装层" in result.stdout
    assert "全过程消耗上下文" in result.stdout


@pytest.mark.slow
def test_control_group_fails_as_predicted(demo_trace: Path) -> None:
    """对照组必须失败——出错的 span 落在 128K 窗口之外。"""
    result = _run("demo_control.py", "--input", str(demo_trace))
    assert result.returncode == 0, result.stderr
    assert "窗口外" in result.stdout
    assert "对照组结论：失败" in result.stdout


@pytest.mark.slow
def test_bench_scanner_meets_threshold() -> None:
    """扫描吞吐验收线 200 MB/s；退出码非 0 即未达标。"""
    result = _run("bench_scanner.py", "20")
    assert result.returncode == 0, result.stdout + result.stderr
