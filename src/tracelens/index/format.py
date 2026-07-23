"""索引文件的二进制布局（方案 3.1、附录 D27）。

    +--------+---------+---------------------+-------------+
    | magic  | version | blake2b-256(原文件) | entry_count |
    | b"TLNS"|   H     |      32 字节        |      I      |
    +--------+---------+---------------------+-------------+
    | entry[0] | entry[1] | ... 按 span_id 升序排列        |
    +----------------------------------------------------- +

    entry = key(32 字节，span_id 的 UTF-8 右侧补零) + offset(Q) + len(Q) + flags(H)

全部小端、无对齐填充（``<`` 前缀），因此布局是语言中立的：同一份 ``trace.idx``
理论上可以被任何语言的实现互读。

**magic 与 version 是二进制格式设计的基本礼貌**：用户把 ``skeleton.txt`` 误传给
``--index`` 时，第一步就被拦下并给出明确报错，而不是读出一堆错位 offset、
直到 expand 阶段才莫名报「摘要不匹配」。

**头部存原文件摘要**是为了在文件被改动或轮转后让 expand 明确失败（退出码 3），
而不是静默返回一段错位的垃圾数据——下游读者是 Agent，喂给它一段「看起来是合法
JSON、实际是错位切片」的数据，它会一本正经地推理出错误结论，这比直接报错危险
得多（附录 A2）。

与方案原文的一处调整：原文写「span_id 原始 8 字节」。这里改成 32 字节的
UTF-8 键，因为——① 并非所有数据源的 span_id 都是 16 位十六进制；② 存字符串
形态让「短前缀匹配」变成直接的字节前缀比较，与用户在骨架里看到的短 id 完全
一致，不需要先把前缀转成半个字节。代价是每条 entry 多 24 字节，对 KB 级的
索引可以忽略。
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass
from typing import Any

from ..errors import InputError

__all__ = [
    "MAGIC",
    "VERSION",
    "KEY_SIZE",
    "IndexEntry",
    "make_key",
    "file_digest",
    "encode_index",
    "decode_index",
]

MAGIC = b"TLNS"
VERSION = 1
KEY_SIZE = 32

_HEADER = struct.Struct(f"<4sH{32}sI")
_ENTRY = struct.Struct(f"<{KEY_SIZE}sQQH")

# flags 位：预留给未来的 entry 级标注（如 --detach 物化、被截断等）
FLAG_NONE = 0


@dataclass(slots=True, frozen=True)
class IndexEntry:
    """一条索引记录。"""

    span_id: str
    offset: int
    length: int
    flags: int = FLAG_NONE

    @property
    def key(self) -> bytes:
        return make_key(self.span_id)


def make_key(span_id: str) -> bytes:
    """把 span_id 规范化成定长排序键。

    统一小写并右侧补零，因此字典序与字符串序一致，短前缀匹配就是字节前缀比较。
    """
    raw = span_id.strip().lower().encode("utf-8")[:KEY_SIZE]
    return raw.ljust(KEY_SIZE, b"\x00")


def file_digest(buf: Any) -> bytes:
    """计算原文件的 blake2b-256 摘要。

    1 GB 文件亚秒级完成，不构成体验瓶颈（附录 B14）。
    """
    h = hashlib.blake2b(digest_size=32)
    view = memoryview(buf)
    step = 8 * 1024 * 1024
    for i in range(0, len(view), step):
        h.update(view[i : i + step])
    return h.digest()


def encode_index(entries: list[IndexEntry], digest: bytes) -> bytes:
    """序列化索引。写入前按 span_id 升序排序——排序是二分与前缀匹配的前提。"""
    if len(digest) != 32:
        raise InputError("摘要长度必须是 32 字节")
    ordered = sorted(entries, key=lambda e: e.key)
    out = bytearray(_HEADER.pack(MAGIC, VERSION, digest, len(ordered)))
    for e in ordered:
        out += _ENTRY.pack(e.key, e.offset, e.length, e.flags)
    return bytes(out)


def decode_index(raw: bytes) -> tuple[bytes, list[IndexEntry]]:
    """反序列化索引，返回 ``(原文件摘要, 记录列表)``。

    :raises InputError: magic 不符、版本不支持或文件长度不匹配
    """
    if len(raw) < _HEADER.size:
        raise InputError("索引文件太短，不是合法的 tracelens 索引")
    magic, version, digest, count = _HEADER.unpack_from(raw, 0)
    if magic != MAGIC:
        raise InputError(f"不是 tracelens 索引文件（magic={magic!r}）")
    if version != VERSION:
        raise InputError(f"索引版本 {version} 不受支持（本工具支持 {VERSION}）")
    expected = _HEADER.size + count * _ENTRY.size
    if len(raw) != expected:
        raise InputError(f"索引文件长度异常：期望 {expected} 字节，实际 {len(raw)} 字节")

    entries: list[IndexEntry] = []
    pos = _HEADER.size
    for _ in range(count):
        key, offset, length, flags = _ENTRY.unpack_from(raw, pos)
        pos += _ENTRY.size
        entries.append(
            IndexEntry(
                span_id=key.rstrip(b"\x00").decode("utf-8"),
                offset=offset,
                length=length,
                flags=flags,
            )
        )
    return digest, entries
