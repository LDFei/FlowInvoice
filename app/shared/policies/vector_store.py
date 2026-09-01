# app/shared/policies/vector_store.py —— 政策条款向量库（PostgreSQL + pgvector）
# 业务：条款文本 → bge-m3 向量 → 落 pgvector（policy_chunks 表）；
#       按查询余弦召回 top-k，作为 PolicyIndex 混合检索的向量侧（docs/03 §3）
# 注意：pgvector/psycopg 在 _connect 内惰性导入——纯 BM25 模式（未配 DSN）不依赖这两个包
from app.shared.policies.embedder import BgeM3Embedder


class PolicyVectorStore:
    """政策条款向量库：连接 / 建表 / 写入 / 余弦检索

    约定：连接惰性（首次真正用库才连）；任何异常由调用方降级回 BM25，不影响主流程
    """

    def __init__(self, dsn: str, embedder=None):
        self._dsn = dsn
        self._embedder = embedder or BgeM3Embedder()
        self._conn = None

    def _connect(self):
        # 作用：惰性建立 PG 连接并注册 pgvector 类型（多次调用复用同一连接）
        # 业务：pgvector/psycopg 在此惰性导入，保证"纯 BM25 离线可跑"不依赖向量栈
        if self._conn is None or self._conn.closed:
            import psycopg
            from pgvector.psycopg import register_vector
            from psycopg.rows import dict_row

            # 作用：连接超时 3s、单条查询超时 5s——PG 不可用/卡死时快速降级 BM25
            # 业务：connect_timeout 只控制"建连"；statement_timeout 控制建连后的查询执行
            self._conn = psycopg.connect(
                self._dsn,
                row_factory=dict_row,
                connect_timeout=3,
                options="-c statement_timeout=5000",
            )
            register_vector(self._conn)
        return self._conn

    def _query(self, sql: str, params: tuple, retry: bool = True):
        """执行查询；PG 断线（连接失效）时丢弃连接重建重试一次，再失败交给调用方降级"""
        import psycopg

        try:
            conn = self._connect()
            return conn.execute(sql, params)
        except psycopg.OperationalError:
            if not retry:
                raise
            self._conn = None            # 断线：清掉失效连接
            conn = self._connect()       # 重建后重试一次
            return conn.execute(sql, params)

    def ensure_schema(self) -> None:
        """建表 + HNSW 余弦索引（幂等；seed 前调用）"""
        conn = self._connect()
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS policy_chunks (
                id BIGSERIAL PRIMARY KEY,
                clause_id TEXT NOT NULL UNIQUE,
                source TEXT NOT NULL,
                text TEXT NOT NULL,
                embedding vector(1024)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS policy_chunks_hnsw
            ON policy_chunks USING hnsw (embedding vector_cosine_ops)
            """
        )
        conn.commit()

    def seed(self, clauses: list[dict]) -> None:
        """把条款切块 upsert 进向量库（clauses 元素含 clause_id/source/text）"""
        conn = self._connect()
        vecs = self._embedder.embed([c["text"] for c in clauses])
        for c, v in zip(clauses, vecs):
            conn.execute(
                """
                INSERT INTO policy_chunks (clause_id, source, text, embedding)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (clause_id) DO UPDATE SET
                    source = EXCLUDED.source,
                    text = EXCLUDED.text,
                    embedding = EXCLUDED.embedding
                """,
                (c["clause_id"], c["source"], c["text"], v),
            )
        conn.commit()

    def search(self, query: str, top_k: int) -> list[dict]:
        """查询 embedding 后按余弦相似度召回 top-k 条款（score = 1 - 余弦距离）"""
        qv = self._embedder.embed([query])[0]
        rows = self._query(
            """
            SELECT clause_id, source, text,
                   1 - (embedding <=> %s::vector) AS score
            FROM policy_chunks
            ORDER BY embedding <=> %s::vector
            LIMIT %s
            """,
            (qv, qv, top_k),
        ).fetchall()
        return [
            {"clause_id": r["clause_id"], "source": r["source"], "text": r["text"], "score": round(r["score"], 3)}
            for r in rows
        ]
