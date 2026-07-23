"""OTLP JSON 适配器。

OTLP（OpenTelemetry Protocol）是 OTel 官方的遥测传输与编码协议，file exporter
导出的 JSON 形态就是本模块的输入（方案附录 C20）。

与 MLflow 的两点结构差异：

* attributes 是**数组**：``[{"key": "...", "value": {"stringValue": "..."}}]``，
  值本身还包着一层类型标签（stringValue / intValue / boolValue …）。
* 类型信息只能靠 GenAI 语义约定（``gen_ai.*``，仍属 Experimental）或 span 名
  启发式推断，所以这里产出的 `KindSource` 通常是 Convention 或 Heuristic。
"""

from __future__ import annotations

import json
from typing import Any

from ..model import ByteRange, SpanMeta, Status
from .common import PayloadField, as_int, member_ranges, pick, quote_segment
from .kinds import infer_kind

__all__ = ["FIELD_MAP", "parse_span", "payload_fields"]

FIELD_MAP: dict[str, tuple[str, ...]] = {
    "name": ("name",),
    "span_id": ("spanId", "span_id"),
    "trace_id": ("traceId", "trace_id"),
    "parent_id": ("parentSpanId", "parent_span_id"),
    "start": ("startTimeUnixNano", "start_time_unix_nano"),
    "end": ("endTimeUnixNano", "end_time_unix_nano"),
    "attributes": ("attributes",),
    "status": ("status",),
}

# OTLP 的 status.code 是枚举整数，也可能是字符串形态
_STATUS_CODE_MAP: dict[int, Status] = {0: Status.UNSET, 1: Status.OK, 2: Status.ERROR}
_STATUS_NAME_MAP: dict[str, Status] = {
    "STATUS_CODE_UNSET": Status.UNSET,
    "STATUS_CODE_OK": Status.OK,
    "STATUS_CODE_ERROR": Status.ERROR,
    "UNSET": Status.UNSET,
    "OK": Status.OK,
    "ERROR": Status.ERROR,
}

# AnyValue 的类型标签，按优先级取第一个命中的
_ANYVALUE_KEYS = (
    "stringValue",
    "intValue",
    "doubleValue",
    "boolValue",
    "arrayValue",
    "kvlistValue",
    "bytesValue",
)

# 判定某个 attribute 属于输入还是输出（用于体积统计）
_INPUT_HINTS = ("input", "prompt", "request", "messages")
_OUTPUT_HINTS = ("output", "completion", "response", "result")


def _unwrap_anyvalue(value: Any) -> Any:
    """剥掉 OTLP AnyValue 的类型标签外壳。"""
    if not isinstance(value, dict):
        return value
    for k in _ANYVALUE_KEYS:
        if k in value:
            return value[k]
    return value


def _decode_attrs(raw_attrs: Any) -> dict[str, Any]:
    """把 OTLP 的 attribute 数组解成普通字典。"""
    out: dict[str, Any] = {}
    if isinstance(raw_attrs, dict):
        # 少数导出器会直接写成对象形态，一并兼容
        return {k: _unwrap_anyvalue(v) for k, v in raw_attrs.items()}
    if not isinstance(raw_attrs, list):
        return out
    for item in raw_attrs:
        if isinstance(item, dict) and "key" in item:
            out[str(item["key"])] = _unwrap_anyvalue(item.get("value"))
    return out


def _norm_status(raw_status: Any) -> tuple[Status, str]:
    if not isinstance(raw_status, dict):
        return Status.UNSET, ""
    code = raw_status.get("code")
    if isinstance(code, str):
        status = _STATUS_NAME_MAP.get(code.strip().upper(), Status.UNSET)
    else:
        status = _STATUS_CODE_MAP.get(as_int(code, 0), Status.UNSET)
    return status, str(raw_status.get("message", "") or "")


def parse_span(buf: Any, start: int, end: int) -> SpanMeta:
    """解析单个 OTLP span，产出 IR 元数据。"""
    raw: dict[str, Any] = json.loads(bytes(buf[start:end]))
    if not isinstance(raw, dict):
        raise TypeError("span 不是 JSON 对象")

    attrs = _decode_attrs(pick(raw, FIELD_MAP["attributes"], []))
    name = str(pick(raw, FIELD_MAP["name"], ""))
    kind, kind_source = infer_kind(name=name, attrs=attrs, explicit=None)
    status, status_msg = _norm_status(pick(raw, FIELD_MAP["status"]))

    in_bytes, out_bytes = _payload_volume(buf, start)

    return SpanMeta(
        span_id=str(pick(raw, FIELD_MAP["span_id"], "")).lower(),
        parent_id=(str(pick(raw, FIELD_MAP["parent_id"], "")).lower() or None),
        trace_id=str(pick(raw, FIELD_MAP["trace_id"], "")).lower(),
        name=name,
        kind=kind,
        kind_source=kind_source,
        status=status,
        start_ns=as_int(pick(raw, FIELD_MAP["start"])),
        end_ns=as_int(pick(raw, FIELD_MAP["end"])),
        input_bytes=in_bytes,
        output_bytes=out_bytes,
        status_message=status_msg,
        raw_range=ByteRange(start, end),
        attributes=attrs,
    )


def _attr_entries(buf: Any, span_start: int) -> list[tuple[str, int, int]]:
    """在不解析大值的前提下，列出每个 attribute 的 ``(key, 值起, 值止)``。"""
    entries: list[tuple[str, int, int]] = []
    hit = member_ranges(buf, span_start).get("attributes")
    if hit is None:
        return entries
    vs = hit[0]
    if buf[vs : vs + 1] == b"{":
        for key, (s, e) in member_ranges(buf, vs).items():
            entries.append((key, s, e))
        return entries
    if buf[vs : vs + 1] != b"[":
        return entries
    # attribute 数组：逐元素只解析 key（短），value 只取字节区间
    from .scanner import iter_array_elements  # 局部导入避免循环依赖之外的额外耦合

    for el_start, _el_end in iter_array_elements(buf, vs):
        if buf[el_start : el_start + 1] != b"{":
            continue
        members = member_ranges(buf, el_start)
        if "key" not in members or "value" not in members:
            continue
        ks, ke = members["key"]
        key = json.loads(bytes(buf[ks:ke]))
        s, e = members["value"]
        entries.append((str(key), s, e))
    return entries


def _payload_volume(buf: Any, span_start: int) -> tuple[int, int]:
    """按 attribute key 的语义线索把体积归到输入/输出两侧。"""
    in_bytes = out_bytes = 0
    for key, s, e in _attr_entries(buf, span_start):
        low = key.lower()
        size = e - s
        if any(h in low for h in _OUTPUT_HINTS):
            out_bytes += size
        elif any(h in low for h in _INPUT_HINTS):
            in_bytes += size
    return in_bytes, out_bytes


def payload_fields(buf: Any, start: int, _end: int) -> list[PayloadField]:
    """列出可截断的大体积字段及其字节区间。"""
    return [
        PayloadField(path=f"$.attributes{quote_segment(key)}", start=s, end=e)
        for key, s, e in _attr_entries(buf, start)
    ]
