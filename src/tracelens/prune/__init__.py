"""prune 层：按规则给 span 定动作，并在删除后重建可信的拓扑。

rules.py     规则与规则集的 schema（pydantic）
engine.py    规则求解 + 硬保护
topology.py  四步拓扑重建，五条不变量的实现处
truncate.py  三种截断策略与自描述截断标记
paths.py     点路径解析与定位（截断与 expand --field 共用）
"""
