"""索引读路径与展开（方案 3.2、3.3）。

读侧把整表载入内存（KB 级）后用 `bisect` 二分，并支持短前缀匹配——和 Git 的
short hash 是同一个体验：骨架里显示 6 位，用户照抄 6 位就能展开。前缀有歧义时
列出全部候选而不是随便挑一个。

`expand` 的实现就是 ``buf[offset : offset + length]``：中间没有解析与再序列化
环节，所以「还原的 Payload 与原始数据逐字节一致」是构造上成立的，测试只是加固。
"""

from __future__ import annotations

import bisect
import os
from pathlib import Path
from typing import Any

from ..errors import IndexMismatchError, InputError, SpanNotFoundError
from ..model import SpanMeta
from .format import IndexEntry, decode_index, encode_index, file_digest, make_key

__all__ = ["TraceIndex", "build_index", "write_index"]


def build_index(spans: list[SpanMeta], buf: Any) -> bytes:
    """由 span 元数据构建索引文件内容。"""
    entries = [
        IndexEntry(
            span_id=s.span_id,
            offset=s.raw_range.start,
            length=s.raw_range.length,
        )
        for s in spans
        if s.span_id
    ]
    return encode_index(entries, file_digest(buf))


def write_index(path: str | os.PathLike[str], spans: list[SpanMeta], buf: Any) -> int:
    """构建并写出索引文件，返回写入字节数。"""
    data = build_index(spans, buf)
    Path(path).write_bytes(data)
    return len(data)


class TraceIndex:
    """已加载的索引表。"""

    def __init__(self, digest: bytes, entries: list[IndexEntry]) -> None:
        self._digest = digest
        # decode_index 保证有序；这里再排一次以容忍手工构造的入参
        self._entries = sorted(entries, key=lambda e: e.key)
        self._keys = [e.key for e in self._entries]

    # ---- 构造 ----------------------------------------------------------

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> TraceIndex:
        p = Path(path)
        if not p.is_file():
            raise InputError(f"索引文件不存在：{p}")
        digest, entries = decode_index(p.read_bytes())
        return cls(digest, entries)

    # ---- 只读属性 ------------------------------------------------------

    @property
    def digest(self) -> bytes:
        return self._digest

    @property
    def entries(self) -> list[IndexEntry]:
        return list(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    # ---- 校验与查找 ----------------------------------------------------

    def verify(self, buf: Any) -> None:
        """校验索引与原文件是否匹配。

        :raises IndexMismatchError: 摘要不符（文件被修改、轮转或传错了文件）
        """
        actual = file_digest(buf)
        if actual != self._digest:
            raise IndexMismatchError(
                "索引与原文件不匹配：原文件可能已被修改或轮转。\n"
                f"  索引记录的摘要：{self._digest.hex()[:16]}…\n"
                f"  当前文件的摘要：{actual.hex()[:16]}…\n"
                "  可预见文件会被轮转时，请在生成骨架时加 --detach 提前物化被剪内容。"
            )

    def find(self, span_id: str) -> IndexEntry:
        """按 span_id 或其短前缀查找唯一记录。

        :raises SpanNotFoundError: 未命中，或前缀匹配到多条（歧义）
        """
        needle = span_id.strip().lower()
        if not needle:
            raise SpanNotFoundError("span_id 不能为空")

        key = make_key(needle)
        pos = bisect.bisect_left(self._keys, key)
        if pos < len(self._entries) and self._entries[pos].span_id == needle:
            return self._entries[pos]

        # 短前缀匹配：有序表上从插入点起连续扫描
        prefix = needle.encode("utf-8")
        matches: list[IndexEntry] = []
        i = pos
        while i < len(self._entries) and self._keys[i].startswith(prefix):
            matches.append(self._entries[i])
            i += 1

        if not matches:
            raise SpanNotFoundError(f"未找到 span_id：{span_id}")
        if len(matches) > 1:
            candidates = ", ".join(m.span_id for m in matches[:8])
            more = "…" if len(matches) > 8 else ""
            raise SpanNotFoundError(
                f"span_id 前缀 {span_id} 有歧义，匹配到 {len(matches)} 条：{candidates}{more}"
            )
        return matches[0]

    # ---- 展开 ----------------------------------------------------------

    def expand(self, buf: Any, span_id: str, verify: bool = True) -> bytes:
        """按 span_id 取回该 span 的**原始字节**。

        :param verify: 是否先校验文件摘要。默认开启——宁可失败也不能给 Agent
            喂错数据。
        """
        if verify:
            self.verify(buf)
        entry = self.find(span_id)
        return bytes(buf[entry.offset : entry.offset + entry.length])
