# app/main.py —— FastAPI 入口（只做装配，不含业务逻辑）
# 业务：启动时装配依赖容器 + 种演示数据；挂载路由（docs/AGENTS.md §2 依赖方向 api→graphs）
from contextlib import asynccontextmanager
from datetime import date, timedelta

from fastapi import FastAPI

from app.api.advance import router as advance_router
from app.api.reimburse import router as reimburse_router
from app.container import Container, build_container


def seed_demo_data(container: Container) -> None:
    """种演示数据：内置一份有效的事前申请，保证差旅闭环开箱即走"""
    # 业务：Demo 员工 1001 差旅，申请区间覆盖今天 → 提交报销即可匹配成功
    today = date.today()
    container.advances.create(
        employee_id="1001",
        direction="travel",
        start_date=(today - timedelta(days=3)).isoformat(),
        end_date=(today + timedelta(days=4)).isoformat(),
        estimated_amount=2000.0,
        purpose="上海客户拜访",
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 作用：进程启动时装配依赖容器，挂到 app.state 供路由取用
    container = build_container()
    app.state.container = container
    seed_demo_data(container)
    print("FlowInvoice 已启动：依赖容器就绪，演示数据已种入")
    yield


app = FastAPI(
    title="FlowInvoice 发票报销智能 Agent 系统",
    description="""
# 发票报销智能 Agent 系统（Demo）

基于 **LangGraph 多 Agent 编排** 的差旅报销闭环演示：上传发票后，Agent 自动完成 **识别 → 验真 → 事前申请匹配 → 合规检查（RAG 制度依据）→ 审批链 → 审核总结 → 人工复核（HITL）→ 领导决策 → 通知财务付款**，全程可追溯。

## 演示角色（Mock 组织，见 `app/adapters/mock_oa.py`）
| 工号 | 姓名 | 角色 |
|---|---|---|
| 1001 | 张三 | 报销人（员工） |
| 2001 | 李四 | 直属上级（审核人） |
| 4001 | 孙七 | 总经理（最终审批） |
| 3001 | 赵六 | 财务（出纳，收款通知） |

## 5 分钟体验闭环
1. **`POST /api/reimburse`** 上传发票（用项目里 `demo/样例-火车票.txt`）→ 返回 `request_id`，挂起在 **审核复核**
2. **小额(≤2000)**：**`POST /api/requests/{id}/decide`**：`action=approve`、`actor=2001` → 直接 **`approved`**（免总经理审批，总经理仅收到告知）
3. **大额(>2000)**：`actor=2001` 批准后挂起到 **总经理终审**，再以 `action=approve`、`actor=4001` 终审 → `approved`
4. **出纳打款**：**`POST /api/requests/{id}/pay`**：`actor=3001` → 单据 **`paid`**，通知报销人到账（审批≠支付，仅出纳可打款）
5. 把 `action` 换成 `return` / `void` 可体验 **退回 / 作废** 分支

## 状态机速查
- `status`：`in_review`（审批中）/ `returned`（退回）/ `approved`（已批准）/ `paid`（已打款）/ `voided`（作废）
- `current_step`：`review`（待审核人复核）/ `leader_decision`（待领导决策）/ `done`（结束）
""",
    version="0.1.0",
    lifespan=lifespan,
    openapi_tags=[
        {"name": "报销", "description": "报销主流程：提交报销、审批决策、状态查询"},
        {"name": "事前申请", "description": "出差事前申请（差旅报销的前置条件）"},
    ],
)

app.include_router(reimburse_router)
app.include_router(advance_router)
