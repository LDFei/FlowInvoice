# app/shared/policies/embedder.py —— bge-m3 文本向量化（sentence-transformers / torch）
# 业务：制度条款与检索查询统一转成 1024 维稠密向量，供 pgvector 余弦检索（docs/03 §3）
class BgeM3Embedder:
    """bge-m3 稠密向量模型封装（惰性加载：首次 embed 才下载/加载模型）"""

    def __init__(self, model_name: str = "BAAI/bge-m3"):
        self._model_name = model_name
        self._model = None

    def _ensure(self):
        # 作用：首次调用才 import sentence_transformers 并加载模型，避免无向量场景也要装依赖
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self._model_name)
        return self._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        """批量文本 → 1024 维稠密向量列表（L2 归一化，余弦检索直接可比）"""
        model = self._ensure()
        return model.encode(texts, normalize_embeddings=True).tolist()
