"""Token 计数（方案 §5.4）。

默认用 ``chars / chars_per_token`` 估算：零依赖、确定性、够用——这和 OpenCode
用 4 字符/token 粗估请求体积做压缩决策是同一个工程判断。装了 ``[tokens]`` extra
时改用 tiktoken 精确计算。

**骨架末尾会显式标注用的是哪种方法**，因为估算值和精确值可能差出 30%，
而下游可能拿这个数字去做「装不装得下」的决策——不标注就是在制造隐性风险。

中英混排单独处理：CJK 字符的信息密度高得多（约 1.5 字符/token），
按纯英文的 4.0 估算会显著低估中文 Trace 的体积。
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["TokenEstimate", "estimate_tokens", "count_tokens", "DEFAULT_CHARS_PER_TOKEN"]

DEFAULT_CHARS_PER_TOKEN = 4.0
"""拉丁文字的默认估算系数。"""

CJK_CHARS_PER_TOKEN = 1.5
"""中日韩字符的估算系数。"""

# 覆盖常用 CJK 区段即可，不追求 Unicode 完备
_CJK_RANGES = (
    (0x3040, 0x30FF),  # 日文假名
    (0x3400, 0x4DBF),  # CJK 扩展 A
    (0x4E00, 0x9FFF),  # CJK 基本区
    (0xAC00, 0xD7AF),  # 谚文
    (0xF900, 0xFAFF),  # CJK 兼容
    (0xFF00, 0xFFEF),  # 全角符号
)


@dataclass(slots=True, frozen=True)
class TokenEstimate:
    """计数结果，始终带上方法说明。"""

    tokens: int
    method: str
    """``estimate(chars/token=4.0,CJK=1.5)`` 或 ``tiktoken:<encoding>``。"""

    def __int__(self) -> int:
        return self.tokens


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return any(lo <= code <= hi for lo, hi in _CJK_RANGES)


def estimate_tokens(
    text: str,
    chars_per_token: float = DEFAULT_CHARS_PER_TOKEN,
    cjk_chars_per_token: float = CJK_CHARS_PER_TOKEN,
) -> int:
    """按字符数估算 token 数，中英分开计权。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if _is_cjk(ch))
    other = len(text) - cjk
    return max(1, round(cjk / cjk_chars_per_token + other / chars_per_token))


def count_tokens(
    text: str,
    exact: bool = False,
    encoding: str = "cl100k_base",
    chars_per_token: float = DEFAULT_CHARS_PER_TOKEN,
) -> TokenEstimate:
    """计数入口。

    :param exact: 请求精确计数。未安装 ``[tokens]`` extra 时**自动回退到估算**，
        并在 ``method`` 里如实说明——不能让「以为是精确值」的误解留在下游。
    """
    if exact:
        try:
            import tiktoken  # type: ignore[import-not-found]

            enc = tiktoken.get_encoding(encoding)
            return TokenEstimate(len(enc.encode(text)), f"tiktoken:{encoding}")
        except ImportError:
            return TokenEstimate(
                estimate_tokens(text, chars_per_token),
                f"estimate(chars/token={chars_per_token},CJK={CJK_CHARS_PER_TOKEN}"
                "；未安装 [tokens] extra，已回退)",
            )
    return TokenEstimate(
        estimate_tokens(text, chars_per_token),
        f"estimate(chars/token={chars_per_token},CJK={CJK_CHARS_PER_TOKEN})",
    )
