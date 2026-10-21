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
from .protocols import WinIndex, WinStore, unlinked_wins
from .reconcile import reconcile
from .sources import ContractStore, load_table_source

log = logging.getLogger("eis2nostroy")


def _store(cfg: Config) -> ContractStore:
    return ContractStore((cfg.path("work_dir") or Path("data/work")) / "eis_contracts.jsonl")


def _wins(cfg: Config) -> WinStore:
    return WinStore((cfg.path("work_dir") or Path("data/work")) / "protocol_wins.jsonl")


def _days(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def cmd_fetch(cfg: Config, start: date, end: date) -> tuple[int, list[str]]:
    """Сначала протоколы за весь период, потом договоры.

    Договор 223-ФЗ без поставщика связывается с членом СРО через протокол,
    поэтому к разбору договоров победы из протоколов уже должны быть известны.
    """
    members = load_members(cfg.path("members"))
    inns = set(members)
    client = EisClient(cfg)
    store, wins_store = _store(cfg), _wins(cfg)
    found, wins_found, errors = 0, 0, []
    feeds = sorted(cfg.feeds, key=lambda f: 0 if f.kind == "protocol" else 1)
    index = None
    for feed in feeds:
        if feed.kind != "protocol" and index is None:
            index = WinIndex(wins_store.load())
        for day in _days(start, end):
            for region in cfg.feed_regions(feed):
                try:
                    for archive in client.fetch_day(feed, region, day):
                        contracts, wins = scan_archive(archive, feed.law, inns, index)
                        found += store.add(contracts)
                        wins_found += wins_store.add(wins)
                except (EisError, OSError) as exc:
                    msg = f"{day} {feed.subsystem} регион {region}: {exc}"
                    log.error(msg)
                    errors.append(msg)
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
