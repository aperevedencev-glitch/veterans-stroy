"""Разбор архивов ЕИС: из XML договоров берём те, где поставщик — член СРО.

Схемы XML у 44-ФЗ, 223-ФЗ и 615-ПП разные и меняются с версиями ЕИС,
поэтому разбор устойчивый: ищем поля по локальным именам элементов, а не
по жёстким путям. ИНН поставщика ищется внутри блоков, в названии которых
есть supplier / participant / contractor / executor.
"""
from __future__ import annotations

import io
import logging
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

from .models import Contract
from .protocols import ProtocolWin, WinIndex, contracts_from_223, is_protocol, parse_protocol_xml
from .tabular import to_date, to_float

log = logging.getLogger(__name__)

SUPPLIER_BLOCKS = ("supplier", "participant", "contractor", "executor", "winner")
CUSTOMER_BLOCKS = ("customer", "placer", "organization")
REG_NUM = ("regnum", "registrynum", "contractregnum", "registrationnumber", "regnumber")
NOTICE_NUM = ("notificationnumber", "purchasenumber", "purchasenoticenumber", "noticenumber")
SIGN_DATE = ("signdate", "contractdate", "conclusiondate", "concludecontractdate", "contractsigndate")
PRICE = ("price", "pricerur", "contractprice", "sum", "summ", "amount")
SUBJECT = ("contractsubject", "subject", "purchaseobjectinfo")
NONCOMPETITIVE_HINTS = ("singlesupplier", "единственн", "неконкурент", "noncompetitive")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _first_text(el: ET.Element, names: tuple[str, ...]) -> str:
    for e in el.iter():
        if _local(e.tag) in names and e.text and e.text.strip():
            return e.text.strip()
    return ""


def _inns_in(el: ET.Element) -> list[str]:
    return [e.text.strip() for e in el.iter() if _local(e.tag) == "inn" and e.text and e.text.strip()]


def _blocks(root: ET.Element, keys: tuple[str, ...]) -> list[ET.Element]:
    found = []
    for e in root.iter():
        name = _local(e.tag)
        if any(k in name for k in keys):
            # берём самые внешние блоки, без вложенных повторов
            if not any(e is f or _contains(f, e) for f in found):
                found.append(e)
    return found


def _contains(parent: ET.Element, child: ET.Element) -> bool:
    return any(x is child for x in parent.iter())


def parse_contract_xml(data: bytes, law: str, member_inns: set[str], raw_file: str = "") -> list[Contract]:
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        log.warning("Не разобран XML %s: %s", raw_file, exc)
        return []
    if is_protocol(root):
        # протокол — не договор: победителей разбирает protocols.py
        return []
    supplier_blocks = _blocks(root, SUPPLIER_BLOCKS)
    supplier_inns = {inn for b in supplier_blocks for inn in _inns_in(b)}
    hits = supplier_inns & member_inns
    if not hits:
        return []
    customer_blocks = _blocks(root, CUSTOMER_BLOCKS)
    customer_inn = next((i for b in customer_blocks for i in _inns_in(b) if i not in supplier_inns), "")
    customer_name = ""
    for b in customer_blocks:
        customer_name = _first_text(b, ("fullname", "shortname", "name"))
        if customer_name:
            break
    method_text = " ".join(
        (e.text or "") + " " + _local(e.tag) for e in root.iter()
        if _local(e.tag) in ("placingway", "purchasemethod", "purchasecodename", "purchasemethodname",
                             "foundation", "singlecustomer", "singlesupplier", "competitive")
    ).lower()
    competitive = None
    if method_text.strip():
        competitive = not any(h in method_text for h in NONCOMPETITIVE_HINTS)
    price_el = None
    for e in root.iter():
        if _local(e.tag) in PRICE and e.text and to_float(e.text) is not None:
            price_el = e
            break
    result = []
    for inn in sorted(hits):
        result.append(Contract(
            source=f"eis_{law}",
            law=law,
            supplier_inn=inn,
            reg_number=_first_text(root, REG_NUM),
            notice_number=_first_text(root, NOTICE_NUM),
            customer_inn=customer_inn,
            customer_name=customer_name,
            sign_date=to_date(_first_text(root, SIGN_DATE)),
            price=to_float(price_el.text) if price_el is not None else None,
            subject=_first_text(root, SUBJECT)[:500],
            competitive=competitive,
            raw_file=raw_file,
        ))
    return result


def scan_xml(data: bytes, law: str, member_inns: set[str], wins: WinIndex | None,
             raw_file: str = "") -> tuple[list[Contract], list[ProtocolWin]]:
    """Один XML: договор члена СРО, победа в протоколе или ничего."""
    if law == "223" and b"urchaseProtocol" in data[:4000]:
        return [], parse_protocol_xml(data, member_inns, raw_file)
    found = parse_contract_xml(data, law, member_inns, raw_file)
    if not found and law == "223" and wins:
        found = contracts_from_223(data, wins, raw_file)
    return found, []


def scan_archive(path: Path, law: str, member_inns: set[str],
                 wins: WinIndex | None = None) -> tuple[list[Contract], list[ProtocolWin]]:
    """Разбирает zip-архив ЕИС (в том числе вложенные zip)."""
    contracts: list[Contract] = []
    protocol_wins: list[ProtocolWin] = []

    def walk(zf: zipfile.ZipFile, prefix: str):
        for info in zf.infolist():
            name = info.filename
            if name.lower().endswith(".zip"):
                with zipfile.ZipFile(io.BytesIO(zf.read(info))) as inner:
                    walk(inner, f"{prefix}{name}/")
            elif name.lower().endswith(".xml"):
                c, w = scan_xml(zf.read(info), law, member_inns, wins, f"{prefix}{name}")
                contracts.extend(c)
                protocol_wins.extend(w)

    with zipfile.ZipFile(path) as zf:
        walk(zf, f"{path.name}:")
    return contracts, protocol_wins


def parse_archive(path: Path, law: str, member_inns: set[str], wins: WinIndex | None = None) -> list[Contract]:
    return scan_archive(path, law, member_inns, wins)[0]
