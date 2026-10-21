"""Запуск модуля на компьютере ассоциации.

    python run.py probe        — проверка токена на одной закупке (1–2 минуты)
    python run.py load 2026    — загрузка протоколов и договоров 223-ФЗ за год
    python run.py reconcile    — сверка и Excel для оператора

Токен ЕИС спрашивается при запуске и никуда не записывается.
Можно заранее задать переменную окружения EIS_TOKEN.
"""
import getpass
import logging
import os
import sys
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
os.chdir(HERE)

from eis2nostroy import cli  # noqa: E402


def need_token():
    if not os.environ.get("EIS_TOKEN"):
        t = getpass.getpass("Вставьте токен ЕИС (символы не видны) и нажмите Enter: ").strip()
        if not t:
            sys.exit("Токен не введён.")
        os.environ["EIS_TOKEN"] = t


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "probe"
    (HERE / "data" / "logs").mkdir(parents=True, exist_ok=True)
    log_file = HERE / "data" / "logs" / f"{cmd}_{date.today():%Y%m%d}.log"
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(log_file, encoding="utf-8"), logging.StreamHandler()])
    if cmd == "probe":
        need_token()
        rc = cli.main(["-c", "config.toml", "probe"])
        print("\nПРОВЕРКА ПРОЙДЕНА" if rc == 0 else "\nПРОВЕРКА НЕ ПРОЙДЕНА — пришлите файл лога из папки data\\logs")
        return rc
    if cmd == "load":
        need_token()
        year = int(sys.argv[2]) if len(sys.argv) > 2 else date.today().year
        start = date(year, 1, 1)
        end = min(date(year, 12, 31), date.today() - timedelta(days=1))
        rc = cli.main(["-c", "config.toml", "fetch", "--from", start.isoformat(), "--to", end.isoformat()])
        cli.main(["-c", "config.toml", "reconcile"])
        return rc
    if cmd == "reconcile":
        return cli.main(["-c", "config.toml", "reconcile"])
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
