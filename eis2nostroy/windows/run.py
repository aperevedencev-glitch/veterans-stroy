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

# Сертификаты из хранилища Windows: сервис ЕИС подписан российским корневым
# сертификатом (Минцифры), которого нет во встроенном списке Python.
# Проверка подлинности сервера при этом остаётся включённой.
try:
    import truststore  # noqa: E402
    truststore.inject_into_ssl()
except ImportError:
    print("Не установлен пакет truststore: запустите «1 Проверка токена.bat», он доставит его сам.")

from eis2nostroy import cli  # noqa: E402


class Tee:
    """Всё, что видно в окне, пишется и в файл лога."""

    def __init__(self, stream, fh):
        self.stream, self.fh = stream, fh

    def write(self, data):
        self.stream.write(data)
        self.fh.write(data)
        self.fh.flush()
        return len(data)

    def flush(self):
        self.stream.flush()
        self.fh.flush()


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
    fh = open(log_file, "a", encoding="utf-8")
    sys.stdout = Tee(sys.__stdout__, fh)
    sys.stderr = Tee(sys.__stderr__, fh)
    print(f"=== {cmd} {date.today():%d.%m.%Y}, Python {sys.version.split()[0]} ===")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])
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
    try:
        code = main()
    except SystemExit:
        raise
    except BaseException:  # noqa: BLE001 — любой сбой должен попасть в лог
        import traceback
        traceback.print_exc()
        code = 1
    sys.exit(code)
