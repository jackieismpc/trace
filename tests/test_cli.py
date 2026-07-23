"""CLI 测试：子命令行为与退出码语义（方案 §5.6）。

退出码是给上层脚本与 Agent harness 用的契约，必须逐条钉死：
    0 成功 / 1 输入问题 / 2 span 未命中 / 3 索引失配 / 4 配置非法
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tracelens.cli import main

FIXTURES = Path(__file__).parent / "fixtures"


def test_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0


def test_inspect_mlflow(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["inspect", "--input", str(FIXTURES / "mlflow_simple.json")])
    out = capsys.readouterr().out
    assert code == 0
    assert "格式      : mlflow" in out
    assert "整体状态  : ERROR" in out
    assert "sql_query" in out


def test_inspect_otlp(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["inspect", "--input", str(FIXTURES / "otlp_simple.json")])
    out = capsys.readouterr().out
    assert code == 0
    assert "格式      : otlp" in out


def test_inspect_missing_file_exit_1(capsys: pytest.CaptureFixture[str]) -> None:
    """输入不存在 → 退出码 1。"""
    assert main(["inspect", "--input", "/nonexistent.json"]) == 1
    assert "错误" in capsys.readouterr().err


def test_inspect_bad_json_exit_1(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_bytes(b"this is not json")
    assert main(["inspect", "--input", str(bad)]) == 1
