"""Step 3 helpers: build goal-aligned home pages and pick a title for a message.

Only titles that are in the target dialect, available at publish time, not withdrawn and not quarantined are
considered (Pack.title_valid). One language variant per content_slug, preferring the app language.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .facts import Facts
from .pack import Pack, Title
from .timeutil import parse

ROW_LEN = 8
MAX_ROWS = 4  # platform allows 6; four goal-ordered rows keeps the page focused


def candidate_pool(pack: Pack, dialect: str, language: str | None, at: datetime) -> dict[str, Title]:
    """content_slug -> one valid Title in this dialect (app-language variant preferred)."""
    pool: dict[str, Title] = {}
    for t in pack.titles_by_dialect.get(dialect, []):
        ok, _ = pack.title_valid(t.title_id, dialect, at)
        if not ok:
            continue
        cur = pool.get(t.slug)
        if cur is None or (t.language == language and cur.language != language):
            pool[t.slug] = t
    return pool


def _ranked(pack: Pack, dialect: str, titles: list[Title]) -> list[Title]:
    pop = pack.popularity.get(dialect, {})
    return sorted(titles, key=lambda t: (-pop.get(t.slug, 0), t.title_id))


def build_home_page(pack: Pack, fs: Facts, goal_id: str | None, dialect: str, at: datetime) -> dict:
    pool = candidate_pool(pack, dialect, fs.get("language"), at)
    watched = set(fs.get("watched_slugs") or [])
    top_genres = fs.get("top_genres") or []
    fresh = [t for s, t in pool.items() if s not in watched]
    used: set[str] = set()
    rows: list[dict] = []
    notes: list[str] = []

    def take(row_type: str, titles: list[Title]) -> None:
        picked = []
        for t in titles:
            if t.slug in used:
                continue
            picked.append(t)
            if len(picked) == ROW_LEN:
                break
        if len(picked) >= 3:
            used.update(t.slug for t in picked)
            rows.append({"row_type": row_type, "title_ids": [t.title_id for t in picked]})
        else:
            notes.append(f"row {row_type} skipped: only {len(picked)} valid titles in {dialect}")

    unfinished = [u for u in fs.get("unfinished_series") or [] if u["slug"] in pool]
    unfinished_titles = [pool[u["slug"]] for u in unfinished]
    hero: Title | None = None
    if goal_id == "G07" and unfinished_titles:
        hero = unfinished_titles[0]
    elif goal_id in ("G04", "G05", "G06"):
        prem = _ranked(pack, dialect, [t for t in fresh if t.is_premium])
        hero = prem[0] if prem else None
    elif goal_id == "G08":
        new = sorted([t for t in fresh if _is_new(t, at)], key=lambda t: t.release_date, reverse=True)
        hero = new[0] if new else None
    elif goal_id in ("G01", "G02", "G03", "G12"):
        short = _ranked(pack, dialect, [t for t in fresh if t.content_type == "micro_drama"])
        hero = short[0] if short else None
    if hero is None:
        ranked = _ranked(pack, dialect, fresh)
        hero = ranked[0] if ranked else None
    if hero:
        used.add(hero.slug)

    if len(unfinished_titles) >= 3:
        take("next_episode", unfinished_titles)
    elif unfinished and goal_id != "G07":
        notes.append("fewer than 3 unfinished series in this dialect; platform Continue Watching covers them")

    by_genre = _ranked(pack, dialect, [t for t in fresh if top_genres and top_genres[0] in t.genres])
    popular = _ranked(pack, dialect, fresh)
    new_rel = _ranked(pack, dialect, [t for t in fresh if _is_new(t, at)])
    short_first = sorted(popular, key=lambda t: t.content_type != "micro_drama")
    premium = _ranked(pack, dialect, [t for t in fresh if t.is_premium])

    order = {
        "G01": [("popular_in_dialect", short_first), ("new_releases", new_rel), ("because_you_watched", by_genre)],
        "G02": [("popular_in_dialect", short_first), ("because_you_watched", by_genre), ("new_releases", new_rel)],
        "G03": [("popular_in_dialect", short_first), ("because_you_watched", by_genre), ("new_releases", new_rel)],
        "G12": [("because_you_watched", by_genre), ("popular_in_dialect", popular), ("new_releases", new_rel)],
        "G07": [("because_you_watched", by_genre), ("popular_in_dialect", popular)],
        "G08": [("new_releases", new_rel), ("because_you_watched", by_genre), ("popular_in_dialect", popular)],
        "G09": [("because_you_watched", by_genre), ("popular_in_dialect", popular), ("new_releases", new_rel)],
        "G04": [("genre", premium), ("popular_in_dialect", popular), ("new_releases", new_rel)],
        "G05": [("genre", premium), ("popular_in_dialect", popular), ("new_releases", new_rel)],
        "G06": [("genre", premium), ("because_you_watched", by_genre), ("popular_in_dialect", popular)],
    }.get(goal_id or "", [("popular_in_dialect", popular)])
    for row_type, titles in order:
        if len(rows) >= MAX_ROWS:
            break
        if row_type == "because_you_watched" and not top_genres:
            continue
        take(row_type, titles)
    return {"dialect": dialect, "hero_title_id": hero.title_id if hero else None, "rows": rows, "notes": notes,
            "pool_size": len(pool)}


def _is_new(t: Title, at: datetime) -> bool:
    rd = parse(t.release_date + "T00:00:00Z")
    return at - timedelta(days=30) <= rd <= at


def pick_message_title(pack: Pack, fs: Facts, goal_id: str, page: dict | None, at: datetime) -> Title | None:
    """A title for a nudge, in the dialect the viewer's app shows (dialect_messages)."""
    d = fs.get("dialect_messages")
    if not d:
        return None
    if goal_id == "G07":
        for u in fs.get("unfinished_series") or []:
            ok, _ = pack.title_valid(u["title_id"], d, at)
            if ok:
                return pack.titles[u["title_id"]]
        return None
    if page and page.get("hero_title_id") and page["dialect"] == d:
        ok, _ = pack.title_valid(page["hero_title_id"], d, at)
        if ok:
            return pack.titles[page["hero_title_id"]]
    pool = candidate_pool(pack, d, fs.get("language"), at)
    watched = set(fs.get("watched_slugs") or [])
    ranked = _ranked(pack, d, [t for s, t in pool.items() if s not in watched])
    return ranked[0] if ranked else None
