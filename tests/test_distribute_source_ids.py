from eqazyna_bitrix.distribute_companies import (
    _choose_existing_allowed_owner,
    _parse_source_responsible_ids,
)


def test_parse_source_responsible_ids_accepts_comma_separated_values():
    assert _parse_source_responsible_ids("36,44", 36) == {36, 44}


def test_parse_source_responsible_ids_falls_back_to_legacy_id_when_empty():
    assert _parse_source_responsible_ids("", 36) == {36}


def test_existing_owner_ignores_all_source_responsible_ids():
    companies = [
        {"ASSIGNED_BY_ID": "36"},
        {"ASSIGNED_BY_ID": "44"},
        {"ASSIGNED_BY_ID": "70"},
    ]
    load = {70: 0}

    assert _choose_existing_allowed_owner(companies, {36, 44}, load) == 70


def test_package_with_only_second_source_id_has_no_existing_allowed_owner():
    companies = [{"ASSIGNED_BY_ID": "44"}]
    load = {70: 0}

    assert _choose_existing_allowed_owner(companies, {36, 44}, load) is None
