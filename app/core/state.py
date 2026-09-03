# app/core/state.py —— 图全局状态（LangGraph 各节点共享）
# 业务：状态只放流程需要的数据；每个节点单向写自己的字段，不改他人字段（docs/AGENTS.md §7）
from typing import Optional, TypedDict


class ReimbursementState(TypedDict, total=False):
    """报销全流程状态"""

    # —— 输入（api 层注入，来自申报人）——
    request_id: str                 # 作用：报销单号；同时作为 LangGraph thread_id（中断恢复依据）
    invoice_input: dict             # 作用：标准输入 DTO（见 core/uploader）

    # —— 分类结果（路由依据）——
    business_type: str  # 分类结果(费用类型组,路由依据;当前 travel=差旅;非用户选择的"业务方向")              # 作用：业务方向；业务：travel/procurement...

    # —— 各节点产出（按节点写入）——
    invoice_data: dict              # OCR 结构化票面（识别节点写）
    verification: dict              # 验真结果（验真节点写）
    advance_application: Optional[dict]  # 命中的事前申请（匹配节点写）
    compliance_checks: list         # 合规检查项（合规节点写）
    policy_basis: list              # 制度条款依据（RAG 检索，合规节点写）
    approval_chain: list            # 计划审批链（审批链节点写）
    summary: str                    # Agent 审核总结（总结节点写）

    # —— 业务闭环（退回 / 作废）——
    return_reason: dict             # 退回/作废原因 {category, message, suggestion}
    process_status: str             # 状态机流转：in_review / returned / approved / paid / voided（paid 由出纳打款节点写入，见 service.pay）
    current_step: str               # 当前挂起点 review / leader_decision / done
    decision: dict                  # HITL 决策（resume 传入）{action, comment, actor}
    approval_records: list          # 各级决策执行记录（驾驶舱统计的数据基础）
