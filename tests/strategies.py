"""hypothesis 策略：随机 JSON 文档与随机 Trace（方案 §六、附录 B15）。

属性测试承担两件大事：
1. 扫描器交叉验证——随机 JSON 上「每个区间 ``json.loads`` 成功且与全量解析一致」；
2. 拓扑五条不变量与字节级往返。

手写用例很难想到「兄弟中第 1、3、5 个被折叠、中间夹一个 ERROR」这类组合，
而 hypothesis 会撞上它，并把失败输入 shrink 成最小反例。
"""

from __future__ import annotations

from hypothesis import strategies as st

# JSON 标量：刻意包含会破坏朴素扫描器的内容——
# 未转义的花括号/方括号、转义引号、反斜杠、\uXXXX、中文与 emoji
_ALPHABET = list('abc {}[]",\\\n\t') + ["中", "文", "é", "́", "👨", "‍", "👧"]

_TEXT = st.text(alphabet=st.sampled_from(_ALPHABET), max_size=30)

json_scalars = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(10**6), max_value=10**6),
    st.floats(allow_nan=False, allow_infinity=False, width=32),
    _TEXT,
)

json_values = st.recursive(
    json_scalars,
    lambda children: st.one_of(
        st.lists(children, max_size=4),
        st.dictionaries(_TEXT, children, max_size=4),
    ),
    max_leaves=12,
)

json_objects = st.dictionaries(_TEXT, json_values, max_size=5)

# spans 数组的元素必须是对象（scanner.iter_object_ranges 只产出对象元素）
span_object_lists = st.lists(json_objects, min_size=0, max_size=8)
