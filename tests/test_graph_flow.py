# tests/test_graph_flow.py —— 总控图端到端流程（正常/退回/作废/异常分支）
# 业务：覆盖 docs/01 §4.1 的退回/作废双闭环与业务异常（识别失败/缺申请）
#       审批链按金额分档：小额(≤2000) 单级"直属上级"免总经理审批；大额(>2000) 需总经理终审
from datetime import date, timedelta

import pytest

from app.api.service import decide, pay, start_reimbursement
from tests.conftest import make_ticket

INVOICE_INPUT = {
    "file_path": "", "direction": "travel", "purpose": "客户拜访",
    "declared_amount": 528.50, "payment_method": "personal",
    "employee_id": "1001", "app_id": "",
}


def _submit(container, req_id, ticket_text, **overrides):
    from tempfile import NamedTemporaryFile
    with NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
        f.write(ticket_text)
        path = f.name
    inp = {**INVOICE_INPUT, "file_path": path, **overrides}
    return start_reimbursement(container, inp, req_id)


def _paused(state) -> bool:
    return "__interrupt__" in state


def test_small_amount_single_step_loop(container):
    """小额(≤2000)：提交 → 直属上级批准 → 直接 approved（免总经理审批，仅告知）+ 通知财务"""
    today = date.today().isoformat()
    state = _submit(container, "T-HAPPY", make_ticket(on_date=today))

    # 第一步：挂起在审核人员复核
    assert state["process_status"] == "in_review"
    assert state["current_step"] == "review"
    assert _paused(state)
    assert "发票类型：火车票" in state["summary"]          # Agent 总结已生成
    assert state["advance_application"]["app_id"]         # 事前申请自动匹配
    assert state["approval_chain"][0]["role"] == "直属上级"
    assert len(state["approval_chain"]) == 1              # 单级链（小额免总经理审批）
    # 业务：RAG 已检索到制度条款依据，并写进总结供复核追溯
    assert state["policy_basis"], "合规节点应产出制度条款依据"
    assert "政策依据" in state["summary"]

    # 第二步：直属上级批准 → 直接终态 approved（不再挂起领导决策）
    s2 = decide(container, "T-HAPPY", "approve", "复核通过", "2001")
    assert s2["process_status"] == "approved"
    assert s2["current_step"] == "done"
    assert not _paused(s2)
    assert len(container.storage.list_emails("T-HAPPY")) == 0   # 小额无领导审批邮件
    msgs = [m["to_role"] for m in container.storage.list_messages("T-HAPPY")]
    assert "财务" in msgs                          # 审批≠支付：最终通知出纳付款
    assert "总经理" in msgs                        # 小额免审批但总经理收到告知
    # 审计记录：单级审批（直属上级）
    assert [r["decision"] for r in s2["approval_records"]] == ["approve"]


def test_large_amount_full_loop(container):
    """大额(>2000)：提交 → 直属上级批准 → 总经理终审 → approved + 通知财务"""
    state = _submit(container, "T-BIG", make_ticket(invoice_type="机票", amount=3500.0),
                    declared_amount=3500.0)
    assert [n["role"] for n in state["approval_chain"]] == ["直属上级", "总经理"]

    # 第二步：直属上级批准 → 挂起到总经理终审（已发邮件）
    s2 = decide(container, "T-BIG", "approve", "复核通过", "2001")
    assert s2["current_step"] == "leader_decision"
    assert _paused(s2)
    assert len(container.storage.list_emails("T-BIG")) == 1

    # 第三步：总经理批准 → approved，且通知财务出纳
    s3 = decide(container, "T-BIG", "approve", "同意", "4001")
    assert s3["process_status"] == "approved"
    assert not _paused(s3)
    msgs = [m["to_role"] for m in container.storage.list_messages("T-BIG")]
    assert "财务" in msgs
    assert "总经理" not in msgs                    # 已参与审批，无需再发告知
    # 审计记录含 直属上级 + 总经理 两级决策
    assert [r["decision"] for r in s3["approval_records"]] == ["approve", "approve"]


def test_pay_transitions_to_paid(container):
    """出纳打款：approved → pay(3001) → paid + 打款记录 + 通知报销人到账"""
    _submit(container, "T-PAY", make_ticket())
    decide(container, "T-PAY", "approve", "复核通过", "2001")
    s3 = pay(container, "T-PAY", "转账流水 8888", "3001")
    assert s3["process_status"] == "paid"
    assert s3["current_step"] == "done"
    assert s3["payment"]["actor"] == "3001"
    assert s3["payment"]["comment"] == "转账流水 8888"
    # 打款记录进审计（出纳 = 财务域动作）
    assert s3["approval_records"][-1]["decision"] == "pay"
    assert s3["approval_records"][-1]["role"] == "财务出纳"
    msgs = [m["to_role"] for m in container.storage.list_messages("T-PAY")]
    assert any("报销人" in m for m in msgs)        # 通知报销人到账


def test_pay_requires_approved_status(container):
    """打款前提：仅 approved 可打款，审批中(in_review)打款 → ValueError"""
    _submit(container, "T-PAY-NA", make_ticket())
    with pytest.raises(ValueError, match="已批准"):
        pay(container, "T-PAY-NA", "备注", "3001")


def test_pay_requires_cashier(container):
    """打款权限：仅出纳(3001)可打款，其他角色 → PermissionError"""
    _submit(container, "T-PAY-AUTH", make_ticket())
    decide(container, "T-PAY-AUTH", "approve", "过", "2001")
    with pytest.raises(PermissionError, match="出纳"):
        pay(container, "T-PAY-AUTH", "备注", "1001")


def test_reviewer_return_loop(container):
    """退回闭环：审核人员退回 → returned + 通知报销人（可重提）"""
    state = _submit(container, "T-RET", make_ticket())
    s2 = decide(container, "T-RET", "return", "票据要素不全", "2001")
    assert s2["process_status"] == "returned"
    assert s2["return_reason"]["category"] == "reviewer_returned"
    assert s2["current_step"] == "done"
    msgs = [m["to_role"] for m in container.storage.list_messages("T-RET")]
    assert any("报销人" in m for m in msgs)


def test_leader_void_loop(container):
    """作废闭环：大额单总经理否决 → voided + 通知全部审批链角色"""
    _submit(container, "T-VOID", make_ticket(invoice_type="机票", amount=3500.0),
            declared_amount=3500.0)
    decide(container, "T-VOID", "approve", "过", "2001")
    s3 = decide(container, "T-VOID", "void", "违反差旅政策", "4001")
    assert s3["process_status"] == "voided"
    assert s3["return_reason"]["category"] == "leader_voided"
    # 通知全部审批链角色（直属上级 + 总经理）
    msgs = [m["to_role"] for m in container.storage.list_messages("T-VOID")]
    assert "直属上级" in msgs and "总经理" in msgs


def test_request_id_continues_after_restart(tmp_path):
    """重启装配容器：序号从库中已有单号续接，不复用旧单号（否则留痕混入历史数据）"""
    from tempfile import NamedTemporaryFile

    from app.api.service import start_reimbursement
    from app.container import build_container
    from app.core import ids
    from app.main import seed_demo_data

    db = tmp_path / "restart.db"
    c1 = build_container(db_path=db)
    seed_demo_data(c1)                    # ADV-001，占序号 1

    def _api_submit(c):
        # 作用：模拟路由行为——先生成单号（new_request_id），再启动图并落库
        with NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
            f.write(make_ticket())
            path = f.name
        inp = {**INVOICE_INPUT, "file_path": path}
        rid = ids.new_request_id()
        start_reimbursement(c, inp, rid)
        return rid

    first = _api_submit(c1)               # REIM-002（已落库）
    second = _api_submit(c1)              # REIM-003（已落库）

    c2 = build_container(db_path=db)      # 模拟重启：同一库文件重新装配，续接序号
    seed_demo_data(c2)
    restarted = _api_submit(c2)           # 应续接，而非回到 REIM-002/003
    assert restarted != first
    assert restarted != second


def test_ocr_fail_returns_structured_reason(container):
    """业务异常：识别失败 → 结构化原因 + returned（不进入人工复核）"""
    state = _submit(container, "T-OCR", "这是一张模糊的图片，无法识别")
    assert state["process_status"] == "returned"
    assert state["return_reason"]["category"] == "ocr_failed"
    assert not _paused(state)  # 未挂起人工复核


def test_missing_advance_returns_structured_reason(container):
    """业务异常：差旅缺有效事前申请（发票日期超申请区间）→ 结构化退回"""
    far_date = (date.today() + timedelta(days=60)).isoformat()
    state = _submit(container, "T-ADV", make_ticket(on_date=far_date))
    assert state["process_status"] == "returned"
    assert state["return_reason"]["category"] == "advance_missing"


def test_amount_threshold_builds_longer_chain(container):
    """金额 ≥2000 → 两级审批链（直属上级 → 总经理）；小额 → 单级"""
    big = _submit(container, "T-AMT", make_ticket(invoice_type="机票", amount=3500.0),
                  declared_amount=3500.0)
    assert [n["role"] for n in big["approval_chain"]] == ["直属上级", "总经理"]
    small = _submit(container, "T-AMT2", make_ticket())
    assert [n["role"] for n in small["approval_chain"]] == ["直属上级"]


def test_review_step_requires_direct_manager(container):
    """权限：审核复核阶段只有直属上级(2001)能操作，总经理(4001)越权被拒"""
    _submit(container, "T-AUTH", make_ticket())
    with pytest.raises(PermissionError, match="直属上级"):
        decide(container, "T-AUTH", "approve", "越权操作", "4001")
    # 正确审批人可继续（小额 → 直接批准终态）
    state = decide(container, "T-AUTH", "approve", "复核通过", "2001")
    assert state["process_status"] == "approved"


def test_leader_step_requires_leader(container):
    """权限：领导决策阶段只有总经理(4001)能操作，直属上级(2001)越权被拒"""
    _submit(container, "T-AUTH2", make_ticket(invoice_type="机票", amount=3500.0),
            declared_amount=3500.0)
    decide(container, "T-AUTH2", "approve", "复核通过", "2001")
    with pytest.raises(PermissionError, match="总经理"):
        decide(container, "T-AUTH2", "approve", "越权终批", "2001")
    # 总经理正确终审 → approved
    state = decide(container, "T-AUTH2", "approve", "同意", "4001")
    assert state["process_status"] == "approved"
