"""Дополнительные источники: файлы, которые оператор кладёт в папку.

* Выгрузка «Финподдержки» — записи основаны на протоколах закупок.
* Уведомления членов СРО о договорах (по регламенту, ст. 55.8 ГрК, приказ 655/пр).
* Уже переданные в НОСТРОЙ сведения (выгрузка из Единого реестра).

Столбцы ищутся по смыслу названия, точный формат не важен.
"""
from __future__ import annotations

import json
from pathlib import Path

from .models import Contract
from .tabular import pick, read_rows, to_date, to_float, to_inn


def _law(v) -> str:
    s = str(v or "")
    for law in ("223", "615", "44"):
        if law in s:
            return law
    return ""


def _competitive(v) -> bool | None:
    s = str(v or "").lower()
    if not s:
        return None
    if "неконкур" in s or "единств" in s:
        return False
    if "конкур" in s:
        return True
    return None


def load_table_source(path: Path, source: str) -> list[Contract]:
    out = []
    for row in read_rows(path):
        inn = to_inn(pick(row, "иннчлена", "иннпоставщика", "иннподрядчика", "иннисполнителя", "инн"))
        if len(inn) not in (10, 12):
            continue
        out.append(Contract(
            source=source,
            law=_law(pick(row, "закон", "фз", "основание")),
            supplier_inn=inn,
            reg_number=str(pick(row, "реестровый", "номерконтракта", "номердоговора", "реестр") or "").strip(),
            notice_number=str(pick(row, "извещен", "закупк") or "").strip(),
            customer_inn=to_inn(pick(row, "иннзаказчика")),
            customer_name=str(pick(row, "заказчик") or ""),
            sign_date=to_date(pick(row, "датадоговора", "датаконтракта", "датазаключ", "дата")),
            price=to_float(pick(row, "сумма", "цена", "стоимость")),
            subject=str(pick(row, "предмет", "объект") or "")[:500],
            competitive=_competitive(pick(row, "способ", "конкурент")),
            raw_file=path.name,
        ))
    return out


class ContractStore:
    """Найденные в ЕИС договоры членов: JSONL, без дублей."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _key(c: Contract) -> tuple:
        return (c.source, c.supplier_inn, c.reg_number or c.raw_file, c.sign_date)

    def load(self) -> list[Contract]:
        if not self.path.exists():
            return []
        seen, out = set(), []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            c = Contract.from_json(json.loads(line))
            k = self._key(c)
            if k not in seen:
                seen.add(k)
                out.append(c)
        return out

    def add(self, contracts: list[Contract]) -> int:
        existing = {self._key(c) for c in self.load()}
        new = [c for c in contracts if self._key(c) not in existing]
        with open(self.path, "a", encoding="utf-8") as fh:
            for c in new:
                fh.write(json.dumps(c.to_json(), ensure_ascii=False) + "\n")
        return len(new)
