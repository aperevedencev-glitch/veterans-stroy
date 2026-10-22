"""Командная строка.

    python -m eis2nostroy fetch --from 2026-10-01 --to 2026-10-15
    python -m eis2nostroy import-archives --law 44 data/archives/
    python -m eis2nostroy reconcile
    python -m eis2nostroy run --days 3          # ежедневный запуск
"""
from __future__ import annotations

import argparse
import logging
import sys
import traceback
from datetime import date, timedelta
from pathlib import Path

from .alerts import notify
from .config import Config, load_config
from .eis_client import EisClient, EisError
from .export import write_operator_workbook
from .members import load_members
from .models import CONFIRMED, DECLARED, MISMATCH, PROBABLE
from .nostroy import NostroyTransport
from .parser import scan_archive
import json
import zipfile

from .protocols import WinIndex, WinStore, parse_protocol_xml, protocol_types_from_nsi, unlinked_wins
from .reconcile import reconcile
from .sources import ContractStore, load_table_source

log = logging.getLogger("eis2nostroy")


def _store(cfg: Config) -> ContractStore:
    return ContractStore((cfg.path("work_dir") or Path("data/work")) / "eis_contracts.jsonl")


def _wins(cfg: Config) -> WinStore:
    return WinStore((cfg.path("work_dir") or Path("data/work")) / "protocol_wins.jsonl")


def _types_file(cfg: Config) -> Path:
    return (cfg.path("work_dir") or Path("data/work")) / "protocol_types.json"


def _apply_protocol_types(cfg: Config) -> None:
    """Типы протоколов из справочника ЕИС (сохраняет команда probe)."""
    f = _types_file(cfg)
    if not f.exists():
        return
    types = json.loads(f.read_text(encoding="utf-8")).get("protocols") or []
    if types:
        for feed in cfg.feeds:
            if feed.kind == "protocol":
                feed.document_types = list(types)


def _load_nsi_protocol_types(cfg: Config, client: EisClient) -> list[str]:
    folder = (cfg.path("cache_dir") or Path("data/cache")) / "nsi"
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob("nsiDocumentTypes223_*.zip"):
        old.unlink()
    blobs = []
    for z in client.fetch_nsi("nsiDocumentTypes223", folder):
        with zipfile.ZipFile(z) as zf:
            blobs += [zf.read(n) for n in zf.namelist() if n.lower().endswith(".xml")]
    protocols, public = protocol_types_from_nsi(blobs)
    f = _types_file(cfg)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"protocols": protocols, "public": public}, ensure_ascii=False, indent=1),
                 encoding="utf-8")
    return protocols


def _days(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def _make_lookup(client: EisClient, inns: set[str], stats: dict, errors: list[str]):
    """Номер закупки → победы членов СРО в её протоколах (один запрос в ЕИС, с кэшем)."""
    memo: dict[str, list] = {}

    def lookup(notice: str) -> list:
        if notice in memo:
            return memo[notice]
        stats["lookups"] = stats.get("lookups", 0) + 1
        wins = []
        try:
            for archive in client.fetch_by_reestr("RI223", notice):
                with zipfile.ZipFile(archive) as zf:
                    for name in zf.namelist():
                        if name.lower().endswith(".xml"):
                            wins += parse_protocol_xml(zf.read(name), inns, f"{archive.name}:{name}")
        except (EisError, OSError, zipfile.BadZipFile) as exc:
            errors.append(f"закупка {notice}: {exc}")
            log.error("закупка %s: %s", notice, exc)
        memo[notice] = wins
        return wins

    return lookup


def cmd_fetch(cfg: Config, start: date, end: date, newest_first: bool = True) -> tuple[int, list[str]]:
    """Договоры 223-ФЗ и протоколы закупок за период, от свежих дат к старым.

    Основной путь: договор без поставщика со строительными ОКПД2 → протоколы этой
    закупки по её номеру → победитель-член СРО. Поток протоколов по регионам
    (kind = "protocol"), если он задан, идёт первым.
    """
    members = load_members(cfg.path("members"))
    inns = set(members)
    client = EisClient(cfg)
    _apply_protocol_types(cfg)
    store, wins_store = _store(cfg), _wins(cfg)
    found, wins_found, errors = 0, 0, []
    feeds = sorted(cfg.feeds, key=lambda f: 0 if f.kind == "protocol" else 1)
    days = list(_days(start, end))
    if newest_first:
        days.reverse()
    index = None
    win_regions: list[str] = []
    stats: dict = {}
    lookup = _make_lookup(client, inns, stats, errors)
    for feed in feeds:
        if feed.kind != "protocol" and index is None:
            all_wins = wins_store.load()
            index = WinIndex(all_wins)
            # регион заказчика ≈ первые две цифры его ИНН
            win_regions = sorted({w.customer_inn[:2] for w in all_wins if len(w.customer_inn) >= 10})
        regions = cfg.feed_regions(feed)
        if feed.regions and any(r.lower() == "wins" for r in feed.regions):
            regions = sorted(set(cfg.regions) | set(win_regions))
            log.info("%s: регионы заказчиков, где члены побеждали: %s", feed.subsystem, ", ".join(regions))
        use_lookup = lookup if (feed.lookup_protocols and feed.law == "223") else None
        for day in days:
            before = (found, wins_found, stats.get("lookups", 0))
            for region in regions:
                try:
                    for archive in client.fetch_day(feed, region, day):
                        contracts, wins = scan_archive(archive, feed.law, inns, index, use_lookup)
                        found += store.add(contracts)
                        wins_found += wins_store.add(wins)
                except (EisError, OSError) as exc:
                    msg = f"{day} {feed.subsystem} регион {region}: {exc}"
                    log.error(msg)
                    errors.append(msg)
            log.info("%s %s: договоров членов +%s, побед +%s, проверено закупок +%s (всего договоров %s)",
                     feed.subsystem, f"{day:%d.%m.%Y}", found - before[0], wins_found - before[1],
                     stats.get("lookups", 0) - before[2], found)
    log.info("Новых договоров членов: %s, побед по протоколам: %s, ошибок: %s", found, wins_found, len(errors))
    return found, errors


def cmd_import_archives(cfg: Config, law: str, folder: Path) -> int:
    """Разбор архивов, скачанных заранее (например, история за прошлые годы).

    Протоколы и договоры можно класть в одну папку: модуль проходит её дважды —
    сначала собирает победы, потом связывает с ними договоры.
    """
    inns = set(load_members(cfg.path("members")))
    store, wins_store = _store(cfg), _wins(cfg)
    zips = sorted(Path(folder).rglob("*.zip"))
    wins_total = 0
    if law == "223":
        for z in zips:
            wins_total += wins_store.add(scan_archive(z, law, inns)[1])
    index = WinIndex(wins_store.load()) if law == "223" else None
    total = 0
    for z in zips:
        total += store.add(scan_archive(z, law, inns, index)[0])
    log.info("Из архивов добавлено договоров: %s, побед по протоколам: %s", total, wins_total)
    return total


def cmd_probe(cfg: Config, protocol_day: date, contract_day: date, region: str,
              expect_reg: str = "") -> bool:
    """Короткая проверка токена и настроек на одном дне и одном регионе.

    По умолчанию — закупка № 32414075550 ООО «ЛУКОЙЛ-ВОЛГОГРАДЭНЕРГО» (регион 34):
    итоговый протокол опубликован 28.10.2024, редакция договора — 24.12.2024,
    победитель — ООО «ВОЛГОГРАДМОНТАЖ».
    """
    members = load_members(cfg.path("members"))
    inns = set(members)
    client = EisClient(cfg)
    store, wins_store = _store(cfg), _wins(cfg)
    ok = True
    print(f"Членов СРО в списке: {len(inns)}")
    print(f"Сервис ЕИС: {cfg.endpoint('223')}")
    try:
        types = _load_nsi_protocol_types(cfg, client)
        print(f"Справочник типов документов 223-ФЗ получен, типов протоколов: {len(types)}")
        print("   " + ", ".join(types[:40]) + (" ..." if len(types) > 40 else ""))
    except (EisError, OSError, zipfile.BadZipFile) as exc:
        print(f"ОШИБКА справочника nsiDocumentTypes223 (токен или доступ): {exc}")
        ok = False
    _apply_protocol_types(cfg)
    for feed in sorted(cfg.feeds, key=lambda f: 0 if f.kind == "protocol" else 1):
        if feed.law != "223":
            continue
        day = protocol_day if feed.kind == "protocol" else contract_day
        index = None if feed.kind == "protocol" else WinIndex(wins_store.load())
        stats: dict = {}
        errs: list[str] = []
        lookup = _make_lookup(client, inns, stats, errs) if feed.lookup_protocols else None
        try:
            archives = client.fetch_day(feed, region, day)
        except (EisError, OSError) as exc:
            print(f"ОШИБКА {feed.subsystem} {feed.document_types} {day} регион {region}: {exc}")
            ok = False
            continue
        contracts, wins = [], []
        for a in archives:
            c, w = scan_archive(a, feed.law, inns, index, lookup)
            contracts += c
            wins += w
        if lookup is not None:
            print(f"   строительных закупок проверено по протоколам: {stats.get('lookups', 0)}")
            for e in errs[:5]:
                print(f"   ОШИБКА {e}")
            ok = ok and not errs
        store.add(contracts)
        wins_store.add(wins)
        print(f"{feed.subsystem} {', '.join(feed.document_types)} за {day:%d.%m.%Y}, регион {region}: "
              f"архивов {len(archives)}, побед членов {len(wins)}, договоров членов {len(contracts)}")
        for w in wins:
            print(f"   победа: {w.supplier_name} ИНН {w.supplier_inn}, протокол {w.protocol_number}, {w.price}")
        for c in contracts:
            print(f"   договор: {c.reg_number} ИНН {c.supplier_inn}, {c.price}, {c.sign_date}")
        if expect_reg and feed.kind != "protocol":
            hit = any(c.reg_number == expect_reg for c in contracts)
            print(("НАЙДЕН" if hit else "НЕ НАЙДЕН") + f" договор {expect_reg}")
            ok = ok and hit
    return ok


def cmd_reconcile(cfg: Config):
    members = load_members(cfg.path("members"))
    contracts = _store(cfg).load()
    pending = unlinked_wins(_wins(cfg).load(), contracts)
    if pending:
        log.info("Побед по протоколам без найденного договора: %s", len(pending))
        contracts.extend(pending)
    for key, source in (("finpod_import", "finpod"), ("member_notices", "notice"),
                        ("nostroy_registry", "nostroy")):
        p = cfg.path(key)
        if p and p.exists():
            items = load_table_source(p, source)
            log.info("%s: загружено %s записей", p.name, len(items))
            contracts.extend(items)
    m = cfg.matching
    records = reconcile(members, contracts, cfg.competitive_from, cfg.noncompetitive_from,
                        int(m["date_tolerance_days"]), float(m["price_tolerance_pct"]))
    out = cfg.path("output_dir") or Path("data/out")
    book = write_operator_workbook(records, out / f"sverka_{date.today():%Y%m%d}.xlsx",
                                   cfg.raw.get("sro", {}).get("name", ""))
    sent = NostroyTransport(cfg.nostroy, out).send(records)
    counts = {s: sum(r.status == s for r in records) for s in (CONFIRMED, PROBABLE, MISMATCH, DECLARED)}
    summary = (f"Членов: {len(members)}. Записей после сверки: {len(records)}.\n"
               f"К передаче: {counts[CONFIRMED]}. Требуют решения: "
               f"{counts[PROBABLE] + counts[MISMATCH] + counts[DECLARED]} "
               f"(вероятные {counts[PROBABLE]}, расхождения {counts[MISMATCH]}, заявлены членами {counts[DECLARED]}).\n"
               f"Файл для оператора: {book.name}. {sent}")
    log.info(summary)
    return records, summary


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="eis2nostroy", description="Сведения о договорах членов СРО: ЕИС → НОСТРОЙ")
    ap.add_argument("-c", "--config", default="config.toml")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", help="скачать договоры из ЕИС за период")
    f.add_argument("--from", dest="start", required=True, type=date.fromisoformat)
    f.add_argument("--to", dest="end", default=date.today(), type=date.fromisoformat)
    i = sub.add_parser("import-archives", help="разобрать заранее скачанные архивы ЕИС")
    i.add_argument("--law", required=True, choices=["44", "223", "615"])
    i.add_argument("folder", type=Path)
    sub.add_parser("reconcile", help="сверить источники и подготовить выгрузки")
    pr = sub.add_parser("probe", help="проверить токен и настройки 223-ФЗ на одной закупке")
    pr.add_argument("--protocol-day", default="2024-10-28", type=date.fromisoformat)
    pr.add_argument("--contract-day", default="2024-12-24", type=date.fromisoformat)
    pr.add_argument("--region", default="34")
    pr.add_argument("--expect", default="53435098928240001840000")
    r = sub.add_parser("run", help="ежедневный прогон: скачать последние дни и сверить")
    r.add_argument("--days", type=int, default=3)
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(args.config)
    try:
        if args.cmd == "fetch":
            _, errors = cmd_fetch(cfg, args.start, args.end)
            return 1 if errors else 0
        if args.cmd == "import-archives":
            cmd_import_archives(cfg, args.law, args.folder)
            return 0
        if args.cmd == "reconcile":
            cmd_reconcile(cfg)
            return 0
        if args.cmd == "probe":
            return 0 if cmd_probe(cfg, args.protocol_day, args.contract_day, args.region, args.expect) else 1
        if args.cmd == "run":
            end = date.today() - timedelta(days=1)
            _, errors = cmd_fetch(cfg, end - timedelta(days=args.days - 1), end)
            _, summary = cmd_reconcile(cfg)
            if errors:
                summary += "\n\nОшибки загрузки из ЕИС:\n" + "\n".join(errors[:20])
            notify(cfg.alerts, "Сверка договоров: " + ("есть ошибки" if errors else "готово"), summary)
            return 1 if errors else 0
    except Exception:  # noqa: BLE001 — любой сбой должен дойти до оператора
        notify(cfg.alerts, "Сверка договоров: сбой", traceback.format_exc()[-3000:])
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
