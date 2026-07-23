"""读取入口：把 mmap、嗅探、扫描、适配器串成一条流水线。

    raw trace.json (mmap 只读)
          ├─► sniff     判格式 + 定位 spans 数组
          ├─► scanner   字节级扫描 → Iterator[(span_start, span_end)]
          └─► adapters  逐 span json.loads → SpanMeta（用后即弃）

内存性质：``mmap`` 只读映射避免整文件读入，物理内存随访问按页加载；
逐 span 解析的产物用完即弃，因此堆峰值与**最大单个 span** 成正比，
而不是与文件大小成正比——这是本路线要验证的核心性质（方案 §六）。
"""

from __future__ import annotations

import mmap
import os
from collections.abc import Iterator
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol

from ..errors import InputError
from ..model import SpanMeta, TraceDoc
from . import mlflow as mlflow_adapter
from . import otlp as otlp_adapter
from .common import PayloadField
from .scanner import iter_object_ranges
from .sniff import FORMAT_MLFLOW, FORMAT_OTLP, sniff

__all__ = ["TraceReader", "Adapter"]


class Adapter(Protocol):
    """适配器协议：新增数据源只需实现这两个函数（方案 §二的可扩展性主张）。"""

    def parse_span(self, buf: Any, start: int, end: int) -> SpanMeta: ...

    def payload_fields(self, buf: Any, start: int, end: int) -> list[PayloadField]: ...


_ADAPTERS: dict[str, Any] = {
    FORMAT_MLFLOW: mlflow_adapter,
    FORMAT_OTLP: otlp_adapter,
}


class TraceReader:
    """一份 trace 文件的只读视图。

    用作上下文管理器，退出时释放 mmap 与文件句柄：

    >>> with TraceReader("trace.json") as reader:   # doctest: +SKIP
    ...     doc = reader.read()
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        if not self.path.is_file():
            raise InputError(f"输入文件不存在：{self.path}")
        size = self.path.stat().st_size
        if size == 0:
            raise InputError(f"输入文件为空：{self.path}")
        self._fh = self.path.open("rb")
        try:
            self._mm = mmap.mmap(self._fh.fileno(), 0, access=mmap.ACCESS_READ)
        except Exception:
            self._fh.close()
            raise
        try:
            self._sniffed = sniff(self._mm)
        except Exception:
            self.close()
            raise
        self._adapter = _ADAPTERS[self._sniffed.format]

    # ---- 上下文管理 ----------------------------------------------------

    def __enter__(self) -> TraceReader:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        mm = getattr(self, "_mm", None)
        if mm is not None and not mm.closed:
            mm.close()
        fh = getattr(self, "_fh", None)
        if fh is not None and not fh.closed:
            fh.close()

    # ---- 只读属性 ------------------------------------------------------

    @property
    def buf(self) -> mmap.mmap:
        """底层只读映射，供索引与展开阶段做字节切片。"""
        return self._mm

    @property
    def format(self) -> str:
        return self._sniffed.format

    @property
    def size(self) -> int:
        return len(self._mm)

    @property
    def adapter(self) -> Any:
        return self._adapter

    # ---- 扫描与解析 ----------------------------------------------------

    def iter_span_ranges(self) -> Iterator[tuple[int, int]]:
        """产出每个 span 对象在原文件中的字节区间。"""
        for array_start in self._sniffed.span_array_offsets:
            yield from iter_object_ranges(self._mm, array_start)

    def read(self) -> TraceDoc:
        """扫描全文并解析出全部 span 元数据。"""
        spans: list[SpanMeta] = []
        for start, end in self.iter_span_ranges():
            try:
                spans.append(self._adapter.parse_span(self._mm, start, end))
            except Exception as exc:  # 单个 span 坏掉不应让整个文件不可用
                raise InputError(f"解析 span 失败（字节 {start}..{end}）：{exc}") from exc
        if not spans:
            raise InputError(f"{self.path} 中没有解析出任何 span")
        trace_id = next((s.trace_id for s in spans if s.trace_id), "")
        return TraceDoc(
            trace_id=trace_id,
            spans=spans,
            source_format=self._sniffed.format,
            file_size=self.size,
        )

    def payload_fields(self, span: SpanMeta) -> list[PayloadField]:
        """列出某个 span 内可截断的大体积字段。"""
        fields: list[PayloadField] = self._adapter.payload_fields(
            self._mm, span.raw_range.start, span.raw_range.end
        )
        return fields

    def slice(self, start: int, end: int) -> bytes:
        """取原始字节切片——expand 的最终动作。"""
        return bytes(self._mm[start:end])
