from __future__ import annotations

from dataclasses import dataclass, field, asdict

IN, OUT, UNKNOWN = "in", "out", "unknown"


@dataclass
class Offer:
    store: str
    title: str
    url: str
    status: str = UNKNOWN          # "in" | "out" | "unknown"
    price: float | None = None     # selling price in INR
    denomination: int | None = None  # face value in INR, parsed from the title
    note: str = ""
    reminder: bool = False           # set when an alert repeats for something already in stock

    @property
    def key(self) -> str:
        return f"{self.store}|{self.url}"

    @property
    def markup_pct(self) -> float | None:
        if self.price and self.denomination:
            return round((self.price / self.denomination - 1) * 100, 1)
        return None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["markup_pct"] = self.markup_pct
        d.pop("reminder", None)
        return d


@dataclass
class StoreResult:
    store: str
    offers: list[Offer] = field(default_factory=list)
    error: str = ""
