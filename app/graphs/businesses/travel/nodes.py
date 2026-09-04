# app/graphs/businesses/travel/nodes.py —— 差旅子图节点
# 业务：#A 批处理（识别→硬闸门→验真入池，每票并发，见 app/graphs/batch.py）→ 事前申请（整批覆盖）→
#       合规软闸门（逐票 + Σ预算）→ 审批链（Σ金额分档）；任一请求级失败 → 结构化退回（return_reason + returned）
from datetime import date

from app.core.logging import get_logger, log_warning
from app.core.money import INF, to_money
from app.graphs.batch import run_process_batch
from app.shared.policies.errors import AdvanceAmbiguousError, AdvanceMissingError

logger = get_logger("travel.nodes")


def _returned(state: dict, category: str, message: str, suggestion: str) -> dict:
    """统一写退回原因并置状态（业务异常闭环的出口）"""
    # 作用：失败分类即失败原因（advance_missing/advance_ambiguous/...），
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


def _request_tickets(state: dict) -> list[dict]:
    """规整为"被接受票列表"：优先 #A state.tickets；单票老路径（无 tickets 键，测试直调/前向兼容）
    用 invoice_data 镜像造单元素伪票——保证 check_compliance 直调单测与整批路径共用同一实现"""
    tickets = state.get("tickets")
    if tickets:
        return list(tickets)
    invoice_data = state.get("invoice_data")
    if not invoice_data:
        return []
    return [{
        "invoice_input": state.get("invoice_input", {}),
        "invoice_data": invoice_data,
        "verification": state.get("verification", {}),
        "compliance_checks": [],
    }]


def build_travel_nodes(container):
    """构造差旅子图全部节点（闭包注入依赖，保持节点签名 (state)

    业务：节点不直接 import 工具，而是通过 container 注入 —— 便于测试替换 Mock
    """
    advance_tool = container.advance_tool
    policies = container.policies
    users = container.users
    policy_rag = container.policy_rag
    llm_tool = container.llm_tool

    def process_batch(state: dict) -> dict:
        """#A 批处理节点：整批并行 识别/硬闸门/验真入池 → accepted[] + rejected[]（见 batch.py）"""
        return run_process_batch(container, state)

    def match_advance(state: dict) -> dict:
        """事前申请匹配节点（请求级，针对整批被接受票）：默认关联事前申请；「直接报销」模式跳过"""
        if state.get("process_status") == "returned":
            return {}
        # 业务：直接报销模式（提交页报销方式选「直接报销」）——不匹配事前申请、不进预算池，
        #       凭票直接走合规→审批链（未做事前申请也能报销，日常主路径之一；docs/02）
        if state.get("invoice_input", {}).get("mode", "advance") == "direct":
            return {"advance_application": None}
        tickets = _request_tickets(state)
        if not tickets:
            return {}
        # 业务：一批票=一趟差旅 → 整批只挂一份申请，且该申请区间须覆盖全部被接受票开票日期；
        #       auto=唯一覆盖全批 / 显式 app_id 须覆盖全批，否则结构化退回（保 #97，不静默取最早）
        dates = sorted({t["invoice_data"]["date"] for t in tickets if t.get("invoice_data", {}).get("date")})
        emp = state.get("invoice_input", {}).get("employee_id", "")
        try:
            app = advance_tool.match_dates(
                employee_id=emp,
                direction=state["business_type"],
                dates=dates,
                app_id=state.get("invoice_input", {}).get("app_id", ""),
            )
        except AdvanceMissingError as exc:
            return _returned(state, "advance_missing", str(exc), "本次开票日期未匹配到有效事前申请：若确未做事前申请，请改选「直接报销」重新提交")
        except AdvanceAmbiguousError as exc:
            return _returned(state, "advance_ambiguous", str(exc), "请在提交页「关联出差申请」中选择本次票据对应的申请后重新提交")
        return {"advance_application": app}

    def check_compliance(state: dict) -> dict:
        """合规节点：逐票软闸门（日期在区间/酒店单晚/席别职级）+ 请求级 Σ 预算占用软闸门"""
        if state.get("process_status") == "returned":
            return {}
        policy = policies.load(state["business_type"])
        app = state.get("advance_application") or {}
        aggregate: list[dict] = []
        updated: list[dict] = []
        for ticket in _request_tickets(state):
            invoice = ticket.get("invoice_data") or {}
            t_input = ticket.get("invoice_input") or {}
            checks: list[dict] = []
            # 业务：发票日期须落在事前申请区间内（防止用非出差期间的票报销）——仅关联了事前申请的
            #       报销检查此条（match_advance 已保证覆盖，冗余把关）；「直接报销」无申请可对照，不产生该项
            if app:
                passed = app["start_date"] <= invoice["date"] <= app["end_date"]
                checks.append({"item": "发票日期在出差申请区间内", "passed": passed, "detail": invoice["date"]})
            # 业务：酒店发票单日住宿费不超标准上限（B1：接明细推算单晚口径，而非把多晚总额误判成单晚超标）
            if invoice.get("invoice_type") == "酒店发票":
                limit = to_money(policy.get("hotel_daily_limit", 0))  # #44 Decimal 比较
                if limit > 0:
                    daily, basis = _hotel_daily_basis(invoice)
                    over = to_money(daily) > limit
                    checks.append({
                        "item": "住宿单价不超标准",
                        "passed": not over,
                        "detail": f"单日最高 ¥{to_money(daily):.2f} / 上限 ¥{limit:.2f}（{basis}）",
                    })
            # 业务：高铁席别不超本人职级标准（#85/#88，条款 3.1 数字化——软闸门：越权乘席标记，
            #       不直接退回，由复核人特批并填意见留痕，见 decide 批注要求）
            if invoice.get("invoice_type") == "火车票" and invoice.get("seat_class"):
                seat = str(invoice["seat_class"]).strip()
                classes = policy.get("rail_seat_classes") or []
                grade_table = policy.get("grade_max_rail_seat") or {}
                if seat in classes and grade_table:
                    grade = None
                    emp_id = t_input.get("employee_id", "")
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
            ticket = dict(ticket)
            ticket["compliance_checks"] = checks
            updated.append(ticket)
            aggregate.extend(checks)

        # —— 请求级 Σ 预算软闸门（#64/#91 预算池累计口径）：本批被接受票合计 + 本申请已占用 > 预估才标超支；
        #    软闸门：只标记不阻断，由复核人特批并填意见（decide 批注要求）。单票文案与 #A 前逐字节一致。
        #    #44：Σ/占用/预估/超支判定全部 Decimal——预算记账比对不受浮点尾数干扰（"合计+已占 vs 预估"分毫必较）
        total = sum(to_money(t["invoice_data"].get("amount")) for t in updated)
        estimated = to_money((app or {}).get("estimated_amount"))
        reserved = to_money((app or {}).get("reserved_amount"))
        if estimated > 0 and total > 0:
            over = reserved + total > estimated
            if len(updated) == 1:
                detail = f"本票 ¥{to_money(updated[0]['invoice_data'].get('amount')):.2f} / 已占用 ¥{reserved:.2f} / 预估 ¥{estimated:.2f}"
            else:
                detail = f"合计 ¥{total:.2f} / 已占用 ¥{reserved:.2f} / 预估 ¥{estimated:.2f}"
            aggregate.append({
                "item": "报销金额未超事前申请预估金额",
                "passed": not over,
                "detail": detail,
            })

        # 作用：RAG 检索制度条款作为合规判断的"依据"（非结构化制度文本，docs/03 §3）
        policy_basis = policy_rag.retrieve_basis(state)
        # 作用：LLM 只做解释层——把确定性检查结果 + 条款讲成人话给报销人（不改判定，docs/03 §3）
        first_inv = updated[0]["invoice_data"] if updated else {}
        explanation = llm_tool.explain_compliance(aggregate, policy_basis, first_inv) if llm_tool else None
        return {
            "tickets": updated,
            "compliance_checks": aggregate,
            "policy_basis": policy_basis,
            "compliance_explanation": explanation,
        }

    def build_approval_chain(state: dict) -> dict:
        """审批链节点：#A 金额阈值按整批 Σ 被接受票面分档（单票 Σ=票面，行为与 #A 前一致）"""
        if state.get("process_status") == "returned":
            return {}
        policy = policies.load(state["business_type"])
        # 作用：多票批取 Σ；单票/老状态无 total_amount 时退化为票面金额或 Σ tickets（直调兼容）
        # #44：分档金额与阈值 min/max_amount 全部 Decimal 比较——2000 元分界线判定精确到分，无浮点越界/踩线
        raw = state.get("total_amount")
        if raw is None:
            raw = (state.get("invoice_data") or {}).get("amount")
        amount = to_money(raw)
        if amount <= 0:
            amount = sum(
                to_money(t["invoice_data"].get("amount"))
                for t in _request_tickets(state) if t.get("invoice_data", {}).get("amount")
            )
        # 作用：取第一条覆盖该金额的审批规则（金额越大审批链越长）
        rule = next(
            (
                r for r in policy.get("approval_rules", [])
                if amount < to_money(r.get("max_amount", INF))
                and amount >= to_money(r.get("min_amount", 0))
            ),
            {"chain": ["直属上级"]},  # 兜底：默认单级（财务不参与审批，见审批≠支付）
        )
        chain = [{"role": role, **users.get_approver(role)} for role in rule["chain"]]
        return {"approval_chain": chain}

    return {
        "process_batch": process_batch,
        "match_advance": match_advance,
        "check_compliance": check_compliance,
        "build_approval_chain": build_approval_chain,
    }
