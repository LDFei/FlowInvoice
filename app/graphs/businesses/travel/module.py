# app/graphs/businesses/travel/module.py —— 差旅业务模块（本期第一条闭环）
# 业务：实现 BusinessModule 接口；注册表注册后即可被总控图驱动（docs/AGENTS.md §4 插件式扩展）
import json

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
        # 业务：#A 子图节点链 = 批处理(识别→硬闸门→验真入池，见 batch.py)→事前申请(整批覆盖)→
        #       合规软闸门(逐票+Σ预算)→审批链(Σ金额分档)
        if getattr(self, "_graph", None) is None:
            nodes = build_travel_nodes(self._container)
            self._graph = build_subgraph(
                self.name,
                [
                    ("process_batch", nodes["process_batch"]),
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
        # #A 多票批：逐票结构化汇总最利于复核人逐张对照，统一走确定性模板（LLM 单票总结仍优先）
        if len(state.get("tickets") or []) > 1:
            return build_travel_summary(state)
        llm = self._container.llm_tool
        if llm is not None:
            text = llm.build_summary(state)
            if text:
                return text
        return build_travel_summary(state)

    def build_summary_agentic(self, state: dict) -> dict:
        # #32 函数调用实战：单票且 LLM 可用时，把只读工具（查制度条款/查组织）绑定给 LLM 自主选择，
        #       检索结果回填 research_notes（可追溯），LLM 据其产出总结；
        #       其余情形（多票批确定性汇总 / 无 key / agent 失败）回落既有 build_summary 路径，行为不变。
        # 边界：工具全部只读；LLM 只做"要不要查/查什么/怎么写总结"，不碰钱与真伪判定（docs/03 §3 混合决策）
        if len(state.get("tickets") or []) > 1 or self._container.llm_tool is None:
            return {"summary": self.build_summary(state)}
        agent = self._container.llm_tool.agentic_summary(state, self._readonly_agent_tools())
        if agent:
            return agent
        return {"summary": self.build_summary(state)}

    def _readonly_agent_tools(self) -> dict:
        """#32 注入给 LLM 的只读工具实现（真正执行方；判定类动作不在此清单）"""
        container = self._container

        def search_policy(query: str, top_k: int = 3) -> str:
            # 制度检索是确定性工具：命中条款原样返回，LLM 只负责引用，不改判定
            try:
                hits = container.policy_rag.search(query, top_k=top_k)
            except Exception as exc:  # noqa: BLE001 —— 检索异常以文本回填模型，不打断 agent 回合
                return f"检索失败: {type(exc).__name__}: {exc}"
            if not hits:
                return "未命中制度条款"
            return "\n".join(f"[{h.get('source')}] {h.get('text')}" for h in hits)

        def lookup_employee(employee_id: str) -> str:
            # 组织查询是确定性工具：返回员工档案，供 LLM 核实报销人/审批人身份
            try:
                emp = container.users.get_employee(str(employee_id))
            except KeyError as exc:
                return f"员工不存在: {exc}"
            return json.dumps(
                {k: emp.get(k) for k in ("id", "name", "grade", "dept", "manager") if k in emp},
                ensure_ascii=False,
            )

        return {"search_policy": search_policy, "lookup_employee": lookup_employee}
