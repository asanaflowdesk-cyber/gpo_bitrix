from eqazyna_bitrix.audit_repair_deal_packages import choose_target, initial_active_deal_load
from eqazyna_bitrix.config.assignment import parse_stage_ids
from eqazyna_bitrix.distribute_companies import ALLOWED_USER_IDS


def test_audit_active_deal_load_uses_configured_stages_only():
    manager_id = ALLOWED_USER_IDS[0]
    records = [
        {"entity_type": "deal", "owner_id": manager_id, "stage_id": "NEW", "closed": False},
        {"entity_type": "deal", "owner_id": manager_id, "stage_id": "C2:EXECUTING", "closed": False},
        {"entity_type": "deal", "owner_id": manager_id, "stage_id": "PREPARATION", "closed": False},
        {"entity_type": "deal", "owner_id": manager_id, "stage_id": "LOSE", "closed": True},
        {"entity_type": "company", "owner_id": manager_id, "stage_id": "NEW", "closed": False},
    ]

    load = initial_active_deal_load(records, parse_stage_ids("NEW,EXECUTING"))

    assert load[manager_id] == 2


def test_audit_choose_target_uses_deal_history_over_limits():
    records = [
        {
            "entity_type": "deal",
            "entity_id": "10",
            "owner_id": 74,
            "entity": {"DATE_CREATE": "2026-01-01T00:00:00+00:00"},
        },
        {
            "entity_type": "deal",
            "entity_id": "11",
            "owner_id": 92,
            "entity": {"DATE_CREATE": "2026-02-01T00:00:00+00:00"},
        },
    ]
    client_load = {user_id: 0 for user_id in ALLOWED_USER_IDS}
    active_deal_load = {user_id: 99 for user_id in ALLOWED_USER_IDS}

    target, reason, debug = choose_target(records, client_load, active_deal_load, {36, 44}, 0, 30)

    assert target == 74
    assert reason == "historical_first_deal_owner"
    assert debug["limits_applied"] is False


def test_audit_choose_target_ignores_company_owner_without_deal_history():
    records = [
        {
            "entity_type": "company",
            "entity_id": "100",
            "owner_id": 74,
            "entity": {"DATE_CREATE": "2026-01-01T00:00:00+00:00"},
        }
    ]
    client_load = {user_id: 0 for user_id in ALLOWED_USER_IDS}
    active_deal_load = {user_id: 0 for user_id in ALLOWED_USER_IDS}
    active_deal_load[74] = 5

    target, reason, debug = choose_target(records, client_load, active_deal_load, {36, 44}, 0, 30)

    assert target != 74
    assert reason == "new_package_lowest_active_deal_load"
    assert debug["limits_applied"] is True


def test_audit_choose_target_ignores_technical_deal_owner():
    records = [
        {
            "entity_type": "deal",
            "entity_id": "10",
            "owner_id": 36,
            "entity": {"DATE_CREATE": "2026-01-01T00:00:00+00:00"},
        }
    ]
    client_load = {user_id: 0 for user_id in ALLOWED_USER_IDS}
    active_deal_load = {user_id: 0 for user_id in ALLOWED_USER_IDS}

    target, reason, debug = choose_target(records, client_load, active_deal_load, {36, 44}, 0, 30)

    assert target in set(ALLOWED_USER_IDS)
    assert reason == "new_package_lowest_active_deal_load"
    assert debug["limits_applied"] is True
