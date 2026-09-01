# app/tools/ocr_tool.py —— 发票识别工具（按票据形态分派的分层管线，docs/04 §3）
# 业务：OcrTool.extract(file) -> dict 契约稳定，下游图节点与测试零改动（docs/04 §6）；
#       新票种 = 注册表加解析器/模板/映射，形态探测与业务解耦，跨业务复用。
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from app.shared.policies.errors import OcrFailedError


# ================= 共享字段归一工具（XML / 文本 / LLM 三路共用） =================

_CNS = {"零": 0, "壹": 1, "贰": 2, "叁": 3, "肆": 4, "伍": 5, "陆": 6, "柒": 7, "捌": 8, "玖": 9}
_CNU = {"拾": 10, "佰": 100, "仟": 1000, "万": 10000, "亿": 100000000}


def _cn_upper_to_float(s: str) -> float | None:
    """中文大写金额 → float（支持 伍佰元整 / 壹仟贰佰捌拾元整 / 壹拾贰万叁仟元整；含角分返回 None）"""
    body = s.replace("元整", "").replace("元", "").replace("圆", "").strip()
    if not body:
        return None
    total = section = 0.0
    num = 0
    for ch in body:
        if ch in _CNS:
            num = _CNS[ch]
        elif ch in _CNU:
            unit = _CNU[ch]
            if unit in (10000, 100000000):
                total += (section + num) * unit
                section = 0.0
                num = 0
            else:
                section += (num or 1) * unit
                num = 0
        else:
            return None  # 角/分或未知字暂不支持 → 上层报"金额非法"
    return total + section + num


def _parse_amount(value) -> float:
    """金额文本 → float（支持 ¥/￥/千分位/中文大写/数字+元后缀）；非法 → OcrFailedError"""
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace(",", "").replace("¥", "").replace("￥", "").replace(" ", "")
    # 作用：去掉"元"/"元整"后缀（880元 / 500元整 常见写法）
    if s.endswith("元整"):
        s = s[:-2]
    elif s.endswith("元"):
        s = s[:-1]
    if not s:
        raise OcrFailedError(f"票面金额格式非法: {value!r}，请确认后重传")
    try:
        return float(s)
    except ValueError:
        cn = _cn_upper_to_float(s)
        if cn is None:
            raise OcrFailedError(f"票面金额格式非法: {value!r}，请确认后重传")
        return cn


def _normalize_date(value) -> str:
    """开票日期 → YYYY-MM-DD（兼容 2026/8/1、2026年8月1日、2026.08.01）"""
    s = str(value).strip()
    m = re.search(r"(\d{4})[-/年.](\d{1,2})[-/月.](\d{1,2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2).zfill(2)}-{m.group(3).zfill(2)}"
    raise OcrFailedError(f"票面日期格式非法: {value!r}，请确认后重传")


# ================= 业务发票类别映射（识别层 → 业务规则层的桥，docs/04 §6） =================
# 业务：新业务/新票种 = 加一行映射，解析器与下游图节点零改动

_INVOICE_CLASS_RULES = [
    (("住宿", "房费", "酒店", "宾馆"), "酒店发票"),
    (("铁路", "火车", "高铁", "动车", "车票"), "火车票"),
    (("航空", "机票", "客票", "行程单"), "机票"),
    (("餐饮", "餐费", "用餐", "餐厅"), "餐饮发票"),
    (("出租", "网约", "打车", "滴滴", "出租车"), "打车行程单"),
]


def _classify_invoice_type(invoice_type: str, title: str) -> str:
    """票种 + 项目名 → 业务发票类别（合规规则消费的类别；命中不了保留票种原文）"""
    text = f"{invoice_type} {title}"
    for keywords, category in _INVOICE_CLASS_RULES:
        if any(k in text for k in keywords):
            return category
    return invoice_type


# ================= L1：数电票 XML / OFD 结构化解析 =================

class XmlInvoiceParser:
    """L1：数电票 XML 直接解析（命名空间无关 + 字段别名归一，零幻觉，docs/04 §3）"""

    # 作用：本地名 → 契约字段（兼容民间流通的 <Invoice> 英文元素 与 财政部 XBRL 入账实例的小驼峰）
    _FIELDS = {
        "invoice_no": ("InvoiceNo", "invoiceNo", "发票号码"),
        "invoice_type": ("InvoiceKind", "invoiceType", "发票种类"),
        "date": ("InvoiceDate", "invoiceDate", "开票日期"),
    }

    def matches(self, raw: bytes) -> bool:
        # 作用：内容嗅探——去除 BOM/空白后以 '<' 开头视为 XML
        return raw.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"<")

    def parse(self, raw: bytes) -> dict:
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            raise OcrFailedError(f"XML 解析失败: {exc}") from exc

        def local(tag: str) -> str:
            return tag.rsplit("}", 1)[-1]  # 去命名空间前缀

        def find_text(*names: str) -> str | None:
            # 作用：按本地名在整棵树里找第一个非空文本（不依赖命名空间）
            for el in root.iter():
                if local(el.tag) in names and (el.text or "").strip():
                    return el.text.strip()
            return None

        data: dict = {}
        for key, names in self._FIELDS.items():
            v = find_text(*names)
            if not v:
                raise OcrFailedError(f"XML 缺少字段: {names[0]}")
            data[key] = v

        # 报销口径 = 价税合计 Total（含税；Amount 是不含税合计，不能当票面金额）
        total = find_text("Total", "total", "价税合计")
        if not total:
            raise OcrFailedError("XML 缺少价税合计字段: Total")
        data["amount"] = _parse_amount(total)

        # 项目 = 第一条明细的品名（BuyerName/SellerName 本地名不同，不会误命中）
        title = next(
            (el.text.strip() for el in root.iter()
             if local(el.tag) in ("Name", "goodsName", "商品名称", "品名") and (el.text or "").strip()),
            None,
        )
        if not title:
            raise OcrFailedError("XML 缺少发票明细项目")
        data["title"] = title

        data["date"] = _normalize_date(data["date"])
        # 业务类别归一：票种 + 项目名 → 合规规则消费的类别（住宿服务费 → 酒店发票）
        data["invoice_type"] = _classify_invoice_type(data["invoice_type"], data["title"])

        # 扩展字段（下游不读，为验真/审计留位）：销方、税额、数字签名存在性
        seller = find_text("SellerName", "salerName", "销方名称")
        tax = find_text("TaxAmount", "taxAmount", "合计税额")
        if seller:
            data["seller_name"] = seller
        if tax:
            data["tax_amount"] = _parse_amount(tax)
        data["has_signature"] = any(local(el.tag) == "Signature" for el in root.iter())
        return data


# ================= L4：文本票面抽取（正则模板库 + 字段别名 + 金额鲁棒 + LLM 兜底） =================

class TextInvoiceParser:
    """L4：文本票据抽取——正则优先（快、免费、可离线）；正则失败 → LLM 强约束抽取兜底（docs/04 §3）"""

    # 作用：业务契约 key → 可能的票面标签（逐行匹配，谁先命中用谁；金额优先价税合计=含税口径）
    _FIELD_ALIASES = {
        "invoice_no": ("发票号码", "发票号", "发票编号", "票号", "车票号"),
        "invoice_type": ("发票类型", "票据类型", "票种"),
        "date": ("开票日期", "开票时间", "乘车日期", "行程日期", "日期"),
        "amount": ("价税合计", "合计金额", "金额", "票价", "费用合计"),
        "title": ("项目", "品名", "服务名称", "商品名称", "行程", "起止站"),
    }

    def __init__(self, llm_tool=None):
        self._llm = llm_tool  # 可选：L4 LLM 兜底抽取（无 key 时为 None，纯正则）

    def matches(self, raw: bytes) -> bool:
        return True  # 兜底解析器：任何非 XML 文本都尝试

    def parse(self, raw: bytes) -> dict:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise OcrFailedError(f"票面文件不是 UTF-8 文本: {exc}") from exc
        try:
            return self._parse_regex(text)
        except OcrFailedError:
            if self._llm is None:
                raise
            data = self._llm.extract_invoice_text(text)
            if data is None:  # LLM 不可用 → 保留正则的具体报错（提示缺哪个字段）
                raise
            return data

    def _parse_regex(self, text: str) -> dict:
        data: dict = {}
        for key, labels in self._FIELD_ALIASES.items():
            value = None
            for label in labels:
                m = re.search(rf"^\s*{re.escape(label)}\s*[:：]\s*(.+)$", text, re.MULTILINE)
                if m:
                    value = m.group(1).strip()
                    break
            if not value:
                raise OcrFailedError(f"票面缺少字段: {labels[0]}")
            if key == "amount":
                data[key] = _parse_amount(value)
            elif key == "date":
                data[key] = _normalize_date(value)
            else:
                data[key] = value

        # 作用：单文件多张票检测（出现多个不同票号 → 结构化退回，避免只认第一张）
        first_label = self._FIELD_ALIASES["invoice_no"][0]
        nums = re.findall(rf"^\s*{re.escape(first_label)}\s*[:：]\s*(\S+)\s*$", text, re.MULTILINE)
        if len(set(nums)) > 1:
            raise OcrFailedError("单文件包含多张票（多个发票号码），请拆分后分别上传")

        data["invoice_type"] = _classify_invoice_type(data["invoice_type"], data["title"])
        return data


# ================= 统一入口（形态分派） =================

class OcrTool:
    """发票票面识别：按票据形态分派到各解析层（docs/04 §3 统一入口，契约不变）"""

    def __init__(self, parsers=None, llm_tool=None):
        # 作用：分派表（顺序 = 精确优先，后置兜底）；新票种在此注册
        self._parsers = parsers or [
            XmlInvoiceParser(),
            TextInvoiceParser(llm_tool=llm_tool),
        ]

    def extract(self, file_path: str) -> dict:
        """识别票面 → 结构化数据；失败抛 OcrFailedError（由识别节点转为结构化退回）"""
        try:
            raw = Path(file_path).read_bytes()
        except Exception as exc:
            raise OcrFailedError(f"文件读取失败: {exc}") from exc
        for parser in self._parsers:
            if parser.matches(raw):
                return parser.parse(raw)
        raise OcrFailedError("无法识别的票据文件格式")
