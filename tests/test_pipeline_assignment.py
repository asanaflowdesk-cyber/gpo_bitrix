from eqazyna_bitrix.models import Application, CompanyEnrichment
from eqazyna_bitrix.pipeline import BitrixPipeline, BitrixPipelineConfig
from eqazyna_bitrix.distribute_companies import ALLOWED_USER_IDS


class FakeClient:
    def __init__(self, companies=None):
        self.companies = companies or []

    def list_all(self, method, payload):
        return list(self.companies)

    def list_eqazyna_deals(self):
        return []


def _pipe(assigned_by_id: str | None = "36", companies=None) -> BitrixPipeline:
    return BitrixPipeline(client=FakeClient(companies), config=BitrixPipelineConfig(assigned_by_id=assigned_by_id))  # type: ignore[arg-type]


def _app(bin_value="123456789012") -> Application:
    return Application(
        created_at_raw="2026-05-26",
        doc_number="123",
        bin=bin_value,
        applicant_name="Тестовая компания",
        doc_type="Заявка на разведку ТПИ",
        status="Принято",
        source_url="https://example.test",
    )


def test_existing_company_owner_has_priority_over_source_fallback():
    pipe = _pipe("36")
    target, reason = pipe._resolve_target_responsible(_app(), CompanyEnrichment(bin="123456789012"), {"ASSIGNED_BY_ID": "70"})

    assert target == 70
    assert reason == "existing_company_owner_seeds_director_package"


def test_source_owner_is_not_used_as_fallback_for_new_package():
    pipe = _pipe("36", companies=[])
    target, reason = pipe._resolve_target_responsible(_app(), CompanyEnrichment(bin="123456789012"), {"ASSIGNED_BY_ID": "36"})

    assert target in set(ALLOWED_USER_IDS)
    assert reason in {"random_lowest_load_new_package", "random_lowest_load_limit_expanded"}


def test_hard_bin_owner_overrides_existing_non_hard_owner():
    pipe = _pipe("36")
    target, reason = pipe._resolve_target_responsible(_app("260540008322"), CompanyEnrichment(bin="260540008322"), {"ASSIGNED_BY_ID": "36"})

    assert target == 100
    assert reason == "hard_bin_owner"


def test_director_package_owner_is_reused_for_new_bin():
    companies = [
        {
            "ID": "1",
            "ASSIGNED_BY_ID": "74",
            "COMMENTS": "Руководитель: ИВАНОВ ИВАН ИВАНОВИЧ",
        }
    ]
    pipe = _pipe("36", companies=companies)
    target, reason = pipe._resolve_target_responsible(_app(), CompanyEnrichment(bin="123456789012", director="ИВАНОВ ИВАН ИВАНОВИЧ"), None)

    assert target == 74
    assert reason == "historical_first_company_owner"


def test_deal_fields_use_explicit_responsible_id():
    pipe = _pipe("36")
    app = _app()
    enrichment = CompanyEnrichment(bin=app.bin, name=app.applicant_name)

    fields = pipe._deal_fields(app, enrichment, "555", responsible_id=70)

    assert fields["ASSIGNED_BY_ID"] == 70

class FakeClientWithDeals(FakeClient):
    def __init__(self, companies=None, deals=None):
        super().__init__(companies)
        self.deals = deals or []

    def list_eqazyna_deals(self):
        return list(self.deals)


def test_new_deal_inherits_failed_stage_and_reason_by_director():
    companies = [
        {
            "ID": "10",
            "ASSIGNED_BY_ID": "74",
            "COMMENTS": "Руководитель: ИВАНОВ ИВАН ИВАНОВИЧ",
        }
    ]
    deals = [
        {
            "ID": "99",
            "TITLE": "Старая проваленная сделка",
            "COMPANY_ID": "10",
            "STAGE_ID": "LOSE",
            "STAGE_SEMANTIC_ID": "F",
            "COMMENTS": "Причина отказа: нецелевой клиент",
            "UF_CRM_1779448756033": "388",
        }
    ]
    pipe = BitrixPipeline(
        client=FakeClientWithDeals(companies, deals),  # type: ignore[arg-type]
        config=BitrixPipelineConfig(assigned_by_id="36"),
    )
    app = _app("222222222222")
    enrichment = CompanyEnrichment(bin=app.bin, name=app.applicant_name, director="ИВАНОВ ИВАН ИВАНОВИЧ")

    inheritance = pipe._failed_deal_inheritance_for_director(enrichment.director)
    fields = pipe._deal_fields(app, enrichment, "555", responsible_id=74, failed_inheritance=inheritance)

    assert inheritance is not None
    assert inheritance.stage_id == "LOSE"
    assert inheritance.source_deal_id == "99"
    assert fields["STAGE_ID"] == "LOSE"
    assert fields["CLOSED"] == "Y"
    assert inheritance.reason == "388"
    assert fields["UF_CRM_1779448756033"] == "388"
    assert "Наследованная причина: 388" in str(fields["COMMENTS"])


class FakeClientWithContactsAndDeals(FakeClientWithDeals):
    def __init__(self, companies=None, deals=None, contact=None):
        super().__init__(companies, deals)
        self.contact = contact

    def find_contact_by_fio(self, last_name, name, second_name=""):
        return self.contact

    def find_contact_by_director_alias(self, director_raw):
        return self.contact


def test_director_package_owner_overrides_existing_company_owner():
    companies = [
        {
            "ID": "1",
            "ASSIGNED_BY_ID": "74",
            "COMMENTS": "Руководитель: ИВАНОВ ИВАН ИВАНОВИЧ",
        }
    ]
    pipe = _pipe("36", companies=companies)

    target, reason = pipe._resolve_target_responsible(
        _app("222222222222"),
        CompanyEnrichment(bin="222222222222", director="ИВАНОВ ИВАН ИВАНОВИЧ"),
        {"ASSIGNED_BY_ID": "92"},
    )

    assert target == 74
    assert reason == "historical_first_company_owner"


def test_director_contact_owner_is_canonical_for_new_deal():
    contact = {
        "ID": "500",
        "LAST_NAME": "ИВАНОВ",
        "NAME": "ИВАН",
        "SECOND_NAME": "ИВАНОВИЧ",
        "ASSIGNED_BY_ID": "92",
    }
    pipe = BitrixPipeline(
        client=FakeClientWithContactsAndDeals(contact=contact),  # type: ignore[arg-type]
        config=BitrixPipelineConfig(assigned_by_id="36"),
    )

    target, reason = pipe._resolve_target_responsible(
        _app("333333333333"),
        CompanyEnrichment(bin="333333333333", director="ИВАНОВ ИВАН ИВАНОВИЧ"),
        {"ASSIGNED_BY_ID": "74"},
    )

    assert target == 92
    assert reason == "existing_director_contact_owner"


def test_split_director_package_uses_oldest_company_owner_when_no_deals_exist():
    companies = [
        {
            "ID": "1",
            "DATE_CREATE": "2026-01-01T00:00:00+00:00",
            "ASSIGNED_BY_ID": "74",
            "COMMENTS": "Руководитель: ИВАНОВ ИВАН ИВАНОВИЧ",
        },
        {
            "ID": "2",
            "DATE_CREATE": "2026-02-01T00:00:00+00:00",
            "ASSIGNED_BY_ID": "92",
            "COMMENTS": "Руководитель: ИВАНОВ ИВАН ИВАНОВИЧ",
        },
    ]
    pipe = _pipe("36", companies=companies)

    target, reason = pipe._resolve_target_responsible(
        _app("444444444444"),
        CompanyEnrichment(bin="444444444444", director="ИВАНОВ ИВАН ИВАНОВИЧ"),
        None,
    )

    assert target == 74
    assert reason == "historical_first_company_owner"


def test_oldest_deal_owner_wins_over_newer_deals_and_contact_owner():
    companies = [
        {
            "ID": "1",
            "DATE_CREATE": "2026-01-01T00:00:00+00:00",
            "ASSIGNED_BY_ID": "92",
            "COMMENTS": "Руководитель: ИВАНОВ ИВАН ИВАНОВИЧ",
        }
    ]
    deals = [
        {
            "ID": "10",
            "DATE_CREATE": "2026-01-02T00:00:00+00:00",
            "COMPANY_ID": "1",
            "ASSIGNED_BY_ID": "74",
            "COMMENTS": "Руководитель: ИВАНОВ ИВАН ИВАНОВИЧ",
        },
        {
            "ID": "11",
            "DATE_CREATE": "2026-02-02T00:00:00+00:00",
            "COMPANY_ID": "1",
            "ASSIGNED_BY_ID": "92",
            "COMMENTS": "Руководитель: ИВАНОВ ИВАН ИВАНОВИЧ",
        },
        {
            "ID": "12",
            "DATE_CREATE": "2026-03-02T00:00:00+00:00",
            "COMPANY_ID": "1",
            "ASSIGNED_BY_ID": "92",
            "COMMENTS": "Руководитель: ИВАНОВ ИВАН ИВАНОВИЧ",
        },
    ]
    contact = {
        "ID": "500",
        "LAST_NAME": "ИВАНОВ",
        "NAME": "ИВАН",
        "SECOND_NAME": "ИВАНОВИЧ",
        "ASSIGNED_BY_ID": "92",
    }
    pipe = BitrixPipeline(
        client=FakeClientWithContactsAndDeals(companies, deals, contact),  # type: ignore[arg-type]
        config=BitrixPipelineConfig(assigned_by_id="36"),
    )

    target, reason = pipe._resolve_target_responsible(
        _app("777777777777"),
        CompanyEnrichment(bin="777777777777", director="ИВАНОВ ИВАН ИВАНОВИЧ"),
        None,
    )

    assert target == 74
    assert reason == "historical_first_deal_owner"


def test_runtime_cache_rejects_two_managers_for_one_director():
    pipe = _pipe("36", companies=[])
    director = "ИВАНОВ ИВАН ИВАНОВИЧ"
    pipe._remember_assignment("555555555555", director, 74, "test")

    try:
        pipe._remember_assignment("666666666666", director, 92, "test")
    except Exception as exc:
        assert "DIRECTOR_PACKAGE_RUNTIME_CONFLICT" in str(exc)
    else:
        raise AssertionError("runtime cache must reject split director assignment")


def test_hard_bin_cannot_split_existing_director_package():
    companies = [
        {
            "ID": "1",
            "ASSIGNED_BY_ID": "74",
            "COMMENTS": "Руководитель: ИВАНОВ ИВАН ИВАНОВИЧ",
        }
    ]
    pipe = _pipe("36", companies=companies)

    try:
        pipe._resolve_target_responsible(
            _app("260540008322"),
            CompanyEnrichment(bin="260540008322", director="ИВАНОВ ИВАН ИВАНОВИЧ"),
            None,
        )
    except Exception as exc:
        assert "DIRECTOR_HARD_BIN_OWNER_CONFLICT" in str(exc)
    else:
        raise AssertionError("hard BIN must not silently split a director package")
