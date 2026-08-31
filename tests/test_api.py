# tests/test_api.py —— FastAPI 路由层测试（提交/决策/打款/状态/事前申请）
from tests.conftest import make_ticket


def _submit(client, ticket_text=None, **form):
    ticket_text = ticket_text or make_ticket()
    form = {
        "direction": "travel",
        "purpose": "客户拜访",
        "declared_amount": "528.50",
        "payment_method": "personal",
        "employee_id": "1001",
        **form,
    }
    resp = client.post(
        "/api/reimburse",
        files={"file": ("ticket.txt", ticket_text.encode("utf-8"), "text/plain")},
        data=form,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_submit_returns_review_pending(client):
    """报销端上传 → 返回首个挂起点（审核人员复核）"""
    body = _submit(client)
    assert body["status"] == "in_review"
    assert body["current_step"] == "review"
    assert body["paused"] is True
    assert body["summary"]
    assert body["approval_chain"][0]["role"] == "直属上级"


def test_large_amount_two_step_loop_via_api(client):
    """走 API 走通 大额(>2000) 提交→直属上级→总经理 全链路"""
    rid = _submit(client, make_ticket(invoice_type="机票", amount=3500.0),
                  declared_amount="3500.0")["request_id"]

    r2 = client.post(f"/api/requests/{rid}/decide", json={"action": "approve", "comment": "复核通过", "actor": "2001"})
    assert r2.status_code == 200
    assert r2.json()["current_step"] == "leader_decision"
    assert r2.json()["paused"] is True

    r3 = client.post(f"/api/requests/{rid}/decide", json={"action": "approve", "comment": "同意", "actor": "4001"})
    assert r3.status_code == 200
    assert r3.json()["status"] == "approved"

    detail = client.get(f"/api/requests/{rid}").json()
    assert detail["status"] == "approved"
    assert len(detail["emails"]) == 1            # 领导收到邮件
    assert any(m["to_role"] == "财务" for m in detail["messages"])  # 出纳收款通知


def test_small_amount_single_step_via_api(client):
    """走 API 走通 小额(≤2000) 提交→直属上级 批准即终态（免总经理审批，仅告知）"""
    rid = _submit(client)["request_id"]

    r2 = client.post(f"/api/requests/{rid}/decide", json={"action": "approve", "comment": "复核通过", "actor": "2001"})
    assert r2.status_code == 200
    body = r2.json()
    assert body["status"] == "approved"
    assert body["paused"] is False
    assert len(body["emails"]) == 0              # 小额无领导审批邮件
    assert any(m["to_role"] == "总经理" for m in body["messages"])   # 总经理仅收到告知
    assert any(m["to_role"] == "财务" for m in body["messages"])     # 出纳收款通知


def test_pay_via_api(client):
    """出纳端：approved → 确认打款 → paid + 打款记录"""
    rid = _submit(client)["request_id"]
    client.post(f"/api/requests/{rid}/decide", json={"action": "approve", "comment": "复核通过", "actor": "2001"})

    r = client.post(f"/api/requests/{rid}/pay", json={"comment": "转账流水 9999", "actor": "3001"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "paid"
    assert body["payment"]["actor"] == "3001"
    assert body["payment"]["comment"] == "转账流水 9999"
    assert any(m["to_role"] == "报销人 1001" for m in body["messages"])  # 通知报销人到账
    # 已打款列表可见
    lst = client.get("/api/requests", params={"status": "paid"}).json()
    assert any(x["request_id"] == rid for x in lst)


def test_pay_wrong_actor_forbidden(client):
    """打款权限：非出纳(非3001)打款 → 403"""
    rid = _submit(client)["request_id"]
    client.post(f"/api/requests/{rid}/decide", json={"action": "approve", "comment": "过", "actor": "2001"})
    r = client.post(f"/api/requests/{rid}/pay", json={"comment": "备注", "actor": "1001"})
    assert r.status_code == 403
    assert "出纳" in r.json()["detail"]


def test_pay_requires_approved(client):
    """打款前提：审批中(in_review)打款 → 400"""
    rid = _submit(client)["request_id"]
    r = client.post(f"/api/requests/{rid}/pay", json={"comment": "", "actor": "3001"})
    assert r.status_code == 400
    assert "已批准" in r.json()["detail"]


def test_invalid_action_at_step_rejected(client):
    """步骤校验：审核复核阶段不允许直接作废"""
    rid = _submit(client)["request_id"]
    r = client.post(f"/api/requests/{rid}/decide", json={"action": "void", "comment": "", "actor": "x"})
    assert r.status_code == 400
    assert "不允许操作" in r.json()["detail"]


def test_wrong_actor_forbidden(client):
    """权限：越权审批 → 403；正确审批人 → 200"""
    rid = _submit(client, make_ticket(invoice_type="机票", amount=3500.0),
                  declared_amount="3500.0")["request_id"]
    # 审核复核阶段：总经理 4001 越权 → 403
    r = client.post(f"/api/requests/{rid}/decide", json={"action": "approve", "comment": "", "actor": "4001"})
    assert r.status_code == 403
    assert "直属上级" in r.json()["detail"]
    # 直属上级 2001 正常审批 → 200
    r2 = client.post(f"/api/requests/{rid}/decide", json={"action": "approve", "comment": "复核通过", "actor": "2001"})
    assert r2.status_code == 200
    assert r2.json()["current_step"] == "leader_decision"
    # 领导决策阶段：直属上级 2001 越权 → 403
    r3 = client.post(f"/api/requests/{rid}/decide", json={"action": "approve", "comment": "", "actor": "2001"})
    assert r3.status_code == 403
    assert "总经理" in r3.json()["detail"]


def test_decide_unknown_request_404(client):
    r = client.post("/api/requests/NOPE/decide", json={"action": "approve", "comment": "", "actor": ""})
    assert r.status_code == 404


def test_create_advance_and_list(client):
    """事前申请接口：创建 + 自动算有效期 + 列表"""
    from datetime import date, timedelta
    body = {
        "employee_id": "1001", "direction": "travel",
        "start_date": (date.today() - timedelta(days=1)).isoformat(),
        "end_date": (date.today() + timedelta(days=2)).isoformat(),
        "estimated_amount": 1500.0, "purpose": "华南出差",
    }
    r = client.post("/api/advance", json=body)
    assert r.status_code == 200
    app = r.json()
    assert app["status"] == "active"
    assert app["valid_until"] >= app["end_date"]     # 有效期 = 结束日 + 30 天

    lst = client.get("/api/advances").json()
    assert any(a["app_id"] == app["app_id"] for a in lst)
