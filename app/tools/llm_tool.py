# app/tools/llm_tool.py —— LLM 工具层（封装适配器，供图节点调用，docs/03 §3 混合决策）
# 业务：每类理解任务一个方法；LLM 不可用返回 None（调用方降级确定性路径），
#       LLM 输出必须过工具校验（防幻觉脏数据进状态，docs/04 识别≠验真）。
import json
from pathlib import Path

from app.adapters.llm import LLMClient, LlmUnavailableError
from app.shared.policies.errors import OcrFailedError
from app.tools.ocr_tool import _normalize_date, _parse_amount

# 作用：LLM 抽取结果成功缓存（按票面文本），避免同一张票重复付费调用；只缓存成功，失败不缓存
_CACHE_MAX = 128


# 作用：LLM 输出 key → 契约字段（模型偶发用中文标签当 key，需同时兼容英文 key 与中文标签）
_FIELD_ALIAS = {
    "invoice_no": ("invoice_no", "发票号码", "发票号", "发票编号", "票号"),
    "invoice_type": ("invoice_type", "发票类型", "票据类型", "票种"),
    "date": ("date", "开票日期", "开票时间", "日期"),
    "amount": ("amount", "金额", "价税合计", "合计金额", "票价"),
    "title": ("title", "项目", "品名", "名称", "服务名称", "行程"),
}


def _validate_invoice(data: dict) -> dict:
    """LLM 抽取结果 → 业务契约（防幻觉脏数据进状态；非法抛 OcrFailedError）"""
    if not isinstance(data, dict):
        raise OcrFailedError("LLM 抽取结果非对象")
    out: dict = {}
    for key, aliases in _FIELD_ALIAS.items():
        value = next((data[a] for a in aliases if data.get(a) is not None and str(data[a]).strip()), None)
        if value is None:
            raise OcrFailedError(f"票面缺少字段: {key}")
        out[key] = str(value).strip()
    out["amount"] = _parse_amount(out["amount"])   # 非法金额 → OcrFailedError（与正则路径一致）
    out["date"] = _normalize_date(out["date"])
    return out


class LlmTool:
    """LLM 理解任务集合（识别兜底 / 总结 / 分类 / 合规解释），每个方法失败都安全降级"""

    def __init__(self, client: LLMClient):
        self._client = client
        self._invoice_cache: dict[str, dict] = {}

    # ---------------- L4 识别兜底 ----------------

    def extract_invoice_text(self, text: str) -> dict | None:
        """文本票面 → LLM 强约束 JSON 抽取；不可用返回 None，输出非法抛 OcrFailedError"""
        cached = self._invoice_cache.get(text)
        if cached is not None:
            return cached
        system = (
            "你是发票票面信息抽取器，只输出 JSON，不要任何解释。\n"
            "JSON 键名固定为英文，不允许改："
            '{"invoice_no": 发票号码, "invoice_type": 发票类型, "date": 开票日期(YYYY-MM-DD), '
            '"amount": 金额(纯数字), "title": 项目/品名}。\n'
            "识别不出的字段给空字符串。"
        )
        try:
            data = self._client.complete(system, f"请从以下票面文本抽取字段：\n{text}")
        except LlmUnavailableError:
            return None
        validated = _validate_invoice(data)   # 非法 → OcrFailedError 上抛
        if len(self._invoice_cache) >= _CACHE_MAX:
            self._invoice_cache.clear()
        self._invoice_cache[text] = validated
        return validated

    # ---------------- 总结 ----------------

    def build_summary(self, state: dict) -> str | None:
        """结构化事实 → 给审核人员的自然语言总结；不可用返回 None（调用方降级模板）"""
        invoice = state.get("invoice_data") or {}
        facts = {
            "发票类型": invoice.get("invoice_type"),
            "票面金额": invoice.get("amount"),
            "票号": invoice.get("invoice_no"),
            "开票日期": invoice.get("date"),
            "验真": (state.get("verification") or {}).get("note", ""),
            "事前申请": (state.get("advance_application") or {}).get("app_id", "无"),
            "合规检查": state.get("compliance_checks", []),
            "政策依据": state.get("policy_basis", []),
            "风险标记": invoice.get("risk_flags", []),
        }
        system = (
            "你是报销审核助理。基于结构化事实，写一段给审核人员的简洁中文总结："
            "包含票面信息、验真结论、合规逐条结论、政策条款依据、风险点。"
            "只输出纯文本，不要 JSON，不要 markdown 标题。"
        )
        return self._complete_text(system, json.dumps(facts, ensure_ascii=False))

    # ---------------- 业务分类 ----------------

    def classify_direction(self, state: dict, valid_directions: list[str]) -> str | None:
        """申报内容 → 业务方向；结果必须 ∈ valid_directions 才返回（防幻觉发明新业务）"""
        hint = state.get("invoice_input") or {}
        system = (
            "你是企业报销单业务分类器。根据申报内容判断业务方向，只能从允许列表中选一个。\n"
            f"允许方向：{', '.join(valid_directions)}。\n"
            '只输出 JSON：{"direction": "方向"}；无法判断则 {"direction": ""}。'
        )
        user = json.dumps({
            "申报方向提示": hint.get("direction", ""),
            "报销事由": hint.get("purpose", ""),
            "申报金额": hint.get("declared_amount", ""),
            "票据片段": _file_snippet(hint.get("file_path", "")),
        }, ensure_ascii=False)
        try:
            data = self._client.complete(system, user)
        except LlmUnavailableError:
            return None
        guess = str(data.get("direction", "") or "").strip()
        return guess if guess in valid_directions else None

    # ---------------- 合规解释（不改判定） ----------------

    def explain_compliance(self, checks: list, policy_basis: list, invoice: dict) -> str | None:
        """确定性检查结果 + 条款 → 中文解释；只解释不改判；不可用返回 None"""
        facts = {
            "发票类型": invoice.get("invoice_type"),
            "票面金额": invoice.get("amount"),
            "合规检查": checks,
            "制度条款": policy_basis,
        }
        system = (
            "你是报销合规解释助手。基于已定的合规检查结论与制度条款，用中文向报销人解释"
            "每条结论（通过/不通过）及依据。不要改变检查结论，不要增删规则，只输出纯文本。"
        )
        return self._complete_text(system, json.dumps(facts, ensure_ascii=False))

    # ---------------- 内部 ----------------

    def _complete_text(self, system: str, user: str) -> str | None:
        try:
            text = self._client.complete(system, user, json_mode=False)["text"].strip()
        except LlmUnavailableError:
            return None
        return text or None


def _file_snippet(file_path: str, limit: int = 300) -> str:
    """读票据文件前 limit 字符作分类线索；读不到返回空串（不打断流程）"""
    if not file_path:
        return ""
    try:
        raw = Path(file_path).read_bytes()[:limit]
        return raw.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 —— 分类只是线索，读不到不影响主流程
        return ""
