# app/registry.py —— 业务模块注册表（插件式扩展点）
# 业务：新增业务 = 新建 businesses/<name>/ 包 + 在此注册，主框架零改动（docs/AGENTS.md §4.2）
from app.graphs.businesses.travel.module import TravelModule
from app.shared.policies.errors import UnknownBusinessError

# 作用：业务方向 → 模块类（懒加载：每次注入容器实例化）
MODULE_BUILDERS: dict[str, type] = {
    "travel": TravelModule,   # 本期：差旅闭环
    # "office": OfficeModule,              # 二期加入
    # "entertainment": EntertainmentModule,  # 三期加入
    # "procurement": ProcurementModule,      # 四期加入
}


def route_to_module(direction: str, container) -> TravelModule:
    """按业务方向路由到业务模块"""
    # 作用：总控图 classify/route_to_business 调用
    # 业务：direction 来自申报人提交；未注册则抛业务异常（由 API 层转结构化响应）
    if direction not in MODULE_BUILDERS:
        raise UnknownBusinessError(direction)
    return MODULE_BUILDERS[direction](container)
