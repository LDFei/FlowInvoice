# app/tools/ocr_tool.py —— 发票识别工具（按票据形态分派的分层管线，docs/04 §3）
# 业务：OcrTool.extract(file) -> dict 契约稳定，下游图节点与测试零改动（docs/04 §6）；
#       新票种 = 注册表加解析器/模板/映射，形态探测与业务解耦，跨业务复用。
# 识别闭环（docs/04 §3.1）：
#   - 形态覆盖 5 层：数电票XML(L1) / OFD(L2) / PDF(L3) / 文本(L4) / 图片OCR(L5)
#   - 契约分两层：核心字段（缺→退回）+ 扩展字段（尽力，缺→null，带 source 标注）
#   - 图片/扫描 OCR 只承诺核心字段，扩展字段尽力而为（表格排版结构上不可靠，不把幻觉当权威）
import io
import os
import re
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from app.shared.policies.errors import OcrFailedError


# ================= 共享字段归一工具（XML / OFD / PDF / 文本 / OCR 五路共用） =================

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


def _local(tag: str) -> str:
    """去命名空间前缀（OFD/XML 双路共用）"""
    return tag.rsplit("}", 1)[-1]


def _looks_like_invoice_xml(content: bytes) -> bool:
    """粗判：内容含发票结构化标签（元素名，非版式文本）→ 可能是数电票 XML。

    注意：识别错方向都不致命——误判随机 XML 为发票 → XmlInvoiceParser 抽不到必抽字段
    抛错被 OFD/PDF 上层 catch，落到版式文本抽取兜底；漏判真发票 XML → 同样走版式兜底。
    故标记宁广勿窄，覆盖 camelCase（Apifox 数电票）/ 民间英文 / 中文 / 拼音缩写。
    """
    markers = (
        b"<InvoiceNo", b"<invoiceNo", b"<invoice_number",
        b"<InvoiceItems", b"<InvoiceItem", b"<InvoiceLine",
        b"<totalAmount", b"<TotalAmount", b"<total_amount",
        b"<totalTaxAmount", b"<TotalTaxAmount",
        b"<salerName", b"<SalerName", b"<salerTaxNo",
        b"<buyerTaxNo", b"<buyerTaxId", b"<BuyerName",
        b"<ghdwsbh", b"<xhdwsbh", b"<Ghdwsbh", b"<Xhdwsbh",
        "<价税合计".encode("utf-8"), "<发票号码".encode("utf-8"),
        # 铁路电子客票数据（GB/T 44554.6 字段 camelCase：trainNum/passengerName/carrierDate/...）
        b"<trainNum", b"<passengerName", b"<electronicTicketNum",
        b"<stationGetOn", b"<carrierDate", b"<seatNo",
        b"<customerIdentityNum",
    )
    return any(m in content for m in markers)


# 铁路电子客票特征标签（GB/T 44554.6-2026 数据交换规范字段，本地名；零自造 tag，见 docs/04 §6）
_RAIL_TAGS = {
    "trainNum", "TrainNum", "stationGetOn", "StationGetOn", "stationGetOff", "StationGetOff",
    "passengerName", "PassengerName", "electronicTicketNum", "ElectronicTicketNum",
    "carrierDate", "CarrierDate", "seatNo", "SeatNo", "customerIdentityNum",
}


def _is_railway(root) -> bool:
    """整树扫描：出现任一铁路客票特征标签 → 判定为铁路电子客票 XML（实名票据，无购方抬头/发票类型字段）"""
    return any(_local(el.tag) in _RAIL_TAGS for el in root.iter())


def _first_text(root, names) -> str | None:
    """整棵树按本地名找第一个非空文本（命名空间无关，全别名命中即返回）"""
    for el in root.iter():
        if _local(el.tag) in names and (el.text or "").strip():
            return el.text.strip()
    return None


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


# ================= 文本/OCR 共用的字段别名（L4/L2版式/L3PDF文本/L5OCR） =================
# 业务：核心字段必抽（报销闭环底线），扩展字段尽力（为验真/抵扣/合规留位，docs/04 §3.1）

# 核心契约：缺任一 → OcrFailedError 结构化退回
_CORE_FIELD_ALIASES = {
    "invoice_no": ("发票号码", "发票号", "发票编号", "票号", "车票号"),
    "invoice_type": ("发票类型", "票据类型", "票种"),
    "date": ("开票日期", "开票时间", "乘车日期", "行程日期", "日期"),
    "amount": ("价税合计", "合计金额", "金额", "票价", "费用合计"),
    "title": ("项目", "品名", "服务名称", "商品名称", "行程", "起止站"),
}

# 扩展字段：命中则归一，缺 → null（不阻塞主流程）
_EXT_FIELD_ALIASES = {
    "tax_amount": ("合计税额", "税额合计", "税额"),
    "tax_rate": ("税率",),
    "buyer_name": ("购买方名称", "购方名称", "购买方"),
    "buyer_tax_no": ("购买方纳税人识别号", "购方纳税人识别号", "购买方税号", "购方税号"),
    "seller_name": ("销售方名称", "销方名称", "销售方"),
    "seller_tax_no": ("销售方纳税人识别号", "销方纳税人识别号", "销售方税号", "销方税号"),
    "check_code": ("校验码",),
    "remark": ("备注",),
    "issuer": ("开票人",),
    # 乘车/行程字段（#86：车票报销真实化——乘车人/车次/起止站/席别结构化，供席别合规节点消费）
    "train_no": ("车次", "列车车次"),
    "departure_station": ("发站", "出发站"),
    "arrival_station": ("到站", "到达站"),
    "seat_class": ("席别", "座别", "座位等级"),
    "seat_no": ("座位号", "席位号"),
    "passenger_name": ("乘车人", "乘车人姓名", "旅客姓名", "乘客姓名"),
}


def _read_label(text: str, label: str) -> str | None:
    """按行取「标签: 值」；MULTILINE 锚行首，兼容中文冒号/半角冒号"""
    m = re.search(rf"^\s*{re.escape(label)}\s*[:：]\s*(.+)$", text, re.MULTILINE)
    return m.group(1).strip() if m else None


def _looks_like_railway_text(text: str) -> bool:
    """粗判：文本票面带乘车要素（乘车日期 + 车次/起止站/席别）→ 铁路电子客票/报销凭证

    铁路实名票据（报销凭证/行程单）常无「发票类型/项目」行，识别层按乘车字段缺省
    invoice_type=火车票、title=起止站拼接（#86）。普通火车票文本票（发票类型: 火车票）
    也命中（有乘车日期+车次/起止站），只是两字段已显式给出不受缺省影响。
    """
    has_date = _read_label(text, "乘车日期") is not None
    ride = any(_read_label(text, label) is not None for label in ("车次", "列车车次", "发站", "出发站", "到站", "到达站"))
    return has_date and ride


def _ocr_image_bytes(provider, img_bytes: bytes) -> str:
    """图片字节 → 临时文件 → OCR provider → 文本（用后即删）"""
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        with open(path, "wb") as f:
            f.write(img_bytes)
        return provider.recognize(path)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# ================= L1：数电票 XML / OFD 结构化解析 =================

class XmlInvoiceParser:
    """L1：数电票 XML 直接解析（命名空间无关 + 字段别名归一，零幻觉，docs/04 §3）"""

    # 作用：本地名 → 契约字段。真实生态字段命名多种并存（docs/04 §3.4）：
    #       camelCase 接口数据（invoiceNo/totalAmount/salerName/buyerTaxNo，Apifox 数电票规范证实）、
    #       民间流通英文（InvoiceNo/SellerTaxId）、中文标签、拼音缩写（ghdwsbh/xhdwsbh 真实报文出现）
    #       → 别名宁广勿窄（find_text 精确匹配本地名 + 逐字段独立，多别名不会串字段）
    _FIELDS = {
        "invoice_no": ("InvoiceNo", "invoiceNo", "invoice_number", "Fphm", "fphm", "发票号码", "发票号"),
        "invoice_type": ("InvoiceKind", "invoiceKind", "invoiceType", "invoice_type",
                         "Fpzl", "fpzl", "发票类型", "发票种类", "票种"),
        "date": ("InvoiceDate", "invoiceDate", "invoice_date", "Kprq", "kprq", "开票日期", "发票日期"),
    }
    # 扩展字段（尽力，缺 → null；命名空间无关 + 多风格别名）
    _EXT_FIELDS = {
        "buyer_name": ("BuyerName", "buyerName", "buyer_name", "PurchaserName", "购买方名称", "购方名称", "买方名称"),
        "buyer_tax_no": ("BuyerTaxId", "buyerTaxId", "PurchaserTaxID", "buyerTaxNo", "BuyerTaxNo",
                         "buyer_tax_no", "Ghdwsbh", "ghdwsbh", "购买方纳税人识别号", "购方纳税人识别号", "购方税号", "购买方税号"),
        "seller_name": ("SellerName", "sellerName", "seller_name", "salerName", "SalerName", "saler_name",
                        "Xfmc", "xfmc", "销售方名称", "销方名称", "卖方名称"),
        "seller_tax_no": ("SellerTaxId", "sellerTaxId", "salerTaxNo", "SalerTaxNo", "sellerTaxNo",
                          "seller_tax_no", "saler_tax_no", "Xhdwsbh", "xhdwsbh", "销售方纳税人识别号",
                          "销方纳税人识别号", "销售方税号", "销方税号"),
        "tax_rate": ("TaxRate", "taxRate", "tax_rate", "税率", "征收率"),
        "check_code": ("CheckCode", "checkCode", "check_code", "校验码"),
        "remark": ("Remark", "remark", "remarks", "备注", "备注栏"),
        "issuer": ("Drawer", "drawer", "开票人", "开票员"),
        # 乘车/行程字段（#86，GB/T 44554.6 铁路客票 camelCase；文本票走 _EXT_FIELD_ALIASES 中文标签）
        "train_no": ("TrainNum", "trainNum", "train_number", "车次"),
        "departure_station": ("StationGetOn", "stationGetOn", "departure_station", "发站", "出发站"),
        "arrival_station": ("StationGetOff", "stationGetOff", "arrival_station", "到站", "到达站"),
        "seat_class": ("Seat", "seat", "SeatClass", "seatClass", "seat_class", "席别", "座别"),
        "seat_no": ("SeatNo", "seatNo", "seat_number", "seat_no", "座位号"),
        "passenger_name": ("PassengerName", "passengerName", "passenger_name", "乘车人", "旅客姓名"),
        "identity_no": ("CustomerIdentityNum", "customerIdentityNum", "customer_identity_num", "证件号码"),
        "issue_date": ("IssueDate", "issueDate", "issue_date", "开票日期"),
        "e_ticket_no": ("ElectronicTicketNum", "electronicTicketNum", "electronic_ticket_num", "电子客票号", "客票号"),
    }
    # 明细行（一对多 → 子表 invoice_items，docs/04 §3.1）：wrapper 元素多风格（含 camelCase 明细）
    _LINE_WRAPPERS = {"InvoiceItem", "InvoiceLine", "Goods", "GoodsItem", "Item", "item", "Items",
                      "Detail", "Details", "LineItem", "Line", "明细行"}
    _LINE_FIELDS = {
        "name": ("Name", "name", "goodsName", "ItemName", "itemName", "品名", "项目名称", "商品名称", "服务名称"),
        "spec": ("Spec", "spec", "Specification", "specification", "规格型号"),
        "unit": ("Unit", "unit", "单位"),
        "quantity": ("Quantity", "quantity", "Qty", "qty", "数量"),
        "unit_price": ("Price", "price", "unitPrice", "UnitPrice", "单价"),
        "amount": ("Amount", "amount", "LineAmount", "金额", "不含税金额"),
        "tax_rate": ("TaxRate", "taxRate", "tax_rate", "税率"),
        "tax_amount": ("TaxAmount", "taxAmount", "tax_amount", "税额"),
    }

    def matches(self, raw: bytes) -> bool:
        # 作用：内容嗅探——去除 BOM/空白后以 '<' 开头视为 XML
        return raw.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"<")

    def parse(self, raw: bytes) -> dict:
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            raise OcrFailedError(f"XML 解析失败: {exc}") from exc
        # 铁路电子客票走专用分支：实名票据无购方抬头/发票类型/明细项目，通用发票契约不适用
        if _is_railway(root):
            return self._parse_rail(root)

        data: dict = {}
        for key, names in self._FIELDS.items():
            v = _first_text(root, names)
            if not v:
                raise OcrFailedError(f"XML 缺少字段: {names[0]}")
            data[key] = v

        # 报销口径 = 价税合计（含税）：Total 民间流通 / totalAmount·total_amount camelCase·snake_case。
        # 注意：invoiceAmount/Amount 是不含税合计，不能当票面金额（含税 vs 不含税差异巨大）
        total = _first_text(root, ("Total", "total", "totalAmount", "TotalAmount", "total_amount",
                                   "TotalAmountIncludingTax", "价税合计"))
        if not total:
            raise OcrFailedError("XML 缺少价税合计字段（totalAmount/Total/价税合计）")
        data["amount"] = _parse_amount(total)

        # 明细行（一对多 → 子表）：先抽，title 优先取首行品名（比全局找 Name 更可靠——避免误抓购/销方名）
        line_items = self._extract_line_items(root)
        title = line_items[0].get("name") if line_items else None
        if not title:
            # 兜底：无明细行的 XML（或 wrapper 命名不识别）→ 全局找品名/项目标签
            title = next(
                (el.text.strip() for el in root.iter()
                 if _local(el.tag) in ("Name", "name", "goodsName", "ItemName", "itemName",
                                       "品名", "项目名称", "商品名称", "服务名称")
                 and (el.text or "").strip()),
                None,
            )
        if not title:
            raise OcrFailedError("XML 缺少发票明细项目")
        data["title"] = title

        data["date"] = _normalize_date(data["date"])
        # 业务类别归一：票种 + 项目名 → 合规规则消费的类别（住宿服务费 → 酒店发票）
        data["invoice_type"] = _classify_invoice_type(data["invoice_type"], data["title"])

        # 扩展字段（尽力，缺 → null；为验真/抵扣/合规留位）
        for key, names in self._EXT_FIELDS.items():
            data[key] = _first_text(root, names)  # tax_rate 保留原文（"13%" 等）
        tax = _first_text(root, ("TaxAmount", "taxAmount", "totalTaxAmount", "TotalTaxAmount", "total_tax", "合计税额", "税额合计"))
        data["tax_amount"] = _parse_amount(tax) if tax else None
        data["has_signature"] = any(_local(el.tag) == "Signature" for el in root.iter())
        data["line_items"] = line_items
        data["source"] = "xml"
        return data

    def _parse_rail(self, root) -> dict:
        """铁路电子客票 XML 专用解析（#86：乘车字段结构化 + 报销闭环必抽字段缺省）

        GB/T 44554.6 铁路电子客票数据无发票类型/明细项目/购方抬头（实名票据），
        字段为 camelCase：invoiceNo / carrierDate(乘车日期) / issueDate(开票日期) /
        trainNum / stationGetOn / stationGetOff / seat / seatNo / passengerName /
        customerIdentityNum / electronicTicketNum / totalAmount / totalTaxAmount。
        报销语义：date 取乘车日期（先 carrierDate 后 issueDate）；金额为票面价税合计；
        invoice_type 固定「火车票」；title 由起止站拼成（无品名项目可依）。
        """
        # 核心契约兜底（与通用发票同底线：缺票号/乘车日期/金额 → 报具体缺字段，供修样张/扩别名）
        invoice_no = _first_text(root, ("invoiceNo", "InvoiceNo", "发票号码", "发票号"))
        if not invoice_no:
            raise OcrFailedError("XML 缺少字段: invoiceNo")
        on_date = _first_text(root, ("carrierDate", "CarrierDate", "乘车日期"))
        if not on_date:
            on_date = _first_text(root, ("issueDate", "IssueDate", "开票日期"))
        if not on_date:
            raise OcrFailedError("XML 缺少字段: carrierDate（乘车日期）")
        total = _first_text(root, ("totalAmount", "TotalAmount", "Total", "total", "价税合计"))
        if not total:
            raise OcrFailedError("XML 缺少价税合计字段（totalAmount/Total/价税合计）")

        data: dict = {
            "invoice_no": invoice_no,
            "date": _normalize_date(on_date),
            "amount": _parse_amount(total),
            "invoice_type": "火车票",
            "title": "火车票",
        }
        dep = _first_text(root, ("stationGetOn", "StationGetOn", "发站"))
        arr = _first_text(root, ("stationGetOff", "StationGetOff", "到站"))
        if dep or arr:
            data["title"] = f"{dep or '?'}-{arr or '?'}"
        # 扩展字段（尽力，缺 → null）
        for key, names in self._EXT_FIELDS.items():
            data[key] = _first_text(root, names)
        tax = _first_text(root, ("TaxAmount", "taxAmount", "totalTaxAmount", "TotalTaxAmount", "合计税额", "税额合计"))
        data["tax_amount"] = _parse_amount(tax) if tax else None
        data["has_signature"] = any(_local(el.tag) == "Signature" for el in root.iter())
        data["line_items"] = []
        data["source"] = "xml"
        return data

    @staticmethod
    def _extract_line_items(root) -> list:
        """明细行尽力抽取：按行包装元素，逐行归一字段（缺的字段 → null，不阻塞）"""
        items = []

        def first_text(el, *names: str) -> str | None:
            for e in el.iter():
                if _local(e.tag) in names and (e.text or "").strip():
                    return e.text.strip()
            return None

        for el in root.iter():
            if _local(el.tag) not in XmlInvoiceParser._LINE_WRAPPERS:
                continue
            item: dict = {}
            for key, names in XmlInvoiceParser._LINE_FIELDS.items():
                v = first_text(el, *names)
                if v is not None and key in ("quantity", "unit_price", "amount", "tax_rate", "tax_amount"):
                    try:
                        item[key] = _parse_amount(v)
                    except OcrFailedError:
                        item[key] = v  # 非常规数值（如 "13%"）保留原文
                else:
                    item[key] = v
            if item.get("name"):
                items.append(item)
        return items


# ================= L2：数电票 OFD 版式解析 =================

class OfdInvoiceParser:
    """L2：OFD（开放版式文档）——zip 容器；优先取内嵌结构化发票 XML（复用 L1）；
        否则抽版式文本（TextObject → 阅读顺序）复用 L4 正则"""

    def matches(self, raw: bytes) -> bool:
        if not raw.startswith(b"PK"):
            return False
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                return "OFD.xml" in z.namelist()
        except (zipfile.BadZipFile, OSError):
            return False

    def parse(self, raw: bytes) -> dict:
        # 1) 内嵌结构化发票 XML（数电票 OFD 有时直接携带）
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                for name in z.namelist():
                    if not name.lower().endswith(".xml") or name.lower() == "ofd.xml":
                        continue
                    content = z.read(name)
                    if _looks_like_invoice_xml(content):
                        return {**XmlInvoiceParser().parse(content), "source": "ofd"}
        except (zipfile.BadZipFile, OcrFailedError, ET.ParseError):
            pass  # 非内嵌结构 → 落到版式文本抽取
        # 2) 版式文本抽取 → L4 正则
        text = _extract_ofd_text(raw)
        if not text.strip():
            raise OcrFailedError("OFD 版式文件无可抽取文本，请提供数电票 XML 或清晰图片")
        return {**TextInvoiceParser().parse_text(text), "source": "ofd"}


def _extract_ofd_text(raw: bytes) -> str:
    """OFD 版式文本抽取：遍历各页 Content.xml 的 TextObject 文本，按 (页, y, x) 恢复阅读顺序"""
    chunks: list[tuple[int, float, float, str]] = []  # (page, y, x, text)
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        content_files = sorted(
            n for n in z.namelist()
            if n.lower().endswith(".xml") and n.lower() != "ofd.xml"
        )
        for page_idx, name in enumerate(content_files):
            try:
                root = ET.fromstring(z.read(name))
            except (ET.ParseError, KeyError):
                continue
            for obj in root.iter():
                if _local(obj.tag) != "TextObject":
                    continue
                text = "".join(c.text or "" for c in obj if _local(c.tag) == "TextCode")
                x = y = 0.0
                b = obj.get("Boundary")  # OFD 位置属性："x y w h"
                if b:
                    parts = b.split()
                    if len(parts) >= 2:
                        try:
                            x, y = float(parts[0]), float(parts[1])
                        except ValueError:
                            pass
                if text.strip():
                    chunks.append((page_idx, y, x, text.strip()))
    chunks.sort(key=lambda t: (t[0], t[1], t[2]))
    return "\n".join(c[3] for c in chunks)


# ================= L3：PDF 解析 =================

class PdfInvoiceParser:
    """L3：PDF——优先内嵌结构化 XML 附件（数电票 PDF 常携带）；否则文本抽取；
        扫描件渲染成图走 OCR（需图片识别组件）"""

    def __init__(self, image_ocr=None):
        self._ocr = image_ocr

    def matches(self, raw: bytes) -> bool:
        return raw.startswith(b"%PDF")

    def parse(self, raw: bytes) -> dict:
        try:
            import fitz  # PyMuPDF
        except ImportError as exc:
            raise OcrFailedError("PDF 解析需要 PyMuPDF，请先安装") from exc
        doc = fitz.open(stream=raw, filetype="pdf")
        # 1) 内嵌结构化 XML 附件
        try:
            for name in doc.embfile_names():
                content = doc.embfile_get(name)
                if _looks_like_invoice_xml(content):
                    return {**XmlInvoiceParser().parse(content), "source": "pdf"}
        except Exception:
            pass  # 无附件/附件非 XML → 走文本
        # 2) 文本抽取（电子版 PDF 文字可选）
        text = "\n".join(p.get_text() for p in doc)
        if text.strip():
            return {**TextInvoiceParser().parse_text(text), "source": "pdf"}
        # 3) 扫描件 → 渲染成图 → OCR（核心字段必抽，扩展尽力）
        if self._ocr is not None:
            lines = []
            for page in doc:
                pix = page.get_pixmap(dpi=150)
                page_text = _ocr_image_bytes(self._ocr, pix.tobytes("png"))
                if page_text.strip():
                    lines.append(page_text)
            if lines:
                return {**TextInvoiceParser().parse_text("\n".join(lines)), "source": "pdf"}
        raise OcrFailedError("PDF 无可抽取文本（扫描件需安装图片识别组件 PaddleOCR）")


# ================= L4：文本票面抽取（正则模板库 + 字段别名 + 金额鲁棒 + LLM 兜底） =================

class TextInvoiceParser:
    """L4：文本票据抽取——正则优先（快、免费、可离线）；正则失败 → LLM 强约束抽取兜底（docs/04 §3）"""

    def __init__(self, llm_tool=None):
        self._llm = llm_tool  # 可选：L4 LLM 兜底抽取（无 key 时为 None，纯正则）

    def matches(self, raw: bytes) -> bool:
        # 作用：UTF-8 可解码 → 文本票面；否则（图片/扫描二进制）交给 L5
        try:
            raw.decode("utf-8")
            return True
        except UnicodeDecodeError:
            return False

    def parse(self, raw: bytes) -> dict:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise OcrFailedError(f"票面文件不是 UTF-8 文本: {exc}") from exc
        data = self.parse_text(text)
        data["source"] = "text"
        return data

    def parse_text(self, text: str) -> dict:
        """对已解码文本抽字段（OFD/PDF/OCR 复用同一抽取逻辑）"""
        try:
            return self._parse_regex(text)
        except OcrFailedError:
            if self._llm is None:
                raise
            data = self._llm.extract_invoice_text(text)
            if data is None:  # LLM 不可用 → 保留正则的具体报错（提示缺哪个字段）
                raise
            # LLM 兜底同样补齐扩展字段默认值，保证下游 add_invoice/契约一致
            data.setdefault("line_items", [])
            data.setdefault("has_signature", False)
            for k in _EXT_FIELD_ALIASES:
                data.setdefault(k, None)
            data["invoice_type"] = _classify_invoice_type(data.get("invoice_type", ""), data.get("title", ""))
            return data

    def _parse_regex(self, text: str) -> dict:
        data: dict = {}
        rail = _looks_like_railway_text(text)
        # 核心字段：缺 → 报错（报销闭环底线，结构化退回）
        # 例外（#86 铁路实名票据）：无「发票类型/项目」行属真实形态，缺省为 None 稍后补 火车票/起止站
        for key, labels in _CORE_FIELD_ALIASES.items():
            value = None
            for label in labels:
                value = _read_label(text, label)
                if value is not None:
                    break
            if value is None:
                if rail and key in ("invoice_type", "title"):
                    data[key] = None
                    continue
                raise OcrFailedError(f"票面缺少字段: {labels[0]}")
            if key == "amount":
                data[key] = _parse_amount(value)
            elif key == "date":
                data[key] = _normalize_date(value)
            else:
                data[key] = value

        # 作用：单文件多张票检测（出现多个不同票号 → 结构化退回，避免只认第一张）
        nums = re.findall(rf"^\s*{re.escape(_CORE_FIELD_ALIASES['invoice_no'][0])}\s*[:：]\s*(\S+)\s*$", text, re.MULTILINE)
        if len(set(nums)) > 1:
            raise OcrFailedError("单文件包含多张票（多个发票号码），请拆分后分别上传")

        # 扩展字段：尽力抽取，缺 → null（不阻塞主流程）
        for key, labels in _EXT_FIELD_ALIASES.items():
            value = None
            for label in labels:
                value = _read_label(text, label)
                if value is not None:
                    break
            if key == "tax_amount" and value is not None:
                try:
                    value = _parse_amount(value)
                except OcrFailedError:
                    value = None  # 税额格式异常 → 宁可留空，不报错
            data[key] = value
        # 文本/OCR 的表格明细行结构上不可靠 → V1 留空（全量明细只在 XML/OFD 承诺）
        data["line_items"] = []
        data["has_signature"] = False

        # 铁路实名票据缺省（#86）：无「发票类型/项目」行 → 按乘车要素补 invoice_type=火车票 / title=起止站
        if rail:
            if not data.get("invoice_type"):
                data["invoice_type"] = "火车票"
            if not data.get("title"):
                dep = data.get("departure_station")
                arr = data.get("arrival_station")
                data["title"] = f"{dep or '?'}-{arr or '?'}" if (dep or arr) else "火车票"
        data["invoice_type"] = _classify_invoice_type(data["invoice_type"], data["title"])
        return data


# ================= L5：图片 / 扫描件 OCR =================

def _box_min_xy(box: list) -> tuple[float, float]:
    """OCR 文本框 → (min_y, min_x) 左上角：兼容点对 [[x,y],...] 与 PaddleX 3.x 扁平 [x0,y0,x1,y1]"""
    if box and isinstance(box[0], (list, tuple)):
        return min(p[1] for p in box), min(p[0] for p in box)
    return float(box[1]), float(box[0])


class OcrImageParser:
    """L5：图片/扫描件 OCR——OCR provider → 版面文本 → 复用 L4 正则（核心必抽，扩展尽力）"""

    def __init__(self, ocr_provider=None):
        self._ocr = ocr_provider

    def matches(self, raw: bytes) -> bool:
        return True  # 兜底：非 XML/OFD/PDF/UTF-8 文本 → 视为图片

    def parse(self, raw: bytes) -> dict:
        if self._ocr is None:
            raise OcrFailedError("图片识别组件（PaddleOCR）未安装，无法识别图片/扫描件")
        fd, path = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        try:
            with open(path, "wb") as f:
                f.write(raw)
            text = self._ocr.recognize(path)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        if not text or not text.strip():
            raise OcrFailedError("图片未能识别出票面文本，请更换更清晰的图片")
        return {**TextInvoiceParser().parse_text(text), "source": "ocr"}


# ================= OCR Provider（PaddleOCR 本地识别，惰性加载） =================

class PaddleOcrProvider:
    """PaddleOCR 本地识别：图片/扫描 → 版面文本（按文本块坐标恢复阅读顺序）

    业务：惰性导入 paddleocr——组件未安装时明确报错（降级路径，核心格式 XML/文本不受影响）；
          L3 扫描 PDF 与 L5 图片共用同一 provider（docs/04 §3.1）
    """

    def __init__(self):
        self._engine = None

    def _load(self):
        if self._engine is not None:
            return
        try:
            # Paddle 3.x oneDNN 路径不支持 PIR 静态图 ArrayAttribute（onednn_instruction.cc:118）
            # → 关 MKLDNN（含 enable_mkldnn=False 兜底），否则图片推理直接崩溃
            os.environ.setdefault("FLAGS_use_mkldnn", "0")
            from paddleocr import PaddleOCR
        except ImportError as exc:
            raise OcrFailedError("图片识别组件（PaddleOCR）未安装，无法识别图片/扫描件") from exc
        try:
            # 3.x API：关闭文档方向/去畸变/行方向分类（数电票正置，省推理时间）
            self._engine = PaddleOCR(
                lang="ch",
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
                enable_mkldnn=False,
            )
        except TypeError:
            self._engine = PaddleOCR(lang="ch")  # 老版本 API 兜底

    def recognize(self, image_path: str) -> str:
        self._load()
        try:
            result = self._engine.predict(image_path)
        except Exception as exc:
            raise OcrFailedError(f"图片识别失败: {exc}") from exc
        rows: list[tuple[float, float, str]] = []  # (y, x, text)
        for res in result:
            data = getattr(res, "json", None)
            if data is None:
                try:
                    data = dict(res)
                except Exception:
                    continue
            # PaddleX 3.x predict 结果 json 是 {"res": {...}} 包装（rec_texts 在里层）
            # → 解包后再取票面字段；老版本直接平铺，判断不命中则原样
            inner = data.get("res") if isinstance(data, dict) else None
            if isinstance(inner, dict) and "rec_texts" in inner:
                data = inner
            texts = data.get("rec_texts") or []
            boxes = data.get("rec_boxes") or []
            for text, box in zip(texts, boxes):
                if not box:
                    rows.append((0.0, 0.0, text))
                    continue
                rows.append((*_box_min_xy(box), text))
        rows.sort(key=lambda r: (r[0], r[1]))  # 左上 → 右下 阅读顺序
        return "\n".join(r[2] for r in rows)


# ================= 统一入口（形态分派） =================

class OcrTool:
    """发票票面识别：按票据形态分派到各解析层（docs/04 §3 统一入口，契约不变）"""

    def __init__(self, parsers=None, llm_tool=None, image_ocr=None):
        # 作用：分派表（顺序 = 精确优先，后置兜底）；新票种在此注册
        self._parsers = parsers or [
            XmlInvoiceParser(),                    # L1 数电票 XML
            PdfInvoiceParser(image_ocr=image_ocr), # L3 PDF
            OfdInvoiceParser(),                    # L2 OFD
            TextInvoiceParser(llm_tool=llm_tool),  # L4 文本票面
            OcrImageParser(ocr_provider=image_ocr),# L5 图片/扫描 OCR（兜底）
        ]

    def extract(self, file_path: str) -> dict:
        """识别票面 → 结构化数据；失败抛 OcrFailedError（由识别节点转为结构化退回）。

        降级边界（A2，docs/04 §3 全链统一哲学——除 Xml 外各层内部都有自降级，唯独顶层分派以前没有）：
        形态命中后解析失败，只有一种情况允许继续尝试后续形态——
        Xml 的 matches 是宽嗅探（'<' 开头即命中），对「无发票结构化标记」的内容属形态误判
        （HTML 收据/随机 XML/带标签头的文本票），此时放行给文本/OCR 兜底，让 L4 正则/LLM 救回；
        而「含发票标记」的 XML（_looks_like_invoice_xml）确属发票但本层不识别/残缺 →
        保留具体报错（缺哪个字段一目了然，供人工修或扩展别名），**不降级**——否则真发票的
        报错会被 OCR 噪音淹没。PDF/OFD/文本/OCR 的 matches 是强嗅探，失败即终局报错。
        """
        try:
            raw = Path(file_path).read_bytes()
        except Exception as exc:
            raise OcrFailedError(f"文件读取失败: {exc}") from exc
        last_error: OcrFailedError | None = None
        for parser in self._parsers:
            if not parser.matches(raw):
                continue
            try:
                return parser.parse(raw)
            except OcrFailedError as exc:
                last_error = exc
                if isinstance(parser, XmlInvoiceParser) and not _looks_like_invoice_xml(raw):
                    continue  # 宽嗅探误命中（非发票）→ 下一形态兜底；真发票走 raise
                raise
        raise last_error if last_error is not None else OcrFailedError("无法识别的票据文件格式")
