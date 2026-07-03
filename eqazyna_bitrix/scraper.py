from __future__ import annotations

import re
import socket
import time
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Iterable
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup, FeatureNotFound
from urllib3.util import connection as urllib3_connection

from .models import Application


def _force_ipv4() -> None:
    """
    GitHub Actions иногда не может подключиться к minerals.e-qazyna.kz
    по маршруту, который выбирает requests/urllib3 автоматически.
    Принудительно используем IPv4.
    """
    urllib3_connection.allowed_gai_family = lambda: socket.AF_INET


_force_ipv4()


BASE_URL = "https://minerals.e-qazyna.kz/ru/guest/reestr/doc/list"
JINA_READER_PREFIX = "https://r.jina.ai/"

DOC_TYPE_FILTER_VALUES = {
    "Заявка на разведку ТПИ": "ТпиЗаявкаНаРазведку",
}

STATUS_FILTER_VALUES = {
    "Отправлено на рассмотрение": "НаРассмотрении",
    "Принято": "Принято",
    "Выдана лицензия": "ВыданаЛицензия",
    "Отклонено": "Отклонено",
    "Отозвано": "Отозвано",
    "Аннулировано": "Аннулировано",
    "Завершено": "Завершено",
}

KNOWN_DOC_TYPES = [
    "Оцифровка контракта",
    "Оцифровка лицензии ТПИ",
    "Переход на лицензионный режим",
    "Заявка на разведку ТПИ",
    "Миграция лицензии на разведку ТПИ",
    "Миграция лицензии на добычу ТПИ",
    "Миграция контракта на разведку ТПИ",
    "Миграция контракта на добычу ТПИ",
    "Миграция контракта на разведку и добычу ТПИ",
    "Миграция контракта на добычу ОПИ",
    "Заявка на продление лицензии на разведку ТПИ",
    "Заявка на добычу ТПИ",
    "Заявка на разведку и добычу",
    "Заявка на доп. соглашение (на рассмотрение экспертной комиссией)",
    "Заявка на доп. соглашение (на рассмотрение рабочей группой)",
    "Заявка на доп. соглашение (на экономическую экспертизу)",
    "Заявка на подписание доп. соглашения",
    "Заявка на использование пространства недр",
    "Соглашение о переработке",
    "Регистрация договора залога ТПИ",
    "Оцифровка лицензии ОПИ",
    "Оцифровка контракта ОПИ",
    "Оцифровка контракта по подземным сооружениям",
    "Заявка на лицензию на добычу ОПИ",
    "Регистрация договора залога ОПИ",
    "Оцифровка лицензии Старательства",
    "Заявка на лицензию Старательства",
    "Заявка на использование ликвидационного фонда",
    "Согласование водоохранных мероприятий",
    "Горный/Геологический отвод",
    "Разрешение на застройку территорий залегания",
    "Разрешение на извелечение горной массы",
    "Переход права недропользования",
    "Преобразование участка недр",
    "Выдача лицензии на экспорт информации",
    "Выдача разрешения на временный вывоз в рамках ТС",
    "Геологическое изучение недр",
    "Заключение об отсутствии полезных ископаемых",
    "Заключение на строительство",
    "Отчетность ЛКУ",
    "Выдача заключения на строительство",
    "Системный документ",
    "Отрисовка участка по старательству",
    "Оцифровка месторождения",
    "Редактирование",
    "Внесение изменений в лицензию",
    "Приобретение геологической информации",
    "Изменения рабочего органа",
    "Внесение сведений по акту ликвидации/обследования",
    "Сдача акта ликвидации",
    "Прекращение действия лицензий",
    "Отзыв Лицензии",
    "Подписание Протоколов",
    "Гео отчетность",
]

KNOWN_STATUSES = [
    "Отправлено на рассмотрение",
    "Принято",
    "Выдана лицензия",
    "Отклонено",
    "Отозвано",
    "Аннулировано",
    "Завершено",
]


@dataclass(slots=True)
class PageLog:
    page: int
    url: str
    rows: int = 0
    accepted: int = 0
    total_after_page: int = 0
    status: str = "ok"  # ok | empty | failed | stopped_by_date
    error: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "page": self.page,
            "url": self.url,
            "rows": self.rows,
            "accepted": self.accepted,
            "total_after_page": self.total_after_page,
            "status": self.status,
            "error": self.error,
        }


@dataclass(slots=True)
class EqazynaScraper:
    timeout: int = 30
    polite_delay_seconds: float = 0.5
    max_retries: int = 2
    retry_base_sleep_seconds: float = 1.0
    continue_on_page_error: bool = True
    max_consecutive_page_errors: int = 1
    session: requests.Session | None = None
    page_logs: list[PageLog] = field(default_factory=list)
    failed_pages: list[int] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.session is None:
            self.session = requests.Session()

        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
                "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Connection": "close",
            }
        )

    def build_url(self, page: int, doc_type: str | None, statuses: Iterable[str]) -> str:
        # Порядок параметров сделан как в рабочей ссылке из браузера:
        # oq → flMineralUserXin → flStatus → flDocNum → flDocType → p
        params: list[tuple[str, str]] = [
            ("oq", ""),
            ("flMineralUserXin", ""),
        ]

        for status in statuses or []:
            status = status.strip()
            filter_status = STATUS_FILTER_VALUES.get(status)
            if filter_status:
                params.append(("flStatus", filter_status))

        params.append(("flDocNum", ""))

        doc_type = doc_type.strip() if doc_type else None
        if doc_type:
            filter_doc_type = DOC_TYPE_FILTER_VALUES.get(doc_type)
            if filter_doc_type:
                params.append(("flDocType", filter_doc_type))

        if page > 1:
            params.append(("p", str(page)))

        return f"{BASE_URL}?{urlencode(params, doseq=True)}"

    def fetch_page(self, page: int, doc_type: str | None, statuses: Iterable[str]) -> tuple[str, str]:
        url = self.build_url(page, doc_type, statuses)

        try:
            return self._fetch_direct(url, page)
        except Exception as direct_exc:
            print(f"    WARN: direct e-Qazyna failed for page {page}; trying Jina fallback: {direct_exc}")

        jina_url = f"{JINA_READER_PREFIX}{url}"

        try:
            return self._fetch_jina(jina_url, page)
        except Exception as jina_exc:
            raise jina_exc

    def _fetch_direct(self, url: str, page: int) -> tuple[str, str]:
        last_error: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            try:
                response = self.session.get(url, timeout=self.timeout)
                response.raise_for_status()
                return response.text, url

            except requests.RequestException as exc:
                last_error = exc

                print(
                    f"    WARN: e-Qazyna page {page} failed on attempt "
                    f"{attempt}/{self.max_retries}: {exc}"
                )

                if attempt < self.max_retries:
                    sleep_for = self.retry_base_sleep_seconds
                    print(f"    sleep {sleep_for:.1f}s")
                    time.sleep(sleep_for)

        raise last_error or RuntimeError(f"e-Qazyna page {page} failed")

    def _fetch_jina(self, jina_url: str, page: int) -> tuple[str, str]:
        last_error: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            try:
                response = self.session.get(
                    jina_url,
                    timeout=max(self.timeout, 30),
                    headers={
                        "User-Agent": "Mozilla/5.0 (compatible; FlowDesk e-Qazyna monitor)",
                        "Accept": "text/plain,text/markdown,*/*",
                    },
                )
                response.raise_for_status()

                text = response.text or ""
                markers = ("Реестр заявок", "Дата создания", "Номер документа", "ИИН/БИН заявителя")

                if not any(marker in text for marker in markers):
                    raise RuntimeError("Jina fallback returned page without registry markers")

                print(f"    JINA_FALLBACK_OK page={page}")
                return text, jina_url

            except Exception as exc:
                last_error = exc

                print(
                    f"    WARN: Jina fallback page {page} failed on attempt "
                    f"{attempt}/{self.max_retries}: {exc}"
                )

                if attempt < self.max_retries:
                    sleep_for = self.retry_base_sleep_seconds
                    print(f"    sleep {sleep_for:.1f}s")
                    time.sleep(sleep_for)

        raise last_error or RuntimeError(f"Jina fallback page {page} failed")

    @staticmethod
    def parse_page_list(value: str | None) -> list[int] | None:
        if not value or not value.strip():
            return None

        pages: set[int] = set()

        for part in value.split(","):
            part = part.strip()
            if not part:
                continue

            if "-" in part:
                left, right = [p.strip() for p in part.split("-", 1)]
                start, end = int(left), int(right)

                if end < start:
                    start, end = end, start

                pages.update(range(start, end + 1))
            else:
                pages.add(int(part))

        return sorted(p for p in pages if p > 0)

    def scrape(
        self,
        pages: int,
        doc_type: str | None,
        statuses: Iterable[str],
        min_created_date: date | None = None,
        stop_on_empty_page: bool = True,
        page_start: int = 1,
        page_list: str | None = None,
    ) -> list[Application]:
        wanted_statuses_list = [s.strip() for s in statuses if s and s.strip()]
        wanted_statuses = set(wanted_statuses_list)
        doc_type = doc_type.strip() if doc_type else None

        results: list[Application] = []
        seen_keys: set[str] = set()

        self.page_logs.clear()
        self.failed_pages.clear()

        explicit_pages = self.parse_page_list(page_list)
        page_numbers = explicit_pages if explicit_pages else list(range(max(1, page_start), max(1, page_start) + pages))
        sequential_mode = explicit_pages is None
        consecutive_failed_pages = 0

        for page in page_numbers:
            url = self.build_url(page, doc_type, wanted_statuses_list)

            try:
                html, url = self.fetch_page(page, doc_type, wanted_statuses_list)

            except Exception as exc:
                error = str(exc)

                self.failed_pages.append(page)
                self.page_logs.append(
                    PageLog(
                        page=page,
                        url=url,
                        status="failed",
                        error=error,
                        total_after_page=len(results),
                    )
                )

                print(
                    f"    ERROR: page {page} failed after retries; "
                    f"continue_on_page_error={self.continue_on_page_error}: {error}"
                )

                if not self.continue_on_page_error:
                    raise

                consecutive_failed_pages += 1

                if (
                    sequential_mode
                    and self.max_consecutive_page_errors > 0
                    and consecutive_failed_pages >= self.max_consecutive_page_errors
                ):
                    print(
                        f"    stop: {consecutive_failed_pages} consecutive failed page(s); "
                        "e-Qazyna unavailable, processing already collected applications"
                    )
                    break

                time.sleep(self.polite_delay_seconds)
                continue

            consecutive_failed_pages = 0

            rows = parse_applications(html, url, doc_types=[doc_type] if doc_type else None)

            if not rows:
                text_preview = clean_text(make_soup(html).get_text(" "))[:500]

                print(f"    page {page}: no rows; html_chars={len(html)} text_preview={text_preview!r}")

                self.page_logs.append(
                    PageLog(
                        page=page,
                        url=url,
                        status="empty",
                        total_after_page=len(results),
                        error=f"no_rows html_chars={len(html)} preview={text_preview}",
                    )
                )

                if stop_on_empty_page and sequential_mode:
                    break

                time.sleep(self.polite_delay_seconds)
                continue

            accepted_on_page = 0
            stop_by_date = False

            for app in rows:
                created_date = parse_created_date(app.created_at_raw)

                if min_created_date and created_date and created_date < min_created_date:
                    stop_by_date = True
                    continue

                if doc_type and app.doc_type.strip() != doc_type:
                    continue

                if wanted_statuses and app.status.strip() not in wanted_statuses:
                    continue

                if app.application_key in seen_keys:
                    continue

                seen_keys.add(app.application_key)
                results.append(app)
                accepted_on_page += 1

            status = "stopped_by_date" if stop_by_date else "ok"

            self.page_logs.append(
                PageLog(
                    page=page,
                    url=url,
                    rows=len(rows),
                    accepted=accepted_on_page,
                    total_after_page=len(results),
                    status=status,
                )
            )

            print(f"    page {page}: rows={len(rows)} accepted={accepted_on_page} total={len(results)} url={url}")

            if stop_by_date and sequential_mode:
                print(f"    stop: reached min_created_date={min_created_date.isoformat() if min_created_date else None}")
                break

            time.sleep(self.polite_delay_seconds)

        return results


def make_soup(html: str) -> BeautifulSoup:
    try:
        return BeautifulSoup(html, "lxml")
    except FeatureNotFound:
        return BeautifulSoup(html, "html.parser")


def clean_text(value: str) -> str:
    value = re.sub(r"\s+", " ", value or "")
    return value.replace("\xa0", " ").strip()


def parse_created_date(value: str) -> date | None:
    value = clean_text(value)

    for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y"):
        try:
            return datetime.strptime(value[:19] if "%H" in fmt else value[:10], fmt).date()
        except ValueError:
            pass

    return None


def parse_applications(html: str, source_url: str, doc_types: list[str | None] | None = None) -> list[Application]:
    parsed_markdown = _parse_markdown_tables(html, source_url)
    if parsed_markdown:
        return parsed_markdown

    soup = make_soup(html)

    parsed_html = _parse_html_tables(soup, source_url)
    if parsed_html:
        return parsed_html

    text = soup.get_text("\n")

    parsed_markdown_from_text = _parse_markdown_tables(text, source_url)
    if parsed_markdown_from_text:
        return parsed_markdown_from_text

    return _parse_text_fallback(text, source_url, doc_types=doc_types)


def _parse_html_tables(soup: BeautifulSoup, source_url: str) -> list[Application]:
    rows: list[Application] = []

    for tr in soup.find_all("tr"):
        cells = [clean_text(td.get_text(" ")) for td in tr.find_all(["td", "th"])]

        if len(cells) < 6:
            continue

        if cells[:6] == [
            "Дата создания",
            "Номер документа",
            "ИИН/БИН заявителя",
            "Наименование заявителя",
            "Тип документа",
            "Статус заявки",
        ]:
            continue

        date_raw, doc, bin_number, name, doc_type, status = cells[:6]

        if not re.match(r"\d{2}\.\d{2}\.\d{4}", date_raw):
            continue

        if not re.fullmatch(r"\d{12}", bin_number):
            continue

        rows.append(
            Application(
                created_at_raw=date_raw,
                doc_number=doc,
                bin=bin_number,
                applicant_name=name,
                doc_type=doc_type,
                status=status,
                source_url=source_url,
            )
        )

    return rows


def _parse_markdown_tables(text: str, source_url: str) -> list[Application]:
    rows: list[Application] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()

        if not line.startswith("|") or not line.endswith("|"):
            continue

        cells = [clean_text(cell) for cell in line.strip("|").split("|")]

        if len(cells) < 6:
            continue

        if all(re.fullmatch(r"[-:\s]+", cell or "") for cell in cells):
            continue

        if cells[:6] == [
            "Дата создания",
            "Номер документа",
            "ИИН/БИН заявителя",
            "Наименование заявителя",
            "Тип документа",
            "Статус заявки",
        ]:
            continue

        date_raw, doc, bin_number, name, doc_type, status = cells[:6]

        if not re.match(r"\d{2}\.\d{2}\.\d{4}", date_raw):
            continue

        if not re.fullmatch(r"\d{12}", bin_number):
            continue

        rows.append(
            Application(
                created_at_raw=date_raw,
                doc_number=doc,
                bin=bin_number,
                applicant_name=name,
                doc_type=doc_type,
                status=status,
                source_url=source_url,
            )
        )

    return rows


def _parse_text_fallback(text: str, source_url: str, doc_types: list[str | None] | None = None) -> list[Application]:
    rows: list[Application] = []

    candidates = [d for d in (doc_types or []) if d] + KNOWN_DOC_TYPES
    candidates = sorted(set(candidates), key=len, reverse=True)

    row_re = re.compile(r"(\d{2}\.\d{2}\.\d{4}\s+\d{2}:\d{2}:\d{2})\s+([A-Z0-9\-]+)\s+(\d{12})\s+(.+)")

    for raw_line in text.splitlines():
        line = clean_text(raw_line)
        match = row_re.match(line)

        if not match:
            continue

        created_at, doc_number, bin_number, tail = match.groups()

        status = None

        for s in KNOWN_STATUSES:
            if tail.endswith(s):
                status = s
                tail = tail[: -len(s)].strip()
                break

        if not status:
            continue

        doc_type = None
        applicant_name = ""

        for candidate in candidates:
            if candidate and candidate in tail:
                idx = tail.rfind(candidate)
                doc_type = candidate
                applicant_name = clean_text(tail[:idx])
                break

        if not doc_type:
            continue

        rows.append(
            Application(
                created_at_raw=created_at,
                doc_number=doc_number,
                bin=bin_number,
                applicant_name=applicant_name,
                doc_type=doc_type,
                status=status,
                source_url=source_url,
            )
        )

    return rows
