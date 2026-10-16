"""Сверка источников и присвоение статусов."""
from __future__ import annotations

from datetime import date

from .models import (CONFIRMED, DECLARED, IN_NOSTROY, MISMATCH, OUT_OF_PERIOD, PROBABLE,
                     Contract, Member, ReconciledRecord)

# Чем выше, тем надёжнее источник для «основной» записи
PRIORITY = {"eis_44": 5, "eis_223": 5, "eis_615": 5, "notice": 3, "finpod": 2, "nostroy": 1}


def _close(a: float | None, b: float | None, pct: float) -> bool:
    if a is None or b is None:
        return True
    if a == b:
        return True
    return abs(a - b) <= max(abs(a), abs(b)) * pct / 100


def _days(a: date | None, b: date | None) -> int | None:
    if a is None or b is None:
        return None
    return abs((a - b).days)


def same_contract(a: Contract, b: Contract, tol_days: int, tol_pct: float) -> bool:
    if a.supplier_inn != b.supplier_inn:
        return False
    if a.reg_number and b.reg_number:
        return a.reg_number == b.reg_number
    if a.notice_number and b.notice_number and a.notice_number == b.notice_number:
        return True
    if a.customer_inn and b.customer_inn and a.customer_inn != b.customer_inn:
        return False
    d = _days(a.sign_date, b.sign_date)
    if d is None or d > tol_days:
        return False
    # без номера считаем совпадением только при близкой цене
    return a.price is not None and b.price is not None and _close(a.price, b.price, tol_pct)


def cluster(contracts: list[Contract], tol_days: int, tol_pct: float) -> list[list[Contract]]:
    groups: list[list[Contract]] = []
    for c in contracts:
        for g in groups:
            if any(same_contract(c, x, tol_days, tol_pct) for x in g):
                g.append(c)
                break
        else:
            groups.append([c])
    return groups


def period_start(member: Member, competitive: bool | None, competitive_from: date,
                 noncompetitive_from: date) -> date:
    if competitive is False:
        return noncompetitive_from
    start = competitive_from
    # без даты уровня ОДО берём дату вступления в СРО
    anchor = member.odo_date or member.joined
    if anchor and anchor > start:
        start = anchor
    return start


def _family(source: str) -> str:
    return "eis" if source.startswith("eis_") else source


def reconcile(members: dict[str, Member], contracts: list[Contract], competitive_from: date,
              noncompetitive_from: date, tol_days: int = 3, tol_pct: float = 1.0) -> list[ReconciledRecord]:
    by_member: dict[str, list[Contract]] = {}
    for c in contracts:
        if c.supplier_inn in members:
            by_member.setdefault(c.supplier_inn, []).append(c)

    records: list[ReconciledRecord] = []
    for inn, items in by_member.items():
        member = members[inn]
        for group in cluster(items, tol_days, tol_pct):
            group.sort(key=lambda c: -PRIORITY.get(c.source, 0))
            primary = group[0]
            families = {_family(c.source) for c in group}
            issues = []
            prices = [c.price for c in group if c.price is not None]
            if prices and not all(_close(prices[0], p, tol_pct) for p in prices):
                issues.append("суммы в источниках различаются: " +
                              ", ".join(f"{c.source} {c.price:,.2f}".replace(",", " ") for c in group if c.price is not None))
            dates = [c.sign_date for c in group if c.sign_date]
            if dates and max(dates) != min(dates) and (max(dates) - min(dates)).days > tol_days:
                issues.append("даты в источниках различаются: " +
                              ", ".join(f"{c.source} {c.sign_date:%d.%m.%Y}" for c in group if c.sign_date))
            competitive = next((c.competitive for c in group if c.competitive is not None), None)
            start = period_start(member, competitive, competitive_from, noncompetitive_from)

            if primary.sign_date and primary.sign_date < start:
                status = OUT_OF_PERIOD
            elif issues:
                status = MISMATCH
            elif "nostroy" in families:
                status = IN_NOSTROY
            elif "eis" in families:
                status = CONFIRMED
            elif "notice" in families:
                status = DECLARED
            elif "finpod" in families:
                status = PROBABLE
            else:
                status = PROBABLE

            records.append(ReconciledRecord(
                member=member, status=status, primary=primary,
                sources=sorted({c.source for c in group}), issues=issues, others=group[1:],
            ))
    records.sort(key=lambda r: (r.member.inn, r.primary.sign_date or date.min))
    return records
