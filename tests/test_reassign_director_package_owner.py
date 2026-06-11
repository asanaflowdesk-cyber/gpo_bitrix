from __future__ import annotations

import pytest

from eqazyna_bitrix.reassign_director_package_owner import build_package_selection, director_input_keys, name_token_count


class FakeBitrix:
    def __init__(self):
        self.companies = {
            "10": {
                "ID": "10",
                "TITLE": "ТОО Ромашка",
                "ASSIGNED_BY_ID": "36",
                "COMMENTS": "Источник: e-Qazyna\nРуководитель: ЛЯБАХ ГЕННАДИЙ",
                "ORIGINATOR_ID": "EQAZYNA",
                "ORIGIN_ID": "123456789012",
            }
        }
        self.contacts = {
            "5": {"ID": "5", "LAST_NAME": "ЛЯБАХ", "NAME": "ГЕННАДИЙ", "ASSIGNED_BY_ID": "44"}
        }
        self.deals = [
            {
                "ID": "101",
                "TITLE": "e-Qazyna № A1",
                "ASSIGNED_BY_ID": "36",
                "COMPANY_ID": "10",
                "CONTACT_ID": "",
                "COMMENTS": "",
                "ORIGINATOR_ID": "EQAZYNA",
                "ORIGIN_ID": "app-1",
                "CLOSED": "N",
            },
            {
                "ID": "102",
                "TITLE": "e-Qazyna № A2",
                "ASSIGNED_BY_ID": "44",
                "COMPANY_ID": "10",
                "CONTACT_ID": "",
                "COMMENTS": "",
                "ORIGINATOR_ID": "EQAZYNA",
                "ORIGIN_ID": "app-2",
                "CLOSED": "Y",
            },
        ]

    def list_companies(self, *, only_eqazyna: bool, limit: int = 0):
        return list(self.companies.values())

    def list_deals(self, *, category_id: str, only_eqazyna: bool, include_closed: bool, limit: int = 0):
        return list(self.deals) if include_closed else [deal for deal in self.deals if deal.get("CLOSED") != "Y"]

    def get_company(self, company_id):
        return self.companies.get(str(company_id), {})

    def get_contact(self, contact_id):
        return self.contacts.get(str(contact_id), {})

    def deal_contact_ids(self, deal_id):
        return set()

    def company_contact_ids(self, company_id):
        return {"5"} if str(company_id) == "10" else set()

    def find_contacts_by_director_alias(self, director_name):
        return [self.contacts["5"]]


def test_director_input_accepts_initial_alias():
    assert name_token_count("ЛЯБАХ Г.Г.") >= 2
    assert director_input_keys("ЛЯБАХ Г.Г.")


def test_build_package_by_director_moves_whole_company_package_without_source_owner():
    bx = FakeBitrix()
    package = build_package_selection(
        bx=bx,
        director_name="ЛЯБАХ Г.Г.",
        only_eqazyna=True,
        include_closed_deals=True,
        deal_category_id="all",
        include_companies=True,
        include_contacts=True,
        include_deals=True,
        include_orphan_companies=True,
        include_company_contacts=True,
        include_matching_director_contacts=True,
    )

    assert set(package.companies) == {"10"}
    assert set(package.deals) == {"101", "102"}
    assert set(package.contacts) == {"5"}
    assert package.companies["10"]["ASSIGNED_BY_ID"] == "36"
    assert package.deals["102"]["ASSIGNED_BY_ID"] == "44"


def test_build_package_can_exclude_closed_deals():
    bx = FakeBitrix()
    package = build_package_selection(
        bx=bx,
        director_name="ЛЯБАХ ГЕННАДИЙ",
        only_eqazyna=True,
        include_closed_deals=False,
        deal_category_id="all",
        include_companies=True,
        include_contacts=False,
        include_deals=True,
        include_orphan_companies=True,
        include_company_contacts=False,
        include_matching_director_contacts=False,
    )

    assert set(package.deals) == {"101"}

from eqazyna_bitrix.reassign_director_package_owner import Bitrix


class FakeUserBitrix(Bitrix):
    def __init__(self, result):
        self.result = result
        self._user_cache = {}
        self._user_object_cache = {}

    def call(self, method, payload=None):
        assert method == "user.get"
        return self.result


def test_target_user_can_be_any_existing_bitrix_user_not_only_managers_file():
    bx = FakeUserBitrix([{"ID": "777", "NAME": "Любой", "LAST_NAME": "Пользователь", "ACTIVE": True}])
    user = bx.validate_target_user_exists("777")

    assert user["ID"] == "777"
    assert bx.get_user_name("777") == "Пользователь Любой"


def test_target_user_must_exist_in_bitrix():
    bx = FakeUserBitrix([])

    with pytest.raises(ValueError, match="was not found in Bitrix"):
        bx.validate_target_user_exists("777")
