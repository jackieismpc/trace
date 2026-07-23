"""格式嗅探与 spans 数组定位（方案 1.4）。

判定分两步：

1. **判格式**：只读文件头部若干 KB，按顶层结构特征区分 MLflow 与 OTLP。
   这一步不做结构解析，是纯特征匹配，因此对超大文件也是常数开销。
2. **定位**：按各格式的固定嵌套路径做结构导航，拿到 spans 数组左方括号的
   字节偏移，直接喂给 `scanner.iter_object_ranges` 的 ``array_start``。

两种格式的嵌套路径不同：

    MLflow  {"info": {...}, "data": {"spans": [ ... ]}}   （也接受顶层 "spans"）
    OTLP    {"resourceSpans": [{"scopeSpans": [{"spans": [ ... ]}]}]}

已知开销：OTLP 的多层数组导航需要跳过各元素以到达下一个元素，相当于对文件
多做一遍结构扫描（纯 C 级正则前进，不解析值）。常见的「单 resourceSpans ×
单 scopeSpans」情形下这部分开销可忽略。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..errors import InputError
from .scanner import iter_array_elements, iter_members, skip_ws

__all__ = ["SniffResult", "sniff", "locate_span_arrays"]

# 头部嗅探窗口：足够覆盖任何合理的顶层结构声明
_HEAD_BYTES = 64 * 1024

FORMAT_MLFLOW = "mlflow"
FORMAT_OTLP = "otlp"


@dataclass(slots=True, frozen=True)
class SniffResult:
    """嗅探结果。"""

    format: str
    """``mlflow`` 或 ``otlp``。"""

    span_array_offsets: list[int]
    """所有 spans 数组左方括号的字节偏移，按出现顺序。"""


def _detect_format(buf: Any) -> str:
    """只看头部特征判定格式。"""
    head = bytes(buf[:_HEAD_BYTES])
    if not head.strip():
        raise InputError("输入文件为空")
    if head.lstrip()[:1] != b"{":
        raise InputError("输入不是 JSON 对象（顶层应为 '{'）")
    if b'"resourceSpans"' in head or b'"scopeSpans"' in head:
        return FORMAT_OTLP
    if b'"spans"' in head:
        return FORMAT_MLFLOW
    raise InputError("无法识别的 trace 格式：头部既没有 resourceSpans 也没有 spans")


def _member(buf: Any, obj_start: int, key: str) -> tuple[int, int] | None:
    """在对象中查找指定 key，返回其值的字节区间；不存在则返回 None。"""
    for k, vs, ve in iter_members(buf, obj_start):
        if k == key:
            return (vs, ve)
    return None


def _require_array(buf: Any, pos: int, what: str) -> int:
    if buf[pos : pos + 1] != b"[":
        raise InputError(f"{what} 不是数组")
    return pos


def locate_span_arrays(buf: Any, fmt: str) -> list[int]:
    """按格式导航到所有 spans 数组，返回左方括号偏移列表。"""
    root = skip_ws(buf, 0)
    if buf[root : root + 1] != b"{":
        raise InputError("输入不是 JSON 对象")

    if fmt == FORMAT_MLFLOW:
        # 顶层直接带 spans 的简化形态
        hit = _member(buf, root, "spans")
        if hit is not None:
            return [_require_array(buf, hit[0], "spans")]
        data = _member(buf, root, "data")
        if data is None:
            raise InputError("MLflow trace 缺少 data 字段")
        if buf[data[0] : data[0] + 1] != b"{":
            raise InputError("MLflow trace 的 data 字段不是对象")
        hit = _member(buf, data[0], "spans")
        if hit is None:
            raise InputError("MLflow trace 的 data 中缺少 spans 数组")
        return [_require_array(buf, hit[0], "data.spans")]

    if fmt == FORMAT_OTLP:
        rs = _member(buf, root, "resourceSpans")
        if rs is None:
            raise InputError("OTLP trace 缺少 resourceSpans 字段")
        _require_array(buf, rs[0], "resourceSpans")
        offsets: list[int] = []
        for rs_start, _rs_end in iter_array_elements(buf, rs[0]):
            ss = _member(buf, rs_start, "scopeSpans")
            if ss is None:
                continue
            _require_array(buf, ss[0], "scopeSpans")
            for ss_start, _ss_end in iter_array_elements(buf, ss[0]):
                sp = _member(buf, ss_start, "spans")
                if sp is None:
                    continue
                offsets.append(_require_array(buf, sp[0], "spans"))
        if not offsets:
            raise InputError("OTLP trace 中没有找到任何 spans 数组")
        return offsets

    raise InputError(f"未知格式：{fmt}")


def sniff(buf: Any) -> SniffResult:
    """判定格式并定位全部 spans 数组。"""
    fmt = _detect_format(buf)
    return SniffResult(format=fmt, span_array_offsets=locate_span_arrays(buf, fmt))
