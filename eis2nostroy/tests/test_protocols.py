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
