"""命令行入口。

子命令（方案 §5.6）：

    tracelens skeleton   生成骨架 + 索引
    tracelens expand     按 span_id 展开原始 Payload
    tracelens inspect    格式嗅探与 Trace 统计

M1 阶段只落地 ``inspect``，其余子命令在 M3 补齐——保证每个里程碑交付的
都是「装上就能跑」的东西。
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter

from . import __version__
from .errors import TraceLensError
from .ingest.reader import TraceReader
from .model import Status


def _cmd_inspect(args: argparse.Namespace) -> int:
    """打印格式判定结果与 Trace 的基本统计。"""
    with TraceReader(args.input) as reader:
        doc = reader.read()

    kinds = Counter(s.kind.value for s in doc.spans)
    sources = Counter(s.kind_source.value for s in doc.spans)
    errors = [s for s in doc.spans if s.status is Status.ERROR]
    roots = [s for s in doc.spans if s.parent_id is None]
    payload = sum(s.payload_bytes for s in doc.spans)

    print(f"文件      : {reader.path}")
    print(f"格式      : {doc.source_format}")
    print(f"trace_id  : {doc.trace_id or '(缺失)'}")
    print(f"文件大小  : {doc.file_size:,} 字节")
    print(f"span 总数 : {doc.span_count}（根节点 {len(roots)} 个）")
    print(f"span 体积 : {payload:,} 字节，占文件 {payload / max(1, doc.file_size):.1%}")
    print(f"整体状态  : {doc.status().value}，ERROR 节点 {len(errors)} 个")
    print("类型分布  : " + ", ".join(f"{k}={v}" for k, v in kinds.most_common()))
    print("类型来源  : " + ", ".join(f"{k}={v}" for k, v in sources.most_common()))
    if errors:
        print("ERROR 节点:")
        for s in errors[:10]:
            msg = s.status_message.splitlines()[0] if s.status_message else ""
            print(f"  {s.span_id[:8]} {s.kind.value:<9} {s.name}  {msg}")
        if len(errors) > 10:
            print(f"  ...（另有 {len(errors) - 10} 个）")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """构造参数解析器。"""
    parser = argparse.ArgumentParser(
        prog="tracelens",
        description="Trace 骨架生成与按需展开：骨架给 Agent 读，细节按 span_id 无损取回",
    )
    parser.add_argument("--version", action="version", version=f"tracelens {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="<子命令>")

    p_inspect = sub.add_parser("inspect", help="格式嗅探与 Trace 统计")
    p_inspect.add_argument("--input", required=True, help="原始 trace 文件路径")
    p_inspect.set_defaults(func=_cmd_inspect)

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI 主入口，返回退出码（见 `tracelens.errors` 的退出码语义）。"""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        code: int = args.func(args)
        return code
    except TraceLensError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return exc.exit_code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
