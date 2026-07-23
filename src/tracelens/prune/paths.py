"""点路径解析与定位（方案 2.4、§5.6）。

支持的语法是 JSONPath 的一个**自实现子集**，够用即止：

    $.a.b[0].c
    $.attributes['mlflow.spanInputs']       ← key 里含点号时用方括号形式
    $.attributes["gen_ai.prompt"]

定位的关键设计澄清（方案 §5.6）：``--field`` **不是**「解析整个 span 再把字段值
重新序列化」——那会破坏字节承诺。这里的实现是对该 span 的切片做一次带路径追踪
的二次扫描，定位目标字段值**自身的字节区间**，返回的仍是原文件字节。
单个 span 的二次扫描是毫秒级开销，换来字段级同样成立的字节一致性。
"""

from __future__ import annotations

import re
from typing import Any

from ..errors import InputError
from ..ingest.scanner import iter_array_elements, iter_members

__all__ = ["parse_path", "format_path", "resolve_path"]

# 一个路径段：.name | ['name'] | ["name"] | [123]
_SEGMENT_RE = re.compile(
    r"""
    \.(?P<dot>[^.\[\]]+)                 # .key
  | \[\s*'(?P<sq>(?:[^'\\]|\\.)*)'\s*\]  # ['key']
  | \[\s*"(?P<dq>(?:[^"\\]|\\.)*)"\s*\]  # ["key"]
  | \[\s*(?P<idx>\d+)\s*\]               # [0]
    """,
    re.VERBOSE,
)


def parse_path(path: str) -> list[str | int]:
    """把点路径解析成段列表；字符串段表示对象 key，整数段表示数组下标。

    >>> parse_path("$.attributes['mlflow.spanInputs']")
    ['attributes', 'mlflow.spanInputs']
    >>> parse_path("$.a.b[0]")
    ['a', 'b', 0]
    """
    text = path.strip()
    if not text.startswith("$"):
        raise InputError(f"路径必须以 $ 开头：{path}")
    pos = 1
    segments: list[str | int] = []
    while pos < len(text):
        m = _SEGMENT_RE.match(text, pos)
        if m is None:
            raise InputError(f"路径在第 {pos} 个字符处无法解析：{path}")
        if m.group("dot") is not None:
            segments.append(m.group("dot"))
        elif m.group("sq") is not None:
            segments.append(m.group("sq").replace("\\'", "'"))
        elif m.group("dq") is not None:
            segments.append(m.group("dq").replace('\\"', '"'))
        else:
            segments.append(int(m.group("idx")))
        pos = m.end()
    if not segments:
        raise InputError(f"路径没有任何字段段：{path}")
    return segments


def format_path(segments: list[str | int]) -> str:
    """段列表 → 点路径字符串（`parse_path` 的逆运算）。"""
    out = ["$"]
    for seg in segments:
        if isinstance(seg, int):
            out.append(f"[{seg}]")
        elif any(ch in seg for ch in ".[]'\""):
            out.append(f"['{seg}']")
        else:
            out.append(f".{seg}")
    return "".join(out)


def resolve_path(buf: Any, start: int, path: str | list[str | int]) -> tuple[int, int]:
    """在 ``start`` 处的 JSON 值里按路径定位，返回目标值的**字节区间**。

    全程只做结构扫描，不解析任何被跳过的值——因此对「兄弟字段是 100 MB 字符串」
    也是常数级开销。

    :raises InputError: 路径不存在或中途类型不匹配
    """
    segments = parse_path(path) if isinstance(path, str) else list(path)
    cur = start
    walked: list[str | int] = []
    for seg in segments:
        head = buf[cur : cur + 1]
        if isinstance(seg, int):
            if head != b"[":
                raise InputError(f"路径 {format_path(walked)} 处不是数组，无法取下标 [{seg}]")
            hit: tuple[int, int] | None = None
            for i, (s, e) in enumerate(iter_array_elements(buf, cur)):
                if i == seg:
                    hit = (s, e)
                    break
            if hit is None:
                raise InputError(f"数组下标越界：{format_path([*walked, seg])}")
            cur = hit[0]
        else:
            if head != b"{":
                raise InputError(f"路径 {format_path(walked)} 处不是对象，无法取字段 {seg}")
            found: tuple[int, int] | None = None
            for key, s, e in iter_members(buf, cur):
                if key == seg:
                    found = (s, e)
                    break
            if found is None:
                raise InputError(f"字段不存在：{format_path([*walked, seg])}")
            cur = found[0]
        walked.append(seg)

    # 末段的结束位置：再跳一次该值即可
    from ..ingest.scanner import skip_value

    return cur, skip_value(buf, cur)
