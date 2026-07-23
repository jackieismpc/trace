"""异常类型与退出码语义。

退出码写死并纳入测试（方案 §5.6）。明确的退出码是给上层脚本和 Agent harness
用的——它们必须能程序化地区分「没找到」和「数据坏了」：

    0  成功
    1  输入不存在或解析失败
    2  span_id 未命中
    3  索引与原文件不匹配（摘要校验失败）
    4  配置非法
"""

from __future__ import annotations


class TraceLensError(Exception):
    """所有本工具异常的基类，携带退出码。"""

    exit_code: int = 1


class InputError(TraceLensError):
    """输入文件不存在、格式无法识别或解析失败。"""

    exit_code = 1


class SpanNotFoundError(TraceLensError):
    """按 span_id（或其短前缀）在索引中未命中，或前缀有歧义。"""

    exit_code = 2


class IndexMismatchError(TraceLensError):
    """索引头部记录的原文件摘要与当前文件不符。

    宁可明确失败，绝不静默返回错位数据——喂给 Agent 一段「看起来是合法 JSON、
    实际是错位切片」的数据，它会一本正经地推理出错误结论（方案附录 A2）。
    """

    exit_code = 3


class ConfigError(TraceLensError):
    """配置或规则集非法（pydantic 校验错误的统一出口）。"""

    exit_code = 4
