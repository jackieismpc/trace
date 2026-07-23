"""MLflow Tracing 适配器。

MLflow Tracing 构建在 OTel SDK 之上，它的 Span 就是 OTel Span，GenAI 特有的
语义以 attribute 承载：类型放 ``mlflow.spanType``、输入输出放
``mlflow.spanInputs`` / ``mlflow.spanOutputs``，拓扑直接用 OTel 原生的
``trace_id`` / ``span_id`` / ``parent_span_id``（方案 §二）。

MLflow 的 attribute 值在导出的 JSON 里通常是**再一层 JSON 编码的字符串**，
本模块解析元数据时会做一次宽松的二次解码；但这条只读路径的产物绝不回写到
输出通路上——展开走的永远是原始字节切片。
"""

from __future__ import annotations

import json
from typing import Any

from ..model import ByteRange, SpanMeta, Status
from .common import PayloadField, as_int, member_ranges, pick, quote_segment
from .kinds import infer_kind

__all__ = ["FIELD_MAP", "parse_span", "payload_fields"]

# 字段候选表：不同 MLflow 版本导出的 key 名有差异，这里集中收口（R2 缓解落点）
FIELD_MAP: dict[str, tuple[str, ...]] = {
    "name": ("name",),
    "span_id": ("span_id", "spanId"),
    "trace_id": ("trace_id", "traceId"),
    "parent_id": ("parent_id", "parentId", "parent_span_id", "parentSpanId"),
    "start": ("start_time_ns", "start_time", "startTimeUnixNano"),
    "end": ("end_time_ns", "end_time", "endTimeUnixNano"),
    "status": ("status_code", "statusCode", "status"),
    "status_message": ("status_message", "statusMessage"),
    "attributes": ("attributes",),
}

ATTR_SPAN_TYPE = "mlflow.spanType"
ATTR_INPUTS = "mlflow.spanInputs"
ATTR_OUTPUTS = "mlflow.spanOutputs"

# 状态字符串 → IR 状态。MLflow 里可能出现 "OK" / "ERROR" / "STATUS_CODE_OK" 等形态
_STATUS_MAP: dict[str, Status] = {
    "OK": Status.OK,
    "ERROR": Status.ERROR,
    "UNSET": Status.UNSET,
    "STATUS_CODE_OK": Status.OK,
    "STATUS_CODE_ERROR": Status.ERROR,
    "STATUS_CODE_UNSET": Status.UNSET,
}


def _norm_status(value: Any) -> Status:
    if isinstance(value, dict):
        value = pick(value, ("status_code", "code", "description"), "UNSET")
    if isinstance(value, str):
        return _STATUS_MAP.get(value.strip().upper(), Status.UNSET)
    return Status.UNSET


def _norm_id(value: Any) -> str:
    """规范化 span_id：去掉 ``0x`` 前缀、统一小写。"""
    if value is None:
        return ""
    s = str(value).strip().strip('"')
    if s.lower().startswith("0x"):
        s = s[2:]
    return s.lower()


def _decode_attrs(raw_attrs: Any) -> dict[str, Any]:
    """把 MLflow 的 attributes 解成普通字典。

    值可能是 JSON 编码的字符串（``'"LLM"'``），也可能已经是原生值；
    这里做一次宽松解码，失败就保留原字符串。
    """
    out: dict[str, Any] = {}
    if not isinstance(raw_attrs, dict):
        return out
    for k, v in raw_attrs.items():
        if isinstance(v, str):
            try:
                out[k] = json.loads(v)
                continue
            except (ValueError, TypeError):
                pass
        out[k] = v
    return out


def parse_span(buf: Any, start: int, end: int) -> SpanMeta:
    """解析单个 MLflow span，产出 IR 元数据。

    :param buf: 整个文件的缓冲区
    :param start: 本 span 对象的起始字节偏移（由 scanner 给出）
    :param end: 结束字节偏移（不含）
    """
    raw: dict[str, Any] = json.loads(bytes(buf[start:end]))
    if not isinstance(raw, dict):
        raise TypeError("span 不是 JSON 对象")

    # MLflow 部分版本把 trace_id/span_id 放在 context 子对象里
    raw_ctx = raw.get("context")
    ctx: dict[str, Any] = raw_ctx if isinstance(raw_ctx, dict) else {}
    span_id = _norm_id(pick(raw, FIELD_MAP["span_id"]) or pick(ctx, FIELD_MAP["span_id"]))
    trace_id = _norm_id(pick(raw, FIELD_MAP["trace_id"]) or pick(ctx, FIELD_MAP["trace_id"]))
    parent_id = _norm_id(pick(raw, FIELD_MAP["parent_id"]))

    attrs = _decode_attrs(pick(raw, FIELD_MAP["attributes"], {}))
    span_type = attrs.get(ATTR_SPAN_TYPE)
    kind, kind_source = infer_kind(
        name=str(pick(raw, FIELD_MAP["name"], "")),
        attrs=attrs,
        explicit=str(span_type) if span_type is not None else None,
    )

    # 输入/输出体积按**原始字节**计，不按解析后的对象计
    in_bytes = out_bytes = 0
    attrs_hit = member_ranges(buf, start).get("attributes")
    if attrs_hit is not None and buf[attrs_hit[0] : attrs_hit[0] + 1] == b"{":
        attr_ranges = member_ranges(buf, attrs_hit[0])
        if ATTR_INPUTS in attr_ranges:
            s, e = attr_ranges[ATTR_INPUTS]
            in_bytes = e - s
        if ATTR_OUTPUTS in attr_ranges:
            s, e = attr_ranges[ATTR_OUTPUTS]
            out_bytes = e - s

    return SpanMeta(
        span_id=span_id,
        parent_id=parent_id or None,
        trace_id=trace_id,
        name=str(pick(raw, FIELD_MAP["name"], "")),
        kind=kind,
        kind_source=kind_source,
        status=_norm_status(pick(raw, FIELD_MAP["status"])),
        start_ns=as_int(pick(raw, FIELD_MAP["start"])),
        end_ns=as_int(pick(raw, FIELD_MAP["end"])),
        input_bytes=in_bytes,
        output_bytes=out_bytes,
        status_message=str(pick(raw, FIELD_MAP["status_message"], "") or ""),
        raw_range=ByteRange(start, end),
        attributes={k: v for k, v in attrs.items() if k not in (ATTR_INPUTS, ATTR_OUTPUTS)},
    )


def payload_fields(buf: Any, start: int, _end: int) -> list[PayloadField]:
    """列出本 span 内可截断的大体积字段及其字节区间。

    只做定位，不取内容——真正读取发生在截断阶段，且只读被规则选中的那些。
    """
    fields: list[PayloadField] = []
    attrs_hit = member_ranges(buf, start).get("attributes")
    if attrs_hit is None or buf[attrs_hit[0] : attrs_hit[0] + 1] != b"{":
        return fields
    for key, (vs, ve) in member_ranges(buf, attrs_hit[0]).items():
        fields.append(PayloadField(path=f"$.attributes{quote_segment(key)}", start=vs, end=ve))
    return fields
