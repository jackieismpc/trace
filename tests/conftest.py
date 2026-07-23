"""pytest 公共夹具。"""

from __future__ import annotations

from pathlib import Path

import pytest

from tracelens.testkit import SynthConfig, build_mlflow_trace, build_otlp_trace

FIXTURE_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def mlflow_trace_file(tmp_path: Path) -> Path:
    """一份小体量的 MLflow 合成 trace 文件。"""
    p = tmp_path / "mlflow_trace.json"
    p.write_bytes(build_mlflow_trace(SynthConfig(span_count=30, seed=7)))
    return p


@pytest.fixture
def otlp_trace_file(tmp_path: Path) -> Path:
    """一份小体量的 OTLP 合成 trace 文件。"""
    p = tmp_path / "otlp_trace.json"
    p.write_bytes(build_otlp_trace(SynthConfig(span_count=30, seed=7)))
    return p
