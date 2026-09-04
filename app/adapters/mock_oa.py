# app/adapters/mock_oa.py —— Mock 外部系统实现（Demo 可离线运行）
# 业务：模拟现有 OA/费控：员工目录、验真、通知、邮件；全部留痕便于演示查看（docs/01 §6 Mock 适配器）
from datetime import datetime

from app.adapters.base import EmailProvider, NotifyProvider, UserProvider, VerifyProvider
from app.adapters.org import seed_org
from app.core.logging import get_logger, log_info

logger = get_logger("mock_oa")


class MockUserProvider(UserProvider):
    """Mock 组织架构查询（#27 去硬编码：数据源 org_data.yaml，构造时 seed 入库再从库加载）"""

    def __init__(self, storage):
        # 作用：每个容器（API/worker/测试）启动时把组织数据全量 seed 进表，再加载为查询缓存。
        #       源文件见 org_data.yaml（grade 与 policy/travel.yaml `grade_max_rail_seat` 键对应，#85 席别合规）。
        seed_org(storage)
        org = storage.load_org()
        self._directory = org["employees"]
        self._role_map = org["roles"]

    def get_employee(self, employee_id: str) -> dict:
        emp = self._directory.get(employee_id)
        if not emp:
            raise KeyError(f"员工不存在: {employee_id}")
        return emp

    def get_manager(self, employee_id: str) -> dict:
        # 业务：审批链第一级=直属上级
        emp = self.get_employee(employee_id)
        manager_id = emp.get("manager")
        if not manager_id or manager_id not in self._directory:
            raise KeyError(f"员工 {employee_id} 未配置直属上级")
        return self._directory[manager_id]

    def get_approver(self, role: str) -> dict:
        emp_id = self._role_map.get(role)
        if not emp_id:
            raise KeyError(f"审批角色未配置: {role}")
        emp = self._directory.get(emp_id)
        if not emp:
            raise KeyError(f"审批角色 {role} 指向的员工不存在: {emp_id}")
        return emp


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
