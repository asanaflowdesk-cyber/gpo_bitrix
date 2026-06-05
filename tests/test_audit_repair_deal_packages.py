from eqazyna_bitrix.audit_repair_deal_packages import initial_active_deal_load
from eqazyna_bitrix.config.assignment import parse_stage_ids


def test_audit_active_deal_load_uses_configured_stages_only():
    records = [
        {"entity_type": "deal", "owner_id": 70, "stage_id": "NEW", "closed": False},
        {"entity_type": "deal", "owner_id": 70, "stage_id": "C2:EXECUTING", "closed": False},
        {"entity_type": "deal", "owner_id": 70, "stage_id": "PREPARATION", "closed": False},
        {"entity_type": "deal", "owner_id": 70, "stage_id": "LOSE", "closed": True},
        {"entity_type": "company", "owner_id": 70, "stage_id": "NEW", "closed": False},
    ]

    load = initial_active_deal_load(records, parse_stage_ids("NEW,EXECUTING"))

    assert load[70] == 2
