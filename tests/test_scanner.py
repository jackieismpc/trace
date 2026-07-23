"""扫描器测试：单元边界用例 + hypothesis 交叉验证。

扫描器是「字节级一致」这条承诺的地基，也是 Python 版技术不确定性最高的一点，
所以它的测试最厚：手写用例盯死已知的危险边界，属性测试负责撞出没想到的组合。
"""

from __future__ import annotations

import json

import pytest
from hypothesis import given, settings

from strategies import json_objects, span_object_lists
from tracelens.errors import InputError
from tracelens.ingest.scanner import (
    iter_array_elements,
    iter_members,
    iter_object_ranges,
    skip_string,
    skip_value,
)


def _ranges(text: str) -> list[tuple[int, int]]:
    buf = text.encode("utf-8")
    return list(iter_object_ranges(buf, buf.index(b"[")))


def _slices(text: str) -> list[str]:
    buf = text.encode("utf-8")
    return [buf[s:e].decode("utf-8") for s, e in _ranges(text)]


# ---- skip_string：转义奇偶规则 -----------------------------------------


@pytest.mark.parametrize(
    ("text", "expected_end"),
    [
        (r'"abc"', 5),
        (r'"a\"b"', 6),  # 转义引号：中间的 " 不结束字符串
        (r'"a\\"', 5),  # 结尾是转义的反斜杠：字符串在此结束
        (r'"a\\\"b"', 8),  # 反斜杠 + 转义引号：奇偶规则的关键用例
        (r'"\u4e2d"', 8),  # 转义形式的中文：8 个 ASCII 字节
        ('""', 2),
        ('"中文"', 8),  # 真实多字节字符：偏移必须按字节算（1+3+3+1）
    ],
)
def test_skip_string_boundaries(text: str, expected_end: int) -> None:
    assert skip_string(text.encode("utf-8"), 0) == expected_end


def test_skip_string_unterminated() -> None:
    with pytest.raises(InputError):
        skip_string(b'"abc', 0)


# ---- iter_object_ranges：结构边界 ---------------------------------------


def test_empty_array() -> None:
    assert _ranges("[]") == []


def test_single_element() -> None:
    assert _slices('[{"a":1}]') == ['{"a":1}']


def test_braces_inside_string_are_ignored() -> None:
    """payload 里未转义的花括号是最典型的翻车点。"""
    assert _slices('[{"a":"}{}{"},{"b":"]["}]') == ['{"a":"}{}{"}', '{"b":"]["}']


def test_escaped_quote_inside_string() -> None:
    assert _slices('[{"a":"he said \\"hi\\" }"}]') == ['{"a":"he said \\"hi\\" }"}']


def test_nested_containers() -> None:
    src = '[{"a":{"b":[1,{"c":2}]}},{"d":[]}]'
    assert _slices(src) == ['{"a":{"b":[1,{"c":2}]}}', '{"d":[]}']


def test_pretty_printed() -> None:
    src = '[\n  {\n    "a": 1\n  },\n  {\n    "b": 2\n  }\n]'
    assert [json.loads(s) for s in _slices(src)] == [{"a": 1}, {"b": 2}]


def test_unicode_escape_and_multibyte() -> None:
    """`\\uXXXX` 与真实多字节字符混排时，偏移仍必须是字节偏移。"""
    src = '[{"a":"\\u4e2d中"},{"b":"👨‍👩‍👧"}]'
    assert [json.loads(s) for s in _slices(src)] == [{"a": "中中"}, {"b": "👨‍👩‍👧"}]


def test_multibyte_prefix_does_not_shift_offsets() -> None:
    """多字节字符前置的回归用例（附录 B13 的 str/bytes 偏移陷阱）。"""
    buf = '{"pad":"中文abc","spans":[{"x":1}]}'.encode()
    start = buf.index(b'"spans":') + len(b'"spans":')
    ((s, e),) = list(iter_object_ranges(buf, start))
    assert buf[s:e] == b'{"x":1}'


def test_unclosed_array_raises() -> None:
    with pytest.raises(InputError):
        list(iter_object_ranges(b'[{"a":1}', 0))


def test_non_array_start_raises() -> None:
    with pytest.raises(InputError):
        list(iter_object_ranges(b'{"a":1}', 0))


def test_scalar_elements_are_skipped() -> None:
    """spans 数组里混入标量时不报错，只产出对象元素。"""
    assert _slices('[1,"x",{"a":1},null]') == ['{"a":1}']


# ---- iter_members / iter_array_elements ---------------------------------


def test_iter_members() -> None:
    buf = b'{"a": 1, "b": {"c": [1,2]}, "d": "x"}'
    got = {k: buf[s:e].decode() for k, s, e in iter_members(buf, 0)}
    assert got == {"a": "1", "b": '{"c": [1,2]}', "d": '"x"'}


def test_iter_members_dotted_key() -> None:
    buf = b'{"mlflow.spanType": "\\"LLM\\""}'
    keys = [k for k, _s, _e in iter_members(buf, 0)]
    assert keys == ["mlflow.spanType"]


def test_iter_array_elements_scalars() -> None:
    buf = b'[1, "a", true, null, {"x":1}, [2]]'
    got = [buf[s:e].decode() for s, e in iter_array_elements(buf, 0)]
    assert got == ["1", '"a"', "true", "null", '{"x":1}', "[2]"]


def test_skip_value_rejects_garbage() -> None:
    with pytest.raises(InputError):
        skip_value(b"}", 0)


# ---- hypothesis 交叉验证 -------------------------------------------------


@settings(max_examples=250, deadline=None)
@given(elements=span_object_lists)
def test_scanner_matches_full_parse(elements: list[dict[str, object]]) -> None:
    """核心性质：扫描器给出的每个区间，`json.loads` 成功且与全量解析一致。

    紧凑与 pretty 两种排版都要过——它们的空白分布完全不同。
    """
    for pretty in (False, True):
        text = json.dumps(
            {"data": {"spans": elements}},
            ensure_ascii=False,
            indent=2 if pretty else None,
            separators=None if pretty else (",", ":"),
        )
        buf = text.encode("utf-8")
        expected = json.loads(text)["data"]["spans"]
        array_start = buf.index(b"[", buf.index(b'"spans"'))
        got = [json.loads(buf[s:e]) for s, e in iter_object_ranges(buf, array_start)]
        assert got == expected


@settings(max_examples=150, deadline=None)
@given(obj=json_objects)
def test_iter_members_matches_full_parse(obj: dict[str, object]) -> None:
    """成员遍历得到的每个值区间也必须与全量解析一致。"""
    text = json.dumps(obj, ensure_ascii=False)
    buf = text.encode("utf-8")
    got = {k: json.loads(buf[s:e]) for k, s, e in iter_members(buf, 0)}
    assert got == json.loads(text)
