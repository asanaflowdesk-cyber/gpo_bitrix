from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

_EMPTY_DIRECTOR_VALUES = {
    "",
    "-",
    "—",
    "не найден",
    "не найдено",
    "нет",
    "null",
    "none",
}

_LEGAL_WORDS_RE = re.compile(
    r"\b(ТОО|ИП|ЖШС|LLP|LIMITED|LTD|КОМПАНИЯ|COMPANY|ПАРТНЕРСТВО|ТОВАРИЩЕСТВО|ОГРАНИЧЕННОЙ|ОТВЕТСТВЕННОСТЬЮ)\b",
    flags=re.IGNORECASE,
)

@dataclass(slots=True)
class DirectorName:
    raw: str
    last_name: str
    name: str
    second_name: str = ""

    @property
    def normalized(self) -> str:
        return normalize_fio(" ".join([self.last_name, self.name, self.second_name]).strip())


def normalize_fio(value: str | None) -> str:
    if not value:
        return ""
    value = str(value).replace("\u00a0", " ")
    value = value.replace("ё", "е").replace("Ё", "Е")
    value = re.sub(r"[\t\r\n]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value.upper()


def clean_director_value(value: str | None) -> str:
    if not value:
        return ""
    value = str(value).strip()
    value = re.split(r"\s*(?:\||;|,\s*тел\.?|\s+тел\.?|\s+БИН\b|\s+Компания\b|\s+Юридический адрес\b)\s*", value, maxsplit=1, flags=re.IGNORECASE)[0]
    value = value.strip(" .;,:—-")
    value = re.sub(r"\s+", " ", value)
    if value.lower() in _EMPTY_DIRECTOR_VALUES:
        return ""
    if _LEGAL_WORDS_RE.search(value):
        # Very likely a company name, not a person's name.
        return ""
    return value


def extract_director_from_text(text: str | None) -> str:
    """Extract director from Bitrix COMMENTS/timeline text line like 'Руководитель: ...'."""
    if not text:
        return ""
    patterns = [
        r"(?im)^\s*Руководитель\s*:\s*(.+?)\s*$",
        r"(?im)^\s*Директор\s*:\s*(.+?)\s*$",
    ]
    for pattern in patterns:
        match = re.search(pattern, str(text))
        if match:
            cleaned = clean_director_value(match.group(1))
            if cleaned:
                return cleaned
    return ""


def split_director_fio(value: str | None) -> Optional[DirectorName]:
    cleaned = clean_director_value(value)
    if not cleaned:
        return None
    normalized = normalize_fio(cleaned)
    parts = [p for p in re.split(r"\s+", normalized) if p]
    if len(parts) < 2:
        return None
    last_name = parts[0]
    name = parts[1]
    second_name = " ".join(parts[2:]) if len(parts) > 2 else ""
    return DirectorName(raw=cleaned, last_name=last_name, name=name, second_name=second_name)
