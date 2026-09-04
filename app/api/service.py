# app/api/service.py —— 接口编排（调用总控图 + 持久化）
# 业务：API 与图之间的薄胶水层：启动图执行 / 恢复 HITL / 落库 / 组装视图
import threading
from datetime import datetime

from langgraph.types import Command

from app.container import Container
from app.core.config import ASYNC_ENABLED
from app.core.logging import get_logger, log_error, log_info, log_warning, set_log_context
from app.core.status import SubmissionStatus

logger = get_logger("api.service")

# 作用：审批/打款是"读-改-写"临界区，进程内锁防并发双写（Demo 单进程；多实例需分布式锁）
_lock = threading.Lock()

# 终态集合：幂等闸/实时态合并/孤儿物化共用（approved=批准、paid=打款、returned=退回、voided=作废）
TERMINAL_STATUSES = ("approved", "paid", "returned", "voided")


def _clean_state(state: dict) -> dict:
    """剔除运行期字段（__interrupt__ 等不可 JSON 序列化），只留业务状态"""
    # 作用：__ 前缀为 langgraph 运行期内部键，不能落库/返回
    return {k: v for k, v in state.items() if not k.startswith("__")}


def _has_failed_soft_checks(state: dict) -> bool:
    """软闸门复核衔接（#88）：存在未通过的合规检查项（超预算/席别越级等，passed=False）"""
    # 业务：合规检查是软闸门——不阻断流程、由人工复核裁决；但"批准未通过项"必须留批注，
    #       否则合规标记形同虚设、审计无据（企业条例可裁量，裁量要可追溯）
    return any(c.get("passed") is False for c in (state.get("compliance_checks") or []))


def _live_state(container: Container, request_id: str) -> dict | None:
    """实时业务态（执行真相优先）：图 checkpoint 权威、requests 行是展示缓存

    业务（#B HITL 双写可靠性）：decide/pay/_authorize 判"当前步骤/审批链/合规"必须对着**图此刻真实位置**，
    不能读可能落后的 requests 行——run_submit_pipeline/decide 都是"图写 checkpoint → upsert 行"两步，
    中间崩溃会让行停在旧步（落库前崩溃窗口），按行判会放行错步/离场角色（越权）。

    合并规则（行终态权威 / 图在途权威）：
    - checkpoint 缺失（MemorySaver 重启 / 该单从未跑过图）→ 退回行（展示缓存兜底）；
    - 行已终态（approved/paid/returned/voided）→ **行权威**：pay 不经图，checkpoint 永远停在 approved，
      行上的 paid 才是最新——读 checkpoint 会把已打款单当 approved 重复放行打款；
    - 行在途 + checkpoint 存在 → checkpoint 权威（可能领先行，正是崩溃窗口要防的错判）。
    """
    row = container.storage.get_request(request_id)
    live = None
    try:
        snap = container.graph.get_state({"configurable": {"thread_id": request_id}})
        values = snap.values if snap is not None else None
        if values:
            live = _clean_state(values)
    except Exception:
        live = None  # 线程不存在/checkpointer 不可用 → 兜底行
    if live is None:
        return row
    if row is None:
        return live            # 孤儿：checkpoint 在、行缺失（落库前崩溃）
    if row.get("process_status") in TERMINAL_STATUSES:
        return row             # 行已终态（含 paid 等不经图写）：以行最新为准
    return live                # 行在途：图实时态为准（可能领先行）


def _release_invoice_on_terminal(container: Container, request_id: str, state: dict) -> None:
    """发票池释放：退回/作废释放票号占用（报销人可修改重提）；打款/审批中保持占用（防重复报销）"""
    # 业务：图执行到终态后统一收口——非支付终态释放占用，否则同一发票无法重提；已打款保持 active 永久拦截
    if state.get("process_status") in ("returned", "voided"):
        container.storage.release_invoice(request_id)


def _reserve_advance_on_terminal(container: Container, state: dict) -> None:
    """事前申请预算占用：报销到达 approved/paid 终态 → 按报销总额占一笔（advance_reservations 台账）"""
    # 业务：预算池核销（docs/02 §12）——approved 即占用（终态不可退回）、paid 兜底幂等
    #       （request_id 唯一，approve+paid 双入口不重复累计）；returned/voided 不占用（报销未成功）。
    #       申请状态不翻 used——一次出差多张票共享同一申请逐笔累计占用，直到区间/有效期自然结束。
    #       #A 多票批：占用额 = Σ 被接受票面（total_amount）；旧单/单票回退 invoice_data.amount。
    #       已知限制：审批中并发提交的"在途"占用不入本单 state 快照（reserved_amount 为提交时点值）。
    if state.get("process_status") not in ("approved", "paid"):
        return
    app = state.get("advance_application") or {}
    amount = state.get("total_amount")
    if amount is None:
        amount = (state.get("invoice_data") or {}).get("amount")
    request_id = state.get("request_id")
    if app.get("app_id") and request_id and amount:
        total = container.advances.reserve(app["app_id"], request_id, float(amount))
        log_info(logger, "事前申请预算占用", app_id=app["app_id"], request_id=request_id,
                 amount=float(amount), total=total)


def _run(graph, payload, config: dict) -> dict:
    """执行图并取最终 state（values 模式；含 __interrupt__ 键表示挂起）"""
    # 作用：stream 能稳定拿到"中断点"或"终态"的完整状态
    values = list(graph.stream(payload, config, stream_mode="values"))
    return values[-1]


def _stamp_pause(state: dict) -> dict:
    """#62：单据进入"人工审批挂起"时记录挂起起始时间（SLA 锚点）

    何时处于人工挂起：current_step=review/leader_decision 且非终态。
    作用：SLA 催办/升级以 paused_at 为锚（每级审批重新计时），而**不是**行 updated_at——
          beat 催办写行会刷新 updated_at，用它作锚会把时钟无限往后推、升级永不触发。
         paused_at 只落行快照（不经图），checkpoint 通道值不含它，decide 恢复后由本函数按新环节重记。
    """
    step = state.get("current_step", "")
    if step in ("review", "leader_decision") and state.get("process_status") not in TERMINAL_STATUSES:
        return {**state, "paused_at": datetime.now().isoformat(timespec="seconds")}
    return state


def _as_inputs(invoice_inputs: dict | list[dict]) -> list[dict]:
    """规整提交输入为列表：#A 多票批 = list[每票 meta]；单票老调用传 dict → [dict]（批大小 1）"""
    if isinstance(invoice_inputs, list):
        return invoice_inputs
    return [invoice_inputs]


def run_submit_pipeline(container: Container, invoice_inputs: dict | list[dict], request_id: str) -> dict:
    """执行提交管线：启动总控图 → 落库 → 发票池终态释放（同步直跑 与 worker 任务共用）

    invoice_inputs：#A 多票批为 list（每元素=单张票的上传 meta）；兼容单票 dict（历史直调/快照重放）。
    """
    # 作用：把"图执行+落库+释放"从 HTTP 请求线程抽出来（#52 D4）——同步模式 start_reimbursement
    #       直接调用；异步模式由 Celery worker 用 submissions.snapshot 重放。
    #       把 request_id 注入关联上下文，本条链路的图节点/适配器日志自动携带，可整条串查
    inputs = _as_inputs(invoice_inputs)
    set_log_context(request_id=request_id)
    log_info(logger, "报销提交：启动图执行", business_type=(inputs[0] or {}).get("direction", ""), tickets=len(inputs))
    initial = {
        "request_id": request_id,
        # 兼容镜像：invoice_input = 首票 meta（请求级共享字段源：employee/direction/mode/app_id/purpose）
        "invoice_input": inputs[0],
        # #A 批输入：识别/验真/硬闸门的最小单元 = 单张票；单票批=[单个]
        "invoice_inputs": inputs,
        "process_status": "in_review",
    }
    config = {"configurable": {"thread_id": request_id}}
    try:
        result = _run(container.graph, initial, config)
    except Exception as exc:
        # 业务：图执行中途异常（非业务退回）——verify 节点可能已把票号入池（active），异常路径若不清理
        #       该票号残留占用 → 报销人被误判"重复提交"无法重提。异常同样要释放（幂等：无 active 则 no-op）
        log_error(logger, "报销提交异常，清理发票池占用", error=str(exc))
        container.storage.release_invoice(request_id)
        raise
    result = _stamp_pause(result)  # #62：挂起即记 SLA 锚点（审批节拍自此刻计）
    container.storage.upsert_request(
        request_id,
        _clean_state(result),
        result.get("process_status", "in_review"),
        result.get("current_step", ""),
    )
    _release_invoice_on_terminal(container, request_id, result)
    _reserve_advance_on_terminal(container, result)
    # 作用：提交结果落日志——退回（业务失败）与挂起（成功待审）区分记录，失败原因已由退回节点分类
    if result.get("process_status") == "returned":
        reason = result.get("return_reason", {})
        log_warning(logger, "报销提交失败（业务退回）", category=reason.get("category", ""), message=reason.get("message", ""))
    else:
        log_info(logger, "报销提交成功，进入审批", current_step=result.get("current_step", ""))
    return result


def submit_outcome_materialized(container: Container, request_id: str) -> bool:
    """该提交的图执行是否已产出可继续的落地结果（requests 行已写且停在 HITL 挂起或终态）

    #94 崩溃窗口语义收口的核心判定：run_submit_pipeline 只在图跑到挂起/终态后才 upsert 行——
    行存在且停在 review/leader_decision（HITL 挂起）或终态 ⇒ 首轮已完整跑完，只差 succeeded 未落
    （succeeded 写前崩溃窗口）；recovery 应补 succeeded，**绝不能重跑**（重跑 = 重发通知 +
    发票重新入池造成假查重覆盖已审单据）。行不存在或停在在途 ⇒ 首轮真中途崩溃 ⇒ 需 requeue 重跑。
    """
    row = container.storage.get_request(request_id)
    if row is None:
        return False
    step = (row.get("current_step") or "").lower()
    return row.get("process_status") in TERMINAL_STATUSES or step in ("review", "leader_decision")


def _deliver_to_worker(container: Container, request_id: str, snapshot: dict) -> None:
    """把任务行投递给 Celery；broker 不可达 → 降级同步直跑并把任务行对齐真实结果"""
    # 作用：提交(首次)与人工重试共用投递路径——外部依赖（Redis/worker）缺失时降级同步（项目一贯哲学），
    #       API 不因 broker 宕机不可用；同步直跑的结果状态写回任务行，前端轮询语义不破坏
    try:
        from app.tasks.submit_task import process_submission

        process_submission.delay(request_id)
        return
    except Exception as exc:  # celery 未装 / broker 不可达
        log_warning(logger, "异步入队失败（broker 不可达），降级同步直跑", error=str(exc))
    try:
        run_submit_pipeline(container, snapshot, request_id)
    except Exception as pipe_exc:
        container.storage.update_submission(
            request_id,
            status=SubmissionStatus.FAILED,
            error={"type": type(pipe_exc).__name__, "message": str(pipe_exc)},
            attempts=1,
        )
        raise
    container.storage.update_submission(request_id, status=SubmissionStatus.SUCCEEDED)


def _submission_status(row: dict) -> dict:
    """任务行 → 对外状态视图（显式映射，避免 snapshot 等内部键误入响应）"""
    return {
        "request_id": row.get("request_id"),
        "status": row.get("status", "pending"),
        "attempts": row.get("attempts", 0),
        "error": row.get("error"),
        "created_at": row.get("created_at", ""),
        "updated_at": row.get("updated_at", ""),
    }


def start_reimbursement(container: Container, invoice_inputs: dict | list[dict], request_id: str) -> dict:
    """提交报销：同步模式直跑；异步模式落任务行入队，返回 pending 受理凭证"""
    # 作用：异步模式把重负载（OCR/LLM/RAG/DB）搬进 Celery worker，HTTP 请求立即返回
    # 快照 = 原始输入列表（单票 dict 也归一为 list），worker 失败可重放整批
    snapshot = _as_inputs(invoice_inputs)
    if not ASYNC_ENABLED:
        return run_submit_pipeline(container, snapshot, request_id)
    # 先落任务行（快照=原始输入，worker 失败可重放），再投递——worker 领单依赖该行已存在
    container.storage.create_submission(request_id, snapshot=snapshot, status=SubmissionStatus.PENDING)
    _deliver_to_worker(container, request_id, snapshot)
    return {"request_id": request_id, "status": SubmissionStatus.PENDING}


def retry_submission(container: Container, request_id: str) -> dict:
    """人工重试失败任务：failed → pending（清错误、归零次数）→ 重新投递（异步 worker / 降级同步）"""
    # 业务：#53 闭环——任务失败后请求人应能"改一改再提交"而不是永远看不到结果；
    #       仅 failed 可重试（pending/processing 是进行中，succeeded 是已完成），重试后前端继续轮询新任务
    sub = container.storage.get_submission(request_id)
    if sub is None:
        raise KeyError(f"异步提交任务不存在: {request_id}")
    if sub.get("status") != SubmissionStatus.FAILED:
        raise ValueError(f"仅处理失败的任务可重试，当前状态({sub.get('status')})")
    container.storage.requeue_submission(request_id)          # failed → pending（错误清空、计数归零）
    _deliver_to_worker(container, request_id, sub["snapshot"])  # 重新投递（快照=原始输入，重放）
    fresh = container.storage.get_submission(request_id)
    log_info(logger, "失败任务人工重试", request_id=request_id, status=(fresh or {}).get("status"))
    return _submission_status(fresh or {})


def decide(container: Container, request_id: str, action: str, comment: str, actor: str) -> dict:
    """审批决策：实时态鉴权（读图 checkpoint）→ 幂等闸 → 动作白名单 → Command(resume=...) 恢复 HITL → 落库"""
    # 业务：越权是报销系统第一安全风险——当前步骤必须由审批链对应角色本人操作，
    #       否则任何人可伪造审批（如 4001 跳过 2001 直接批准）。不通过抛 PermissionError。
    # 作用（#B）：当前步骤/审批链/合规一律读 _live_state（checkpoint 实时态、行兜底）——
    #       双写落后（行停在旧步）时不再放行错步/离场角色；权限校验与 resume 同一把锁内，
    #       并发双击第二次读到的是已推进状态 → 幂等闸直接返回，杜绝 TOCTOU 双跑/双通知。
    set_log_context(request_id=request_id)
    resume = {"action": action, "comment": comment, "actor": actor}
    config = {"configurable": {"thread_id": request_id}}
    with _lock:
        state = _live_state(container, request_id)
        if state is None:
            raise ValueError(f"报销单不存在: {request_id}")
        step = state.get("current_step", "")
        status = state.get("process_status", "")
        prior = state.get("decision") or {}
        # 幂等闸（②）：已终态且本次决策与已记录完全一致 → 不二次 resume、不重发通知（双击/崩溃重试安全）。
        #   行若落后于图（落库前崩溃窗口：图已终态、行还没写）→ 顺手把行对齐，双写自愈。
        if status in TERMINAL_STATUSES and prior.get("action") == action and prior.get("actor") == actor:
            row = container.storage.get_request(request_id)
            if row is not None and row.get("process_status") in TERMINAL_STATUSES:
                log_info(logger, "审批决策幂等命中（终态已含同动作），直接返回现状", request_id=request_id, status=status)
                return row
            container.storage.upsert_request(request_id, _clean_state(state), status, step)
            log_info(logger, "审批决策幂等命中：落后行已对齐，返回实时态", request_id=request_id, status=status)
            return state
        # 动作白名单（#B 从路由层收敛到服务）：按实时步判定合法动作；终态（done）后乱点 → 400。
        #   路由层只做存在性 + 错误映射，实时步校验全部在 service 内一处完成，避免两层各读各的状态
        allowed = {
            "review": {"approve", "return"},
            "leader_decision": {"approve", "void"},
        }.get(step, set())
        if action not in allowed:
            raise ValueError(f"当前步骤({step})不允许操作 {action}，允许: {sorted(allowed) or '无'}")
        _authorize(container, request_id, actor, state=state)
        # 业务：硬闸门已把硬性问题挡在退回；仍走到人工复核的未通过项属可裁量的软闸门——
        #       批准须填复核意见（特批理由），否则拒绝（复核≠橡皮图章，意见留痕供审计追溯）
        if action == "approve" and _has_failed_soft_checks(state):
            if not (comment or "").strip():
                raise ValueError("存在未通过的合规检查项（如超预算/席别超标准），批准须填写复核意见（特批理由）")
        result = _run(container.graph, Command(resume=resume), config)
        result = _stamp_pause(result)  # #62：进入下一级人工审批 → 重新计时（每级独立 SLA 节拍）
        container.storage.upsert_request(
            request_id,
            _clean_state(result),
            result.get("process_status", "in_review"),
            result.get("current_step", ""),
        )
    _release_invoice_on_terminal(container, request_id, result)
    _reserve_advance_on_terminal(container, result)
    log_info(logger, "审批决策完成", action=action, actor=actor, status=result.get("process_status", ""))
    return result


def pay(container: Container, request_id: str, comment: str, actor: str) -> dict:
    """出纳打款：审批通过(approved)后由财务出纳确认打款 → 单据 paid"""
    # 业务：审批≠支付——批准是审批链终点，打款是财务域动作（出纳身份按制度角色"财务"解析）
    # 作用（#B）：approved 判定读实时态——行落后于图（落库前崩溃）时打款不被误拒；反过来，
    #       paid 是行写（不经图），_live_state 的"行终态权威"保证已打款单读到 paid、不会二次放行。
    set_log_context(request_id=request_id)
    with _lock:
        state = _live_state(container, request_id)
        if state is None:
            raise ValueError(f"报销单不存在: {request_id}")
        if state.get("process_status") != "approved":
            raise ValueError(f"仅已批准单据可打款，当前状态({state.get('process_status')})")
        finance = container.users.get_approver("财务")
        if actor != finance["id"]:
            raise PermissionError(f"打款仅限出纳 {finance['id']} 执行，实际 {actor or '未指定'}")
        now = datetime.now().isoformat(timespec="seconds")
        records = list(state.get("approval_records", []))
        record = {
            "role": "财务出纳",
            "decision": "pay",
            "actor": actor,
            "comment": comment,
            "time": now,
        }
        records.append(record)
        # 拆表落库（docs/06 §5：approval_records 表为权威）
        container.storage.add_approval_record(request_id, record)
        new_state = {
            **state,
            "process_status": "paid",
            "current_step": "done",
            "approval_records": records,
            "payment": {"actor": actor, "comment": comment, "time": now},
        }
        container.storage.upsert_request(request_id, new_state, "paid", "done")
    # 业务：打款完成后通知报销人到账；事前申请在 approved 已占用，paid 幂等兜底（不重复累计）
    _reserve_advance_on_terminal(container, new_state)
    container.notify_tool.notify_paid(request_id, new_state)
    return new_state


def _authorize(container: Container, request_id: str, actor: str, *, state: dict | None = None) -> None:
    """权限校验：按实时态审批链 + 当前步骤确定唯一审批人，actor 必须与其匹配"""
    # 业务：review 步=审批链第一级（直属上级）；leader_decision 步=审批链末级（总经理/最终决策人）
    # 作用（#B）：决策方通常已带实时态（decide 内同一份），避免二次读；外部直调则自取 _live_state——
    #       双写落后时按图真实位置判"该谁决策"，不按落后的行放行离场角色
    state = state if state is not None else _live_state(container, request_id)
    if state is None:
        raise ValueError(f"报销单不存在: {request_id}")
    chain = state.get("approval_chain") or []
    step = state.get("current_step", "")
    if step not in ("review", "leader_decision"):
        raise PermissionError(f"当前步骤({step})不可审批")
    if not chain:
        raise PermissionError("审批链信息缺失，无法校验权限")
    expected = chain[0] if step == "review" else chain[-1]
    if actor != expected["id"]:
        raise PermissionError(
            f"权限校验失败：当前步骤({step})应由 {expected['role']}({expected['id']}) 决策，"
            f"实际提交人 {actor or '未指定'}"
        )


def _checkpointer_thread_ids(container: Container) -> list[str]:
    """枚举 checkpointer 中现存全部 thread_id（孤儿物化用）——thread_id 恒等于 request_id"""
    # 作用（#28）：langgraph saver 无"列全部线程"API，直接读其 checkpoints 表（固定表名/列名）。
    #       checkpointer 已统一持久化（同步也落盘，见 container.build_checkpointer）→ 崩溃跨重启残留，
    #       孤儿物化两模式都执行。此处 MemorySaver 分支仅兜底"直接 new Container"（不经 build_container）
    #       的测试直构——那是内存态，本进程内无跨重启孤儿，跳过合理。
    from langgraph.checkpoint.memory import MemorySaver

    cp = container.checkpointer
    if isinstance(cp, MemorySaver):
        return []
    conn = getattr(cp, "conn", None)  # SqliteSaver.conn = sqlite3.Connection；PostgresSaver.conn = psycopg 连接/池
    if conn is None:
        return []
    try:
        # SQLite（异步本地替身）：连接对象有 execute、无 fetchall（后者在 cursor 上）→ 按类型分派
        import sqlite3

        if isinstance(conn, sqlite3.Connection):
            rows = conn.execute("SELECT DISTINCT thread_id FROM checkpoints").fetchall()
            return [r[0] for r in rows]                       # 默认 row_factory=None → 元组
        # PostgreSQL（生产）：懒加载仅在 PG saver 分支触碰 psycopg（SQLite 测试不依赖它）
        import psycopg
        from psycopg_pool import ConnectionPool

        if isinstance(conn, ConnectionPool):
            with conn.connection() as c:
                rows = c.execute("SELECT DISTINCT thread_id FROM checkpoints").fetchall()
        elif isinstance(conn, psycopg.Connection):
            rows = conn.execute("SELECT DISTINCT thread_id FROM checkpoints").fetchall()
        else:
            return []
        # build_checkpointer 用 dict_row → 行是 dict；防御 tuple 兼容
        return [r["thread_id"] if isinstance(r, dict) else r[0] for r in rows]
    except Exception as exc:
        log_warning(logger, "checkpointer 线程枚举失败，孤儿对账跳过本轮", error=str(exc))
    return []


def materialize_orphan_checkpoints(container: Container) -> int:
    """孤儿 checkpoint → 物化 requests 行（③ 启动/周期对账）：解决"图已挂起/终态、行缺失不可见"的崩溃窗口"""
    # 业务：run_submit_pipeline/decide 都是"图写 checkpoint → upsert 行"，两步间崩溃（进程/连接断开）
    #      会让状态只存在于 checkpoints 表（发票池可能已占用、通知已发），requests 无行 → 报销单不可见、
    #      无法继续审批。本函数把这类"孤儿"按图实时态补行，交还正常流程。
    # 只物化"已停在人工挂起（review/leader_decision）或已终态"的图——worker 在途的中途态不物化，
    #   避免给半成品占位（在途任务由 reclaim 复位重跑，跑到挂起/终态自会落行）。
    count = 0
    for thread_id in _checkpointer_thread_ids(container):
        if container.storage.get_request(thread_id) is not None:
            continue  # 已有行：非孤儿
        state = _live_state(container, thread_id)
        if not state:
            continue
        status = state.get("process_status", "")
        step = state.get("current_step", "")
        if status not in TERMINAL_STATUSES and step not in ("review", "leader_decision"):
            continue  # 图在途/中途：等 worker 重跑续到挂起点再落行
        container.storage.upsert_request(thread_id, _clean_state(state), status or "in_review", step or "")
        _release_invoice_on_terminal(container, thread_id, state)  # 孤儿若停在 returned/voided 需释放票池占用
        log_info(logger, "孤儿 checkpoint 已物化为报销单", request_id=thread_id, status=status, step=step)
        count += 1
    return count


def _strip_local_path(ticket: dict) -> dict:
    """视图脱敏（#93 对象存储取用接线）：票据里的 file_path 是本地临时缓存路径（不跨主机、会被清理），
    对调用方无意义且泄漏服务器本地布局——对外只暴露 object_key（源文件权威副本的取用凭据）
    """
    if not isinstance(ticket, dict) or "invoice_input" not in ticket:
        return ticket
    inv = ticket["invoice_input"]
    if not isinstance(inv, dict):
        return ticket
    # 浅拷贝：只摘掉 file_path，保留 object_key/file_name/declared_amount 等（不改底层 state）
    return {**ticket, "invoice_input": {k: v for k, v in inv.items() if k != "file_path"}}


def _view_tickets(clean: dict) -> list | None:
    """tickets/rejected 视图：逐票摘掉本地 file_path（#93 视图层，不污染存储的权威 state）"""
    tickets = clean.get("tickets")
    if isinstance(tickets, list):
        tickets = [_strip_local_path(t) for t in tickets]
    rejected = clean.get("rejected")
    if isinstance(rejected, list):
        rejected = [_strip_local_path(t) for t in rejected]
    return tickets, rejected


def request_view(container: Container, state: dict) -> dict:
    """组装给前端/调用方的视图（含通知与邮件留痕）"""
    # 业务：审核端/报销端关心的字段；paused 标记当前是否处于人工挂起
    clean = _clean_state(state)
    # 作用：paused 由 process_status + current_step 推导，不再依赖 __interrupt__ 运行期键——
    #       落库 state 经 _clean_state 剥离 __ 键，GET 详情拿不到它（此前恒 False 的 bug）。
    #       实时图状态含 __interrupt__ 时以它为准（等价于挂起）
    terminal = clean.get("process_status") in ("approved", "returned", "voided", "paid")
    paused = ("__interrupt__" in state) or (
        not terminal and clean.get("current_step") in ("review", "leader_decision")
    )
    tickets, rejected = _view_tickets(clean)
    return {
        "request_id": clean.get("request_id"),
        "status": clean.get("process_status", "in_review"),
        "current_step": clean.get("current_step", ""),
        "business_type": clean.get("business_type", ""),
        # 作用：#89 退回重提留痕——本次报销若由某报销单退回/作废后重提，记录其原单号（无则不填）
        "parent_request_id": (clean.get("invoice_input") or {}).get("parent_request_id", "") or None,
        "summary": clean.get("summary", ""),
        # #32 函数调用留痕：agent 检索轨迹（工具/参数/结果截断）——审核人员可追溯总结依据；无 agent 为空
        "research_notes": clean.get("research_notes", []),
        "invoice_data": clean.get("invoice_data"),
        "tickets": tickets,
        "rejected": rejected,
        "total_amount": clean.get("total_amount"),
        "verification": clean.get("verification"),
        "advance_application": clean.get("advance_application"),
        "compliance_checks": clean.get("compliance_checks"),
        "policy_basis": clean.get("policy_basis"),
        "approval_chain": clean.get("approval_chain"),
        "return_reason": clean.get("return_reason"),
        "approval_records": clean.get("approval_records"),
        "decision": clean.get("decision"),
        "payment": clean.get("payment"),
        "paused": paused,
        "messages": container.storage.list_messages(clean["request_id"]),
        "emails": container.storage.list_emails(clean["request_id"]),
    }
