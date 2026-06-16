import pytest

from eqazyna_bitrix.reassign_crm_owner import (
    CLOSE_REASON_OPTIONS,
    DEFAULT_FAILURE_REASON_FIELD,
    close_reason_name,
    parse_close_reason_id,
)


def test_parse_close_reason_id_plain_value():
    assert parse_close_reason_id("394") == "394"
    assert close_reason_name("394") == "Клиент отказался"


def test_parse_close_reason_id_ui_friendly_value():
    assert parse_close_reason_id("388 - Уже работает с конкурентом") == "388"


def test_parse_close_reason_id_none_values():
    assert parse_close_reason_id("none") == ""
    assert parse_close_reason_id("") == ""


def test_close_reason_catalog_contains_expected_values():
    assert DEFAULT_FAILURE_REASON_FIELD == "UF_CRM_1779448756033"
    assert CLOSE_REASON_OPTIONS == {
        "400": "Дубль сделки",
        "394": "Клиент отказался",
        "402": "Не ведёт деятельность / компания неактивна",
        "386": "Не дозвонились",
        "396": "Не подходит по критериям",
        "392": "Нет данных организации",
        "398": "Ошибка данных",
        "404": "Проиграли аукцион",
        "390": "Уже работает с Евразией",
        "388": "Уже работает с конкурентом",
    }


def test_parse_close_reason_id_rejects_unknown_value():
    with pytest.raises(ValueError):
        parse_close_reason_id("999")

from eqazyna_bitrix.reassign_crm_owner import BitrixClient


class FakePagedBitrixClient(BitrixClient):
    def __init__(self):
        self.calls = []

    def call_full(self, method, params=None):
        params = list(params or [])
        self.calls.append((method, params))
        start = dict(params).get("start", 0)
        if start == 0:
            return {"result": [{"ID": "1"}], "next": 50}
        if start == 50:
            return {"result": [{"ID": "2"}]}
        raise AssertionError(f"unexpected start={start}")


def test_reassign_paged_list_uses_top_level_next():
    client = FakePagedBitrixClient()

    rows = client._paged_list("crm.deal.list", [("select[]", "ID")], limit=0)

    assert rows == [{"ID": "1"}, {"ID": "2"}]
    assert dict(client.calls[0][1])["start"] == 0
    assert dict(client.calls[1][1])["start"] == 50


def test_reassign_paged_list_respects_limit_across_pages():
    client = FakePagedBitrixClient()

    rows = client._paged_list("crm.deal.list", [("select[]", "ID")], limit=1)

    assert rows == [{"ID": "1"}]
    assert len(client.calls) == 1
