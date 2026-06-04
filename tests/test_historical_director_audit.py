from eqazyna_bitrix.audit_repair_director_package_owners import choose_historical_target


def test_historical_audit_uses_oldest_deal_owner_not_majority():
    deals = [
        {"ID": "10", "DATE_CREATE": "2026-01-01T00:00:00+00:00", "ASSIGNED_BY_ID": "74"},
        {"ID": "11", "DATE_CREATE": "2026-02-01T00:00:00+00:00", "ASSIGNED_BY_ID": "92"},
        {"ID": "12", "DATE_CREATE": "2026-03-01T00:00:00+00:00", "ASSIGNED_BY_ID": "92"},
    ]
    target, reason, conflict, evidence = choose_historical_target(
        ["ИВАНОВ ИВАН ИВАНОВИЧ"], deals, {}, {}, {"36", "44"}, {}
    )

    assert target == "74"
    assert reason == "historical_first_deal_owner"
    assert conflict == ""
    assert evidence["historical_entity_id"] == "10"


def test_manual_director_owner_overrides_historical_deal_owner():
    deals = [
        {"ID": "10", "DATE_CREATE": "2026-01-01T00:00:00+00:00", "ASSIGNED_BY_ID": "74"},
    ]
    target, reason, _, _ = choose_historical_target(
        ["ТАТАЕВ НУРЛАН САПАРАЛИЕВИЧ"],
        deals,
        {},
        {},
        {"36", "44"},
        {"surname_first_patronymic|ТАТАЕВ|НУРЛАН|САПАРАЛИЕВИЧ": "78"},
    )

    assert target == "78"
    assert reason == "manual_director_owner"
