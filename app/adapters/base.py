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
