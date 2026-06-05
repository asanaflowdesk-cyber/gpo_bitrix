from eqazyna_bitrix.distribute_companies import (
    _company_group_key,
    _parse_source_responsible_ids,
)


def test_parse_source_responsible_ids_accepts_comma_separated_values():
    assert _parse_source_responsible_ids("36,44", 36) == {36, 44}


def test_parse_source_responsible_ids_falls_back_to_legacy_id_when_empty():
    assert _parse_source_responsible_ids("", 36) == {36}


def test_company_group_key_uses_director_from_company_comment_first():
    key, group_type, readable = _company_group_key(
        {"ID": "10", "TITLE": "Тест", "ORIGIN_ID": "123456789012", "COMMENTS": "Руководитель: Иванов Иван Иванович"},
        [{"ID": "20", "COMMENTS": "Руководитель: Петров Петр Петрович"}],
    )

    assert group_type == "director"
    assert key.startswith("director|")
    assert readable == "Иванов Иван Иванович"


def test_company_group_key_uses_director_from_related_deal_when_company_comment_is_empty():
    key, group_type, readable = _company_group_key(
        {"ID": "10", "TITLE": "Тест", "ORIGIN_ID": "123456789012", "COMMENTS": ""},
        [
            {"ID": "20", "DATE_CREATE": "2026-02-01", "COMMENTS": "Руководитель: Петров Петр Петрович"},
            {"ID": "19", "DATE_CREATE": "2026-01-01", "COMMENTS": "Руководитель: Иванов Иван Иванович"},
        ],
    )

    assert group_type == "director"
    assert key.startswith("director|")
    assert readable == "Иванов Иван Иванович"
