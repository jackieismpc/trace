"""点路径解析与定位测试。

`--field` 的字节承诺就落在这里：定位返回的是**字段值自身的字节区间**，
展开时切出来的仍是原文件字节，不经过任何再序列化。
"""

from __future__ import annotations

import pytest

from tracelens.errors import InputError
from tracelens.prune.paths import format_path, parse_path, resolve_path


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("$.a", ["a"]),
        ("$.a.b.c", ["a", "b", "c"]),
        ("$.a[0]", ["a", 0]),
        ("$.a[0].b[12]", ["a", 0, "b", 12]),
        ("$.attributes['mlflow.spanInputs']", ["attributes", "mlflow.spanInputs"]),
        ('$.attributes["gen_ai.prompt"]', ["attributes", "gen_ai.prompt"]),
    ],
)
def test_parse_path(path: str, expected: list[str | int]) -> None:
    assert parse_path(path) == expected


@pytest.mark.parametrize("path", ["a.b", "", "$.a[", "$["])
def test_parse_path_rejects_garbage(path: str) -> None:
    with pytest.raises(InputError):
        parse_path(path)


def test_format_path_round_trip() -> None:
    for path in ("$.a.b", "$.a[0]", "$.attributes['mlflow.spanInputs']"):
        assert format_path(parse_path(path)) == path


def test_resolve_returns_original_bytes() -> None:
    """定位结果切出来必须与原文逐字节相同——包括原文里的空白与转义形式。"""
    raw = b'{"a": {"b": [1, {"c": 2.50}]}, "d": "\\u4e2d"}'
    start, end = resolve_path(raw, 0, "$.a.b[1].c")
    assert raw[start:end] == b"2.50"  # 不是 2.5：没有经过 loads→dumps 往返

    start, end = resolve_path(raw, 0, "$.d")
    assert raw[start:end] == b'"\\u4e2d"'  # 转义形式原样保留


def test_resolve_dotted_key() -> None:
    raw = b'{"attributes": {"mlflow.spanInputs": "{\\"q\\": 1}"}}'
    start, end = resolve_path(raw, 0, "$.attributes['mlflow.spanInputs']")
    assert raw[start:end] == b'"{\\"q\\": 1}"'


def test_resolve_skips_huge_siblings_without_parsing() -> None:
    """兄弟字段是巨型字符串时也只做结构扫描，不解析它。"""
    huge = b"x" * 200_000
    raw = b'{"big": "' + huge + b'", "small": 42}'
    start, end = resolve_path(raw, 0, "$.small")
    assert raw[start:end] == b"42"


@pytest.mark.parametrize(
    "path",
    ["$.nope", "$.a.nope", "$.a[9]", "$.a.b.c"],
)
def test_resolve_missing_path_raises(path: str) -> None:
    raw = b'{"a": {"b": [1]}}'
    with pytest.raises(InputError):
        resolve_path(raw, 0, path)


def test_resolve_type_mismatch_raises() -> None:
    raw = b'{"a": [1, 2]}'
    with pytest.raises(InputError, match="不是对象"):
        resolve_path(raw, 0, "$.a.b")
    with pytest.raises(InputError, match="不是数组"):
        resolve_path(raw, 0, "$[0]")
