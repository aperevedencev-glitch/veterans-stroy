"""Договоры 223-ФЗ через протоколы закупок.

Фикстуры — настоящие XML из ЕИС (подписи и вложения вырезаны):
итоговый протокол закупки № 32414075550 и договор № 53435098928240001840000
ООО «ЛУКОЙЛ-ВОЛГОГРАДЭНЕРГО», победитель ООО «ВОЛГОГРАДМОНТАЖ» (ИНН 3448018550).
В карточке договора поставщика нет — связь только через протокол.
"""
from __future__ import annotations

import io
import zipfile
from datetime import date
from pathlib import Path

import pytest

from eis2nostroy.models import CONFIRMED, PROBABLE, Member
from eis2nostroy.parser import parse_contract_xml, scan_archive
from eis2nostroy.protocols import WinIndex, WinStore, contracts_from_223, parse_protocol_xml, unlinked_wins
from eis2nostroy.reconcile import reconcile

FIX = Path(__file__).parent / "fixtures"
PROTOCOL = (FIX / "eis223_protocol_32414075550.xml").read_bytes()
CONTRACT = (FIX / "eis223_contract_53435098928240001840000.xml").read_bytes()
VOLGO = "3448018550"


def test_protocol_gives_member_win():
    wins = parse_protocol_xml(PROTOCOL, {VOLGO})
    assert len(wins) == 1
    w = wins[0]
    assert w.notice_number == "32414075550"
    assert w.protocol_number == "32414075550-03"
    assert w.protocol_guid == "9601f85a-1fb0-4f1d-bece-f009c5b31b1c"
    assert w.customer_inn == "3435098928"
    assert w.price == pytest.approx(156_400_000)
    assert w.lot_guid == "6968c3d7-62a6-4e6e-a153-01336af24556"
    assert w.reason  # первое место / договор заключён


def test_protocol_ignores_other_suppliers():
    assert parse_protocol_xml(PROTOCOL, {"7700000001"}) == []


def test_protocol_is_not_a_contract():
    # раньше протокол с ИНН участника разбирался как договор с неверным номером
    assert parse_contract_xml(PROTOCOL, "223", {VOLGO}) == []


def test_contract_without_supplier_is_not_found_directly():
    assert parse_contract_xml(CONTRACT, "223", {VOLGO}) == []


def test_contract_linked_through_protocol():
    index = WinIndex(parse_protocol_xml(PROTOCOL, {VOLGO}))
    got = contracts_from_223(CONTRACT, index)
    assert len(got) == 1
    c = got[0]
    assert c.supplier_inn == VOLGO
    assert c.reg_number == "53435098928240001840000"
    assert c.notice_number == "32414075550"
    assert c.sign_date == date(2024, 11, 22)
    assert c.price == pytest.approx(187_680_000)          # цена последней редакции договора
    assert c.extra["protocol"] == "32414075550-03"
    assert c.extra["version"] == "2"


def test_link_by_notice_and_lot_when_guid_missing():
    w = parse_protocol_xml(PROTOCOL, {VOLGO})[0]
    w.protocol_guid = "другой-guid"
    got = contracts_from_223(CONTRACT, WinIndex([w]))
    assert [c.reg_number for c in got] == ["53435098928240001840000"]


def _zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for n, b in files.items():
            zf.writestr(n, b)
    return buf.getvalue()


def test_archives_protocols_then_contracts(tmp_path):
    p = tmp_path / "protocols.zip"
    p.write_bytes(_zip({"purchaseProtocol_1.xml": PROTOCOL}))
    c = tmp_path / "contracts.zip"
    c.write_bytes(_zip({"contract_1.xml": CONTRACT}))
    contracts, wins = scan_archive(p, "223", {VOLGO})
    assert contracts == [] and len(wins) == 1
    store = WinStore(tmp_path / "wins.jsonl")
    assert store.add(wins) == 1 and store.add(wins) == 0
    found, _ = scan_archive(c, "223", {VOLGO}, WinIndex(store.load()))
    assert [x.reg_number for x in found] == ["53435098928240001840000"]


def test_reconcile_statuses():
    members = {VOLGO: Member(inn=VOLGO, name="ВОЛГОГРАДМОНТАЖ", odo_date=date(2017, 7, 1))}
    wins = parse_protocol_xml(PROTOCOL, {VOLGO})
    # только протокол — «Вероятный»
    pending = unlinked_wins(wins, [])
    recs = reconcile(members, pending, date(2017, 7, 1), date(2026, 3, 1))
    assert [r.status for r in recs] == [PROBABLE]
    # протокол + договор — «Подтверждён», без дубля
    linked = contracts_from_223(CONTRACT, WinIndex(wins))
    assert unlinked_wins(wins, linked) == []
    recs = reconcile(members, linked, date(2017, 7, 1), date(2026, 3, 1))
    assert [r.status for r in recs] == [CONFIRMED]


def test_probe_with_fake_eis(tmp_path, monkeypatch, capsys):
    from openpyxl import Workbook
    from eis2nostroy import cli
    from eis2nostroy.eis_client import EisClient
    wb = Workbook(); ws = wb.active
    ws.append(["ИНН", "Наименование"]); ws.append([VOLGO, "ООО ВОЛГОГРАДМОНТАЖ"])
    (tmp_path / "data").mkdir(); wb.save(tmp_path / "data" / "members.xlsx")
    cfg = (Path(__file__).parents[1] / "config.example.toml").read_text(encoding="utf-8")
    (tmp_path / "config.toml").write_text(cfg, encoding="utf-8")
    zp = tmp_path / "p.zip"; zp.write_bytes(_zip({"p.xml": PROTOCOL}))
    zc = tmp_path / "c.zip"; zc.write_bytes(_zip({"c.xml": CONTRACT}))
    zn = tmp_path / "n.zip"; zn.write_bytes(_zip({"nsi.xml": NSI}))
    monkeypatch.setattr(EisClient, "fetch_nsi", lambda self, code, dest, law="223": [zn])
    seen = []

    def fake_day(self, feed, region, day):
        seen.append((feed.kind, tuple(feed.document_types)))
        return [zp] if feed.kind == "protocol" else [zc]
    monkeypatch.setattr(EisClient, "fetch_day", fake_day)
    asked = []
    monkeypatch.setattr(EisClient, "fetch_by_reestr",
                        lambda self, sub, num, law="223": asked.append((sub, num)) or [zp])
    assert cli.main(["-c", str(tmp_path / "config.toml"), "probe"]) == 0
    out = capsys.readouterr().out
    assert "НАЙДЕН договор 53435098928240001840000" in out
    # протоколы запрошены по номеру закупки из договора
    assert asked == [("RI223", "32414075550")]


NSI = """<?xml version="1.0" encoding="UTF-8"?>
<nsiDocumentTypes xmlns="http://zakupki.gov.ru/223fz/reference/1"><body>
 <item><code>purchaseProtocol</code><name>Протокол закупки</name><distributedPublicly>true</distributedPublicly></item>
 <item><code>purchaseProtocolZK</code><name>Протокол запроса котировок</name><distributedPublicly>true</distributedPublicly></item>
 <item><code>purchaseProtocolSecret</code><name>Протокол</name><distributedPublicly>false</distributedPublicly></item>
 <item><code>contract</code><name>Сведения о договоре</name><distributedPublicly>true</distributedPublicly></item>
</body></nsiDocumentTypes>""".encode()


def test_protocol_types_from_nsi():
    from eis2nostroy.protocols import protocol_types_from_nsi
    protocols, public = protocol_types_from_nsi([NSI])
    assert protocols == ["purchaseProtocol", "purchaseProtocolZK"]
    assert ("contract", "Сведения о договоре") in public


def test_candidate_needs_construction_okpd():
    from eis2nostroy.protocols import contract_candidate
    assert contract_candidate(CONTRACT) == [{"notice": "32414075550", "reg": "53435098928240001840000",
                                             "okpd2": ["42.21.22.110"]}]
    assert contract_candidate(CONTRACT, ("62.",)) == []


def test_client_waits_when_network_drops(tmp_path, monkeypatch):
    import requests
    from eis2nostroy.config import load_config
    from eis2nostroy.eis_client import EisClient
    cfg_text = (Path(__file__).parents[1] / "config.example.toml").read_text(encoding="utf-8")
    (tmp_path / "c.toml").write_text(cfg_text.replace("min_interval_sec = 0.75", "min_interval_sec = 0"), encoding="utf-8")
    cfg = load_config(tmp_path / "c.toml")
    monkeypatch.setattr("time.sleep", lambda s: None)
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise requests.ConnectionError("proxy down")
        return "ok"
    assert EisClient(cfg)._net(flaky) == "ok" and len(calls) == 3


def test_fetch_prefetches_and_waits_in_parallel(tmp_path, monkeypatch):
    """Архивы за день заказываются сразу, неготовые (404) забираются по кругу."""
    import re
    from datetime import date as d
    from openpyxl import Workbook
    from eis2nostroy import cli, eis_client
    from eis2nostroy.config import load_config
    wb = Workbook(); ws = wb.active
    ws.append(["ИНН", "Наименование"]); ws.append([VOLGO, "ООО ВОЛГОГРАДМОНТАЖ"])
    (tmp_path / "data").mkdir(); wb.save(tmp_path / "data" / "members.xlsx")
    cfg_text = (Path(__file__).parents[1] / "config.example.toml").read_text(encoding="utf-8")
    cfg_text = cfg_text.replace("min_interval_sec = 0.75", "min_interval_sec = 0")
    (tmp_path / "config.toml").write_text(cfg_text, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    cfg = load_config(tmp_path / "config.toml")
    monkeypatch.setenv("EIS_TOKEN", "t")
    for f in cfg.feeds:
        if f.law == "223":
            f.regions = ["34", "50"]
    sleeps = []
    monkeypatch.setattr(eis_client.time, "sleep", lambda s: sleeps.append(s))
    zc = _zip({"c.xml": CONTRACT}); zp = _zip({"p.xml": PROTOCOL})
    empty = _zip({})
    hits: dict[str, int] = {}

    class R:
        def __init__(self, code, content): self.status_code, self.content, self.text = code, content, ""

    class S:
        trust_env = True
        def post(self, url, data, headers, timeout):
            body = data.decode()
            if "reestrNumber" in body:
                u = "https://x/reestr/" + re.search(r"<reestrNumber>(\d+)", body).group(1)
            else:
                reg = re.search(r"<orgRegion>(\d+)", body).group(1)
                dt = re.search(r"documentType223>(\w+)<", body).group(1)
                u = f"https://x/day/{reg}/{dt}"
            return R(200, f"<r><archiveUrl>{u}</archiveUrl></r>".encode())
        def get(self, url, headers, timeout):
            hits[url] = hits.get(url, 0) + 1
            if hits[url] == 1:          # первый раз архив «ещё не готов»
                return R(404, b"x")
            if "/reestr/" in url:
                return R(200, zp)
            return R(200, zc if url.endswith("/34/contractCutted") else empty)
    monkeypatch.setattr(eis_client.requests, "Session", S)
    found, errors = cli.cmd_fetch(cfg, d(2024, 12, 24), d(2024, 12, 24), laws={"223"})
    assert errors == [] and found == 1
    # 4 архива за день и 1 закупка ждали параллельно: по одной паузе на пакет, а не на каждый архив
    assert sleeps.count(20.0) == 2


def test_parallel_requests_keep_rate_limit(tmp_path, monkeypatch):
    """В несколько потоков запросы идут не чаще min_interval_sec."""
    import time as t
    from eis2nostroy.config import load_config
    from eis2nostroy.eis_client import EisClient
    cfg_text = (Path(__file__).parents[1] / "config.example.toml").read_text(encoding="utf-8")
    (tmp_path / "c.toml").write_text(cfg_text.replace("min_interval_sec = 0.75", "min_interval_sec = 0.05"), encoding="utf-8")
    client = EisClient(load_config(tmp_path / "c.toml"))
    stamps = []

    def call(x):
        client._throttle()
        stamps.append(t.monotonic())
        t.sleep(0.12)  # «медленный ответ ЕИС»
        return x
    start = t.monotonic()
    assert sorted(client._parallel(call, list(range(12)))) == list(range(12))
    stamps.sort()
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert min(gaps) >= 0.045
    # три потока: быстрее, чем по одному (12 × 0.12 = 1.44 с)
    assert t.monotonic() - start < 1.0


def test_retry_after_network_error_uses_new_request_id(tmp_path, monkeypatch):
    """После обрыва связи запрос повторяется с новым index/id (ЕИС отклоняет повтор гуида)."""
    import re
    import requests
    from eis2nostroy.config import load_config
    from eis2nostroy.eis_client import EisClient
    cfg_text = (Path(__file__).parents[1] / "config.example.toml").read_text(encoding="utf-8")
    (tmp_path / "c.toml").write_text(cfg_text.replace("min_interval_sec = 0.75", "min_interval_sec = 0"), encoding="utf-8")
    monkeypatch.setattr("time.sleep", lambda s: None)
    ids = []

    class R:
        status_code, content, text = 200, b"<r/>", ""

    class S:
        trust_env = True
        def post(self, url, data, headers, timeout):
            ids.append(re.search(rb"<id>([^<]+)</id>", data).group(1))
            if len(ids) == 1:
                raise requests.ConnectionError("proxy down")
            return R()
    client = EisClient(load_config(tmp_path / "c.toml"), session=S())
    client._post("223", client.build_reestr_request("RI223", "32514550394"))
    assert len(ids) == 2 and ids[0] != ids[1]
