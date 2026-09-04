# app/graphs/batch.py —— #A 多票据批处理引擎（单张票 = 批大小 1，统一走本引擎，消除单票专用分支）
# 业务：一报销单（request_id）= 1..10 张票。单张票的重活单元为
#       OCR 识别 → 确定性硬闸门（票种/抬头/时限/非法金额/未来日期）→ 验真 → 发票池原子入池（active 唯一索引）。
#       #106 F-1：硬闸门整体前移到验真(入池)之前——坏票/错票/超期票从不占用发票池，拒绝原因即重传依据；
#       假票/重复/超期属"重传也解决不了"的硬性拦截，发票池天然挡下。
#       各票经全局共享线程池并行（OCR 等 I/O 重活），as_completed 后按下标归位，顺序确定不随线程完成乱序。
# 并发前提：storage 每次操作独立连接 + 全局锁（见 SqliteStorage 注释）——DB 写天然串行化，无竞态；
#       批内只是把"识别/验真/读票面"这些纯 I/O 摊到线程，入池仍原子。
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

from app.core.logging import get_logger, log_error, log_info, log_warning
from app.core.money import to_money
from app.shared.policies.errors import OcrFailedError

logger = get_logger("graphs.batch")

# 作用：批并行度上限（策略 = 小批全开、大批封顶，避免单请求打满线程池饿死其他在途任务）
MAX_BATCH_WORKERS = 10
# 部分接受 / 整批退回时，给报销人的通知里最多枚举的被拒票数（明细一律在单据 rejected 列表）
MAX_REJECTED_HINT = 5


def _file_name(t_input: dict) -> str:
    """被拒票展示名：优先上传原始文件名（A3 起 API 层透传），否则退化取落盘路径基名"""
    name = t_input.get("file_name")
    if name:
        return name
    return Path(t_input.get("file_path", "")).name


def _reject(t_input: dict, invoice_data: dict | None, category: str, message: str, suggestion: str) -> dict:
    """单张被拒票的结构化原因（不进审批；category=失败分类，与结构化日志/审计一致）"""
    inv = invoice_data or {}
    return {
        "invoice_input": t_input,
        "invoice_no": inv.get("invoice_no", ""),
        "invoice_type": inv.get("invoice_type", ""),
        "amount": inv.get("amount"),
        "file_name": _file_name(t_input),
        "category": category,
        "message": message,
        "suggestion": suggestion,
    }


def _reason_of(reject: dict) -> dict:
    """把被拒票原因还原成请求级 return_reason（单票退回 = 该票原因，语义与 #A 前一致）"""
    return {k: reject.get(k) for k in ("category", "message", "suggestion")}


def _source_bytes(container, t_input: dict) -> bytes:
    """取票据源字节（#93 对象存储取用接线）：优先本地临时 file_path，缺失/失效 → 对象存储权威副本

    业务：本地临时文件是处理缓存（docs/06 Phase 2），跨进程/重启/超 7 天清理后 file_path 会失效；
      而对象存储以同一 key 存了源文件权威副本（uploader.save_and_parse：本地落盘 + 对象存储 put 同 key），
      识别链路必须能按 object_key 取字节继续，而不是"本地文件没了 → 整票 OCR 失败"。
      失败抛 OcrFailedError（与 ocr.extract 原读取失败同语义，由调用方转结构化退回）。
    """
    fp = t_input.get("file_path") or ""
    if fp:
        try:
            return Path(fp).read_bytes()
        except OSError:
            pass  # 本地副本被清理/跨主机不存在 → 落对象存储
    key = t_input.get("object_key") or ""
    if key:
        try:
            return container.object_storage.get(key)
        except Exception as exc:  # 对象不存在/MinIO 不可达等
            raise OcrFailedError(f"对象存储取件失败: {exc}") from exc
    raise OcrFailedError("票据源文件缺失（无本地 file_path 且无 object_key）")


def process_ticket(container, request_id: str, business_type: str, t_input: dict) -> dict:
    """处理单张票：识别 → 硬闸门 → 验真入池 → 产出 accepted ticket 或 rejected 原因

    返回 {"status": "accepted", "ticket": {...}} 或 {"status": "rejected", "reject": {...}}。
    非业务异常（DB/OCR 系统故障）向上抛，由 run_submit_pipeline 兜底释放已入池占用后重抛。
    """
    ocr = container.ocr
    verify_tool = container.verify_tool
    storage = container.storage
    policy = container.policies.load(business_type)
    threshold = container.amount_threshold

    # ---------- ① OCR 识别：票面抽结构化字段 ----------
    # 业务：#93 源读取降级——file_path（本地缓存）可用则直读；否则按 object_key 从对象存储取权威副本
    try:
        invoice_data = ocr.extract_bytes(_source_bytes(container, t_input))
    except OcrFailedError as exc:
        log_error(logger, "发票识别失败", category="ocr_failed", error=str(exc), request_id=request_id)
        return {"status": "rejected", "reject": _reject(t_input, None, "ocr_failed", str(exc), "请确认票面清晰后重新上传")}
    invoice_no = invoice_data.get("invoice_no", "")
    log_info(logger, "发票识别完成", invoice_no=invoice_no,
             invoice_type=invoice_data.get("invoice_type", ""), amount=invoice_data.get("amount", ""))

    # ---------- ② 金额/日期非法校验（可重传修复的输入错，前移防空票/负额/倒签刷单） ----------
    # #44：金额判定一律走 Decimal（to_money）——票面/申报/差异占比对比不再受浮点尾数干扰
    amount = to_money(invoice_data["amount"])
    if amount <= 0:
        return {"status": "rejected", "reject": _reject(
            t_input, invoice_data, "invalid_amount", f"票面金额非法: {amount}", "请核对票面金额后重新上传")}
    declared = to_money(t_input.get("declared_amount"))
    if declared < 0:
        return {"status": "rejected", "reject": _reject(
            t_input, invoice_data, "invalid_amount", f"申报金额不可为负: {declared}", "请填写正确的申报金额")}
    if invoice_data["date"] > date.today().isoformat():
        return {"status": "rejected", "reject": _reject(
            t_input, invoice_data, "invalid_date", f"开票日期晚于今天: {invoice_data['date']}", "请核对开票日期")}
    # 申报金额与票面金额差异 → 风险标记（复核人注意核对）
    # #A 口径：declared_amount==0 = 按票面申报（多票批 UI 不逐票收集申报额，默认按票面）
    #       → 与票面无差异，不做风险标记；显式申报额（>0）与票面差超阈值才标记。
    diff = (abs(declared - amount) / amount) if declared else to_money(0)
    invoice_data["risk_flags"] = ["申报金额与票面金额不一致"] if diff > to_money(threshold) else []

    # ---------- ③ 发票合规确定性硬闸门（#87；#106 前移到入池之前：坏票/错票从不占用池） ----------
    itype = (invoice_data.get("invoice_type") or "").strip()
    allowed = policy.get("invoice_types") or []
    if itype and allowed and itype not in allowed:
        return {"status": "rejected", "reject": _reject(
            t_input, invoice_data, "unsupported_invoice_type",
            f"票种「{itype}」不属于本业务（{business_type}）可报销范围",
            f"请核对业务方向；可报销票种：{'、'.join(allowed)}")}
    company = (policy.get("company_name") or "").strip()
    buyer = (invoice_data.get("buyer_name") or "").strip()
    if buyer and company and company not in buyer:
        return {"status": "rejected", "reject": _reject(
            t_input, invoice_data, "buyer_mismatch",
            f"发票购方抬头「{buyer}」与本报销主体（{company}）不一致，无法入账",
            "请核对票面抬头为公司全称的发票后重新上传")}
    deadline_days = float(policy.get("invoice_reimburse_deadline_days", 0) or 0)
    if deadline_days > 0:
        age = (date.today() - date.fromisoformat(invoice_data["date"])).days
        if age > deadline_days:
            return {"status": "rejected", "reject": _reject(
                t_input, invoice_data, "invoice_expired",
                f"票面日期（{invoice_data['date']}）距今 {age} 天，超过报销时限 {int(deadline_days)} 天",
                "请核对票面日期；超期票据原则上不可报销，特殊情形请走人工特批")}

    # ---------- ④ 验真（真伪）+ 发票池原子入池（查重；active 唯一索引兜底并发） ----------
    result = verify_tool.check(invoice_data)
    if not result.get("verified"):
        return {"status": "rejected", "reject": _reject(
            t_input, invoice_data, "verify_failed", result.get("note", "验真失败"), "请核对发票真伪后重新提交")}
    registered = storage.add_invoice({
        **invoice_data,
        "request_id": request_id,
        # 业务：file_key 指向对象存储持久副本（docs/06 §5），本地临时路径仅处理用，不作权威引用
        "file_key": t_input.get("object_key") or t_input.get("file_path", ""),
    })
    if not registered:
        existing = storage.find_invoice(invoice_no)
        log_warning(logger, "发票查重命中", category="duplicate_invoice", invoice_no=invoice_no,
                    owner_request_id=existing.get("request_id", "") if existing else "")
        return {"status": "rejected", "reject": _reject(
            t_input, invoice_data, "duplicate_invoice",
            "该发票已报销或正在报销流程中，请勿重复提交",
            "请核对票号；若确属本人待报销单据请走人工复核")}
    log_info(logger, "发票验真通过并已入发票池", verified=result.get("verified"), invoice_no=invoice_no)
    return {
        "status": "accepted",
        "ticket": {
            "invoice_input": t_input,
            "invoice_data": invoice_data,
            "verification": result,
            "compliance_checks": [],  # 软闸门在请求级 check_compliance 节点逐票补写
        },
    }


def run_process_batch(container, state: dict) -> dict:
    """#A 批处理节点体：并行跑整批 → 汇聚 accepted[] / rejected[] → 组装请求级状态

    产出（LangGraph 节点返回的增量键）：
    - accepted 非空：tickets[]、total_amount=Σ、invoice_data/verification=首票镜像（兼容单票视图）、
      process_status=in_review 继续后续请求级阶段；
    - accepted 空（整批被拒）：process_status=returned + return_reason（单票=该票原因逐字节兼容老行为；
      多票=汇总原因，逐票明细在 rejected 列表），拒绝原因即报销人重传依据。
    - 部分接受：被拒票不进审批、不入池，发一条站内消息告知报销人（明细/原因可在单据详情查看）。
    """
    inputs = state.get("invoice_inputs") or ([state["invoice_input"]] if state.get("invoice_input") else [])
    if not inputs:
        raise ValueError("批处理输入为空：state 缺少 invoice_input/invoice_inputs")
    business_type = state.get("business_type", "")
    request_id = state["request_id"]
    batch_size = len(inputs)

    # 作用：并行度 = min(票数, 上限)。futures 按下标保留顺序（归位不依赖完成顺序，输出确定性）
    workers = min(MAX_BATCH_WORKERS, max(1, batch_size))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(process_ticket, container, request_id, business_type, t) for t in inputs]
        results = [f.result() for f in futures]  # 异常在此上抛 → run_submit_pipeline 兜底释放已入池占用

    accepted = [r["ticket"] for r in results if r["status"] == "accepted"]
    rejected = [r["reject"] for r in results if r["status"] == "rejected"]
    updates: dict = {"rejected": rejected}

    if not accepted:
        # —— 整批被拒：请求级退回。单票直接复用它自己的原因（老行为逐字节不变）；
        #    多票给汇总原因（明细在 rejected 列表，报销人按票重传）
        reason = _reason_of(rejected[0]) if (batch_size == 1 and rejected) else {
            "category": "batch_rejected",
            "message": f"本批 {batch_size} 张票据均未通过（{_hint(rejected)}）",
            "suggestion": "请按每张票据的被拒原因修改后重新上传；单张票据对应一张报销单",
        }
        updates.update({"process_status": "returned", "return_reason": reason})
        if batch_size > 1:
            _notify_submitter(container, state, reason)
        log_warning(logger, "整批被拒（多票汇总原因）" if batch_size > 1 else "报销退回",
                    category=reason["category"], request_id=request_id)
        return updates

    # #44：Σ 在 Decimal 内完成（金额运算精确），float() 仅作 state/DB 边界的 JSON 原生数承载
    total_amount = float(sum(to_money(t["invoice_data"].get("amount")) for t in accepted))
    updates.update({
        "tickets": accepted,
        "total_amount": total_amount,
        # 兼容镜像（单票视图/直调老路径）：invoice_data/verification = 首张被接受票；invoice_input = 首票 meta
        "invoice_data": accepted[0]["invoice_data"],
        "verification": accepted[0]["verification"],
        "invoice_input": accepted[0]["invoice_input"],
        "process_status": "in_review",
    })
    if rejected:
        # —— 部分接受：好票进审批，坏票被拒不入池；给报销人一条站内消息列明被拒票，明细见详情
        note = {
            "category": "partial_rejected",
            "message": f"本批 {len(accepted)} 张进入审批，{len(rejected)} 张被拒：{_hint(rejected)}",
            "suggestion": "被拒票据请核对原因后另建报销单重新上传（单张票据对应一张报销单）",
        }
        _notify_submitter(container, state, note)
        log_warning(logger, "批处理部分接受", accepted=len(accepted), rejected=len(rejected), request_id=request_id)
    log_info(logger, "批处理完成", accepted=len(accepted), rejected=len(rejected),
             total_amount=total_amount, request_id=request_id)
    return updates


def _hint(rejected: list[dict]) -> str:
    """被拒票提示串（枚举到 MAX_REJECTED_HINT 条，避免通知内容过长）"""
    parts = []
    for r in rejected[:MAX_REJECTED_HINT]:
        label = r.get("file_name") or r.get("invoice_no") or "票据"
        parts.append(f"{label}({r.get('category', '')})")
    return "；".join(parts) + ("…" if len(rejected) > MAX_REJECTED_HINT else "")


def _notify_submitter(container, state: dict, reason: dict) -> None:
    """部分接受 / 整批多票被拒：站内消息告知报销人（复用退回通知的 消息+建议 文案结构）"""
    try:
        container.notify_tool.notify_submitter(state["request_id"], {**state, "return_reason": reason})
    except Exception as exc:  # 通知失败不影响主流程（退回/审批结论以 state 为准）
        log_warning(logger, "报销人被拒通知发送失败", error=str(exc), request_id=state.get("request_id", ""))
