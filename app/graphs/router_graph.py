# app/graphs/router_graph.py —— 总控图（LangGraph 主编排）
# 业务：分类路由 → 业务子图 → 总结 → 通知审核人 → 人工复核(HITL) → 邮件领导 → 领导决策 → 批准/作废
#       退回/作废双闭环，结构化原因回传（docs/01 §4.1）
from datetime import datetime

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from app.core.state import ReimbursementState


def build_router_graph(container):
    """构建总控图（闭包注入依赖；返回已编译图，含 MemorySaver 支持 HITL）"""
    businesses = container.businesses        # dict[direction -> BusinessModule]（注册表装配）
    notify = container.notify_tool
    email = container.email_tool

    # ================= 节点 =================

    def classify(state: dict) -> dict:
        """分类路由节点：确定业务方向"""
        # 业务：LLM 决策 + 规则兜底（面试点：何时用 LLM 分类、何时用规则）——真实场景由 LLM classify，此处按申报人指定方向 + 注册表校验
        direction = state["invoice_input"].get("direction", "")
        if direction in businesses:
            return {"business_type": direction}
        return {
            "business_type": "",
            "return_reason": {
                "category": "unknown_business",
                "message": f"无法识别业务类型（direction={direction}）",
                "suggestion": "请选择正确的报销业务类型",
            },
            "process_status": "returned",
        }

    def route_to_business(state: dict) -> dict:
        """调用业务子图（同步 invoke，结果合并回父状态）"""
        # 作用：取业务模块并执行其子图
        module = businesses[state["business_type"]]
        return module.build_graph().invoke(state)

    def summarize(state: dict) -> dict:
        """总结节点：生成 Agent 审核总结（供审核人员复核）"""
        module = businesses[state["business_type"]]
        return {"summary": module.build_summary(state)}

    def notify_reviewer(state: dict) -> dict:
        """通知审核人员：发送 Agent 总结，进入人工复核"""
        notify.notify_reviewer(state["request_id"], state)
        return {"current_step": "review"}

    def wait_reviewer(state: dict) -> dict:
        """人工复核挂起：interrupt 等待审核人员决策（approve/return）"""
        # 作用：HITL 人工断点——interrupt 挂起等人工，Command(resume=...) 恢复后由条件边路由
        decision = interrupt({
            "step": "review",
            "question": "请复核 Agent 总结并给出决策",
            "summary": state["summary"],
            "options": ["approve", "return"],
        })
        return {"decision": decision}

    def email_leader(state: dict) -> dict:
        """邮件触达审批领导（审核人员批准后）"""
        # 业务：末级审批人=最终决策人（本闭环内）；真实场景按审批链逐级触发
        leader = state["approval_chain"][-1]
        email.email_leader(state["request_id"], state, leader)
        return {
            "current_step": "leader_decision",
            # 作用：把"审核人员通过"也记入审计记录（驾驶舱统计各级决策的基础）
            "approval_records": _record(state, state["approval_chain"][0]["role"], "approve"),
        }

    def wait_leader(state: dict) -> dict:
        """领导最终决策挂起：interrupt 等待（approve/void）"""
        decision = interrupt({
            "step": "leader_decision",
            "question": "领导最终决策：批准或作废",
            "summary": state["summary"],
            "options": ["approve", "void"],
        })
        return {"decision": decision}

    def _record(state: dict, role: str, action: str) -> list:
        """追加一条决策执行记录（审计留痕 / 可观测性，驾驶舱统计的数据基础）"""
        decision = state.get("decision", {})
        records = list(state.get("approval_records", []))
        records.append({
            "role": role,
            "decision": action,
            "actor": decision.get("actor", "system"),
            "comment": decision.get("comment", ""),
            "time": datetime.now().isoformat(timespec="seconds"),
        })
        return records

    def approve_node(state: dict) -> dict:
        """批准节点：置 approved，通知财务出纳付款；总经理未参与审批的单据仅告知（小额免总经理审批）"""
        # 业务：审批≠支付——批准是审批链终点，打款仅出纳执行，Agent 不接触资金
        notify.notify_finance(state["request_id"], state)
        chain = state.get("approval_chain") or []
        # 业务：按制度，小额单总经理不在审批链 → 批准后仅发一条告知，不占审批资源
        if "总经理" not in [n.get("role") for n in chain]:
            notify.notify_gm(state["request_id"], state)
        # 作用：审计记录标记真实终审人（单级链=直属上级；两级链=总经理）
        final_role = chain[-1]["role"] if chain else "最终审批"
        return {
            "process_status": "approved",
            "current_step": "done",
            "approval_records": _record(state, final_role, "approve"),
        }

    def return_node(state: dict) -> dict:
        """退回节点：置 returned，通知报销人（可修改重提）"""
        decision = state.get("decision", {})
        reason = {
            "category": "reviewer_returned",
            "message": decision.get("comment", "审核人员退回"),
            "suggestion": "请按审核意见修改后重新提交",
        }
        # 作用：先把原因并入 state 再通知（通知内容依赖原因）
        notify.notify_submitter(state["request_id"], {**state, "return_reason": reason})
        return {
            "process_status": "returned",
            "current_step": "done",
            "return_reason": reason,
            "approval_records": _record(state, "审核人员", "return"),
        }

    def void_node(state: dict) -> dict:
        """作废节点：置 voided，通知全部审批链角色（流程终止，全员知晓原因）"""
        decision = state.get("decision", {})
        reason = {
            "category": "leader_voided",
            "message": decision.get("comment", "领导最终否决"),
            "suggestion": "流程已作废，如需报销请重新发起",
        }
        notify.notify_all_chain(state["request_id"], {**state, "return_reason": reason})
        return {
            "process_status": "voided",
            "current_step": "done",
            "return_reason": reason,
            "approval_records": _record(state, "领导", "void"),
        }

    # ================= 路由判定函数 =================

    def route_after_classify(state: dict) -> str:
        # 作用：未知业务 → 直接结束；否则进业务子图
        return "route_to_business" if state.get("business_type") else "END"

    def route_after_business(state: dict) -> str:
        # 作用：子图内退回（识别失败等）→ 结束；否则进总结
        return "END" if state.get("process_status") == "returned" else "summarize"

    def route_review_decision(state: dict) -> str:
        # 作用：审核人员退回 → 退回节点；批准 → 按制度链长分流：
        #       链>1（大额）→ 邮件领导走领导决策；单级链（小额）→ 直达批准节点，免总经理审批
        if state["decision"]["action"] != "approve":
            return "return_node"
        chain = state.get("approval_chain") or []
        return "email_leader" if len(chain) > 1 else "approve_node"

    def route_leader_decision(state: dict) -> str:
        # 作用：领导批准 → 批准节点；否决 → 作废节点
        return "approve_node" if state["decision"]["action"] == "approve" else "void_node"

    # ================= 组装图 =================

    builder = StateGraph(ReimbursementState)
    for node_name, fn in [
        ("classify", classify),
        ("route_to_business", route_to_business),
        ("summarize", summarize),
        ("notify_reviewer", notify_reviewer),
        ("wait_reviewer", wait_reviewer),
        ("email_leader", email_leader),
        ("wait_leader", wait_leader),
        ("approve_node", approve_node),
        ("return_node", return_node),
        ("void_node", void_node),
    ]:
        builder.add_node(node_name, fn)

    # 主线：分类 → 业务子图 → 总结 → 通知审核人 → 人工复核
    builder.add_edge(START, "classify")
    # 条件边给显式 path_map（返回值 -> 目标节点）：否则 get_graph()/display(graph) 可视化
    # 会塌缩成 2 条主干边。path_map 只影响可视化与校验，运行时路由行为不变（官方推荐写法）
    builder.add_conditional_edges("classify", route_after_classify, {
        "route_to_business": "route_to_business",
        "END": END,
    })
    builder.add_conditional_edges("route_to_business", route_after_business, {
        "END": END,
        "summarize": "summarize",
    })
    builder.add_edge("summarize", "notify_reviewer")
    builder.add_edge("notify_reviewer", "wait_reviewer")
    builder.add_conditional_edges("wait_reviewer", route_review_decision, {
        "return_node": "return_node",
        "email_leader": "email_leader",          # 大额（链长>1）→ 领导终审
        "approve_node": "approve_node",          # 小额（单级链）→ 直达批准，免总经理审批
    })

    # 领导决策链：邮件领导 → 领导决策 → 批准/作废
    builder.add_edge("email_leader", "wait_leader")
    builder.add_conditional_edges("wait_leader", route_leader_decision, {
        "approve_node": "approve_node",
        "void_node": "void_node",
    })

    # 终态
    builder.add_edge("approve_node", END)
    builder.add_edge("void_node", END)
    builder.add_edge("return_node", END)

    # 作用：必须启用 checkpointer，interrupt() 才能跨调用暂停/恢复
    graph = builder.compile(checkpointer=MemorySaver())
    gr = graph.get_graph()
    print(gr.draw_mermaid())                                          # ① 图结构（mermaid，可粘贴 mermaid.live 看渲染）
    print({n: builder.nodes[n].runnable.func.__name__ for n in builder.nodes})  # ② 内部：节点 -> 函数
    return graph
