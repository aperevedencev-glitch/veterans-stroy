"""Загрузка настроек из config.toml."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path


@dataclass
class Feed:
    law: str              # "44", "223", "615"
    endpoint_law: str     # "44" или "223": какой сервер ЕИС
    subsystem: str        # RGK, RD223, RD615
    document_types: list[str]


@dataclass
class Config:
    root: Path
    raw: dict
    feeds: list[Feed] = field(default_factory=list)

    # --- пути -------------------------------------------------------------
    def path(self, key: str) -> Path | None:
        value = self.raw.get("paths", {}).get(key, "")
        if not value:
            return None
        p = Path(value)
        return p if p.is_absolute() else self.root / p

    # --- ЕИС --------------------------------------------------------------
    @property
    def eis(self) -> dict:
        return self.raw.get("eis", {})

    @property
    def token(self) -> str:
        return os.environ.get("EIS_TOKEN") or self.eis.get("token", "")

    def endpoint(self, endpoint_law: str) -> str:
        key = f"{self.eis.get('access', 'LE')}_{endpoint_law}"
        return self.eis["endpoints"][key]

    @property
    def regions(self) -> list[str]:
        return [str(r) for r in self.eis.get("regions", [])]

    # --- период -----------------------------------------------------------
    @property
    def competitive_from(self) -> date:
        return date.fromisoformat(self.raw.get("period", {}).get("competitive_from", "2017-07-01"))

    @property
    def noncompetitive_from(self) -> date:
        return date.fromisoformat(self.raw.get("period", {}).get("noncompetitive_from", "2026-03-01"))

    @property
    def matching(self) -> dict:
        return {"date_tolerance_days": 3, "price_tolerance_pct": 1.0, **self.raw.get("matching", {})}

    @property
    def alerts(self) -> dict:
        return self.raw.get("alerts", {})

    @property
    def nostroy(self) -> dict:
        return self.raw.get("nostroy", {"mode": "file"})


def load_config(path: str | Path) -> Config:
    path = Path(path)
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)
    feeds = [
        Feed(
            law=str(f["law"]),
            endpoint_law=str(f.get("endpoint_law", "44")),
            subsystem=f["subsystem"],
            document_types=list(f.get("document_types", [])),
        )
        for f in raw.get("eis", {}).get("feeds", [])
    ]
    return Config(root=path.parent.resolve(), raw=raw, feeds=feeds)
