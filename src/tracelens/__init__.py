"""tracelens —— Trace 骨架生成与按需展开组件。

核心思路（详见 `docs/架构.md`）：
    原始 trace 文件是「存档」，骨架是「视图」，字节偏移索引是两者之间的桥。
    expand 执行的就是 ``mm[off:off+len]``，中间没有解析与再序列化环节，
    因此「还原的 Payload 与原始数据逐字节一致」是构造上成立的性质。

许可证：Mulan PSL v2
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
