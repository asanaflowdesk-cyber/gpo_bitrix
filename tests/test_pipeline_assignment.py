from eqazyna_bitrix.models import Application, CompanyEnrichment
from eqazyna_bitrix.pipeline import BitrixPipeline, BitrixPipelineConfig


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

    assert target in {70, 72, 74, 76, 78, 80, 82, 84, 86, 88, 90, 92, 94, 98, 100, 102}
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
