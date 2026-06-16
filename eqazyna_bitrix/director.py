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

_NAME_TOKEN_RE = re.compile(r"[A-ZА-ЯӘҒҚҢӨҰҮҺІЁ]+", flags=re.IGNORECASE)

# Минимальный словарь нужен только для безопасного разворота формата
# "Имя Фамилия" -> "Фамилия Имя". Если имени нет в списке, старый порядок
# не ломаем. Это не справочник людей, а страховка против дублей вида
# "Геннадий Лябах" vs "ЛЯБАХ Г.Г.".
_COMMON_FIRST_NAMES = {
    "АЛЕКСАНДР", "АЛЕКСЕЙ", "АНАТОЛИЙ", "АНДРЕЙ", "АНТОН", "АРТЕМ", "АРТУР",
    "АСЕТ", "АСХАТ", "БАХЫТ", "БОРИС", "ВАДИМ", "ВАЛЕРИЙ", "ВАСИЛИЙ", "ВИКТОР",
    "ВЛАДИМИР", "ВЛАДИСЛАВ", "ГЕННАДИЙ", "ГЕОРГИЙ", "ДМИТРИЙ", "ЕВГЕНИЙ",
    "ЕРКЕБУЛАН", "ИВАН", "ИГОРЬ", "ИЛЬЯ", "КОНСТАНТИН", "МАКСИМ", "МАРАТ",
    "МИХАИЛ", "МУРАТ", "НИКОЛАЙ", "НУРЛАН", "НУРСУЛТАН", "ОЛЕГ", "ПАВЕЛ",
    "ПЕТР", "РИНАТ", "РОМАН", "РУСЛАН", "САГИ", "СЕРГЕЙ", "СТАНИСЛАВ",
    "ТИМУР", "ЮРИЙ", "ЯРОСЛАВ",
    "АЛИЯ", "АСЕЛЬ", "АСЕМГУЛЬ", "АЙГЕРИМ", "АНАСТАСИЯ", "ГУЛЬНАР", "ДАНА",
    "ЕКАТЕРИНА", "КСЕНИЯ", "МАРАЛ", "МИЛАНА", "НАДЕЖДА", "ОЛЬГА", "СВЕТЛАНА",
    "ТАТЬЯНА", "ЮЛИЯ",
}

@dataclass(slots=True)
class DirectorName:
    raw: str
    last_name: str
    name: str
    second_name: str = ""

    @property
    def normalized(self) -> str:
        return normalize_fio(" ".join([self.last_name, self.name, self.second_name]).strip())

    @property
    def identity_key(self) -> str:
        return director_identity_key(self.raw)


def normalize_fio(value: str | None) -> str:
    if not value:
        return ""
    value = str(value).replace("\u00a0", " ")
    value = value.replace("ё", "е").replace("Ё", "Е")
    value = re.sub(r"[\t\r\n]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value.upper()


def _tokens(value: str | None) -> list[str]:
    normalized = normalize_fio(value)
    return [match.group(0).upper() for match in _NAME_TOKEN_RE.finditer(normalized)]


def _is_initial(token: str) -> bool:
    return len(token) == 1


def _add_key(keys: list[str], key: str | None) -> None:
    if key and key not in keys:
        keys.append(key)


def director_identity_keys(value: str | None) -> list[str]:
    """Return stable identity keys for one physical director.

    Bitrix/eGov can store the same person in several forms:
    - ЛЯБАХ Г.Г.
    - ЛЯБАХ ГЕННАДИЙ
    - ГЕННАДИЙ ЛЯБАХ

    Exact text comparison splits those into different packages. These keys keep
    the exact key, but also add surname+initial aliases so old abbreviated cards
    and new full-name cards can be linked.
    """
    parts = _tokens(clean_director_value(value) or value)
    keys: list[str] = []
    if not parts:
        return keys

    exact = " ".join(parts)
    _add_key(keys, f"exact|{exact}")

    # Форма: ФАМИЛИЯ И.О. / ФАМИЛИЯ И О
    if len(parts) >= 2 and not _is_initial(parts[0]) and all(_is_initial(part) for part in parts[1:]):
        surname = parts[0]
        initials = "".join(parts[1:])
        if initials:
            _add_key(keys, f"surname_initial|{surname}|{initials[0]}")
            _add_key(keys, f"surname_initials|{surname}|{initials}")

    # Форма: ФАМИЛИЯ ИМЯ [ОТЧЕСТВО]
    if len(parts) >= 2 and not _is_initial(parts[0]) and not _is_initial(parts[1]):
        surname = parts[0]
        first = parts[1]
        patronymic = parts[2] if len(parts) >= 3 and not _is_initial(parts[2]) else ""
        _add_key(keys, f"surname_first|{surname}|{first}")
        _add_key(keys, f"surname_initial|{surname}|{first[0]}")
        if patronymic:
            _add_key(keys, f"surname_first_patronymic|{surname}|{first}|{patronymic}")
            _add_key(keys, f"surname_initials|{surname}|{first[0]}{patronymic[0]}")

    # Форма: ИМЯ ФАМИЛИЯ. Добавляем только если первый токен похож на имя,
    # чтобы не разворачивать все двухсловные ФИО подряд.
    if len(parts) == 2 and parts[0] in _COMMON_FIRST_NAMES and not _is_initial(parts[1]):
        first = parts[0]
        surname = parts[1]
        _add_key(keys, f"surname_first|{surname}|{first}")
        _add_key(keys, f"surname_initial|{surname}|{first[0]}")

    return keys


def director_identity_key(value: str | None) -> str:
    keys = director_identity_keys(value)
    if not keys:
        return ""
    # Для группировки важнее alias surname+initial: именно он склеивает
    # "ЛЯБАХ Г.Г." и "Геннадий Лябах".
    for prefix in ("surname_first_patronymic|", "surname_first|", "surname_initials|", "surname_initial|"):
        for key in keys:
            if key.startswith(prefix):
                return key
    return keys[0]


def director_keys_match(left: str | None, right: str | None) -> bool:
    left_keys = set(director_identity_keys(left))
    right_keys = set(director_identity_keys(right))
    return bool(left_keys and right_keys and left_keys.intersection(right_keys))


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

    # ЛЯБАХ Г.Г. -> LAST_NAME=ЛЯБАХ, NAME=Г, SECOND_NAME=Г
    if not _is_initial(parts[0]) and all(_is_initial(part) for part in parts[1:]):
        last_name = parts[0]
        name = parts[1]
        second_name = " ".join(parts[2:]) if len(parts) > 2 else ""
        return DirectorName(raw=cleaned, last_name=last_name, name=name, second_name=second_name)

    # Геннадий Лябах -> LAST_NAME=ЛЯБАХ, NAME=ГЕННАДИЙ
    if len(parts) == 2 and parts[0] in _COMMON_FIRST_NAMES:
        return DirectorName(raw=cleaned, last_name=parts[1], name=parts[0], second_name="")

    last_name = parts[0]
    name = parts[1]
    second_name = " ".join(parts[2:]) if len(parts) > 2 else ""
    return DirectorName(raw=cleaned, last_name=last_name, name=name, second_name=second_name)
