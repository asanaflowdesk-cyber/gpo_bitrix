from __future__ import annotations

from pathlib import Path
from typing import Iterable

import xlsxwriter

from .models import ProcessResult

COLUMNS = [
    ("created_at_raw", "Дата заявки"),
    ("doc_number", "Номер заявки"),
    ("bin", "БИН"),
    ("applicant_name", "Заявитель e-Qazyna"),
    ("doc_type", "Тип документа"),
    ("status", "Статус заявки"),
    ("egov_name", "Название eGov"),
    ("legal_address", "Юридический адрес"),
    ("region", "Регион"),
    ("city", "Город"),
    ("director", "Руководитель"),
    ("activity", "Деятельность"),
    ("oked", "ОКЭД"),
    ("registration_date", "Дата регистрации"),
    ("phone", "Телефон eGov"),
    ("egov_name_score", "eGov совпадение названия %"),
    ("egov_oked_tpi", "eGov ОКЭД ТПИ (инфо)"),
    ("egov_match_reason", "eGov причина/ОКЭД инфо"),
    ("egov_error", "Ошибка eGov"),
    ("egov_raw_preview", "eGov raw preview"),
    ("action", "Действие Bitrix"),
    ("company_id", "Bitrix Company ID"),
    ("deal_id", "Bitrix Deal ID"),
    ("lead_id", "Bitrix Lead ID"),
    ("requisite_id", "Bitrix Requisite ID"),
    ("director_contact_id", "Bitrix Contact ID руководителя"),
    ("director_contact_action", "Действие по контакту руководителя"),
    ("director_contact_error", "Ошибка контакта руководителя"),
    ("assigned_by_id", "Ответственный ID"),
    ("assigned_by_name", "Ответственный"),
    ("assignment_reason", "Причина распределения"),
    ("inherited_failed_stage_id", "Наследованная провальная стадия"),
    ("inherited_failed_reason", "Наследованная причина неудачи"),
    ("inherited_failed_from_deal_id", "ID сделки-источника провала"),
    ("error", "Ошибка"),
    ("source_url", "Источник e-Qazyna"),
]


def write_xlsx(results: Iterable[ProcessResult], output_path: str | Path) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook = xlsxwriter.Workbook(str(path))
    ws = workbook.add_worksheet("eqazyna_to_bitrix")

    header_fmt = workbook.add_format({"bold": True, "bg_color": "#EDEDED", "border": 1})
    text_fmt = workbook.add_format({"text_wrap": True, "valign": "top"})
    link_fmt = workbook.add_format({"font_color": "blue", "underline": True, "text_wrap": True, "valign": "top"})

    for col, (_, title) in enumerate(COLUMNS):
        ws.write(0, col, title, header_fmt)
        ws.set_column(col, col, 18)
    ws.set_column(3, 3, 38)
    ws.set_column(7, 7, 50)
    ws.set_column(7, 7, 50)
    ws.set_column(14, 20, 38)
    ws.set_column(24, 24, 42)
    ws.set_column(25, 25, 45)

    for row_idx, result in enumerate(results, start=1):
        data = result.as_dict()
        for col_idx, (key, _) in enumerate(COLUMNS):
            value = data.get(key)
            if key == "source_url" and value:
                ws.write_url(row_idx, col_idx, value, link_fmt, string=value)
            else:
                ws.write(row_idx, col_idx, value or "", text_fmt)

    ws.autofilter(0, 0, max(row_idx if 'row_idx' in locals() else 1, 1), len(COLUMNS) - 1)
    ws.freeze_panes(1, 0)
    workbook.close()
    return path
