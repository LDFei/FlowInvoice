# tests/test_policy.py —— 政策规则加载
from app.shared.policies.errors import UnknownBusinessError
from app.shared.policies.loader import PolicyLoader


def test_load_travel_policy(tmp_path):
    # 作用：travel.yaml 可加载，审批链规则存在
    from app.core.config import POLICY_DIR
    policy = PolicyLoader(POLICY_DIR).load("travel")
    assert policy["advance_required"] is True
    assert policy["hotel_daily_limit"] == 500
    assert policy["advance_valid_days"] == 30
    assert len(policy["approval_rules"]) == 2


def test_unknown_business_error_message():
    err = UnknownBusinessError("crypto")
    assert "crypto" in str(err)
