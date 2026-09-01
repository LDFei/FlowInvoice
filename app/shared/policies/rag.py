# app/shared/policies/rag.py —— 制度条款检索（RAG 核心）
# 业务：把非结构化政策文本（app/policy/clauses/*.md）切块索引，
#       按报销场景检索相关条款，作为合规判断/总结/退回的"依据"（可解释性，docs/03 §3）
import logging
import math
import re
from collections import Counter
from pathlib import Path

logger = logging.getLogger(__name__)


def _bigrams(text: str) -> Counter[str]:
    """轻量分词：英文/数字整词 + 中文相邻字符二元组（无第三方依赖，确定性可复现）"""
    # 作用：中文无空格分词，用二元组捕捉短语；英文/数字直接整词；计数供 BM25 TF 用
    tokens: Counter[str] = Counter()
    s = text.lower()
    for word in re.findall(r"[a-z0-9]+", s):
        tokens[word] += 1
    chars = re.findall(r"[一-鿿]", s)
    for a, b in zip(chars, chars[1:]):
        tokens[a + b] += 1
    return tokens


class ClauseChunk:
    """一条制度条款（切块单元）"""

    def __init__(self, clause_id: str, source: str, text: str):
        self.clause_id = clause_id   # 条款号，如 "差旅费:3.1 城市间交通费标准"（供总结引用）
        self.source = source         # 来源文档名
        self.text = text
        self.freq = _bigrams(text)           # 词频（BM25 TF）
        self.tokens = set(self.freq)         # 词集合（IDF 计算用）
        self.dl = sum(self.freq.values())    # 文档长度（token 数，BM25 长度归一）


def _rrf_fuse(bm25_hits: list[dict], vec_hits: list[dict], top_k: int, k: int = 60) -> list[dict]:
    """倒数排名融合（RRF）：合并 BM25 与向量两份榜单

    作用：两份榜单分数单位不同（词频 vs 余弦），不可直接比；
          改按名次计分（第 n 名得 1/(k+n)），同一条款在榜单越靠前分越高、跨榜叠加
    """
    scores: dict[str, float] = {}
    meta: dict[str, dict] = {}
    for hits in (bm25_hits, vec_hits):
        for i, h in enumerate(hits):
            cid = h["clause_id"]
            meta.setdefault(cid, {"clause_id": cid, "source": h["source"], "text": h["text"]})
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + i + 1)
    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    return [{**meta[cid], "score": round(s, 4)} for cid, s in ranked[:top_k]]


class PolicyIndex:
    """制度条款索引：文档 → 条款切块 → BM25 词法检索（可选 + pgvector 向量混合）

    业务：vector_store 为 None 时纯 BM25（默认，离线可跑）；
          传入 PolicyVectorStore 时走"BM25 + 向量"混合检索（RRF 融合），
          PG 不可用/未种子自动降级回 BM25 —— 调用方（图节点/工具）零改动
    """

    def __init__(self, clauses_dir: Path, top_k: int = 3, vector_store=None):
        self.chunks = self._load(clauses_dir)
        self.top_k = top_k
        self.vector_store = vector_store
        self._idf = self._compute_idf()
        # 作用：BM25 长度归一的分母（avgdl），文档平均 token 数
        self._avgdl = sum(c.dl for c in self.chunks) / len(self.chunks) if self.chunks else 1.0

    def _load(self, clauses_dir: Path) -> list[ClauseChunk]:
        """按 `## ` 小节把制度文档切成条款块"""
        chunks: list[ClauseChunk] = []
        for md in sorted(clauses_dir.glob("*.md")):
            doc = md.read_text(encoding="utf-8")
            source = md.stem
            # 作用：[1:] 跳过首个分片（首个 ## 之前的文档前言/标题块），避免前言变伪条款
            for section in re.split(r"^##\s+", doc, flags=re.MULTILINE)[1:]:
                lines = [ln for ln in section.splitlines() if ln.strip()]
                if len(lines) < 2:          # 只有标题没有正文的跳过
                    continue
                title = lines[0].strip()
                body = "\n".join(lines[1:]).strip()
                if not body:
                    continue
                chunks.append(ClauseChunk(
                    clause_id=f"{source}:{title}",
                    source=source,
                    text=f"{title}\n{body}",
                ))
        return chunks

    def _compute_idf(self) -> dict[str, float]:
        """计算逆文档频率（BM25 公式：出现条款越少的词越有区分度）"""
        n = len(self.chunks)
        df: dict[str, int] = {}
        for c in self.chunks:
            for t in c.tokens:
                df[t] = df.get(t, 0) + 1
        return {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in df.items()}

    def _bm25_hits(self, query: str, top_k: int) -> list[dict]:
        """BM25 词法检索（混合检索的词法侧；词频+长度归一，精确数字/术语命中稳）

        公式：score = Σ idf(t) · tf·(k1+1) / (tf + k1·(1-b+b·dl/avgdl))，k1=1.5, b=0.75
        """
        k1, b = 1.5, 0.75
        q_tokens = set(_bigrams(query))
        scored: list[tuple[float, ClauseChunk]] = []
        for c in self.chunks:
            norm = k1 * (1 - b + b * c.dl / self._avgdl)
            score = 0.0
            for t in q_tokens:
                tf = c.freq.get(t, 0)
                if tf:
                    score += self._idf.get(t, 0.0) * tf * (k1 + 1) / (tf + norm)
            if score > 0:
                scored.append((score, c))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [
            {"clause_id": c.clause_id, "source": c.source, "text": c.text, "score": round(s, 3)}
            for s, c in scored[:top_k]
        ]

    def retrieve(self, query: str, top_k: int | None = None) -> list[dict]:
        """按查询检索 top-k 条款：{clause_id, source, text, score}（无命中返回空）

        有向量后端 → BM25 + 向量 RRF 融合（词与语义双兜底）；
        无后端 / PG 挂了 / 未种子 / 模型加载失败 → 自动降级纯 BM25，不打断主流程
        """
        top_k = top_k or self.top_k
        bm25_hits = self._bm25_hits(query, top_k)
        if self.vector_store is None:
            return bm25_hits
        try:
            vec_hits = self.vector_store.search(query, top_k)
        except Exception as exc:
            logger.warning("向量检索失败，降级纯 BM25：%r", exc)
            return bm25_hits
        if not vec_hits:
            return bm25_hits
        logger.info(
            "混合检索：BM25=%d 条 + 向量=%d 条 → RRF 融合 top-%d",
            len(bm25_hits), len(vec_hits), top_k,
        )
        return _rrf_fuse(bm25_hits, vec_hits, top_k)
