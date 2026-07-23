"""索引与展开测试——「字节级一致」这条承诺的闭环验证。

expand 执行的就是 ``buf[offset : offset + length]``，中间没有解析与再序列化
环节，所以一致性是构造上成立的。这里的测试是**加固**，不是正确性的来源；
真正的价值在于钉死「构造被破坏时立刻红」。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tracelens.errors import IndexMismatchError, InputError, SpanNotFoundError
from tracelens.index.format import (
    MAGIC,
    IndexEntry,
    decode_index,
    encode_index,
    file_digest,
    make_key,
)
from tracelens.index.reader import TraceIndex, build_index, write_index
from tracelens.ingest.reader import TraceReader
from tracelens.testkit import SynthConfig, build_mlflow_trace, build_otlp_trace

FIXTURES = Path(__file__).parent / "fixtures"


# ---- 二进制格式 -----------------------------------------------------------


def test_round_trip_encode_decode() -> None:
    digest = b"\x01" * 32
    entries = [
        IndexEntry("b4e1d2c3a4958677", 100, 50),
        IndexEntry("a3f2c1d4e5b60789", 10, 90),
    ]
    raw = encode_index(entries, digest)
    assert raw.startswith(MAGIC)
    got_digest, got_entries = decode_index(raw)
    assert got_digest == digest
    # 写入前按 span_id 升序排序——这是二分与前缀匹配的前提
    assert [e.span_id for e in got_entries] == [
        "a3f2c1d4e5b60789",
        "b4e1d2c3a4958677",
    ]
    assert got_entries[0].offset == 10
    assert got_entries[0].length == 90


def test_wrong_magic_is_rejected_immediately() -> None:
    """用户把 skeleton.txt 误传给 --index，第一步就要拦下。"""
    with pytest.raises(InputError, match="不是 tracelens 索引文件"):
        decode_index(b"trace 4bf92f35  status=ERROR" + b"\x00" * 40)


def test_truncated_index_is_rejected() -> None:
    raw = encode_index([IndexEntry("aabb", 0, 1)], b"\x02" * 32)
    with pytest.raises(InputError):
        decode_index(raw[:-3])


def test_too_short_index_is_rejected() -> None:
    with pytest.raises(InputError):
        decode_index(b"TLNS")


def test_make_key_is_order_preserving() -> None:
    assert make_key("a3f2") < make_key("a3f3")
    assert make_key("ABCD") == make_key("abcd")  # 统一小写


# ---- 查找与前缀 -----------------------------------------------------------


def _index() -> TraceIndex:
    return TraceIndex(
        b"\x00" * 32,
        [
            IndexEntry("a3f2c1d4e5b60789", 0, 10),
            IndexEntry("a3f2ffffffffffff", 10, 10),
            IndexEntry("b4e1d2c3a4958677", 20, 10),
        ],
    )


def test_find_full_id() -> None:
    assert _index().find("a3f2c1d4e5b60789").offset == 0


def test_find_by_short_prefix() -> None:
    """骨架里显示 6 位，用户照抄 6 位就能展开——和 Git short hash 一样。"""
    assert _index().find("a3f2c1").offset == 0
    assert _index().find("b4e1").offset == 20


def test_ambiguous_prefix_lists_candidates() -> None:
    """前缀有歧义时列出全部候选，而不是随便挑一个。"""
    with pytest.raises(SpanNotFoundError) as exc:
        _index().find("a3f2")
    assert exc.value.exit_code == 2
    assert "有歧义" in str(exc.value)
    assert "a3f2c1d4e5b60789" in str(exc.value)


def test_missing_id_exit_code_2() -> None:
    with pytest.raises(SpanNotFoundError) as exc:
        _index().find("deadbeef")
    assert exc.value.exit_code == 2


def test_empty_id_is_rejected() -> None:
    with pytest.raises(SpanNotFoundError):
        _index().find("  ")


# ---- 摘要校验 -------------------------------------------------------------


def test_digest_mismatch_exit_code_3(tmp_path: Path) -> None:
    """原文件被改动后必须明确失败，绝不静默返回错位数据。"""
    src = tmp_path / "trace.json"
    src.write_bytes(build_mlflow_trace(SynthConfig(span_count=10, seed=1)))
    idx_path = tmp_path / "trace.idx"
    with TraceReader(src) as reader:
        doc = reader.read()
        write_index(idx_path, doc.spans, reader.buf)

    # 改动原文件（长度不变，只改内容）——朴素实现会返回一段看似合法的错位数据
    data = bytearray(src.read_bytes())
    data[len(data) // 2] = data[len(data) // 2] ^ 0x01
    src.write_bytes(bytes(data))

    index = TraceIndex.load(idx_path)
    with TraceReader(src) as reader, pytest.raises(IndexMismatchError) as exc:
        index.expand(reader.buf, doc.spans[0].span_id)
    assert exc.value.exit_code == 3


def test_index_file_missing() -> None:
    with pytest.raises(InputError):
        TraceIndex.load("/nonexistent/trace.idx")


# ---- 字节级往返 -----------------------------------------------------------


@pytest.mark.parametrize("builder", [build_mlflow_trace, build_otlp_trace])
@pytest.mark.parametrize("pretty", [False, True])
def test_expand_is_byte_identical(tmp_path: Path, builder, pretty: bool) -> None:  # type: ignore[no-untyped-def]
    """核心承诺：expand 的输出与原文件对应切片**逐字节**相等。"""
    src = tmp_path / "trace.json"
    raw = builder(SynthConfig(span_count=30, pretty=pretty, seed=11))
    src.write_bytes(raw)

    with TraceReader(src) as reader:
        doc = reader.read()
        index_bytes = build_index(doc.spans, reader.buf)
        digest, _entries = decode_index(index_bytes)
        assert digest == file_digest(reader.buf)

        index = TraceIndex(*decode_index(index_bytes))
        for span in doc.spans:
            out = index.expand(reader.buf, span.span_id, verify=False)
            assert out == raw[span.raw_range.start : span.raw_range.end]
            # 交叉验证：取回的内容本身是合法 JSON，且就是那个 span
            assert json.loads(out)["name"] == span.name


@settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    span_count=st.integers(min_value=1, max_value=40),
    pretty=st.booleans(),
    seed=st.integers(min_value=0, max_value=500),
)
def test_expand_byte_round_trip_property(
    tmp_path_factory: pytest.TempPathFactory,
    span_count: int,
    pretty: bool,
    seed: int,
) -> None:
    """对任意合成 Trace，`expand(span_id)` 都与原文件切片逐字节相等。"""
    src = tmp_path_factory.mktemp("rt") / "trace.json"
    raw = build_mlflow_trace(SynthConfig(span_count=span_count, pretty=pretty, seed=seed))
    src.write_bytes(raw)

    with TraceReader(src) as reader:
        doc = reader.read()
        index = TraceIndex(*decode_index(build_index(doc.spans, reader.buf)))
        for span in doc.spans:
            assert (
                index.expand(reader.buf, span.span_id, verify=False)
                == raw[span.raw_range.start : span.raw_range.end]
            )


def test_expand_on_real_fixture_preserves_escapes() -> None:
    """原文里的转义形式、空白、数字书写形态一概不变——这正是不走 loads→dumps 的意义。"""
    with TraceReader(FIXTURES / "mlflow_simple.json") as reader:
        doc = reader.read()
        index = TraceIndex(*decode_index(build_index(doc.spans, reader.buf)))
        tool = next(s for s in doc.spans if s.name == "sql_query")
        out = index.expand(reader.buf, tool.span_id)
    # 原文里 spanOutputs 是三重转义的字符串，原样取回
    assert b'\\\\\\"revenue_q3\\\\\\"' in out
    # 中文原样是 UTF-8 字节，没有被改写成 \uXXXX
    assert "字符串里带".encode() in out
