# app/core/seed_vectors.py —— 政策条款 → bge-m3 向量 → pgvector（手动执行）
# 业务：把 app/policy/clauses/*.md 切块向量化入库，供混合 RAG 检索（docs/03 §3）
# 使用：FLOWINVOICE_PG_DSN=postgresql://flowinvoice:flowinvoice_dev@localhost:5432/flowinvoice \
#       python -m app.core.seed_vectors
#       国内网络可先 export HF_ENDPOINT=https://hf-mirror.com 加速模型下载
from app.core.config import CLAUSES_DIR, RAG_VECTOR_DSN
from app.shared.policies.rag import PolicyIndex
from app.shared.policies.vector_store import PolicyVectorStore


def main() -> None:
    if not RAG_VECTOR_DSN:
        print("未设置 FLOWINVOICE_PG_DSN，跳过向量种子（纯 BM25 模式）")
        return
    store = PolicyVectorStore(RAG_VECTOR_DSN)
    store.ensure_schema()
    # 复用 PolicyIndex 的切块逻辑（同一份条款来源，检索与入库不漂移）
    index = PolicyIndex(CLAUSES_DIR)
    clauses = [
        {"clause_id": c.clause_id, "source": c.source, "text": c.text}
        for c in index.chunks
    ]
    print(f"切块 {len(clauses)} 条，开始 bge-m3 向量化并入库（首次会下载模型）...")
    store.seed(clauses)
    print(f"完成：policy_chunks 已写入 {len(clauses)} 条")


if __name__ == "__main__":
    main()
