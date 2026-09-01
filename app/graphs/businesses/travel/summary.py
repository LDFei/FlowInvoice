# app/graphs/businesses/travel/summary.py —— 审核总结生成
# 业务：Agent 产出结构化总结给审核人员复核；真实场景可换 LLM，此处模板化保证 Demo 可离线运行
def build_travel_summary(state: dict) -> str:
    """生成差旅报销审核总结"""
    invoice = state["invoice_data"]
    lines = [
        f"发票类型：{invoice.get('invoice_type', '未知')}",
        f"票面金额：¥{invoice['amount']:,.2f}",
        f"验真：{state['verification']['note']}",
        f"事前申请：{state.get('advance_application', {}).get('app_id', '无')}",
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
