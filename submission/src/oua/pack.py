"""Loads the evidence pack: catalogue, episodes, profiles, arms, policy config.

Catalogue text (titles, descriptions) is content, never instruction. It is scanned for instruction-like text when
loaded; flagged titles are quarantined from promotion and reported, and no free text from the catalogue is ever
passed to a model.
"""
from __future__ import annotations

import csv
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .timeutil import parse

VALID_DIALECTS = {"d1", "d2", "d3", "d4", "d5", "d6"}

_INJECTION_PATTERNS = [
    r"\bignore (all |your )?(previous|prior) (rules|instructions)",
    r"\bsystem note\b",
    r"\b(assistant|ai|agent)s? instruction",
    r"\bfor ai agents\b",
    r"\bskip (all )?checks\b",
    r"\bmark every user\b",
    r"\b(message|send .*?) to every user\b",
    r"\bpre-approved\b",
    r"\bincluding users who opted out\b",
]
_INJECTION_RE = re.compile("|".join(_INJECTION_PATTERNS), re.IGNORECASE)


def looks_like_instruction(text: str | None) -> str | None:
    """Returns the matched fragment if free text contains instruction-like content aimed at an agent."""
    if not text:
        return None
    m = _INJECTION_RE.search(text)
    return m.group(0) if m else None


@dataclass
class Title:
    title_id: str
    slug: str
    name: str
    dialect: str
    language: str
    content_type: str
    genres: list[str]
    release_date: str
    available_from: datetime
    available_until: datetime | None
    is_premium: bool
    description: str
    injection: str | None = None

    def available_at(self, at: datetime) -> bool:
        return self.available_from <= at and (self.available_until is None or at < self.available_until)


@dataclass
class Pack:
    root: Path
    titles: dict[str, Title] = field(default_factory=dict)
    titles_by_dialect: dict[str, list[Title]] = field(default_factory=lambda: defaultdict(list))
    episodes: dict[str, list[tuple[int, int, str, int]]] = field(default_factory=dict)  # slug -> (s, e, id, minutes)
    profiles: dict[str, list[dict]] = field(default_factory=lambda: defaultdict(list))
    arms: dict[str, str] = field(default_factory=dict)
    goals_doc: dict = field(default_factory=dict)
    interventions: dict = field(default_factory=dict)
    pricing: dict = field(default_factory=dict)
    withdrawn: dict[str, datetime] = field(default_factory=dict)
    popularity: dict[str, dict[str, int]] = field(default_factory=dict)  # dialect -> slug -> plays

    # ---- loading -------------------------------------------------------------------------------------------
    @classmethod
    def load(cls, root: Path) -> "Pack":
        p = cls(root=Path(root))
        with open(p.root / "data/catalogue.csv", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                t = Title(
                    title_id=r["title_id"], slug=r["content_slug"], name=r["title"], dialect=r["dialect"],
                    language=r["language"], content_type=r["content_type"], genres=r["genres"].split("|"),
                    release_date=r["release_date"], available_from=parse(r["available_from"]),
                    available_until=parse(r["available_until"]) if r["available_until"] else None,
                    is_premium=r["is_premium"] == "true", description=r["description"],
                )
                t.injection = looks_like_instruction(t.description) or looks_like_instruction(t.name)
                p.titles[t.title_id] = t
                p.titles_by_dialect[t.dialect].append(t)
        eps: dict[str, list] = defaultdict(list)
        with open(p.root / "data/episodes.csv", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                eps[r["content_slug"]].append((int(r["season"]), int(r["episode"]), r["episode_id"], int(r["minutes"])))
        p.episodes = {k: sorted(v) for k, v in eps.items()}
        with open(p.root / "data/profiles_snapshot.csv", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                p.profiles[r["user_id"]].append(r)
        with open(p.root / "replay/arms.csv", encoding="utf-8") as f:
            p.arms = {r["user_id"]: r["arm"] for r in csv.DictReader(f)}
        p.goals_doc = json.loads((p.root / "config/goals.json").read_text(encoding="utf-8"))
        p.interventions = json.loads((p.root / "config/interventions.json").read_text(encoding="utf-8"))
        p.pricing = json.loads((p.root / "config/pricing.json").read_text(encoding="utf-8"))
        return p

    def compute_popularity(self, events) -> None:
        """Plays per content_slug per dialect over the history window (for popular_in_dialect rows)."""
        pop: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for e in events:
            if e["event"] == "play_started":
                t = self.titles.get((e.get("properties") or {}).get("title_id"))
                if t:
                    pop[t.dialect][t.slug] += 1
        self.popularity = {d: dict(v) for d, v in pop.items()}

    # ---- queries -------------------------------------------------------------------------------------------
    def title_valid(self, title_id: str, dialect: str, at: datetime) -> tuple[bool, str]:
        t = self.titles.get(title_id)
        if t is None:
            return False, "unknown_title"
        if t.dialect != dialect:
            return False, f"wrong_dialect:{t.dialect}"
        if title_id in self.withdrawn and at >= self.withdrawn[title_id]:
            return False, "withdrawn"
        if not t.available_at(at):
            return False, "not_available"
        if t.injection:
            return False, "quarantined_injected_text"
        return True, "ok"

    def withdraw(self, title_id: str, at: datetime) -> None:
        self.withdrawn[title_id] = at

    def injected_titles(self) -> list[Title]:
        return [t for t in self.titles.values() if t.injection]

    def plan_price(self, plan_id: str) -> int | None:
        for pl in self.pricing.get("plans", []):
            if pl["plan_id"] == plan_id:
                return pl["price_inr"]
        return None

    def valid_prices(self) -> set[int]:
        return {pl["price_inr"] for pl in self.pricing.get("plans", [])}
