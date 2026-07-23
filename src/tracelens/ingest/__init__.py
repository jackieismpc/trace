"""ingest 层：从原始 trace 文件拿到「span 元数据 + 字节区间」。

sniff    读头部若干 KB，判定 MLflow / OTLP，定位 spans 数组位置
scanner  字节级扫描，输出每个 span 对象的字节区间
mlflow   / otlp   逐 span 解析元数据（用后即弃），产出 IR
"""
