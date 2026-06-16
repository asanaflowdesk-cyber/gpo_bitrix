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


def test_existing_company_owner_does_not_seed_new_director_package():
    pipe = _pipe("36")
    target, reason = pipe._resolve_target_responsible(_app(), CompanyEnrichment(bin="123456789012"), {"ASSIGNED_BY_ID": "70"})

    assert target in set(ALLOWED_USER_IDS)
    assert reason in {"lowest_active_deal_load_new_director", "lowest_active_deal_load_limit_expanded"}


def test_source_owner_is_not_used_as_fallback_for_new_package():
    pipe = _pipe("36", companies=[])
    target, reason = pipe._resolve_target_responsible(_app(), CompanyEnrichment(bin="123456789012"), {"ASSIGNED_BY_ID": "36"})

    assert target in set(ALLOWED_USER_IDS)
    assert reason in {"lowest_active_deal_load_new_director", "lowest_active_deal_load_limit_expanded"}


def test_new_director_package_uses_load_balancing_for_any_bin():
    pipe = _pipe("36")
    target, reason = pipe._resolve_target_responsible(_app("260540008322"), CompanyEnrichment(bin="260540008322"), {"ASSIGNED_BY_ID": "36"})

    assert target in set(ALLOWED_USER_IDS)
    assert reason in {"lowest_active_deal_load_new_director", "lowest_active_deal_load_limit_expanded"}


def test_deal_history_owner_is_reused_for_new_bin():
    companies = [
        {
            "ID": "1",
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
            "COMMENTS": "",
        }
    ]
    pipe = BitrixPipeline(
        client=FakeClientWithDeals(companies, deals),  # type: ignore[arg-type]
        config=BitrixPipelineConfig(assigned_by_id="36"),
    )
    target, reason = pipe._resolve_target_responsible(_app(), CompanyEnrichment(bin="123456789012", director="ИВАНОВ ИВАН ИВАНОВИЧ"), None)

    assert target == 74
    assert reason == "historical_first_deal_owner"


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


def test_company_owner_does_not_override_missing_deal_history():
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

    assert target in set(ALLOWED_USER_IDS)
    assert reason in {"lowest_active_deal_load_new_director", "lowest_active_deal_load_limit_expanded"}


def test_director_contact_owner_is_not_automatic_history():
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

    assert target in set(ALLOWED_USER_IDS)
    assert reason in {"lowest_active_deal_load_new_director", "lowest_active_deal_load_limit_expanded"}


def test_company_only_package_uses_load_when_no_deal_history_exists():
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

    assert target in set(ALLOWED_USER_IDS)
    assert reason in {"lowest_active_deal_load_new_director", "lowest_active_deal_load_limit_expanded"}


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


def test_bin_does_not_use_company_only_history_as_director_anchor():
    companies = [
        {
            "ID": "1",
            "ASSIGNED_BY_ID": "74",
            "COMMENTS": "Руководитель: ИВАНОВ ИВАН ИВАНОВИЧ",
        }
    ]
    pipe = _pipe("36", companies=companies)

    target, reason = pipe._resolve_target_responsible(
        _app("260540008322"),
        CompanyEnrichment(bin="260540008322", director="ИВАНОВ ИВАН ИВАНОВИЧ"),
        None,
    )

    assert target in set(ALLOWED_USER_IDS)
    assert reason in {"lowest_active_deal_load_new_director", "lowest_active_deal_load_limit_expanded"}


class FakeClientExistingDeal(FakeClient):
    def __init__(self):
        super().__init__([])
        self.updated = False
        self.created = False

    def find_deal_by_origin(self, deal_key):
        return {"ID": "900", "COMPANY_ID": "800", "ASSIGNED_BY_ID": "74"}

    def find_company_by_origin(self, bin_number):
        raise AssertionError("company must not be searched when deal already exists")

    def find_company_by_requisite_bin(self, bin_number, bin_field="RQ_BIN"):
        raise AssertionError("company must not be searched when deal already exists")

    def update_company(self, *args, **kwargs):
        self.updated = True
        raise AssertionError("existing deal must not update company")

    def update_deal(self, *args, **kwargs):
        self.updated = True
        raise AssertionError("existing deal must not update deal")

    def create_deal(self, *args, **kwargs):
        self.created = True
        raise AssertionError("existing deal must not create deal")


def test_existing_deal_is_skipped_without_any_updates():
    client = FakeClientExistingDeal()
    pipe = BitrixPipeline(client=client, config=BitrixPipelineConfig(assigned_by_id="36"))  # type: ignore[arg-type]

    result = pipe.process(_app(), CompanyEnrichment(bin="123456789012"))

    assert result.action == "existing_deal_skipped"
    assert result.deal_id == "900"
    assert result.company_id == "800"
    assert result.assigned_by_id == 74
    assert not client.updated
    assert not client.created


def test_manager_load_counts_only_configured_limit_stages():
    deals = [
        {"ID": "1", "ASSIGNED_BY_ID": "70", "STAGE_ID": "NEW", "CLOSED": "N"},
        {"ID": "2", "ASSIGNED_BY_ID": "70", "STAGE_ID": "C2:EXECUTING", "CLOSED": "N"},
        {"ID": "3", "ASSIGNED_BY_ID": "70", "STAGE_ID": "PREPARATION", "CLOSED": "N"},
        {"ID": "4", "ASSIGNED_BY_ID": "70", "STAGE_ID": "LOSE", "CLOSED": "Y"},
    ]
    pipe = BitrixPipeline(
        client=FakeClientWithDeals(deals=deals),  # type: ignore[arg-type]
        config=BitrixPipelineConfig(assigned_by_id="36", assignment_load_stage_ids="NEW,EXECUTING"),
    )

    assert pipe._manager_load(70) == 2


def test_projected_load_ignores_new_deal_outside_limit_stages():
    pipe = BitrixPipeline(
        client=FakeClientWithDeals(deals=[]),  # type: ignore[arg-type]
        config=BitrixPipelineConfig(
            assigned_by_id="36",
            deal_stage_id="PREPARATION",
            assignment_load_stage_ids="NEW,EXECUTING",
        ),
    )

    pipe._remember_new_package_load(70, "lowest_active_deal_load_new_director")

    assert pipe._projected_load_delta[70] == 0


def test_projected_load_counts_every_new_runtime_cached_deal():
    pipe = BitrixPipeline(
        client=FakeClientWithDeals(deals=[]),  # type: ignore[arg-type]
        config=BitrixPipelineConfig(
            assigned_by_id="36",
            deal_stage_id="NEW",
            assignment_load_stage_ids="NEW,EXECUTING",
        ),
    )

    pipe._remember_created_deal_load(70)
    pipe._remember_created_deal_load(70)
    pipe._remember_created_deal_load(70)

    assert pipe._projected_load_delta[70] == 3
    assert pipe._effective_manager_load(70) == 3


def test_failed_deal_inheritance_matches_director_from_deal_comment_without_company_comment():
    deals = [
        {
            "ID": "101",
            "TITLE": "Провал по комментарию сделки",
            "COMPANY_ID": "999",
            "STAGE_ID": "LOSE",
            "STAGE_SEMANTIC_ID": "F",
            "COMMENTS": "Руководитель: ИВАНОВ ИВАН ИВАНОВИЧ\nПричина: отказ",
            "UF_CRM_1779448756033": "394",
        }
    ]
    pipe = BitrixPipeline(
        client=FakeClientWithDeals(companies=[], deals=deals),  # type: ignore[arg-type]
        config=BitrixPipelineConfig(assigned_by_id="36"),
    )

    inheritance = pipe._failed_deal_inheritance_for_director("ИВАНОВ ИВАН ИВАНОВИЧ")

    assert inheritance is not None
    assert inheritance.source_deal_id == "101"
    assert inheritance.stage_id == "LOSE"
    assert inheritance.reason == "394"


def test_failed_deal_inheritance_matches_prefixed_failed_stage_id_without_semantic_flag():
    deals = [
        {
            "ID": "102",
            "TITLE": "Провал с C2:LOSE",
            "COMPANY_ID": "999",
            "STAGE_ID": "C2:LOSE",
            "COMMENTS": "Руководитель: ИВАНОВ ИВАН ИВАНОВИЧ",
            "UF_CRM_1779448756033": "394",
        }
    ]
    pipe = BitrixPipeline(
        client=FakeClientWithDeals(companies=[], deals=deals),  # type: ignore[arg-type]
        config=BitrixPipelineConfig(assigned_by_id="36", failed_deal_stage_ids="LOSE"),
    )

    inheritance = pipe._failed_deal_inheritance_for_director("ИВАНОВ ИВАН ИВАНОВИЧ")

    assert inheritance is not None
    # Old category-prefixed failed stages may be recognized as history,
    # but new deals must be written into the plain configured LOSE stage.
    assert inheritance.stage_id == "LOSE"
    assert inheritance.source_stage_id == "C2:LOSE"
    assert inheritance.source_deal_id == "102"


def test_default_assignment_load_counts_only_new_and_in_work():
    deals = [
        {"ID": "1", "ASSIGNED_BY_ID": "70", "STAGE_ID": "NEW", "CLOSED": "N"},
        {"ID": "2", "ASSIGNED_BY_ID": "70", "STAGE_ID": "C2:EXECUTING", "CLOSED": "N"},
        {"ID": "3", "ASSIGNED_BY_ID": "70", "STAGE_ID": "PREPARATION", "CLOSED": "N"},
        {"ID": "4", "ASSIGNED_BY_ID": "70", "STAGE_ID": "LOSE", "CLOSED": "Y"},
    ]
    pipe = BitrixPipeline(
        client=FakeClientWithDeals(deals=deals),  # type: ignore[arg-type]
        config=BitrixPipelineConfig(assigned_by_id="36"),
    )

    assert pipe._manager_load(70) == 2


def test_lowest_loaded_owner_is_deterministic_and_prefers_real_minimum():
    expected_target = ALLOWED_USER_IDS[-1]
    deals = []
    for user_id in ALLOWED_USER_IDS:
        if user_id == expected_target:
            continue
        deals.append({"ID": str(user_id), "ASSIGNED_BY_ID": str(user_id), "STAGE_ID": "NEW", "CLOSED": "N"})
    pipe = BitrixPipeline(
        client=FakeClientWithDeals(deals=deals),  # type: ignore[arg-type]
        config=BitrixPipelineConfig(assigned_by_id="36", assignment_limit_per_manager=30),
    )

    target, reason = pipe._lowest_loaded_owner_from_current_load()

    assert target == expected_target
    assert reason == "lowest_active_deal_load_new_director"
