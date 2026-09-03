# app/graphs/businesses/travel/module.py —— 差旅业务模块（本期第一条闭环）
# 业务：实现 BusinessModule 接口；注册表注册后即可被总控图驱动（docs/AGENTS.md §4 插件式扩展）
from app.graphs.base_subgraph import build_subgraph
from app.graphs.businesses import BusinessModule
from app.graphs.businesses.travel.nodes import build_travel_nodes
from app.graphs.businesses.travel.summary import build_travel_summary


class TravelModule(BusinessModule):
    """差旅报销业务模块"""

    name = "travel"
    invoice_types = ["火车票", "机票", "酒店发票", "打车行程单"]

    def __init__(self, container):
        # 作用：注入依赖容器（图节点闭包使用）
        self._container = container

    def build_graph(self):
        # 作用：构建并编译差旅子图（缓存，避免每次 invoke 重复编译）
        # 业务：子图节点链 = 识别→验真→发票合规确定性闸门(票种/抬头/时限)→事前申请→合规(软闸门)→审批链
        if getattr(self, "_graph", None) is None:
            nodes = build_travel_nodes(self._container)
            self._graph = build_subgraph(
                self.name,
                [
                    ("recognize", nodes["recognize"]),
                    ("verify", nodes["verify"]),
                    ("check_invoice_compliance", nodes["check_invoice_compliance"]),
                    ("match_advance", nodes["match_advance"]),
                    ("check_compliance", nodes["check_compliance"]),
                    ("build_approval_chain", nodes["build_approval_chain"]),
                ],
            )
        return self._graph

    def policy_rules(self) -> dict:
        # 作用：读 policy/travel.yaml
        return self._container.policies.load(self.name)

    def bind_tools(self) -> list[str]:
        return ["ocr_tool", "verify_tool", "advance_tool", "notify_tool", "email_tool"]

    def build_summary(self, state: dict) -> str:
        # 业务：真实场景可换 LLM 生成总结；LLM 不可用/无 key → 模板降级，保证可离线运行（docs/03 §3）
        llm = self._container.llm_tool
        if llm is not None:
            text = llm.build_summary(state)
            if text:
                return text
        return build_travel_summary(state)
