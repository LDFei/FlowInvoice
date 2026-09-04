# app/core/money.py —— 金额运算 Decimal 核（#44：代码内 float 运算收敛）
# 架构：金额在 JSON/存储/API 边界以原生数承载（float，保证跨层契约不变、DB 行读回稳定）；
#       凡对金额做"运算/比较/判定"（Σ、预算占用、阈值分档、限额判断、百分比差），一律先进本模块
#       转 Decimal 再算——彻底消除 0.1+0.2 这类浮点误差污染记账与合规判定。
# 关键：float → Decimal 必须经 str(float)（最短往返表示）而非 Decimal(float)（会把二进制展开的
#       尾数带进来）；2 位小数的金额文本由此可干净还原（0.30000000000000004 → 0.3）。
# 写作边界：运算结果若需落回 state/DB（如 total_amount），用 `float(money)` 转回 JSON 原生数
#          （2 位小数金额的 float 距其精确值 ≤ 0.5 ulp，格式化 `:.2f` 恒打印原值，round-trip 无损）。
from decimal import Decimal

# 兜底符号：审批链阈值上限默认无穷大（比 float("inf") 更利于 Decimal 比较）
INF = Decimal("Infinity")


def to_money(value) -> Decimal:
    """任意金额形态 → Decimal（None/空→0；float 经 str 还原；字符串去 千分位/货币符/元 后缀）"""
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, str):
        s = value.strip().replace(",", "").replace("¥", "").replace("￥", "").replace(" ", "").replace("元", "")
        return Decimal(s) if s else Decimal("0")
    return Decimal(str(value))
