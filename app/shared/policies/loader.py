# app/shared/policies/loader.py —— 政策规则加载
# 业务：一个业务一个 YAML；配置驱动，改制度不改代码（docs/AGENTS.md §9）
from functools import lru_cache
from pathlib import Path

import yaml


class PolicyLoader:
    """按业务方向加载政策规则"""

    def __init__(self, policy_dir: Path):
        # 作用：指定政策 YAML 目录（见 core/config.POLICY_DIR）
        self._policy_dir = policy_dir

    @lru_cache(maxsize=16)
    def load(self, direction: str) -> dict:
        # 作用：读 policy/<direction>.yaml 并解析为 dict；带缓存避免重复 IO
        # 业务：差旅读 travel.yaml，采购读 procurement.yaml……规则外置
        path = self._policy_dir / f"{direction}.yaml"
        if not path.exists():
            raise FileNotFoundError(f"未找到政策规则: {path}")
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
