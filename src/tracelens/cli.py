"""命令行入口（方案 §5.6）。

    tracelens skeleton   生成骨架 + 索引
    tracelens expand     按 span_id 展开原始 Payload
    tracelens inspect    格式嗅探与 Trace 统计

退出码语义写死并纳入测试：
    0 成功 / 1 输入不存在或解析失败 / 2 span_id 未命中 /
    3 索引与原文件不匹配 / 4 配置非法
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

from . import __version__
from .config import load_config
from .errors import InputError, TraceLensError
from .index.reader import TraceIndex, write_index
from .ingest.reader import TraceReader
from .model import Status
from .prune.engine import prune
from .prune.paths import resolve_path
from .render import fit_to_budget, render_json, render_md, render_tree

_RENDERERS = {"tree": render_tree, "json": render_json, "md": render_md}


# ---------------------------------------------------------------------------
# skeleton
# ---------------------------------------------------------------------------


def _cmd_skeleton(args: argparse.Namespace) -> int:
    config = load_config(
        config_path=args.config,
        cli_overrides={
            "format": args.format,
            "max_tokens": args.max_tokens,
            "strict_grapheme": True if args.strict_grapheme else None,
            "exact_tokens": True if args.exact_tokens else None,
            "chars_per_token": args.chars_per_token,
        },
    )

    with TraceReader(args.input) as reader:
        doc = reader.read()
        skeleton = prune(
            doc,
            config.rules,
            buf=reader.buf,
            payload_fields_fn=reader.payload_fields,
            strict_grapheme=config.strict_grapheme,
            source_file=str(reader.path),
        )

        renderer = _RENDERERS[config.format]
        skeleton, text = fit_to_budget(
            skeleton,
            renderer,
            config.max_tokens,
            exact=config.exact_tokens,
            chars_per_token=config.chars_per_token,
        )

        if args.emit_index:
            size = write_index(args.emit_index, doc.spans, reader.buf)
            print(
                f"索引已写出：{args.emit_index}（{len(doc.spans)} 条记录，{size} 字节）",
                file=sys.stderr,
            )

        if args.detach:
            count = _detach(reader, doc, Path(args.detach))
            print(f"已物化 {count} 个 span 到 {args.detach}/", file=sys.stderr)

    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        print(f"骨架已写出：{args.out}", file=sys.stderr)
    else:
        print(text)
    return 0


def _detach(reader: TraceReader, doc: object, out_dir: Path) -> int:
    """把每个 span 的原文单独物化一份（方案附录 A2 的 ``--detach``）。

    应对原文件会被轮转的场景：用空间换可用性。落盘的是**原始字节切片**，
    与 expand 的输出通路完全一致，所以字节承诺在这条路径上同样成立。
    """
    from .model import TraceDoc

    assert isinstance(doc, TraceDoc)
    out_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    for span in doc.spans:
        if not span.span_id:
            continue
        raw = reader.slice(span.raw_range.start, span.raw_range.end)
        (out_dir / f"{span.span_id}.json").write_bytes(raw)
        count += 1
    return count


# ---------------------------------------------------------------------------
# expand
# ---------------------------------------------------------------------------


def _write_out(data: bytes, out: str | None, raw: bool) -> None:
    """输出展开结果。

    ``--raw`` 直接写字节到 stdout，不加换行、不做任何编码转换——
    需要把输出重定向到文件再做逐字节比对时用这个。
    """
    if out:
        Path(out).write_bytes(data)
        print(f"已写出 {len(data)} 字节到 {out}", file=sys.stderr)
        return
    if raw:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
        return
    print(data.decode("utf-8", errors="replace"))


def _cmd_expand(args: argparse.Namespace) -> int:
    index = TraceIndex.load(args.index)

    if args.detach:
        # 原文件不在场时从物化目录取；此时无法做全文件摘要校验，如实提示
        entry = index.find(args.span_id)
        path = Path(args.detach) / f"{entry.span_id}.json"
        if not path.is_file():
            raise InputError(f"物化目录中没有该 span：{path}")
        data = path.read_bytes()
        if args.field:
            start, end = resolve_path(data, 0, args.field)
            data = data[start:end]
        _write_out(data, args.out, args.raw)
        return 0

    with TraceReader(args.input) as reader:
        # 摘要校验：文件被改动或轮转时明确失败（退出码 3），
        # 绝不静默返回一段错位的垃圾数据
        index.verify(reader.buf)
        entry = index.find(args.span_id)
        span_start = entry.offset
        span_end = entry.offset + entry.length

        if not args.field:
            _write_out(reader.slice(span_start, span_end), args.out, args.raw)
            return 0

        start, end = _resolve_field(reader, span_start, span_end, args.field)
        _write_out(reader.slice(start, end), args.out, args.raw)
    return 0


def _resolve_field(
    reader: TraceReader, span_start: int, span_end: int, field: str
) -> tuple[int, int]:
    """定位字段值的字节区间。

    先看适配器给出的规范化路径（OTLP 的 attribute 是数组形态，
    ``$.attributes['gen_ai.prompt']`` 是一条**虚拟**路径，只有适配器认得），
    再退回通用的结构导航。两条路都返回原文件的字节区间，不做任何再序列化。
    """
    fields = reader.adapter.payload_fields(reader.buf, span_start, span_end)
    for f in fields:
        if f.path == field:
            return f.start, f.end
    start, end = resolve_path(reader.buf, span_start, field)
    return start, end


# ---------------------------------------------------------------------------
# inspect
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# 解析器
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """构造参数解析器。"""
    parser = argparse.ArgumentParser(
        prog="tracelens",
        description="Trace 骨架生成与按需展开：骨架给 Agent 读，细节按 span_id 无损取回",
    )
    parser.add_argument("--version", action="version", version=f"tracelens {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="<子命令>")

    p_skel = sub.add_parser("skeleton", help="生成骨架与索引")
    p_skel.add_argument("--input", required=True, help="原始 trace 文件路径")
    p_skel.add_argument("--config", help="规则/配置 TOML 文件")
    p_skel.add_argument("--format", choices=list(_RENDERERS), help="输出格式（默认 tree）")
    p_skel.add_argument("--max-tokens", type=int, help="骨架的 token 预算，超出则逐级增压")
    p_skel.add_argument("--out", help="骨架输出文件；不给则打到 stdout")
    p_skel.add_argument("--emit-index", help="同时写出字节偏移索引文件")
    p_skel.add_argument("--detach", help="把每个 span 的原文物化到该目录（应对文件轮转）")
    p_skel.add_argument("--strict-grapheme", action="store_true", help="按 grapheme cluster 截断")
    p_skel.add_argument("--exact-tokens", action="store_true", help="用 tiktoken 精确计数")
    p_skel.add_argument(
        "--chars-per-token", type=float, help="token 估算系数（默认 4.0；--exact-tokens 时不生效）"
    )
    p_skel.set_defaults(func=_cmd_skeleton)

    p_exp = sub.add_parser("expand", help="按 span_id 展开原始 Payload")
    p_exp.add_argument("--input", help="原始 trace 文件路径（用 --detach 时可省略）")
    p_exp.add_argument("--index", required=True, help="索引文件路径")
    p_exp.add_argument("--span-id", required=True, help="span_id，支持短前缀")
    p_exp.add_argument("--field", help="只取某个字段，如 \"$.attributes['mlflow.spanOutputs']\"")
    p_exp.add_argument("--raw", action="store_true", help="原始字节写 stdout，不加换行")
    p_exp.add_argument("--out", help="写入文件而不是 stdout")
    p_exp.add_argument("--detach", help="从 --detach 物化目录读取，而不是原文件")
    p_exp.set_defaults(func=_cmd_expand)

    p_ins = sub.add_parser("inspect", help="格式嗅探与 Trace 统计")
    p_ins.add_argument("--input", required=True, help="原始 trace 文件路径")
    p_ins.set_defaults(func=_cmd_inspect)

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI 主入口，返回退出码（见 `tracelens.errors` 的退出码语义）。"""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "expand" and not args.input and not args.detach:
        parser.error("expand 需要 --input 或 --detach 之一")
    try:
        code: int = args.func(args)
        return code
    except TraceLensError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return exc.exit_code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
