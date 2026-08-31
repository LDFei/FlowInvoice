# app/core/ids.py —— 单号与 ID 生成
# 业务：报销单/事前申请单需要企业可读的单号（格式：{缩写}-{日期}-{序号}），
#       同时要求线程级唯一 + 跨重启唯一，供 LangGraph thread_id 复用（中断后按单号恢复）
import itertools
import re
import threading
from datetime import date

# 作用：进程内自增序号（线程安全）
# 业务：同一时刻多个请求并发提交也能拿到不重复序号
_counter = itertools.count(1)
_lock = threading.Lock()


def new_sequence(digits: int = 3) -> str:
    """生成进程内自增序号（补零到指定位数）"""
    # 作用：取自增序号并补零，如 001 / 002
    with _lock:
        return str(next(_counter)).zfill(digits)


def seed_from_existing(ids: list[str], digits: int = 3) -> None:
    """用历史单号校准自增序号（进程重启后计数器归零，须续号避免复用旧单号）"""
    # 业务：messages/emails 只追加不清理，若重启后复用同一单号，新单据的留痕会混入上一次流程的数据
    global _counter
    max_n = 0
    for rid in ids:
        m = re.search(r"-(\d{%d})$" % digits, rid)
        if m:
            max_n = max(max_n, int(m.group(1)))
    with _lock:
        _counter = itertools.count(max_n + 1)


def new_bill_no(kind: str) -> str:
    """生成业务单号，如 REIM20260831-001"""
    # 业务：REIM=报销单 / ADV=事前申请单；格式符合企业单号习惯，便于人工读和检索
    return f"{kind}{date.today():%Y%m%d}-{new_sequence()}"


def new_request_id() -> str:
    """生成报销请求 ID（LangGraph thread_id 复用，保证中断可恢复）"""
    # 作用：每次提交一张报销单生成一个唯一 ID
    return new_bill_no("REIM")
