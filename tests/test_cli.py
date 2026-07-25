"""CLI 测试：子命令行为、全链路走查与退出码语义（方案 §5.6）。

退出码是给上层脚本与 Agent harness 用的契约，必须逐条钉死：
    0 成功 / 1 输入问题 / 2 span 未命中 / 3 索引失配 / 4 配置非法
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tracelens.cli import main
from tracelens.testkit import SynthConfig, build_mlflow_trace

FIXTURES = Path(__file__).parent / "fixtures"
MLFLOW = str(FIXTURES / "mlflow_simple.json")


# ---- 基本 -----------------------------------------------------------------


def test_help_exits_zero() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0


def test_inspect_mlflow(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["inspect", "--input", MLFLOW]) == 0
    out = capsys.readouterr().out
    assert "格式      : mlflow" in out
    assert "整体状态  : ERROR" in out
    assert "sql_query" in out


def test_inspect_otlp(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["inspect", "--input", str(FIXTURES / "otlp_simple.json")]) == 0
    assert "格式      : otlp" in capsys.readouterr().out


# ---- skeleton -------------------------------------------------------------


def test_skeleton_tree_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["skeleton", "--input", MLFLOW]) == 0
    out = capsys.readouterr().out
    assert out.startswith("trace ")
    assert "sql_query" in out


@pytest.mark.parametrize("fmt", ["tree", "json", "md"])
def test_skeleton_formats(tmp_path: Path, fmt: str) -> None:
    out = tmp_path / f"skeleton.{fmt}"
    assert main(["skeleton", "--input", MLFLOW, "--format", fmt, "--out", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert text
    if fmt == "json":
        json.loads(text)  # 必须是合法 JSON
    if fmt == "md":
        assert text.startswith("# Trace 骨架")


def test_skeleton_with_max_tokens(tmp_path: Path) -> None:
    src = tmp_path / "big.json"
    src.write_bytes(build_mlflow_trace(SynthConfig(span_count=100, seed=9)))
    out = tmp_path / "skeleton.txt"
    assert main(["skeleton", "--input", str(src), "--max-tokens", "2500", "--out", str(out)]) == 0
    assert "预算 2500" in out.read_text(encoding="utf-8")


def test_skeleton_emits_index(tmp_path: Path) -> None:
    idx = tmp_path / "trace.idx"
    assert main(["skeleton", "--input", MLFLOW, "--emit-index", str(idx)]) == 0
    assert idx.is_file() and idx.read_bytes().startswith(b"TLNS")


def test_skeleton_chars_per_token_reaches_estimate(tmp_path: Path) -> None:
    """--chars-per-token 必须一路传到 token 估算，否则就是个静默失效的开关。"""
    lo = tmp_path / "lo.txt"
    hi = tmp_path / "hi.txt"
    assert main(["skeleton", "--input", MLFLOW, "--chars-per-token", "1.0", "--out", str(lo)]) == 0
    assert main(["skeleton", "--input", MLFLOW, "--chars-per-token", "8.0", "--out", str(hi)]) == 0
    assert "chars/token=1.0" in lo.read_text(encoding="utf-8")
    n_lo = int(lo.read_text(encoding="utf-8").split("token 估算：")[1].split("（")[0])
    n_hi = int(hi.read_text(encoding="utf-8").split("token 估算：")[1].split("（")[0])
    assert n_lo > n_hi  # 系数越小，估出的 token 越多


def test_skeleton_with_example_config(tmp_path: Path) -> None:
    """示例配置能被 CLI 直接吃下——文档里的命令必须真的能跑。"""
    config = Path(__file__).parents[1] / "examples" / "rules.toml"
    out = tmp_path / "s.txt"
    assert main(["skeleton", "--input", MLFLOW, "--config", str(config), "--out", str(out)]) == 0
    assert out.read_text(encoding="utf-8").startswith("trace ")


# ---- expand：全链路字节一致 ------------------------------------------------


def _prepare(tmp_path: Path) -> tuple[Path, Path]:
    src = tmp_path / "trace.json"
    src.write_bytes(build_mlflow_trace(SynthConfig(span_count=20, seed=4)))
    idx = tmp_path / "trace.idx"
    assert (
        main(
            [
                "skeleton",
                "--input",
                str(src),
                "--emit-index",
                str(idx),
                "--out",
                str(tmp_path / "s.txt"),
            ]
        )
        == 0
    )
    return src, idx


def test_expand_round_trip_bytes(tmp_path: Path) -> None:
    """skeleton | expand 全链路：取回的内容与原文件切片逐字节相等。"""
    src, idx = _prepare(tmp_path)
    out = tmp_path / "span.json"
    assert (
        main(
            [
                "expand",
                "--input",
                str(src),
                "--index",
                str(idx),
                "--span-id",
                "000000000000000a",
                "--out",
                str(out),
            ]
        )
        == 0
    )
    raw = out.read_bytes()
    assert raw in src.read_bytes()  # 原文件里能原样找到这段字节
    assert json.loads(raw)["context"]["span_id"] == "000000000000000a"


def test_expand_short_prefix(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """骨架里显示 6 位 id，用户照抄 6 位就能展开——和 Git short hash 一样。"""
    idx = tmp_path / "fixture.idx"
    assert (
        main(
            [
                "skeleton",
                "--input",
                MLFLOW,
                "--emit-index",
                str(idx),
                "--out",
                str(tmp_path / "s.txt"),
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert main(["expand", "--input", MLFLOW, "--index", str(idx), "--span-id", "d6a3f4"]) == 0
    assert '"name": "sql_query"' in capsys.readouterr().out


def test_expand_field(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    src, idx = _prepare(tmp_path)
    assert (
        main(
            [
                "expand",
                "--input",
                str(src),
                "--index",
                str(idx),
                "--span-id",
                "000000000000000a",
                "--field",
                '$.attributes["mlflow.spanOutputs"]',
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert out.startswith('"{')


def test_expand_detach_mode(tmp_path: Path) -> None:
    """原文件不在场时，从 --detach 物化目录取回。"""
    src, idx = _prepare(tmp_path)
    detach = tmp_path / "spans"
    assert (
        main(
            [
                "skeleton",
                "--input",
                str(src),
                "--detach",
                str(detach),
                "--out",
                str(tmp_path / "s2.txt"),
            ]
        )
        == 0
    )
    out = tmp_path / "span.json"
    assert (
        main(
            [
                "expand",
                "--index",
                str(idx),
                "--span-id",
                "000000000000000a",
                "--detach",
                str(detach),
                "--out",
                str(out),
            ]
        )
        == 0
    )
    assert out.read_bytes() in src.read_bytes()


# ---- 退出码 ---------------------------------------------------------------


def test_exit_1_missing_input() -> None:
    assert main(["inspect", "--input", "/nonexistent.json"]) == 1


def test_exit_1_bad_json(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_bytes(b"this is not json")
    assert main(["inspect", "--input", str(bad)]) == 1


def test_exit_2_span_not_found(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    src, idx = _prepare(tmp_path)
    assert main(["expand", "--input", str(src), "--index", str(idx), "--span-id", "ffffff"]) == 2
    assert "未找到" in capsys.readouterr().err


def test_exit_2_ambiguous_prefix(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    src, idx = _prepare(tmp_path)
    assert main(["expand", "--input", str(src), "--index", str(idx), "--span-id", "0"]) == 2
    assert "有歧义" in capsys.readouterr().err


def test_exit_3_index_mismatch(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """原文件被改动 → 明确失败，绝不返回错位数据。"""
    src, idx = _prepare(tmp_path)
    data = bytearray(src.read_bytes())
    data[len(data) // 2] ^= 0x01
    src.write_bytes(bytes(data))
    assert (
        main(["expand", "--input", str(src), "--index", str(idx), "--span-id", "000000000000000a"])
        == 3
    )
    assert "索引与原文件不匹配" in capsys.readouterr().err


def test_exit_4_invalid_config(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """配置非法 → 退出码 4，而不是静默忽略这条规则。"""
    cfg = tmp_path / "rules.toml"
    cfg.write_text('[[rules]]\naction = "colapse"\n', encoding="utf-8")
    assert main(["skeleton", "--input", MLFLOW, "--config", str(cfg)]) == 4
    assert "配置校验失败" in capsys.readouterr().err


def test_exit_4_unknown_top_level_key(tmp_path: Path) -> None:
    cfg = tmp_path / "rules.toml"
    cfg.write_text('formatt = "tree"\n', encoding="utf-8")
    assert main(["skeleton", "--input", MLFLOW, "--config", str(cfg)]) == 4


def test_exit_1_bad_index_file(tmp_path: Path) -> None:
    fake = tmp_path / "not.idx"
    fake.write_bytes(b"definitely not an index file, just some text here")
    assert main(["expand", "--input", MLFLOW, "--index", str(fake), "--span-id", "a3f2c1"]) == 1


def test_expand_requires_input_or_detach() -> None:
    with pytest.raises(SystemExit):
        main(["expand", "--index", "x.idx", "--span-id", "a"])
