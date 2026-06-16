from types import SimpleNamespace

from eqazyna_bitrix.models import Application, CompanyEnrichment, ProcessResult
import eqazyna_bitrix.main as main_module


class FakeSettings:
    request_timeout = 1
    polite_delay_seconds = 0
    egov_api_key = None
    bitrix_webhook_url = "https://bitrix.example/rest/1/token/"

    @classmethod
    def from_env(cls):
        return cls()


class FakeScraper:
    failed_pages = []
    page_logs = []

    def __init__(self, *args, **kwargs):
        pass

    def scrape(self, *args, **kwargs):
        return [
            Application(
                created_at_raw="01.06.2026 10:00:00",
                doc_number="1-NEA",
                bin="123456789012",
                applicant_name="Test LLP",
                doc_type="Заявка на разведку ТПИ",
                status="Принято",
                source_url="https://example.test",
            )
        ]


class FakeEgov:
    def __init__(self, *args, **kwargs):
        pass

    def get_company(self, bin_number, name):
        return CompanyEnrichment(bin=bin_number, name=name)


class FakeClient:
    def __init__(self, *args, **kwargs):
        pass


class FakePipeline:
    instances = []

    def __init__(self, client, config):
        self.client = self
        self.calls = 0
        FakePipeline.instances.append(self)

    def find_deal_by_origin(self, deal_key):
        return None

    def process(self, app, enrichment):
        self.calls += 1
        return ProcessResult(app, enrichment, action="dry_run_company_and_deal", assigned_by_id=116)


def test_main_processes_each_application_once(monkeypatch, tmp_path):
    FakePipeline.instances.clear()
    monkeypatch.setattr(main_module, "Settings", FakeSettings)
    monkeypatch.setattr(main_module, "EqazynaScraper", FakeScraper)
    monkeypatch.setattr(main_module, "EgovClient", FakeEgov)
    monkeypatch.setattr(main_module, "BitrixClient", FakeClient)
    monkeypatch.setattr(main_module, "BitrixPipeline", FakePipeline)
    monkeypatch.setattr(main_module, "write_xlsx", lambda results, path: path)
    monkeypatch.setattr(
        main_module,
        "parse_args",
        lambda: SimpleNamespace(
            pages=1,
            page_start=1,
            page_list=None,
            doc_type="Заявка на разведку ТПИ",
            statuses="Принято",
            min_created_date=None,
            out=str(tmp_path / "log.xlsx"),
            json_out=str(tmp_path / "log.json"),
            no_egov=False,
            push_bitrix=True,
            dry_run=True,
            crm_mode="deal",
            deal_category_id="0",
            deal_stage_id="NEW",
            lead_status_id="NEW",
            assigned_by_id="36",
            assignment_limit_per_manager=30,
            assignment_load_stage_ids="ALL",
            inherit_failed_deals_by_director="true",
            failed_deal_stage_ids="LOSE",
            failed_deal_reason_fields="UF_CRM_1779448756033",
            requisite_preset_id=None,
            requisite_bin_field="RQ_BIN",
            strict_page_errors=False,
            max_consecutive_page_errors=5,
        ),
    )

    assert main_module.main() == 0
    assert len(FakePipeline.instances) == 1
    assert FakePipeline.instances[0].calls == 1
