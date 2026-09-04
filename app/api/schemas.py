# app/api/schemas.py —— DTO（接口出入参，Pydantic v2）
# 业务：接口边界参数校验；只做结构约束，不写业务逻辑
from typing import Optional

from pydantic import BaseModel, Field


class AdvanceCreate(BaseModel):
    """创建事前申请单"""
    employee_id: str = Field(default="1001", description="员工工号")
    direction: str = Field(default="travel", description="业务方向")
    start_date: str = Field(..., description="出差开始日期 YYYY-MM-DD")
    end_date: str = Field(..., description="出差结束日期 YYYY-MM-DD")
    estimated_amount: float = Field(..., gt=0, description="预估金额")
    purpose: str = Field(default="", description="出差事由")


class DecideRequest(BaseModel):
    """审批决策（人工复核 / 领导决策共用）"""
    action: str = Field(..., pattern="^(approve|return|void)$", description="approve/return/void")
    comment: str = Field(default="", description="意见/原因")
    actor: str = Field(default="", description="决策人（真实系统从登录态取）")


class PayRequest(BaseModel):
    """出纳打款（审批通过后由财务出纳确认打款）"""
    comment: str = Field(default="", description="打款备注（如转账流水号）")
    actor: str = Field(default="", description="打款人（出纳工号，真实系统从登录态取）")


class RequestSummary(BaseModel):
    """报销单摘要（列表项）"""
    request_id: str = Field(..., description="报销单号")
    status: str = Field(..., description="流程状态：in_review=审批中 / returned=退回 / approved=已批准 / paid=已打款 / voided=作废")
    current_step: str = Field(..., description="当前步骤：review=待审核复核 / leader_decision=待领导决策 / done=结束")
    business_type: str = Field("", description="业务方向（travel=差旅）")
    amount: Optional[float] = Field(None, description="票面金额")
    employee_id: str = Field("", description="员工工号")
    created_at: str = Field(..., description="创建时间")
    updated_at: str = Field(..., description="更新时间")


class RequestDetail(BaseModel):
    """报销单详情（提交 / 审批决策 / 详情查询的通用返回结构）"""
    request_id: str = Field(..., description="报销单号（也是 LangGraph 图执行的 thread_id）")
    status: str = Field(..., description="流程状态：in_review=审批中 / returned=退回 / approved=已批准 / paid=已打款 / voided=作废")
    current_step: str = Field("", description="当前挂起点：review=待审核人复核 / leader_decision=待领导决策 / done=结束")
    business_type: str = Field("", description="业务方向（travel=差旅）")
    parent_request_id: Optional[str] = Field(None, description="退回重提留痕：本次报销由哪个原单退回/作废后重提（无则不填）")
    summary: str = Field("", description="Agent 生成的审核总结（含『政策依据』条款引用，供复核追溯）")
    invoice_data: Optional[dict] = Field(None, description="票面信息：发票号码 / 类型 / 开票日期 / 金额 / 项目 / 风险标记（多票批=首张被接受票镜像，总览看 tickets）")
    tickets: Optional[list] = Field(None, description="#A 多票批：被接受并入审的票列表，每张含 invoice_input/invoice_data/verification/compliance_checks")
    rejected: Optional[list] = Field(None, description="#A 多票批：被拒票列表（每张含 category/message/suggestion 原因，不进审批，供报销人重传）")
    total_amount: Optional[float] = Field(None, description="#A 多票批：Σ 被接受票面金额（审批链档位 / 预算占用 / 通知金额的请求级口径；单票=票面金额）")
    verification: Optional[dict] = Field(None, description="验真结果：verified=真伪 / duplicate=是否重复报销 / note=说明")
    advance_application: Optional[dict] = Field(None, description="匹配到的事前申请单（含有效期；匹配失败则为 None）")
    compliance_checks: Optional[list] = Field(None, description="合规检查逐项结果（item / passed / detail）")
    policy_basis: Optional[list] = Field(None, description="制度条款依据（RAG 检索命中：clause_id / source / text / score）")
    approval_chain: Optional[list] = Field(None, description="审批角色链（金额越大链越长；role / 姓名 / 部门）")
    return_reason: Optional[dict] = Field(None, description="退回原因（category / message / suggestion 重提建议）")
    approval_records: Optional[list] = Field(None, description="已产生的审批记录（角色 / 决策 / 意见）")
    decision: Optional[dict] = Field(None, description="最近一次决策内容：{action: approve/return/void, comment: 意见, actor: 决策人工号}")
    payment: Optional[dict] = Field(None, description="打款记录：{actor: 出纳工号, comment: 备注, time}（出纳确认打款后产生）")
    paused: bool = Field(False, description="是否处于人工挂起（等待审核复核 / 领导决策）")
    messages: list = Field(default_factory=list, description="站内消息留痕（通知对象 / 内容）")
    emails: list = Field(default_factory=list, description="邮件留痕（发给领导的审批邮件）")


class SubmissionStatus(BaseModel):
    """异步任务状态（#52：异步模式提交后立即返回，worker 处理后更新；前端轮询用，docs/06 §2）"""
    request_id: str = Field(..., description="报销单号（任务主键）")
    status: str = Field("pending", description="任务状态：pending=排队中 / processing=处理中 / succeeded=已完成 / failed=失败")
    attempts: int = Field(0, description="已重试次数（失败自动退避重试）")
    error: Optional[dict] = Field(None, description="失败原因 {type, message}（重试耗尽后出现）")
    created_at: str = Field("", description="创建时间")
    updated_at: str = Field("", description="更新时间")


class AdvanceDetail(BaseModel):
    """事前申请单详情（创建 / 列表返回结构）"""
    app_id: str = Field(..., description="事前申请单号")
    employee_id: str = Field("", description="员工工号")
    direction: str = Field("", description="业务方向（travel=差旅）")
    start_date: str = Field("", description="出差开始日期 YYYY-MM-DD")
    end_date: str = Field("", description="出差结束日期 YYYY-MM-DD")
    valid_until: str = Field("", description="有效期截止日期（过期后不可再匹配报销）")
    estimated_amount: float = Field(0, description="预估金额")
    purpose: str = Field("", description="出差事由")
    status: str = Field("", description="active=有效 / expired=已过期（按 valid_until 实时派生，不物理写 expired，#104）")
    created_at: str = Field("", description="创建时间")
    reserved_amount: Optional[float] = Field(0, description="已占用合计（approved 报销单按票面累计，列表接口附）")
    remaining_amount: Optional[float] = Field(0, description="剩余额度 = 预估 - 已占用（可为负=超支）")
