# app/tools/policy_rag_tool.py —— 政策制度 RAG 检索工具
# 业务：按报销场景（发票类型/事由/风险）检索制度条款，作为合规判断与总结的"依据"，
#       让"为什么通过/为什么退回"有条款可引（可解释性，docs/03 §3）
from app.shared.policies.rag import PolicyIndex


class PolicyRagTool:
    """制度条款检索：封装 PolicyIndex，供合规节点调用"""

    # 业务：发票类型 → 检索关键词提示（让 query 更贴近相关条款）
    TYPE_HINTS = {
        "酒店发票": "住宿 酒店 限额 标准 宾馆",
        "火车票": "交通 高铁 火车 等级 标准 报销",
        "机票": "飞机 交通 等级 经济舱 报销",
        "打车行程单": "市内交通 出租车 网约车 事由",
    }

    def __init__(self, index: PolicyIndex, enabled: bool = True):
        self._index = index
        self._enabled = enabled

    def retrieve_basis(self, state: dict, top_k: int = 3) -> list[dict]:
        """按当前报销单状态检索制度条款依据"""
        if not self._enabled:
            return []
        invoice = state.get("invoice_data") or {}
        invoice_input = state.get("invoice_input") or {}
        # 作用：构造检索 query = 类型提示 + 票面/事由/风险
        parts = [self.TYPE_HINTS.get(invoice.get("invoice_type", ""), "")]
        parts += [invoice.get("invoice_type", ""), invoice.get("title", "")]
        parts += [invoice_input.get("purpose", "")]
        parts += list(invoice.get("risk_flags", []))
        query = " ".join(p for p in parts if p)
        return self._index.retrieve(query, top_k)

    def search(self, query: str, top_k: int = 3) -> list[dict]:
        """#32 函数调用：按关键词直接检索制度条款（给 LLM 自主调用），返回 {clause_id, source, text, score}"""
        if not self._enabled or not (query or "").strip():
            return []
        return self._index.retrieve(query.strip(), top_k)
