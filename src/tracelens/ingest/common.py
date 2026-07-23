"""适配器公用工具：字节区间辅助与字段抽取表。

适配器的纪律（方案 §5.1、1.5）：
* 逐 span ``json.loads(mm[s:e])`` 抽元数据，**用后即弃**——堆内存与最大单个
  span 成正比，而不是与文件成正比。
* 大 Payload 一律不进内存，只记 ``(start, end)`` 字节区间。
* 字段名走 ``FIELD_MAP`` 候选表而不是硬编码，上游改名时只动一行配置。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from .scanner import iter_members

__all__ = ["PayloadField", "member_ranges", "pick", "as_int", "quote_segment"]


@dataclass(slots=True, frozen=True)
class PayloadField:
    """span 内一个大体积字段的定位信息。

    ``path`` 是点路径（如 ``$.attributes['mlflow.spanOutputs']``），
    ``start/end`` 是该**字段值本身**在原始文件中的字节区间——截断标记里的
    ``expand_hint`` 与 ``expand --field`` 都以它为准。
    """

    path: str
    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start


def member_ranges(buf: Any, obj_start: int) -> dict[str, tuple[int, int]]:
    """把一个 JSON 对象的成员展开成 ``{key: (value_start, value_end)}``。

    只走结构扫描，不解析任何值——因此对「值是 100 MB 字符串」也是常数级开销。
    """
    return {k: (vs, ve) for k, vs, ve in iter_members(buf, obj_start)}


def pick(raw: dict[str, Any], candidates: Sequence[str], default: Any = None) -> Any:
    """按候选 key 列表依次取值，命中即返回（FIELD_MAP 的取值语义）。"""
    for key in candidates:
        if key in raw and raw[key] is not None:
            return raw[key]
    return default


def as_int(value: Any, default: int = 0) -> int:
    """把可能是 int / 数字字符串（OTLP 的纳秒时间戳是字符串）的值转成 int。"""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return default
    return default


def quote_segment(key: str) -> str:
    """把 attribute key 拼进点路径。

    key 里含点号（``mlflow.spanInputs``）时用方括号加**双引号**形式，
    否则用普通点号形式，保证路径可被 `prune.paths` 无歧义解析。

    用双引号而不是单引号是为了 shell 友好：截断标记里的 ``expand_hint`` 会把
    整条路径用单引号包起来交给 shell，路径内部再出现单引号就会破坏引用。
    """
    if any(ch in key for ch in ".[]'\""):
        return f'["{key}"]'
    return f".{key}"
