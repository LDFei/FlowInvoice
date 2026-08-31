# app/shared/policies/rag.py —— 制度条款检索（RAG 核心）
# 业务：把非结构化政策文本（app/policy/clauses/*.md）切块索引，
#       按报销场景检索相关条款，作为合规判断/总结/退回的"依据"（可解释性，docs/03 §3）
import math
import re
from pathlib import Path


def _bigrams(text: str) -> set[str]:
    """轻量分词：英文/数字整词 + 中文相邻字符二元组（无第三方依赖，确定性可复现）"""
    # 作用：中文无空格分词，用二元组捕捉短语；英文/数字直接整词
    tokens: set[str] = set()
    s = text.lower()
    for word in re.findall(r"[a-z0-9]+", s):
        tokens.add(word)
    chars = re.findall(r"[一-鿿]", s)
    for a, b in zip(chars, chars[1:]):
        tokens.add(a + b)
    return tokens


class ClauseChunk:
    """一条制度条款（切块单元）"""

    def __init__(self, clause_id: str, source: str, text: str):
        self.clause_id = clause_id   # 条款号，如 "差旅费:3.1 城市间交通费标准"（供总结引用）
        self.source = source         # 来源文档名
        self.text = text
        self.tokens = _bigrams(text)


class PolicyIndex:
    """制度条款索引：文档 → 条款切块 → BM25 词法检索

    业务：真实向量 RAG（嵌入+Chroma）只需替换 retrieve() 内部实现，
          调用方（图节点/工具）零改动 —— 这就是可插拔边界
    """

    def __init__(self, clauses_dir: Path, top_k: int = 3):
        self.chunks = self._load(clauses_dir)
        self.top_k = top_k
        self._idf = self._compute_idf()

    def _load(self, clauses_dir: Path) -> list[ClauseChunk]:
        """按 `## ` 小节把制度文档切成条款块"""
        chunks: list[ClauseChunk] = []
        for md in sorted(clauses_dir.glob("*.md")):
            doc = md.read_text(encoding="utf-8")
            source = md.stem
            for section in re.split(r"^##\s+", doc, flags=re.MULTILINE):
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

    def retrieve(self, query: str, top_k: int | None = None) -> list[dict]:
        """按查询检索 top-k 条款：{clause_id, source, text, score}（无命中返回空）"""
        q_tokens = _bigrams(query)
        top_k = top_k or self.top_k
        scored: list[tuple[float, ClauseChunk]] = []
        for c in self.chunks:
            score = sum(
                self._idf.get(t, 0.0)
                for t in q_tokens if t in c.tokens
            )
            if score > 0:
                scored.append((score, c))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [
            {"clause_id": c.clause_id, "source": c.source, "text": c.text, "score": round(s, 3)}
            for s, c in scored[:top_k]
        ]
