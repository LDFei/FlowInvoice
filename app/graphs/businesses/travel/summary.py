# app/graphs/businesses/travel/summary.py —— 审核总结生成
# 业务：Agent 产出结构化总结给审核人员复核；真实场景可换 LLM，此处模板化保证 Demo 可离线运行
# #A 单票（n==1）逐字节保留老文案；多票（n>1）逐票清单 + 请求级汇总（复核人逐张对照）
from app.core.money import to_money


def build_travel_summary(state: dict) -> str:
    """生成差旅报销审核总结（单票 / 多票批）"""
    tickets = state.get("tickets")
    if tickets and len(tickets) > 1:
        return _aggregate_summary(state, tickets)
    return _legacy_summary(state)


def _legacy_summary(state: dict) -> str:
    """单票审核总结（#A 前逐字节文案；invoice_data/verification = 批节点写下的首票镜像）"""
    invoice = state["invoice_data"]
    lines = [
        f"发票类型：{invoice.get('invoice_type', '未知')}",
        f"票面金额：¥{to_money(invoice.get('amount')):,.2f}",
        f"验真：{state['verification']['note']}",
        f"事前申请：{(state.get('advance_application') or {}).get('app_id', '无')}",
    ]
    # 作用：逐条列出合规检查结果
    for check in state.get("compliance_checks", []):
        lines.append(f"合规「{check['item']}」：{'通过' if check['passed'] else '不通过'}（{check['detail']}）")
    # 作用：引用 RAG 检索到的制度条款号 + 首条正文要点（可解释性：复核人有据可查）
    for hit in state.get("policy_basis", [])[:2]:
        first_point = next((ln for ln in hit["text"].splitlines()[1:] if ln.strip()), "")
        lines.append(f"政策依据 [{hit['clause_id']}]：{first_point}")
    if invoice.get("risk_flags"):
        lines.append(f"风险：{'；'.join(invoice['risk_flags'])}")
    if state.get("compliance_explanation"):
        lines.append(f"合规说明：{state['compliance_explanation']}")
    return "\n".join(lines)


def _aggregate_summary(state: dict, tickets: list[dict]) -> str:
    """多票批总结：逐票清单（类型/金额/票号/逐票合规）+ 请求级合计与预算/依据"""
    rejected = state.get("rejected") or []
    # #44：票面/申报合计在 Decimal 内求和，格式化 `:,.2f` 输出精确（浮点尾数不再进审核文案）
    total = to_money(state.get("total_amount"))
    declared_total = sum(
        to_money((t.get("invoice_input") or {}).get("declared_amount")) for t in tickets)
    lines = [
        f"本单共 {len(tickets)} 张票据入审，票面合计 ¥{total:,.2f}"
        + (f"，申报合计 ¥{declared_total:,.2f}" if declared_total > 0 else ""),
        f"事前申请：{(state.get('advance_application') or {}).get('app_id', '无')}",
    ]
    # 逐票清单：识别/金额/票号 + 该票逐项合规结果
    for idx, ticket in enumerate(tickets, 1):
        inv = ticket.get("invoice_data") or {}
        amount = to_money(inv.get("amount"))
        head = f"{idx}. {inv.get('invoice_type', '未知')} ¥{amount:,.2f}"
        if inv.get("invoice_no"):
            head += f"（{inv['invoice_no']}）"
        lines.append(head)
        for check in ticket.get("compliance_checks", []):
            lines.append(f"   合规「{check['item']}」：{'通过' if check['passed'] else '不通过'}（{check['detail']}）")
        if inv.get("risk_flags"):
            lines.append(f"   风险：{'；'.join(inv['risk_flags'])}")
    if rejected:
        hint = "；".join(
            f"{r.get('invoice_no') or r.get('file_name') or '票'}({r.get('category', '')})"
            for r in rejected[:5]
        ) + ("…" if len(rejected) > 5 else "")
        lines.append(f"被拒 {len(rejected)} 张（未入审）：{hint}")
    # 请求级合规项（不在任何单票内，如 Σ 预算占用）单列
    covered = {c["item"] for t in tickets for c in t.get("compliance_checks", [])}
    for check in state.get("compliance_checks", []):
        if check["item"] not in covered:
            lines.append(f"合规「{check['item']}」：{'通过' if check['passed'] else '不通过'}（{check['detail']}）")
    # 制度依据（复核人有据可查）
    for hit in state.get("policy_basis", [])[:2]:
        first_point = next((ln for ln in hit["text"].splitlines()[1:] if ln.strip()), "")
        lines.append(f"政策依据 [{hit['clause_id']}]：{first_point}")
    if state.get("compliance_explanation"):
        lines.append(f"合规说明：{state['compliance_explanation']}")
    return "\n".join(lines)
