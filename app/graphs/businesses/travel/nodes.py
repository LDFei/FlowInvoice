# app/graphs/businesses/travel/nodes.py —— 差旅子图节点
# 业务：识别→验真→事前申请→合规→审批链；任一环节失败 → 结构化退回（return_reason + returned）
from app.core.logging import get_logger, log_error, log_info, log_warning, set_log_context
from app.shared.policies.errors import AdvanceMissingError, OcrFailedError

logger = get_logger("travel.nodes")


def _returned(state: dict, category: str, message: str, suggestion: str) -> dict:
    """统一写退回原因并置状态（业务异常闭环的出口）"""
    # 作用：失败分类即失败原因（ocr_failed/verify_failed/duplicate_invoice/...），
    #       结构化落日志——后期业务审计（docs/06）直接消费此分类
    log_warning(logger, f"报销退回：{category}", category=category, message=message, business_type=state.get("business_type", ""))
    # 业务：所有业务问题统一走此结构回传给上层，供报销人修改重提（docs/AGENTS.md §8）
    return {
        "return_reason": {"category": category, "message": message, "suggestion": suggestion},
        "process_status": "returned",
    }


def build_travel_nodes(container):
    """构造差旅子图全部节点（闭包注入依赖，保持节点签名 (state)）

    业务：节点不直接 import 工具，而是通过 container 注入 —— 便于测试替换 Mock
    """
    ocr = container.ocr
    verify_tool = container.verify_tool
    advance_tool = container.advance_tool
    policies = container.policies
    users = container.users
    threshold = container.amount_threshold
    policy_rag = container.policy_rag
    llm_tool = container.llm_tool

    def recognize(state: dict) -> dict:
        """识别节点：OCR 抽票面；申报金额与票面差异打风险标记"""
        # 业务：识别失败 → 退回并说明"为什么"+重提建议（票面不清晰等）
        try:
            invoice_data = ocr.extract(state["invoice_input"]["file_path"])
        except OcrFailedError as exc:
            log_error(logger, "发票识别失败", category="ocr_failed", error=str(exc))
            return _returned(state, "ocr_failed", str(exc), "请确认票面清晰后重新上传")
        # 作用：把票号注入关联上下文，后续节点/通知日志自动带 invoice_no 可串查
        set_log_context(invoice_no=invoice_data.get("invoice_no", ""))
        log_info(
            logger, "发票识别完成",
            invoice_no=invoice_data.get("invoice_no", ""),
            invoice_type=invoice_data.get("invoice_type", ""),
            amount=invoice_data.get("amount", ""),
        )
        # 作用：比对申报金额与票面金额，超阈值打风险标记
        declared = float(state["invoice_input"].get("declared_amount") or 0)
        amount = invoice_data["amount"]
        diff = abs(declared - amount) / amount if amount else 0
        invoice_data["risk_flags"] = ["申报金额与票面金额不一致"] if diff > threshold else []
        return {"invoice_data": invoice_data}

    def verify(state: dict) -> dict:
        """验真节点：真伪查验 + 发票池查重"""
        # 作用：上游已退回则短路，不再执行
        if state.get("process_status") == "returned":
            return {}
        result = verify_tool.check(state["invoice_data"])
        if not result.get("verified"):
            return _returned(state, "verify_failed", result.get("note", "验真失败"), "请核对发票真伪后重新提交")
        # 业务：查重走发票池真库（docs/06 §3.1）——OCR 成功后注册入池，active 部分唯一索引兜底；
        #       冲突 → 该票号已在途/已报销 → 结构化退回（真实查重，不再依赖 Mock 子串标记）
        invoice_no = state["invoice_data"]["invoice_no"]
        registered = container.storage.add_invoice({
            **state["invoice_data"],
            "request_id": state["request_id"],
            # 业务：file_key 指向对象存储持久副本（docs/06 §5），本地临时路径仅处理用，不作权威引用
            "file_key": state["invoice_input"].get("object_key") or state["invoice_input"].get("file_path", ""),
        })
        if not registered:
            existing = container.storage.find_invoice(invoice_no)
            log_warning(
                logger, "发票查重命中", category="duplicate_invoice", invoice_no=invoice_no,
                owner_request_id=existing.get("request_id", "") if existing else "",
            )
            return _returned(
                state,
                "duplicate_invoice",
                "该发票已报销或正在报销流程中，请勿重复提交",
                "请核对票号；若确属本人待报销单据请走人工复核",
            )
        log_info(logger, "发票验真通过并已入发票池", verified=result.get("verified"), invoice_no=invoice_no)
        return {"verification": result}

    def match_advance(state: dict) -> dict:
        """事前申请匹配节点：差旅强制前置申请"""
        if state.get("process_status") == "returned":
            return {}
        try:
            app = advance_tool.match(
                employee_id=state["invoice_input"]["employee_id"],
                direction=state["business_type"],
                on_date=state["invoice_data"]["date"],
                app_id=state["invoice_input"].get("app_id", ""),
            )
        except AdvanceMissingError as exc:
            return _returned(state, "advance_missing", str(exc), "请先在出差前提交事前申请单")
        return {"advance_application": app}

    def check_compliance(state: dict) -> dict:
        """合规节点：发票日期在申请区间内；酒店住宿单价不超标准"""
        if state.get("process_status") == "returned":
            return {}
        policy = policies.load(state["business_type"])
        invoice = state["invoice_data"]
        app = state.get("advance_application") or {}
        checks = []
        # 业务：发票日期须落在事前申请区间内（防止用非出差期间的票报销）
        passed = bool(app) and app["start_date"] <= invoice["date"] <= app["end_date"]
        checks.append({"item": "发票日期在出差申请区间内", "passed": passed, "detail": invoice["date"]})
        # 业务：酒店发票单价不超住宿标准上限（超额 → 标记，提示需特殊审批）
        if invoice.get("invoice_type") == "酒店发票":
            over = invoice["amount"] > policy.get("hotel_daily_limit", 0)
            checks.append({
                "item": "住宿单价不超标准",
                "passed": not over,
                "detail": f"¥{invoice['amount']:.2f} / 上限 ¥{policy.get('hotel_daily_limit', 0):.2f}",
            })
        # 作用：RAG 检索制度条款作为合规判断的"依据"（非结构化制度文本，docs/03 §3）
        # 业务：确定性检查之外，把相关条款（如交通等级/票据规范）检索出来，
        #       供总结引用与人工复核追溯，提升可解释性
        policy_basis = policy_rag.retrieve_basis(state)
        # 作用：LLM 只做解释层——把确定性检查结果 + 条款讲成人话给报销人（不改判定，docs/03 §3）
        # 业务：无 key/失败时 explanation 为 None，跳过解释，确定性结论不受影响
        explanation = llm_tool.explain_compliance(checks, policy_basis, invoice) if llm_tool else None
        return {
            "compliance_checks": checks,
            "policy_basis": policy_basis,
            "compliance_explanation": explanation,
        }

    def build_approval_chain(state: dict) -> dict:
        """审批链节点：金额阈值 × 业务类型 → 审批角色链"""
        if state.get("process_status") == "returned":
            return {}
        policy = policies.load(state["business_type"])
        amount = state["invoice_data"]["amount"]
        # 作用：取第一条覆盖该金额的审批规则（金额越大审批链越长）
        rule = next(
            (
                r for r in policy.get("approval_rules", [])
                if amount < r.get("max_amount", float("inf")) and amount >= r.get("min_amount", 0)
            ),
            {"chain": ["直属上级"]},  # 兜底：默认单级（财务不参与审批，见审批≠支付）
        )
        chain = [{"role": role, **users.get_approver(role)} for role in rule["chain"]]
        return {"approval_chain": chain}

    return {
        "recognize": recognize,
        "verify": verify,
        "match_advance": match_advance,
        "check_compliance": check_compliance,
        "build_approval_chain": build_approval_chain,
    }
