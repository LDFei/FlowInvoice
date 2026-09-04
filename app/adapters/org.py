# app/adapters/org.py —— 组织架构数据装载（#27 去硬编码）
# 业务：单一事实源 org_data.yaml → storage.seed_org() 幂等写入 employees/approver_roles 表。
#       每个新容器（API / worker / 测试 build_container）构造 MockUserProvider 时都会 seed 一次：
#       行数极少，先清后插重建开销可忽略，同时保证任何进程拿到的组织数据一致。
from pathlib import Path

import yaml

from app.core.logging import get_logger, log_info

logger = get_logger("org")

ORG_DATA_FILE = Path(__file__).with_name("org_data.yaml")


def load_org_data() -> dict:
    """读 YAML 源 → seed 形态：{"employees": {id: {...}}, "roles": {role: employee_id}}"""
    with open(ORG_DATA_FILE, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    employees = {str(e["id"]): e for e in raw.get("employees", [])}
    roles = {str(role): str(emp_id) for role, emp_id in (raw.get("approver_roles") or {}).items()}
    return {"employees": employees, "roles": roles}


def seed_org(storage) -> None:
    """幂等 seed：storage 必须实现 seed_org/load_org（#27 抽象已加入 StorageProvider）"""
    payload = load_org_data()
    storage.seed_org(payload)
    log_info(logger, f"组织数据 seed 完成（employees={len(payload['employees'])} roles={len(payload['roles'])}）")
