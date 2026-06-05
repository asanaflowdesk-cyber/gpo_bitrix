from eqazyna_bitrix.scraper import parse_applications


def test_parse_text_fallback():
    html = """
    Дата создания Номер документа ИИН/БИН заявителя Наименование заявителя Тип документа Статус заявки
    21.05.2026 14:42:23 42480-NEA 260140012851 Товарищество с ограниченной ответственностью "Жетісу Минерал Ресорс"Заявка на разведку ТПИ Отправлено на рассмотрение
    21.05.2026 14:35:23 42479-NOA 060440012256 ТОО "Другой"Отчетность ЛКУ Отправлено на рассмотрение
    """
    rows = parse_applications(html, "https://example.com", doc_types=["Заявка на разведку ТПИ"])
    assert len(rows) == 2
    first = rows[0]
    assert first.doc_number == "42480-NEA"
    assert first.bin == "260140012851"
    assert first.doc_type == "Заявка на разведку ТПИ"
    assert first.status == "Отправлено на рассмотрение"
