# app/main.py —— FastAPI 入口（只做装配，不含业务逻辑）
# 业务：启动时装配依赖容器 + 种演示数据；挂载路由（docs/AGENTS.md §2 依赖方向 api→graphs）
import re
from contextlib import asynccontextmanager
from datetime import date, timedelta

from fastapi import FastAPI

from app.core.logging import get_logger, log_info, setup_logging

# 作用：企业级技术日志装配（JSON 文件轮转 + 控制台；级别/目录 env 可配，见 app/core/logging.py）
# 业务：让业务模块（rag 等）的 INFO/WARNING 日志在 uvicorn 下可见 + 落盘可查
setup_logging()
logger = get_logger("app.main")

from app.api.advance import router as advance_router
from app.api.reimburse import router as reimburse_router
from app.container import Container, build_container
from app.core.config import ASYNC_ENABLED, RAG_VECTOR_DSN


def seed_demo_data(container: Container) -> None:
    """种演示数据：内置一份有效的事前申请，保证差旅闭环开箱即走"""
    # 业务：Demo 员工 1001 差旅，申请区间覆盖今天 → 提交报销即可匹配成功
    today = date.today()
    # 幂等守卫：已存在覆盖今天的 active 申请则跳过——异步模式每次重启都会跑 lifespan，
    #       若无条件重插会让重叠申请逐次累积（历史上已累积 ~30 条），自动匹配失去唯一性
    if container.storage.find_active_advances("1001", "travel", today.isoformat()):
        log_info(logger, "演示事前申请已存在，跳过种入")
        return
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
    if RAG_VECTOR_DSN:
        dsn_display = re.sub(r":([^@/]+)@", ":****@", RAG_VECTOR_DSN)   # 脱敏：不打印口令
        log_info(logger, "RAG 混合检索已启用：BM25 + bge-m3 向量", vector_store=dsn_display)
    else:
        log_info(logger, "RAG 纯 BM25 模式（未配置 FLOWINVOICE_PG_DSN，向量检索关闭）")
    if ASYNC_ENABLED:
        # #94 崩溃窗口语义收口：不用盲 reset_stuck_submissions（只 processing→pending 不复位重投 →
        #      制造"永久 pending 僵尸"）。启动那刻所有 processing/pending 均来自已死 worker → force 全量判定：
        #      结果已落地（requests 行停在 HITL/终态，succeeded 写前崩溃）→ 补 succeeded 不重跑；
        #      真中途崩溃（行缺失/停在在途）→ 清残留入池票 + 复位重投，worker 领单重跑。
        from app.tasks.reclaim_task import recover_stuck_submissions

        fixed = recover_stuck_submissions(container, force=True)
        log_info(logger, "异步模式：崩溃窗口粘滞任务收口完成", fixed_count=fixed)
    # 作用（#B ③ + #28）：孤儿 checkpoint 对账——checkpointer 现已统一持久化（同步也落盘），
    #      启动时扫上次运行"图写 checkpoint → upsert 行"间崩溃的单据，把图实时态物化为 requests 行，
    #      交还正常审批（行缺失的单据不可见=崩溃窗口数据丢失）。同步 MemorySaver 时代无此问题（无持久化）；
    #      持久化后两模式一致对账，杜绝幽灵单据积压。
    from app.api.service import materialize_orphan_checkpoints
    orphan = materialize_orphan_checkpoints(container)
    log_info(logger, "孤儿 checkpoint 物化对账完成", materialized=orphan)
    # 作用：启动即 sweep 过期上传临时文件（#68，防 data/uploads 无限增长；源文件权威副本在对象存储）
    removed = container.uploader.cleanup_stale()
    log_info(logger, "上传临时文件清理完成", removed=removed)
    log_info(logger, "FlowInvoice 已启动：依赖容器就绪，演示数据已种入")
    yield


app = FastAPI(
    title="FlowInvoice 发票报销智能 Agent 系统",
    description="""
# 发票报销智能 Agent 系统（Demo）

基于 **LangGraph 多 Agent 编排** 的差旅报销闭环演示：上传发票后，Agent 自动完成 **识别 → 验真 → 事前申请匹配 → 合规检查（RAG 制度依据）→ 审批链 → 审核总结 → 人工复核（HITL）→ 领导决策 → 通知财务付款**，全程可追溯。

## 演示角色（Mock 组织，数据源见 `app/adapters/org_data.yaml`，seed 进 employees/approver_roles 表，#27）
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
