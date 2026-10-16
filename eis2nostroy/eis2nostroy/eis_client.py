"""Клиент сервиса отдачи информации ЕИС (getDocsIP / getDocsLE).

Сервис не ищет по ИНН поставщика. Он отдаёт архивы документов за дату
по региону заказчика и подсистеме (RGK, RD223, RD615). Модуль скачивает
архивы и сам отбирает договоры членов СРО (см. parser.py).

Доступ с 01.01.2025 — только с токеном пользователя машиночитаемых данных,
полученным после регистрации в ЕИС через ЕСИА.
"""
from __future__ import annotations

import logging
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import date, datetime
from pathlib import Path
from xml.sax.saxutils import escape

import requests

from .config import Config, Feed

log = logging.getLogger(__name__)

NS = {
    "44": "http://zakupki.gov.ru/fz44/get-docs-ip/ws",
    "223": "http://zakupki.gov.ru/fz223/get-docs-ip/ws",
}


class EisError(RuntimeError):
    pass


class EisClient:
    def __init__(self, cfg: Config, session: requests.Session | None = None):
        self.cfg = cfg
        self.http = session or requests.Session()
        self._last_call = 0.0
        if not cfg.token:
            log.warning("Токен ЕИС не задан: сервис отдачи информации ответит отказом")

    # ------------------------------------------------------------------ SOAP
    def build_region_request(self, feed: Feed, region: str, day: date, doc_type: str) -> str:
        ns = NS[feed.endpoint_law]
        doc_tag = "documentType223" if feed.endpoint_law == "223" else "documentType44"
        token_tag = self.cfg.eis.get("token_header", "individualPerson_token")
        return f"""<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:ws="{ns}">
  <soapenv:Header>
    <{token_tag}>{escape(self.cfg.token)}</{token_tag}>
  </soapenv:Header>
  <soapenv:Body>
    <ws:getDocsByOrgRegionRequest>
      <index>
        <id>{uuid.uuid4()}</id>
        <createDateTime>{datetime.now().strftime('%Y-%m-%dT%H:%M:%S')}</createDateTime>
        <mode>{self.cfg.eis.get('mode', 'PROD')}</mode>
      </index>
      <selectionParams>
        <orgRegion>{escape(region)}</orgRegion>
        <subsystemType>{escape(feed.subsystem)}</subsystemType>
        <{doc_tag}>{escape(doc_type)}</{doc_tag}>
        <periodInfo>
          <exactDate>{day.isoformat()}</exactDate>
        </periodInfo>
      </selectionParams>
    </ws:getDocsByOrgRegionRequest>
  </soapenv:Body>
</soapenv:Envelope>"""

    def _throttle(self):
        wait = float(self.cfg.eis.get("min_interval_sec", 1.5)) - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def request_archives(self, feed: Feed, region: str, day: date, doc_type: str) -> list[str]:
        """Возвращает ссылки на архивы (их может быть несколько)."""
        self._throttle()
        body = self.build_region_request(feed, region, day, doc_type)
        resp = self.http.post(
            self.cfg.endpoint(feed.endpoint_law),
            data=body.encode("utf-8"),
            headers={"Content-Type": "text/xml; charset=utf-8"},
            timeout=self.cfg.eis.get("timeout_sec", 60),
        )
        if resp.status_code != 200:
            raise EisError(f"ЕИС вернул HTTP {resp.status_code}: {resp.text[:300]}")
        return parse_archive_urls(resp.content)

    def download_archive(self, url: str, dest: Path) -> Path:
        """Скачивает архив. Архив готовится на стороне ЕИС, поэтому повторяем."""
        if dest.exists() and dest.stat().st_size > 0:
            return dest
        dest.parent.mkdir(parents=True, exist_ok=True)
        token_tag = self.cfg.eis.get("token_header", "individualPerson_token")
        retries = int(self.cfg.eis.get("archive_retries", 6))
        pause = float(self.cfg.eis.get("archive_retry_wait_sec", 20))
        last = None
        for attempt in range(1, retries + 1):
            self._throttle()
            r = self.http.get(url, headers={token_tag: self.cfg.token}, timeout=120)
            if r.status_code == 200 and r.content[:2] == b"PK":
                dest.write_bytes(r.content)
                return dest
            last = f"HTTP {r.status_code}, {len(r.content)} байт"
            log.info("Архив ещё не готов (%s), попытка %s из %s", last, attempt, retries)
            time.sleep(pause)
        raise EisError(f"Архив не скачан: {url} ({last})")

    def fetch_day(self, feed: Feed, region: str, day: date) -> list[Path]:
        """Все архивы подсистемы за день по региону, с кэшем на диске."""
        cache = self.cfg.path("cache_dir") or Path("data/cache")
        paths: list[Path] = []
        for doc_type in feed.document_types:
            folder = cache / feed.subsystem / region / day.isoformat() / doc_type
            done = folder / ".done"
            if done.exists():
                paths.extend(sorted(folder.glob("*.zip")))
                continue
            urls = self.request_archives(feed, region, day, doc_type)
            for i, url in enumerate(urls):
                paths.append(self.download_archive(url, folder / f"part{i:03d}.zip"))
            folder.mkdir(parents=True, exist_ok=True)
            done.write_text(datetime.now().isoformat())
        return paths


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_archive_urls(xml_bytes: bytes) -> list[str]:
    root = ET.fromstring(xml_bytes)
    fault = [e for e in root.iter() if _local(e.tag) in ("faultstring", "errorInfo", "message")]
    urls = [e.text.strip() for e in root.iter() if _local(e.tag) == "archiveUrl" and e.text]
    if not urls and fault:
        raise EisError("ЕИС вернул ошибку: " + "; ".join((f.text or "").strip() for f in fault))
    return urls
