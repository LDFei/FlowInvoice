# app/adapters/mock_oa.py —— Mock 外部系统实现（Demo 可离线运行）
# 业务：模拟现有 OA/费控：员工目录、验真、通知、邮件；全部留痕便于演示查看（docs/01 §6 Mock 适配器）
from datetime import datetime

from app.adapters.base import EmailProvider, NotifyProvider, UserProvider, VerifyProvider
from app.core.logging import get_logger, log_info

logger = get_logger("mock_oa")

# 业务：演示组织架构 —— 员工目录（真实系统从 OA 拉取）
#       结构：工号 → {姓名/部门/邮箱/直属上级}
DIRECTORY = {
    "1001": {"id": "1001", "name": "张三", "dept": "销售部", "email": "zhangsan@demo.com", "manager": "2001"},
    "2001": {"id": "2001", "name": "李四", "dept": "销售部", "email": "lisi@demo.com"},   # 直属上级（销售部经理）
    "2002": {"id": "2002", "name": "王五", "dept": "销售部", "email": "wangwu@demo.com"},  # 部门负责人
    "3001": {"id": "3001", "name": "赵六", "dept": "财务部", "email": "zhaoliu@demo.com"},  # 财务（出纳）
    "4001": {"id": "4001", "name": "孙七", "dept": "总经办", "email": "sunqi@demo.com"},    # 总经理
}

# 业务：审批角色 → 人员（与 policy/travel.yaml 审批链 role 名对应）
ROLE_MAP = {
    "直属上级": "2001",
    "部门负责人": "2002",
    "财务": "3001",
    "总经理": "4001",
}


class MockUserProvider(UserProvider):
    """Mock 组织架构查询"""

    def get_employee(self, employee_id: str) -> dict:
        emp = DIRECTORY.get(employee_id)
        if not emp:
            raise KeyError(f"员工不存在: {employee_id}")
        return emp

    def get_manager(self, employee_id: str) -> dict:
        # 业务：审批链第一级=直属上级
        emp = self.get_employee(employee_id)
        return DIRECTORY[emp["manager"]]

    def get_approver(self, role: str) -> dict:
        emp_id = ROLE_MAP.get(role)
        if not emp_id:
            raise KeyError(f"审批角色未配置: {role}")
        return DIRECTORY[emp_id]


class MockVerifyProvider(VerifyProvider):
    """Mock 验真：票号含 INVALID 视为验真失败，其余通过"""

    def verify(self, invoice_data: dict) -> dict:
        # 业务：真实实现对接税务查验平台 / 企业发票池查重；
        #      此处仅保留真伪规则化（票号含 INVALID 视为验真失败）。
        #      查重不再用 DUP 子串标记——已由发票池真库接管（verify 节点 add_invoice 唯一索引拦截，见 travel/nodes.py）
        invoice_no = str(invoice_data.get("invoice_no", ""))
        if "INVALID" in invoice_no:
            return {"verified": False, "duplicate": False, "note": "验真失败：票号疑似无效"}
        return {"verified": True, "duplicate": False, "note": "票面信息一致（真伪通过；查重走发票池）"}


class MockNotifyProvider(NotifyProvider):
    """Mock 通知：打印 + 落库留痕"""

    def __init__(self, storage):
        self._storage = storage

    def send(self, request_id: str, to_role: str, title: str, content: str) -> None:
        # 作用：结构化日志 + 写入 messages 表
        # 业务：真实实现调用钉钉/企微/站内信；留痕供驾驶舱/审核端展示
        log_info(logger, f"[通知] {to_role} | {title}\n    {content}", to_role=to_role, title=title)
        self._storage.add_message(request_id, to_role, f"{title}\n{content}")


class MockEmailProvider(EmailProvider):
    """Mock 邮件：打印 + 落库留痕"""

    def __init__(self, storage):
        self._storage = storage

    def send(self, request_id: str, to: str, subject: str, body: str) -> None:
        # 作用：结构化日志 + 写入 emails 表
        # 业务：真实实现走邮件网关
        log_info(logger, f"[邮件] → {to} | {subject}\n    {body}", to=to, subject=subject)
        self._storage.add_email(request_id, to, subject, body)
