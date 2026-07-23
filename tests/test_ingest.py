"""ingest 层测试：格式嗅探、双适配器、类型分级推断。"""

from __future__ import annotations

from pathlib import Path

import pytest

from tracelens.errors import InputError
from tracelens.ingest.kinds import infer_kind
from tracelens.ingest.reader import TraceReader
from tracelens.ingest.sniff import FORMAT_MLFLOW, FORMAT_OTLP, sniff
from tracelens.model import KindSource, SpanKind, Status
from tracelens.testkit import SynthConfig, build_mlflow_trace, build_otlp_trace

FIXTURES = Path(__file__).parent / "fixtures"


# ---- 嗅探 ---------------------------------------------------------------


def test_sniff_mlflow() -> None:
    buf = FIXTURES.joinpath("mlflow_simple.json").read_bytes()
    result = sniff(buf)
    assert result.format == FORMAT_MLFLOW
    assert len(result.span_array_offsets) == 1
    assert buf[result.span_array_offsets[0] : result.span_array_offsets[0] + 1] == b"["


def test_sniff_otlp() -> None:
    buf = FIXTURES.joinpath("otlp_simple.json").read_bytes()
    result = sniff(buf)
    assert result.format == FORMAT_OTLP
    assert len(result.span_array_offsets) == 1


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (b"", "空文件"),
        (b"not json at all", "非 JSON"),
        (b'{"info": {"a": 1}}', "无 spans"),
    ],
)
def test_sniff_rejects_bad_input(payload: bytes, reason: str) -> None:
    with pytest.raises(InputError):
        sniff(payload)


def test_sniff_top_level_spans() -> None:
    """顶层直接带 spans 的简化 MLflow 形态也要支持。"""
    assert sniff(b'{"spans":[{"name":"a"}]}').format == FORMAT_MLFLOW


# ---- 适配器 -------------------------------------------------------------


def test_mlflow_adapter_parses_forest() -> None:
    with TraceReader(FIXTURES / "mlflow_simple.json") as reader:
        doc = reader.read()

    assert doc.source_format == FORMAT_MLFLOW
    assert doc.span_count == 4
    by_name = {s.name: s for s in doc.spans}

    root = by_name["research_agent"]
    assert root.parent_id is None
    assert root.kind is SpanKind.AGENT
    assert root.kind_source is KindSource.EXPLICIT

    llm = by_name["gpt-4o"]
    assert llm.kind is SpanKind.MODEL
    assert llm.parent_id == "a3f2c1d4e5b60789"  # 0x 前缀已规范化掉
    assert llm.input_bytes > llm.output_bytes  # prompt 比输出大

    tool = by_name["sql_query"]
    assert tool.status is Status.ERROR
    assert "revenue_q3" in tool.status_message

    # 没有 spanType、名字也没有类型线索的 span，如实标 Unknown
    unnamed = by_name["unnamed_step"]
    assert unnamed.kind is SpanKind.UNKNOWN
    assert unnamed.kind_source is KindSource.UNKNOWN


def test_otlp_adapter_parses_forest() -> None:
    with TraceReader(FIXTURES / "otlp_simple.json") as reader:
        doc = reader.read()

    assert doc.source_format == FORMAT_OTLP
    assert doc.span_count == 3
    by_name = {s.name: s for s in doc.spans}

    assert by_name["research_agent"].kind is SpanKind.AGENT
    assert by_name["research_agent"].kind_source is KindSource.CONVENTION
    assert by_name["research_agent"].parent_id is None

    llm = by_name["chat gpt-4o"]
    assert llm.kind is SpanKind.MODEL
    assert llm.attributes["gen_ai.request.model"] == "gpt-4o"
    assert llm.input_bytes > 0 and llm.output_bytes > 0

    tool = by_name["sql_query"]
    assert tool.status is Status.ERROR
    assert tool.kind is SpanKind.TOOL


def test_raw_range_slices_back_to_valid_json() -> None:
    """每个 span 的字节区间必须能独立 `json.loads`——expand 的地基。"""
    import json

    with TraceReader(FIXTURES / "mlflow_simple.json") as reader:
        doc = reader.read()
        for span in doc.spans:
            raw = reader.slice(span.raw_range.start, span.raw_range.end)
            assert json.loads(raw)["name"] == span.name


def test_payload_fields_locate_big_values() -> None:
    with TraceReader(FIXTURES / "mlflow_simple.json") as reader:
        doc = reader.read()
        llm = next(s for s in doc.spans if s.name == "gpt-4o")
        fields = {f.path: f for f in reader.payload_fields(llm)}
        assert "$.attributes['mlflow.spanInputs']" in fields
        target = fields["$.attributes['mlflow.spanInputs']"]
        # 返回的是原始字节区间，切出来仍是原文
        assert reader.slice(target.start, target.end).startswith(b'"{')


# ---- 合成数据 -----------------------------------------------------------


@pytest.mark.parametrize("pretty", [False, True])
@pytest.mark.parametrize("builder", [build_mlflow_trace, build_otlp_trace])
def test_synthetic_traces_round_trip(tmp_path: Path, builder, pretty: bool) -> None:  # type: ignore[no-untyped-def]
    """合成 trace 在紧凑与 pretty 两种排版下都要被完整解析。"""
    p = tmp_path / "t.json"
    p.write_bytes(builder(SynthConfig(span_count=25, pretty=pretty, seed=3)))
    with TraceReader(p) as reader:
        doc = reader.read()
    assert doc.span_count == 25
    assert sum(1 for s in doc.spans if s.parent_id is None) == 1


def test_missing_file() -> None:
    with pytest.raises(InputError):
        TraceReader("/nonexistent/trace.json")


# ---- 类型推断分级 -------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "attrs", "explicit", "kind", "source"),
    [
        ("whatever", {}, '"TOOL"', SpanKind.TOOL, KindSource.EXPLICIT),
        (
            "whatever",
            {"gen_ai.operation.name": "chat"},
            None,
            SpanKind.MODEL,
            KindSource.CONVENTION,
        ),
        (
            "whatever",
            {"openinference.span.kind": "RETRIEVER"},
            None,
            SpanKind.RETRIEVER,
            KindSource.CONVENTION,
        ),
        ("web_search", {}, None, SpanKind.TOOL, KindSource.HEURISTIC),
        ("my_agent", {}, None, SpanKind.AGENT, KindSource.HEURISTIC),
        ("step_17", {}, None, SpanKind.UNKNOWN, KindSource.UNKNOWN),
    ],
)
def test_infer_kind_levels(
    name: str,
    attrs: dict[str, object],
    explicit: str | None,
    kind: SpanKind,
    source: KindSource,
) -> None:
    assert infer_kind(name, attrs, explicit) == (kind, source)
