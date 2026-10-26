"""Относится ли договор к Градостроительному кодексу (ст. 55.8 ГрК РФ).

В реестр НОСТРОЙ вносятся договоры строительного подряда и подряда на снос
объектов капитального строительства. Признаки берутся из кода закупки:

ИКЗ (44-ФЗ), 36 цифр:
  1–2   год размещения извещения
  3–22  код заказчика (ИКУ)
  23–29 номер закупки в плане-графике
  30–33 код ОКПД2 до подкласса без точек (например, 4120 = 41.20)
  34–36 код вида расходов (КВР)

Позиции вынесены в настройки ([grk] okpd_pos, kvr_pos) — их можно поправить
по приказу Минфина о порядке формирования ИКЗ, не меняя код.

У договоров 223-ФЗ ИКЗ нет — признак берётся из кодов ОКПД2 позиций договора.
"""
from __future__ import annotations

import re

GRK = "ГрК"                 # строительство, реконструкция, капремонт, снос
GRK_PROBABLE = "Вероятно ГрК"  # строительные работы, но может быть текущий ремонт
NOT_GRK = "Не ГрК"
UNKNOWN = "Нет данных"

# ОКПД2: 41.2 — здания, 42 — сооружения, 43 — специализированные работы (в т.ч. снос).
# 41.1 («разработка строительных проектов») к строительному подряду не относится.
CONSTRUCTION = ("41.2", "42.", "43.")
EXCLUDED = ("41.1",)
# КВР: 243 — закупка в целях капитального ремонта; 4xx — капитальные вложения
# (например, 414 — бюджетные инвестиции в объекты капитального строительства).
KVR_GRK = re.compile(r"^(243|4\d\d)$")


def okpd_from_ikz(ikz: str, pos: tuple[int, int] = (30, 33)) -> str:
    """'4120' → '41.20'; '0000' или нет кода → ''."""
    digits = re.sub(r"\D", "", ikz or "")
    if len(digits) != 36:
        return ""
    code = digits[pos[0] - 1:pos[1]]
    if not code.strip("0"):
        return ""
    return f"{code[:2]}.{code[2:]}"


def kvr_from_ikz(ikz: str, pos: tuple[int, int] = (34, 36)) -> str:
    digits = re.sub(r"\D", "", ikz or "")
    return digits[pos[0] - 1:pos[1]] if len(digits) == 36 else ""


def is_construction(okpd: str) -> bool:
    okpd = (okpd or "").strip()
    if not okpd or okpd.startswith(EXCLUDED):
        return False
    norm = okpd if "." in okpd else f"{okpd[:2]}.{okpd[2:]}"
    return norm.startswith(CONSTRUCTION) or norm[:3] in ("42.", "43.")


def classify(ikz: str = "", okpd_codes: list[str] | None = None,
             okpd_pos: tuple[int, int] = (30, 33), kvr_pos: tuple[int, int] = (34, 36)) -> tuple[str, str]:
    """Возвращает (класс, пояснение)."""
    okpd_codes = [c for c in (okpd_codes or []) if c]
    ikz_okpd = okpd_from_ikz(ikz, okpd_pos)
    kvr = kvr_from_ikz(ikz, kvr_pos)
    codes = [ikz_okpd] if ikz_okpd else okpd_codes
    if not codes:
        return UNKNOWN, "нет ни ИКЗ с кодом ОКПД2, ни кодов ОКПД2 в позициях"
    building = [c for c in codes if is_construction(c)]
    src = "ИКЗ" if ikz_okpd else "позиции договора"
    if not building:
        return NOT_GRK, f"ОКПД2 {', '.join(codes[:3])} ({src}) — не строительные работы"
    if kvr and KVR_GRK.match(kvr):
        what = "капремонт" if kvr == "243" else "капитальные вложения"
        return GRK, f"ОКПД2 {building[0]} ({src}), КВР {kvr} — {what}"
    if kvr:
        return GRK_PROBABLE, f"ОКПД2 {building[0]} ({src}), КВР {kvr} — проверить: может быть текущий ремонт"
    return GRK_PROBABLE, f"ОКПД2 {building[0]} ({src}), КВР нет (223-ФЗ) — строительные работы"
