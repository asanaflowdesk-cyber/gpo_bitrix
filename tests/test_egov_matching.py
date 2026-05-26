from eqazyna_bitrix.egov_client import _name_similarity, _is_tpi_activity, _select_trusted_record


def test_company_name_similarity_removes_legal_forms():
    a = 'Товарищество с ограниченной ответственностью "Металл-синтез"'
    b = 'Товарищество с ограниченной ответственностью "Металл-синтез"'
    assert _name_similarity(a, b) == 100


def test_company_name_similarity_rejects_different_name():
    assert _name_similarity('Металл-синтез', 'KERMET ENERGY') < 90


def test_tpi_activity_from_russian_okedru():
    ok, reason = _is_tpi_activity(None, 'ДОБЫЧА ДРАГОЦЕННЫХ МЕТАЛЛОВ И РУД РЕДКИХ МЕТАЛЛОВ', {})
    assert ok is True
    assert 'tpi' in reason


def test_oil_gas_is_not_tpi_by_default():
    ok, reason = _is_tpi_activity('06.10', 'Добыча сырой нефти и природного газа', {})
    assert ok is False
    assert 'oil_gas' in reason


def test_select_trusted_record_requires_name_only_oked_is_info():
    records = [
        {
            'bin': '260340002871',
            'nameru': 'Товарищество с ограниченной ответственностью "Другая компания"',
            'okedru': 'ДОБЫЧА ДРАГОЦЕННЫХ МЕТАЛЛОВ И РУД РЕДКИХ МЕТАЛЛОВ',
        },
        {
            'bin': '260340002871',
            'nameru': 'Товарищество с ограниченной ответственностью "Металл-синтез"',
            'okedru': 'Добыча сырой нефти и природного газа',
        },
    ]
    selected = _select_trusted_record('260340002871', 'Товарищество с ограниченной ответственностью "Металл-синтез"', records)
    assert selected is not None
    assert selected.enrichment.name.endswith('"Металл-синтез"')
    assert selected.name_score >= 75
    assert selected.oked_tpi is False
