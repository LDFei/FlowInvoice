# app/adapters/llm.py —— LLM 适配器（DeepSeek，OpenAI 兼容 Chat Completions）
# 业务：LLM 只做"理解与表达"（识别/总结/分类/解释），不碰钱与真伪的判定（docs/03 §3 混合决策 + docs/04 识别≠验真）。
#       无 key / 网络失败 / 返回异常 → 抛 LlmUnavailableError，调用方降级确定性路径，主流程不被打断。
#       可替换适配器：换 OpenAI/通义/Kimi 只改 base_url 与模型名（对应 docs/01 §6 依赖倒置）。
import json
import re

import httpx

from app.core.config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL


class LlmUnavailableError(Exception):
    """LLM 不可用（无 key / 网络 / 接口错误 / 输出非 JSON）——调用方应降级到确定性路径"""


def _extract_json(content: str) -> dict:
    """容错解析 LLM 输出：直接 JSON / 包裹在 ```json 代码块 / 文本里嵌一段 JSON"""
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass
    # 作用：模型偶发把 JSON 包在 ```json ... ``` 或前后缀里，取第一个 { ... } 完整段
    m = re.search(r"\{.*\}", content, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    raise LlmUnavailableError(f"LLM 输出非合法 JSON: {content[:120]!r}")


class LLMClient:
    """OpenAI 兼容 Chat Completions 客户端（默认 DeepSeek；client 可注入便于测试 MockTransport）"""

    def __init__(
        self,
        api_key: str = LLM_API_KEY,
        model: str = LLM_MODEL,
        base_url: str = LLM_BASE_URL,
        client: httpx.Client | None = None,
    ):
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=30)

    def complete(
        self,
        system: str,
        user: str,
        *,
        json_mode: bool = True,
        temperature: float = 0,
    ) -> dict:
        """调用一次 Chat Completions；json_mode 强制 JSON 输出（抽取/分类用，便于工具校验）

        返回：json_mode=True → 解析后的 dict；json_mode=False → {"text": "<模型输出>"}
        失败：一律抛 LlmUnavailableError（不抛原始异常，调用方只需处理这一种）
        """
        if not self._api_key:
            raise LlmUnavailableError("未配置 FLOWINVOICE_LLM_API_KEY")

        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        try:
            resp = self._client.post(
                f"{self._base_url}/v1/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=payload,
            )
        except httpx.HTTPError as exc:
            raise LlmUnavailableError(f"LLM 请求失败: {exc}") from exc

        if resp.status_code != 200:
            raise LlmUnavailableError(f"LLM 接口返回 {resp.status_code}: {resp.text[:200]}")

        try:
            content = resp.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, json.JSONDecodeError) as exc:
            raise LlmUnavailableError(f"LLM 返回结构异常: {exc}") from exc

        if not json_mode:
            return {"text": content}
        return _extract_json(content)
