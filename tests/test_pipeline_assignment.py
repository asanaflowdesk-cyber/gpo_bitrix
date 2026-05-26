from eqazyna_bitrix.models import Application, CompanyEnrichment
from eqazyna_bitrix.pipeline import BitrixPipeline, BitrixPipelineConfig
from eqazyna_bitrix.distribute_companies import ALLOWED_USER_IDS


class FakeClient:
    def __init__(self, companies=None):
        self.companies = companies or []

    def list_all(self, method, payload):
        return list(self.companies)


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
    assert reason == "existing_company_owner"


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
    assert reason == "existing_director_package_owner"


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
