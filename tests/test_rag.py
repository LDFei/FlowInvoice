# tests/test_rag.py —— 制度条款 RAG 检索
# 业务：验证条款切块、按场景检索命中相关制度（docs/03 §3）
from app.core.config import CLAUSES_DIR
from app.shared.policies.rag import PolicyIndex, _rrf_fuse
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


class _FakeVectorStore:
    """假向量后端：返回预设命中（单测验证融合/降级，不碰 PG 与模型）"""

    def __init__(self, hits=None, broken=False):
        self._hits = hits or []
        self._broken = broken

    def search(self, query, top_k):
        if self._broken:
            raise ConnectionError("pg down")
        return self._hits[:top_k]


def test_rrf_fuses_two_rankings():
    # 作用：两份榜单按名次计分合并，双榜都有的条款排第一（score 不可比，只比名次）
    bm25 = [
        {"clause_id": "A", "source": "s", "text": "a", "score": 1.0},
        {"clause_id": "B", "source": "s", "text": "b", "score": 0.5},
    ]
    vec = [
        {"clause_id": "B", "source": "s", "text": "b", "score": 0.9},
        {"clause_id": "C", "source": "s", "text": "c", "score": 0.8},
    ]
    fused = _rrf_fuse(bm25, vec, top_k=2)
    assert fused[0]["clause_id"] == "B"          # 两榜都进前二 → 名次分叠加
    assert {f["clause_id"] for f in fused} == {"A", "B"}


def test_hybrid_degrades_to_bm25_when_vector_down():
    # 作用：PG 不可用/未种子时自动降级纯 BM25，检索仍命中（降级不打断主流程）
    index = PolicyIndex(CLAUSES_DIR, vector_store=_FakeVectorStore(broken=True))
    hits = index.retrieve("酒店 住宿 限额 宾馆 标准 报销")
    assert hits
    assert any("差旅费" in h["clause_id"] for h in hits)


def test_hybrid_merges_vector_hits():
    # 作用：向量侧召回的词法未命中条款，混合结果应包含它（双路兜底）
    index = PolicyIndex(
        CLAUSES_DIR,
        vector_store=_FakeVectorStore([
            {"clause_id": "业务招待费:3.2 宴请标准", "source": "业务招待费",
             "text": "宴请接待每人标准 200 元", "score": 0.99},
        ]),
    )
    hits = index.retrieve("酒店 住宿 限额 宾馆 标准 报销", top_k=5)
    assert any("业务招待费" in h["clause_id"] for h in hits)
