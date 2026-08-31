# tests/test_rag.py —— 制度条款 RAG 检索
# 业务：验证条款切块、按场景检索命中相关制度（docs/03 §3）
from app.core.config import CLAUSES_DIR
from app.shared.policies.rag import PolicyIndex
from app.tools.policy_rag_tool import PolicyRagTool


def _index() -> PolicyIndex:
    return PolicyIndex(CLAUSES_DIR, top_k=3)


def test_clauses_loaded():
    # 作用：全部制度文档被切块索引
    index = _index()
    assert len(index.chunks) >= 8                     # 4 份文档 × 若干条款
    sources = {c.source for c in index.chunks}
    assert {"差旅费", "票据管理", "通用报销原则"} <= sources


def test_hotel_query_hits_travel_clause():
    # 业务：酒店发票场景 → 命中差旅住宿条款（作为依据引用）
    hits = _index().retrieve("酒店 住宿 限额 宾馆 标准 报销")
    assert hits, "应检索到条款"
    assert any("差旅费" in h["clause_id"] for h in hits)
    assert all({"clause_id", "source", "text", "score"} <= set(h) for h in hits)


def test_receipt_query_hits_receipt_clause():
    # 业务：票据合规场景 → 命中票据管理条款
    hits = _index().retrieve("发票 抬头 税号 税务 监制章 报销 凭证")
    assert hits
    assert any("票据管理" in h["clause_id"] for h in hits)


def test_rag_tool_builds_query_from_state(container):
    # 作用：工具从报销状态构造检索 query，产出 policy_basis
    state = {
        "invoice_data": {"invoice_type": "酒店发票", "title": "住宿", "amount": 450.0},
        "invoice_input": {"purpose": "客户拜访住宿"},
    }
    basis = container.policy_rag.retrieve_basis(state)
    assert basis, "酒店发票场景应检索到住宿条款"
    assert any("差旅费" in b["clause_id"] for b in basis)


def test_rag_tool_disabled_returns_empty():
    # 作用：关闭 RAG 时返回空依据（确定性检查仍照常）
    tool = PolicyRagTool(PolicyIndex(CLAUSES_DIR), enabled=False)
    assert tool.retrieve_basis({"invoice_data": {}}) == []
