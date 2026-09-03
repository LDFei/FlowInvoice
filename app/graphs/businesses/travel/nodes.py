# app/graphs/businesses/travel/nodes.py —— 差旅子图节点
# 业务：识别→验真→事前申请→合规→审批链；任一环节失败 → 结构化退回（return_reason + returned）
from datetime import date

from app.core.logging import get_logger, log_error, log_info, log_warning, set_log_context
from app.shared.policies.errors import AdvanceAmbiguousError, AdvanceMissingError, OcrFailedError

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


def _hotel_daily_basis(invoice: dict) -> tuple[float, str]:
    """酒店发票 → 单日住宿费比较口径（B1：明细优先，别把多晚总额误判成单晚超标）

    口径推导（保守方向——宁可多标、不可放过超标单晚）：
    - 明细行 quantity>1 且 unit_price×quantity≈amount（发票自身证明单价是单日/单价，晚数拆分常见）
      → 用 unit_price 作为每晚口径；
    - 其余行（quantity=1 / 无单价 / 单价与金额对不上）→ 行金额即口径（不明夜数不臆测拆分，宁标不放过）；
    - 无明细（文本/OCR 票面不承诺 line_items，V1 留空）→ 总额近似（维持原口径，文案注明"近似"）。
    返回 (比较值, 口径说明)。
    """
    amount = float(invoice.get("amount", 0) or 0)
    lines = invoice.get("line_items") or []
    if not lines:
        return amount, "票面无明细，按总额近似"
    values: list[float] = []
    for line in lines:
        line_amt = line.get("amount")
        if not isinstance(line_amt, (int, float)) or line_amt <= 0:
            continue
        qty = line.get("quantity")
        price = line.get("unit_price")
        # 发票自证单价的拆分行：qty×price≈金额 → 单价即每晚；否则行金额兜底（保守）
        if (isinstance(qty, (int, float)) and qty > 1 and isinstance(price, (int, float))
                and price > 0 and abs(price * qty - line_amt) <= max(abs(line_amt) * 0.01, 0.01)):
            values.append(float(price))
        else:
            values.append(float(line_amt))
    if not values:
        return amount, "明细无有效金额，按总额近似"
    return max(values), f"明细 {len(values)} 行推算"


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
        # 业务：票面金额必须为正（0/负数 → 无报销意义，防空票/负额刷单放行进审批链）
        amount = invoice_data["amount"]
        if amount <= 0:
            return _returned(state, "invalid_amount", f"票面金额非法: {amount}", "请核对票面金额后重新上传")
        # 业务：申报金额不可为负（负数申报无意义，且会让差异比对失真）
        declared = float(state["invoice_input"].get("declared_amount") or 0)
        if declared < 0:
            return _returned(state, "invalid_amount", f"申报金额不可为负: {declared}", "请填写正确的申报金额")
        # 业务：开票日期不可晚于今天（未来票面异常：填错日期/倒签，不应进入审批）
        if invoice_data["date"] > date.today().isoformat():
            return _returned(state, "invalid_date", f"开票日期晚于今天: {invoice_data['date']}", "请核对开票日期")
        # 作用：比对申报金额与票面金额，超阈值打风险标记
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
        """事前申请匹配节点：默认关联事前申请（差旅行程票挂到已批出差申请）；「直接报销」模式跳过"""
        if state.get("process_status") == "returned":
            return {}
        # 业务：直接报销模式（提交页报销方式选「直接报销」）——不匹配事前申请、不进预算池，
        #       凭票直接走合规→审批链（未做事前申请也能报销，日常主路径之一；docs/02）
        if state["invoice_input"].get("mode", "advance") == "direct":
            return {"advance_application": None}
        try:
            app = advance_tool.match(
                employee_id=state["invoice_input"]["employee_id"],
                direction=state["business_type"],
                on_date=state["invoice_data"]["date"],
                app_id=state["invoice_input"].get("app_id", ""),
            )
        except AdvanceMissingError as exc:
            return _returned(state, "advance_missing", str(exc), "本次开票日期未匹配到有效事前申请：若确未做事前申请，请改选「直接报销」重新提交")
        except AdvanceAmbiguousError as exc:
            return _returned(state, "advance_ambiguous", str(exc), "请在提交页「关联出差申请」中选择本次票据对应的申请后重新提交")
        return {"advance_application": app}

    def check_invoice_compliance(state: dict) -> dict:
        """发票合规确定性闸门（#87：发票"本身是否合规" → 硬闸门直接退回，不进人工复核）

        两闸门分工（用户定案）：verify 节点负责政府条例级 真伪/查重（发票池唯一索引兜底）；
        本节点承接其余发票级确定性闸门——票种白名单 / 购方抬头 / 报销时限，
        全部 YAML 配置化（policy/travel.yaml），制度替换不改代码。
        任一失败 → _returned 结构化退回（票号已在 verify 入池，返回后由 run_submit_pipeline 终态释放）。
        """
        if state.get("process_status") == "returned":
            return {}
        policy = policies.load(state["business_type"])
        invoice = state["invoice_data"]
        # 1) 票种白名单：业务只收其登记票种（餐饮/办公等错票种在 travel 下 → 拒收，不产生"差旅口径"结果）
        allowed = policy.get("invoice_types") or []
        itype = (invoice.get("invoice_type") or "").strip()
        if itype and allowed and itype not in allowed:
            return _returned(
                state, "unsupported_invoice_type",
                f"票种「{itype}」不属于本业务（{state['business_type']}）可报销范围",
                f"请核对业务方向；可报销票种：{'、'.join(allowed)}",
            )
        # 2) 购方抬头：票面带购方且非本公司 → 拒收（入账/抵扣主体不一致）
        #    （实名票据火车/机票/打车及纸质票无购方可核 → 无法证明有误，不误伤放行）
        company = (policy.get("company_name") or "").strip()
        buyer = (invoice.get("buyer_name") or "").strip()
        if buyer and company and company not in buyer:
            return _returned(
                state, "buyer_mismatch",
                f"发票购方抬头「{buyer}」与本报销主体（{company}）不一致，无法入账",
                "请核对票面抬头为公司全称的发票后重新上传",
            )
        # 3) 报销时限：票面日期距今超上限 → 超期拒收（#63 数字化；未来日期已在 recognize 拦截）
        days = float(policy.get("invoice_reimburse_deadline_days", 0) or 0)
        if days > 0:
            ticket_date = date.fromisoformat(invoice["date"])
            age = (date.today() - ticket_date).days
            if age > days:
                return _returned(
                    state, "invoice_expired",
                    f"票面日期（{invoice['date']}）距今 {age} 天，超过报销时限 {int(days)} 天",
                    "请核对票面日期；超期票据原则上不可报销，特殊情形请走人工特批",
                )
        return {}

    def check_compliance(state: dict) -> dict:
        """合规节点：发票日期在申请区间内；酒店住宿单价不超标准"""
        if state.get("process_status") == "returned":
            return {}
        policy = policies.load(state["business_type"])
        invoice = state["invoice_data"]
        app = state.get("advance_application") or {}
        checks = []
        # 业务：发票日期须落在事前申请区间内（防止用非出差期间的票报销）——仅关联了事前申请的
        #       报销检查此条（match_advance 已保证日期在区间，冗余把关）；「直接报销」无申请可对照，不产生该项
        if app:
            passed = app["start_date"] <= invoice["date"] <= app["end_date"]
            checks.append({"item": "发票日期在出差申请区间内", "passed": passed, "detail": invoice["date"]})
        # 业务：酒店发票单日住宿费不超标准上限（B1：接明细推算单晚口径，而非把多晚总额误判成单晚超标）
        if invoice.get("invoice_type") == "酒店发票":
            limit = float(policy.get("hotel_daily_limit", 0) or 0)
            if limit > 0:
                daily, basis = _hotel_daily_basis(invoice)
                over = daily > limit
                checks.append({
                    "item": "住宿单价不超标准",
                    "passed": not over,
                    "detail": f"单日最高 ¥{daily:.2f} / 上限 ¥{limit:.2f}（{basis}）",
                })
        # 业务：报销金额不应超出事前申请预估金额——预算池累计口径（#91/F-A）：本票 + 本申请已占用
        #       的 approved 报销合计 > 预估才算超支（一次出差多张票共享同一申请，各自累计；
        #       reserved_amount 由 match 附在申请快照上，见 advance/service.py）。超支 → 标记，
        #       不阻断流程，最终由复核人员批注特批（软闸门）
        estimated = float((app or {}).get("estimated_amount") or 0)
        reserved = float((app or {}).get("reserved_amount") or 0)
        if estimated > 0 and invoice.get("amount"):
            amount = float(invoice["amount"])
            over = reserved + amount > estimated
            checks.append({
                "item": "报销金额未超事前申请预估金额",
                "passed": not over,
                "detail": f"本票 ¥{amount:.2f} / 已占用 ¥{reserved:.2f} / 预估 ¥{estimated:.2f}",
            })
        # 业务：高铁席别不超本人职级标准（#85/#88，条款 3.1 数字化——软闸门：越权乘席标记，
        #       不直接退回，由复核人特批并填意见留痕，见 decide 批注要求）
        if invoice.get("invoice_type") == "火车票" and invoice.get("seat_class"):
            seat = str(invoice["seat_class"]).strip()
            classes = policy.get("rail_seat_classes") or []
            grade_table = policy.get("grade_max_rail_seat") or {}
            if seat in classes and grade_table:
                grade = None
                emp_id = state.get("invoice_input", {}).get("employee_id", "")
                if emp_id:
                    try:
                        grade = users.get_employee(emp_id).get("grade")
                    except KeyError:
                        grade = None  # 未知员工（目录外）→ 不臆测职级，跳过本项
                max_seat = grade_table.get(grade) if grade else None
                if max_seat and max_seat in classes:
                    over = classes.index(seat) > classes.index(max_seat)
                    checks.append({
                        "item": "高铁席别不超本人职级标准",
                        "passed": not over,
                        "detail": f"{seat} / 职级「{grade}」可报至 {max_seat}",
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
        "check_invoice_compliance": check_invoice_compliance,
        "match_advance": match_advance,
        "check_compliance": check_compliance,
        "build_approval_chain": build_approval_chain,
    }
