# app/adapters/base.py —— 外部系统接口抽象（依赖倒置）
# 业务：上层（tools/图）只依赖接口不依赖具体实现；真实 OA/费控/邮件网关实现即可无缝替换（docs/01 §6）
from abc import ABC, abstractmethod


class UserProvider(ABC):
    """用户 / 组织架构：员工信息、直属上级、各审批角色"""

    @abstractmethod
    def get_employee(self, employee_id: str) -> dict:
        """按工号取员工信息（姓名/部门/邮箱/上级）"""

    @abstractmethod
    def get_manager(self, employee_id: str) -> dict:
        """取直属上级"""

    @abstractmethod
    def get_approver(self, role: str) -> dict:
        """按审批角色取人员（直属上级/部门负责人/财务/总经理）"""


class VerifyProvider(ABC):
    """发票验真 / 查重"""

    @abstractmethod
    def verify(self, invoice_data: dict) -> dict:
        """返回 {verified, duplicate, note}"""


class NotifyProvider(ABC):
    """站内 / IM 通知（审核人、报销人、审批链、财务）"""

    @abstractmethod
    def send(self, request_id: str, to_role: str, title: str, content: str) -> None:
        """给某角色/人发一条通知"""


class EmailProvider(ABC):
    """邮件（审批领导触达）"""

    @abstractmethod
    def send(self, request_id: str, to: str, subject: str, body: str) -> None:
        """发一封邮件"""


class StorageProvider(ABC):
    """请求与单据持久化（请求状态 / 事前申请 / 消息 / 邮件）"""

    # ---- 报销请求 ----
    @abstractmethod
    def upsert_request(self, request_id: str, state: dict, status: str, current_step: str) -> None:
        """保存或更新一条报销请求（state 整体持久化）"""

    @abstractmethod
    def get_request(self, request_id: str) -> dict | None:
        """按单号取请求状态（dict）"""

    @abstractmethod
    def list_requests(self, status: str | None = None) -> list[dict]:
        """列出请求摘要（可按状态过滤，供列表/驾驶舱使用）"""

    # ---- 事前申请 ----
    @abstractmethod
    def create_advance(self, advance: dict) -> None:
        """新增事前申请单（upsert，便于状态流转 used/expired）"""

    @abstractmethod
    def get_advance(self, app_id: str) -> dict | None:
        """按单号取事前申请"""

    @abstractmethod
    def find_active_advance(self, employee_id: str, direction: str, on_date: str) -> dict | None:
        """按 员工+方向+日期 匹配有效事前申请"""

    @abstractmethod
    def list_advances(self, status: str | None = None) -> list[dict]:
        """列出事前申请（可按状态过滤）"""

    # ---- 通知 / 邮件留痕 ----
    @abstractmethod
    def add_message(self, request_id: str, to_role: str, content: str) -> None:
        """记录一条通知（演示留痕/驾驶舱查看）"""

    @abstractmethod
    def list_messages(self, request_id: str) -> list[dict]:
        """列出某请求的通知记录"""

    @abstractmethod
    def add_email(self, request_id: str, to: str, subject: str, body: str) -> None:
        """记录一封邮件"""

    @abstractmethod
    def list_emails(self, request_id: str) -> list[dict]:
        """列出某请求的邮件记录"""

    # ================= 异步任务（submissions，docs/06 §2） =================

    @abstractmethod
    def create_submission(self, request_id: str, snapshot: dict, status: str = "pending") -> None:
        """提交时落任务行：快照 = 原始输入（file_key + 表单），支持重放"""

    @abstractmethod
    def get_submission(self, request_id: str) -> dict | None:
        """按单号取任务状态（前端轮询）"""

    @abstractmethod
    def update_submission(
        self,
        request_id: str,
        *,
        status: str | None = None,
        error: dict | None = None,
        attempts: int | None = None,
    ) -> None:
        """更新任务状态/失败原因/重试计数"""

    @abstractmethod
    def list_submissions(self, status: str | None = None) -> list[dict]:
        """任务列表（可按状态过滤，worker 领单/启动恢复用）"""

    @abstractmethod
    def reset_stuck_submissions(self) -> int:
        """启动恢复：processing → pending（上次进程崩溃），返回重置数"""

    # ================= 发票池（真查重，docs/06 §3.1） =================

    @abstractmethod
    def add_invoice(self, invoice: dict) -> bool:
        """票号入池（status=active）；True=入池成功，False=同票号 active 冲突（查重拦截）"""

    @abstractmethod
    def release_invoice(self, request_id: str) -> None:
        """请求进入 returned/voided 终态 → 票号释放（active→released，可再次提交）"""

    @abstractmethod
    def find_invoice(self, invoice_no: str) -> dict | None:
        """按票号查占用中的发票（查重命中返回占用行）"""

    # ================= 审批记录（审计拆表，docs/06 §2） =================

    @abstractmethod
    def add_approval_record(self, request_id: str, record: dict) -> None:
        """追加一条审批执行记录（审计/驾驶舱数据源）"""

    @abstractmethod
    def list_approval_records(self, request_id: str) -> list[dict]:
        """列出某请求的审批记录"""


class ObjectStorage(ABC):
    """发票源文件对象存储（MinIO/S3，docs/06 Phase 2）
    key 即定位符：DB 只存 file_key，文件本体不进数据库（存对象存储）"""

    @abstractmethod
    def put(self, key: str, content: bytes) -> str:
        """写入对象（幂等覆盖），返回对象 key"""

    @abstractmethod
    def get(self, key: str) -> bytes:
        """读取对象内容"""

    @abstractmethod
    def exists(self, key: str) -> bool:
        """对象是否存在"""

    @abstractmethod
    def delete(self, key: str) -> None:
        """删除对象（幂等，不存在不报错）"""
