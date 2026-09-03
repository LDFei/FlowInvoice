# app/registry.py —— 费用类型组模块注册表（个人报销通道内的插件式扩展点）
# 业务：员工个人垫付费用统一走"个人报销通道"，场景（差旅/办公/招待…）不并列成业务方向，
#       由 费用类型组 承载政策差异，以同名模块注册进本表、复用同一报销子图模板（docs/02 §2）。
#       travel = 差旅费用组（本期首个闭环）；新增费用类型组 = 新建 businesses/<name>/ 包 + 在此注册，
#       主框架零改动（docs/AGENTS.md §4.2）。采购/对公（公司直付供应商）是另一独立通道，不注册在个人报销名下。
from app.graphs.businesses.travel.module import TravelModule
from app.shared.policies.errors import UnknownBusinessError

# 作用：费用类型组 → 模块类（懒加载：每次注入容器实例化）
MODULE_BUILDERS: dict[str, type] = {
    "travel": TravelModule,   # 差旅费用组：本期首个个人报销闭环（火车票/机票/酒店/出差打车）
    # 备注：更多个人费用类型组（办公 office、招待 entertainment…）= 复制 travel 结构 + 各自 policy YAML
    #       注册进本表即复用同一条报销闭环，不新增并列框架；采购/对公付款另立独立通道（未来）。
}


def route_to_module(direction: str, container) -> TravelModule:
    """按业务方向路由到业务模块"""
    # 作用：总控图 classify/route_to_business 调用
    # 业务：direction 来自申报人提交；未注册则抛业务异常（由 API 层转结构化响应）
    if direction not in MODULE_BUILDERS:
        raise UnknownBusinessError(direction)
    return MODULE_BUILDERS[direction](container)
