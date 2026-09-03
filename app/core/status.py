# app/core/status.py —— 全局状态常量
# 业务：把散落的魔法字符串收拢为常量，状态机可读可审计（docs/06 §2 异步任务状态机）
from __future__ import annotations


class SubmissionStatus:
    """异步任务状态（submissions 表，docs/06 §2）—— 消灭 P7 建议的魔法字符串

    PENDING → PROCESSING → SUCCEEDED
                    │          │
                    └── FAILED ←┘（重试耗尽）
    """

    PENDING = "pending"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"

    # 终态集合：已终态的任务不可再领取/重试（claim/retry 幂等判定用）
    TERMINAL = {SUCCEEDED, FAILED}
