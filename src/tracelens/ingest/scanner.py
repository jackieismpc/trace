"""字节级 JSON 扫描器——整个方案的核心（方案 §5.2、附录 B12）。

它只识别结构边界，**不解析任何值**。目的只有一个：拿到每个 span 对象
在原始文件中的字节区间 ``(start, end)``。有了它，expand 就是
``mm[off:off+len]`` 的字节切片，中间不存在解析与再序列化环节——
想违反「字节级一致」都没有代码路径可走。

为什么不用 ``json.loads``：它不暴露字节位置；而且 ``loads → dumps`` 往返
必然改写文本（``ensure_ascii`` 把中文写成 ``\\uXXXX``、``2.50`` 变 ``2.5``、
空白丢失、重复 key 被静默吞掉），字节一致这关过不去（附录 B11）。

**为什么它在 Python 里也够快**：双模式状态机，热路径全部落在 C 实现上。

* 字符串外：用编译好的正则 ``search(buf, pos)`` 一次 C 级扫描跳到下一个
  结构字符，Python 层只处理稀疏的结构事件。
* 字符串内：用 ``buf.find(b'"', pos)`` 直接快进到下一个引号（C 实现，
  字符串越大跳得越快），命中后向前回看连续反斜杠的奇偶判断是否转义。
  一个 100 MB 的 prompt 字符串在这里是一次内存搜索，而不是一亿次循环。

**记账单位强制为 bytes**：``len("中") == 1`` 但它占 3 字节，str 偏移一旦
混进索引，expand 就会切出错位垃圾（附录 B13）。本模块只接受 bytes-like。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any

from ..errors import InputError

__all__ = [
    "iter_object_ranges",
    "iter_array_elements",
    "iter_members",
    "skip_string",
    "skip_value",
    "skip_ws",
]

# 字符串外只有这四类字符会改变结构状态；引号意味着进入字符串模式。
_STRUCT_RE = re.compile(rb'["{}\[\]]')

_WS = b" \t\n\r"
_BACKSLASH = 0x5C
# 一个 JSON 标量（数字 / true / false / null）在这些字符处结束
_SCALAR_END = b",}] \t\n\r"


def skip_string(buf: Any, pos: int) -> int:
    """跳过一个 JSON 字符串，返回闭引号之后的位置。

    :param buf: bytes / mmap / memoryview 等支持 ``find`` 与下标的缓冲区
    :param pos: 开引号 ``"`` 所在的位置
    :return: 闭引号的下一个位置

    转义判定：闭引号候选位置向前回看连续反斜杠的个数——奇数个说明这个引号
    本身被转义、仍在字符串里；偶数个才是真正的字符串结束。
    ``"a\\\\"`` 结束，``"a\\""`` 不结束，这就是奇偶规则要防的两种情况。
    """
    if buf[pos : pos + 1] != b'"':
        raise InputError(f"位置 {pos} 处不是字符串起始")
    p = pos + 1
    while True:
        q = buf.find(b'"', p)
        if q < 0:
            raise InputError(f"字符串在位置 {pos} 处未闭合")
        # 向前回看连续反斜杠个数；绝大多数情况是 0 次循环
        n = 0
        b = q - 1
        while b >= pos and buf[b] == _BACKSLASH:
            n += 1
            b -= 1
        if n % 2 == 0:
            return int(q) + 1
        p = q + 1


def skip_ws(buf: Any, pos: int, end: int | None = None) -> int:
    """跳过空白字符，返回第一个非空白字符的位置。"""
    limit = len(buf) if end is None else end
    while pos < limit and buf[pos] in _WS:
        pos += 1
    return pos


def _skip_container(buf: Any, pos: int) -> int:
    """跳过一个 ``{...}`` 或 ``[...]``，返回闭合括号之后的位置。"""
    depth = 0
    p = pos
    while True:
        m = _STRUCT_RE.search(buf, p)
        if m is None:
            raise InputError(f"容器在位置 {pos} 处未闭合")
        i = m.start()
        c = m.group()
        if c == b'"':
            p = skip_string(buf, i)
            continue
        if c in (b"{", b"["):
            depth += 1
            p = i + 1
            continue
        # 闭合括号
        depth -= 1
        p = i + 1
        if depth == 0:
            return p


def skip_value(buf: Any, pos: int) -> int:
    """跳过 ``pos`` 处的一个完整 JSON 值，返回其结束位置（不含）。

    ``pos`` 必须指向值的第一个非空白字符。标量（数字 / true / false / null）
    不做合法性校验——扫描器只管边界，值的正确性交给需要它的那一方去 ``loads``。
    """
    c = buf[pos : pos + 1]
    if c == b'"':
        return skip_string(buf, pos)
    if c in (b"{", b"["):
        return _skip_container(buf, pos)
    p = pos
    n = len(buf)
    while p < n and buf[p] not in _SCALAR_END:
        p += 1
    if p == pos:
        raise InputError(f"位置 {pos} 处不是合法的 JSON 值起始")
    return p


def iter_object_ranges(buf: Any, array_start: int) -> Iterator[tuple[int, int]]:
    """遍历 ``array_start`` 处 JSON 数组的每个**对象**元素，产出其字节区间。

    :param buf: 整个文件的缓冲区（通常是 mmap）
    :param array_start: spans 数组左方括号 ``[`` 的字节偏移，由 `sniff` 给出
    :return: 迭代器，每项为 ``(start, end)``，满足 ``json.loads(buf[start:end])``
             恰好等于该元素

    数组里的非对象元素（数字、字符串等）会被跳过而不产出——spans 数组里出现
    它们本身就不正常，但扫描器不为此报错，判定交给上层适配器。
    """
    if buf[array_start : array_start + 1] != b"[":
        raise InputError(f"位置 {array_start} 处不是数组起始")

    depth = 0
    obj_start = -1
    p = array_start
    while True:
        m = _STRUCT_RE.search(buf, p)
        if m is None:
            raise InputError(f"spans 数组在位置 {array_start} 处未闭合")
        i = m.start()
        c = m.group()

        if c == b'"':
            # 进入字符串模式：C 级快进，跳过 payload 里未转义的花括号
            p = skip_string(buf, i)
            continue

        if c in (b"{", b"["):
            depth += 1
            if depth == 2 and c == b"{":
                obj_start = i
            p = i + 1
            continue

        depth -= 1
        p = i + 1
        if depth == 1 and c == b"}" and obj_start >= 0:
            yield (obj_start, p)
            obj_start = -1
        elif depth == 0:
            return


def iter_array_elements(buf: Any, array_start: int) -> Iterator[tuple[int, int]]:
    """遍历数组的**全部**元素（含标量），产出各自的字节区间。"""
    if buf[array_start : array_start + 1] != b"[":
        raise InputError(f"位置 {array_start} 处不是数组起始")
    p = skip_ws(buf, array_start + 1)
    if buf[p : p + 1] == b"]":
        return
    while True:
        start = p
        end = skip_value(buf, p)
        yield (start, end)
        p = skip_ws(buf, end)
        c = buf[p : p + 1]
        if c == b",":
            p = skip_ws(buf, p + 1)
            continue
        if c == b"]":
            return
        raise InputError(f"数组元素之后出现非法字符 {c!r}（位置 {p}）")


def iter_members(buf: Any, obj_start: int) -> Iterator[tuple[str, int, int]]:
    """遍历 ``obj_start`` 处 JSON 对象的成员，产出 ``(key, value_start, value_end)``。

    key 用 ``json.loads`` 解码（key 是短元数据，解码它不影响字节承诺——
    字节承诺约束的是**值**的输出通路，见 `find_field_range` 的用法）。
    """
    if buf[obj_start : obj_start + 1] != b"{":
        raise InputError(f"位置 {obj_start} 处不是对象起始")
    p = skip_ws(buf, obj_start + 1)
    if buf[p : p + 1] == b"}":
        return
    while True:
        if buf[p : p + 1] != b'"':
            raise InputError(f"位置 {p} 处期望对象的 key")
        key_end = skip_string(buf, p)
        key = json.loads(bytes(buf[p:key_end]).decode("utf-8"))
        p = skip_ws(buf, key_end)
        if buf[p : p + 1] != b":":
            raise InputError(f"位置 {p} 处期望冒号")
        p = skip_ws(buf, p + 1)
        value_start = p
        value_end = skip_value(buf, p)
        yield (key, value_start, value_end)
        p = skip_ws(buf, value_end)
        c = buf[p : p + 1]
        if c == b",":
            p = skip_ws(buf, p + 1)
            continue
        if c == b"}":
            return
        raise InputError(f"对象成员之后出现非法字符 {c!r}（位置 {p}）")
