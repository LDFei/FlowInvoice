# app/core/json_codec.py —— state JSON 序列化编解码（Decimal 安全）
# 业务：LangGraph 状态里允许出现 Decimal（PG NUMERIC 读回的天然类型，docs/06 §3.4）；
#       JSON 不是 Decimal 的原生类型，统一在落库边界转字符串（"2000.00"），读回后业务层按需取用。
#       这样无论图里哪个节点注入 Decimal，持久化永不崩溃。
import json
from decimal import Decimal
from datetime import datetime, date


def json_default(obj):
    """json.dumps 的 default：非原生类型 → 可序列化值（金额保留精度转字符串）"""
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def dumps(obj) -> str:
    """带 Decimal 兜底的 JSON 序列化（ensure_ascii=False 保中文可读）"""
    return json.dumps(obj, ensure_ascii=False, default=json_default)
