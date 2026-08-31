# app/graphs/businesses/__init__.py —— 业务模块基类
# 业务：所有业务（差旅/采购/招待/办公）遵循同一接口，总控图统一路由（docs/AGENTS.md §4.1）
from abc import ABC, abstractmethod


class BusinessModule(ABC):
    """业务模块基类：新业务继承它即可被总控图驱动"""

    name: str                        # 作用：业务标识（注册表路由依据）
    invoice_types: list[str]         # 作用：该业务接受的发票类型清单

    @abstractmethod
    def build_graph(self):
        """构建本业务专属 LangGraph 子图（差旅/采购……内部节点各异）"""

    @abstractmethod
    def policy_rules(self) -> dict:
        """返回本业务政策规则（读 policy/<name>.yaml）"""

    @abstractmethod
    def bind_tools(self) -> list[str]:
        """返回本业务绑定的工具清单（展示/审计用）"""

    @abstractmethod
    def build_summary(self, state: dict) -> str:
        """生成 Agent 审核总结（真实场景可换 LLM）"""
