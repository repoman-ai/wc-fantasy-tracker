#!/usr/bin/env python3
"""
Fantasy Soccer World Cup 2026 — pool scraper.

Scrapes:
  1. The pool leaderboard table  (<div id="table-rounds">)
  2. Each player's full prediction page (stage 1196 / grand-total)

Outputs (written only when a scrape succeeds and has data):
  rankings.json     current leaderboard snapshot
  history.json      append-only log of leaderboard snapshots (deduped on no change)
  predictions.json  per-player fixture/bonus breakdown, keyed by team_id

Design notes
------------
* No hardcoded player names / row counts — everything is extracted per run.
* Player set can grow/shrink between runs; history.json keys everything by team_id
  so the UI can chart a varying set of players over time.
* Network is fetched with `requests`. The pages are server-rendered (the table data
  is present in the initial HTML — see the samples), so no headless browser is needed.
  If the site ever switches to client-side rendering, swap `fetch()` for a Playwright
  call; nothing else changes.
* Retries: the whole run is retried up to MAX_ATTEMPTS times with RETRY_GAP_SECONDS
  between attempts if the leaderboard yields zero rows or throws. A single player page
  failing does NOT abort the run — that player keeps their previously-scraped data.

Usage
-----
  python scraper.py                       # live scrape
  python scraper.py --samples             # parse the local sample HTML files (offline test)
  python scraper.py --out ./data          # write JSON into ./data instead of repo root
  MAX_ATTEMPTS=4 RETRY_GAP_SECONDS=1800 python scraper.py
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except ImportError:  # pragma: no cover
    ZoneInfo = None

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
BASE = "https://www.tournamentsoccer.us"
POOL_ID = "34193"
POOL_SLUG = "bobby-and-friends"
LEADERBOARD_URL = f"{BASE}/fantasy-soccer-world-cup/2026/pool/{POOL_ID}/{POOL_SLUG}/"
PLAYER_URL_TMPL = f"{BASE}/fantasy-soccer-world-cup/2026/team/{{team_id}}/{{slug}}/stage/1196-grand-total"

# Flag SVGs reuse the source CDN (the codes match exactly, e.g. FRA/ESP/RSA).
FLAG_URL_TMPL = "https://assets.tournamentsoccer.us/flags/4x3/{code}.svg"

MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", "4"))          # 1 try + 3 retries
RETRY_GAP_SECONDS = int(os.environ.get("RETRY_GAP_SECONDS", "1800"))  # 30 minutes
REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", "30"))
HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

FLAG_RE = re.compile(r"flags/4x3/([A-Za-z0-9]+)\.svg")
TEAM_HREF_RE = re.compile(r"/team/(\d+)/([^/]+)/")


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def soup_of(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def to_int(text: str | None):
    """Return an int if `text` is a clean integer, else None ('-' -> None)."""
    t = clean(text)
    if not t or t == "-":
        return None
    m = re.search(r"-?\d+", t.replace(",", ""))
    return int(m.group()) if m else None


def flag_code_from_style(style: str | None):
    if not style:
        return None
    m = FLAG_RE.search(style)
    return m.group(1).upper() if m else None


def to_utc_iso(data_datetime: str | None, tz_name: str | None):
    """Interpret 'YYYY-MM-DD HH:MM' as wall-clock time in `tz_name`, return UTC ISO 8601."""
    if not data_datetime:
        return None
    try:
        naive = datetime.strptime(data_datetime.strip(), "%Y-%m-%d %H:%M")
    except ValueError:
        return None
    if ZoneInfo and tz_name:
        try:
            aware = naive.replace(tzinfo=ZoneInfo(tz_name))
        except Exception:
            aware = naive.replace(tzinfo=timezone.utc)
    else:
        aware = naive.replace(tzinfo=timezone.utc)
    return aware.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# Networking
# --------------------------------------------------------------------------- #
def fetch(url: str) -> str:
    resp = requests.get(url, headers=HTTP_HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.text


# --------------------------------------------------------------------------- #
# Leaderboard parsing
# --------------------------------------------------------------------------- #
def parse_leaderboard(html: str) -> list[dict]:
    """Return a list of player dicts in rank order. Ad rows (<tr> without id) are skipped."""
    soup = soup_of(html)
    container = soup.find(id="table-rounds")
    scope = container if container else soup
    table = scope.find("table")
    if not table:
        return []

    players = []
    for tr in table.select("tbody > tr"):
        row_id = tr.get("id")
        if not row_id:  # ad row
            continue
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 4:
            continue

        rank = to_int(tds[0].get_text())

        link = tds[1].find("a", href=True)
        if not link:
            continue
        name = clean(link.get_text())
        href = link["href"]
        m = TEAM_HREF_RE.search(href)
        if not m:
            continue
        team_id, slug = m.group(1), m.group(2)

        flag_span = tds[1].select_one("span.flag-icon")
        flag = flag_code_from_style(flag_span.get("style")) if flag_span else None

        exact = to_int(tds[2].get_text()) or 0
        points = to_int(tds[3].get_text()) or 0

        players.append(
            {
                "team_id": team_id,
                "slug": slug,
                "name": name,
                "flag": flag,
                "rank": rank,
                "exact": exact,
                "points": points,
            }
        )
    return players


# --------------------------------------------------------------------------- #
# Player-page parsing
# --------------------------------------------------------------------------- #
def _team_from_col(col) -> dict:
    """Extract {code, name, hidden} from a .col div (flag+name, or eye-slash = hidden)."""
    if col is None:
        return {"code": None, "name": None, "hidden": False}
    if col.select_one(".fa-eye-slash"):
        return {"code": None, "name": None, "hidden": True}
    flag = col.select_one(".flag-icon")
    code = flag_code_from_style(flag.get("style")) if flag else None
    # Prefer the long (desktop) name span, else the short, else whatever text remains.
    long_name = col.select_one(".d-none.d-md-inline-block")
    short_name = col.select_one(".d-md-none")
    name = clean(long_name.get_text()) if long_name else (
        clean(short_name.get_text()) if short_name else clean(col.get_text())
    )
    return {"code": code, "name": name or None, "hidden": False}


def _parse_group_fixture(tds, fixture_number, round_name) -> dict:
    date_td, match_td, pred_td, score_td, pts_td = tds[0], tds[1], tds[2], tds[3], tds[4]

    badge = date_td.select_one(".badge")
    jstz = date_td.select_one(".js-tz")
    dt_raw = jstz.get("data-datetime") if jstz else None
    tz_name = jstz.get("data-timezone") if jstz else None

    cols = match_td.find_all("div", class_="col")
    home = _team_from_col(cols[0]) if len(cols) > 0 else {"code": None, "name": None, "hidden": False}
    away = _team_from_col(cols[1]) if len(cols) > 1 else {"code": None, "name": None, "hidden": False}

    predicted_score = clean(pred_td.get_text())
    actual_score = clean(score_td.get_text())
    points = to_int(pts_td.select_one(".dotted_underline").get_text()) if pts_td.select_one(".dotted_underline") else to_int(pts_td.get_text())

    status = clean(badge.get_text()) if badge else (actual_score or None)

    return {
        "fixture_number": fixture_number,
        "round": round_name,
        "stage_type": "group",
        "status": status,
        "kickoff_utc": to_utc_iso(dt_raw, tz_name),
        "kickoff_local": dt_raw,
        "kickoff_timezone": tz_name,
        "hidden": False,
        "match": {"home": home, "away": away},
        "predicted": {"type": "score", "score": predicted_score or None, "hidden": False},
        "actual_score": actual_score or None,
        "points": points,
        "is_exact": points == 90,
    }


def _parse_knockout_fixture(tds, fixture_number, round_name) -> dict:
    date_td = tds[0]
    match_td = tds[1]      # composite: "Predicted" teams + "Score" (bracket slots)
    score_td = tds[2]      # actual result (NS / real score)
    pts_td = tds[3]        # Countries pts + Score pts

    badge = date_td.select_one(".badge")
    jstz = date_td.select_one(".js-tz")
    dt_raw = jstz.get("data-datetime") if jstz else None
    tz_name = jstz.get("data-timezone") if jstz else None

    # Split the composite cell into labelled sections, preferring desktop rows.
    sections: dict[str, dict] = {}
    current = None
    for child in match_td.find_all("div", recursive=False):
        cls = child.get("class", [])
        if "small" in cls:
            current = clean(child.get_text())
            sections.setdefault(current, {"desktop": [], "mobile": []})
        elif "row" in cls:
            sections.setdefault(current, {"desktop": [], "mobile": []})
            bucket = "mobile" if "d-md-none" in cls else "desktop"
            sections[current][bucket].append(child)

    def pick(label):
        s = sections.get(label)
        if not s:
            return None
        rows = s["desktop"] or s["mobile"]
        return rows[0] if rows else None

    # Player's predicted teams (or hidden) live under "Predicted".
    pred_row = pick("Predicted")
    pred_cols = pred_row.find_all("div", class_="col") if pred_row else []
    p_home = _team_from_col(pred_cols[0]) if len(pred_cols) > 0 else {"code": None, "name": None, "hidden": True}
    p_away = _team_from_col(pred_cols[1]) if len(pred_cols) > 1 else {"code": None, "name": None, "hidden": True}
    hidden = bool(p_home.get("hidden") or p_away.get("hidden"))

    # The actual matchup identity (bracket slot labels like "2A"/"W73") lives under "Score".
    slot_row = pick("Score")
    slot_cols = slot_row.find_all("div", class_="col") if slot_row else []
    m_home = {"code": None, "name": clean(slot_cols[0].get_text()) or None, "hidden": False} if len(slot_cols) > 0 else {"code": None, "name": None, "hidden": False}
    m_away = {"code": None, "name": clean(slot_cols[1].get_text()) or None, "hidden": False} if len(slot_cols) > 1 else {"code": None, "name": None, "hidden": False}

    # Actual result
    actual_link = score_td.find("a")
    actual_score = clean(actual_link.get_text()) if actual_link else clean(score_td.get_text())

    # Points split into Countries + Score components.
    du = pts_td.select_one(".dotted_underline")
    pts_countries = to_int(du.get_text()) if du else None
    # The score-component points are the trailing text after the second label.
    pts_text_nodes = [clean(t) for t in pts_td.find_all(string=True) if clean(t) and clean(t) not in ("Countries", "Score")]
    pts_score = None
    if pts_text_nodes:
        # last meaningful node is the score points ('-' or a number)
        pts_score = to_int(pts_text_nodes[-1])
    total = (pts_countries or 0) + (pts_score or 0)
    if pts_countries is None and pts_score is None:
        total = None

    status = clean(badge.get_text()) if badge else (actual_score or None)

    return {
        "fixture_number": fixture_number,
        "round": round_name,
        "stage_type": "knockout",
        "status": status,
        "kickoff_utc": to_utc_iso(dt_raw, tz_name),
        "kickoff_local": dt_raw,
        "kickoff_timezone": tz_name,
        "hidden": hidden,
        "match": {"home": m_home, "away": m_away},
        "predicted": {
            "type": "teams",
            "hidden": hidden,
            "home": None if hidden else p_home,
            "away": None if hidden else p_away,
        },
        "actual_score": actual_score or None,
        "points": total,
        "points_breakdown": {"countries": pts_countries, "score": pts_score},
        "is_exact": False,
    }


def _parse_fixture(tr, round_name):
    fixture_number = to_int(tr.get("data-fixture-number"))
    tds = tr.find_all("td", recursive=False)
    try:
        if len(tds) >= 5:
            return _parse_group_fixture(tds, fixture_number, round_name)
        if len(tds) == 4:
            return _parse_knockout_fixture(tds, fixture_number, round_name)
    except Exception as exc:  # never let one weird row kill the page
        return {
            "fixture_number": fixture_number,
            "round": round_name,
            "stage_type": "unknown",
            "status": None,
            "error": f"{type(exc).__name__}: {exc}",
        }
    return None


def _pref_text(cell) -> str:
    """Prefer the desktop (long) variant of a cell that has mobile/desktop spans."""
    long = cell.select_one(".d-none.d-md-inline-block")
    short = cell.select_one(".d-md-none")
    if long:
        return clean(long.get_text())
    if short:
        return clean(short.get_text())
    return clean(cell.get_text())


def _parse_bonus(nested_table) -> dict:
    out = {"questions": [], "subtotal": None}
    body = nested_table.find("tbody") or nested_table
    for tr in body.find_all("tr", recursive=False):
        if tr.find("th"):  # "Bonus Questions" header
            continue
        tds = tr.find_all("td", recursive=False)
        if not tds:
            continue
        label = clean(tds[0].get_text())
        last_val = to_int(tds[-1].get_text())
        if label.lower() == "subtotal":
            out["subtotal"] = last_val
            continue
        if label.lower() == "total":
            out["grand_total"] = last_val
            continue
        if len(tds) < 4:
            continue
        pred_cell, actual_cell = tds[1], tds[2]
        pred_flag = pred_cell.select_one(".flag-icon")
        actual_flag = actual_cell.select_one(".flag-icon")
        out["questions"].append(
            {
                "question": label,
                "prediction": {
                    "text": _pref_text(pred_cell) or None,
                    "flag": flag_code_from_style(pred_flag.get("style")) if pred_flag else None,
                },
                "actual": {
                    "text": _pref_text(actual_cell) or None,
                    "flag": flag_code_from_style(actual_flag.get("style")) if actual_flag else None,
                },
                "points": last_val,
            }
        )
    return out


def parse_player_page(html: str) -> dict:
    """Return {matchdays:[...], bonus:{...}, grand_total, totals}."""
    soup = soup_of(html)
    # The outer matches table is the first .table-sm (the bonus table is nested inside it).
    outer = soup.find("table", class_="table-sm")
    if not outer:
        return {"matchdays": [], "bonus": {"questions": []}, "grand_total": None}
    tbody = outer.find("tbody")
    rows = tbody.find_all("tr", recursive=False) if tbody else []

    matchdays: list[dict] = []
    current = None
    bonus = {"questions": []}
    grand_total = None

    for tr in rows:
        cls = tr.get("class", [])
        if "d-md-none" in cls:
            continue  # mobile-only duplicate

        # Round / matchday header row
        th = tr.find("th", recursive=False)
        if th is not None:
            current = {"round": clean(th.get_text()), "subtotal": None, "fixtures": []}
            matchdays.append(current)
            continue

        # Bonus block: a row whose cell contains a nested table
        nested = tr.find("table")
        if nested is not None:
            bonus = _parse_bonus(nested)
            grand_total = bonus.pop("grand_total", None)
            continue

        # Fixture row
        if tr.get("data-fixture-number") is not None:
            fixture = _parse_fixture(tr, current["round"] if current else None)
            if fixture:
                if current is None:
                    current = {"round": None, "subtotal": None, "fixtures": []}
                    matchdays.append(current)
                current["fixtures"].append(fixture)
            continue

        # Subtotal row (outer table)
        tds = tr.find_all("td", recursive=False)
        if tds and clean(tds[0].get_text()).lower() == "subtotal":
            if current is not None:
                current["subtotal"] = to_int(tds[-1].get_text())
            continue

    return {
        "matchdays": matchdays,
        "bonus": bonus,
        "grand_total": grand_total,
    }


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def scrape_once(get_leaderboard_html, get_player_html, prev_predictions: dict) -> tuple[list[dict], dict]:
    """Run a single scrape pass. Returns (players, predictions_by_team_id)."""
    leaderboard_html = get_leaderboard_html()
    players = parse_leaderboard(leaderboard_html)
    if not players:
        raise RuntimeError("Leaderboard produced 0 rows")

    predictions = {}
    for p in players:
        tid = p["team_id"]
        try:
            html = get_player_html(p)
            parsed = parse_player_page(html)
            predictions[tid] = {
                "team_id": tid,
                "name": p["name"],
                "slug": p["slug"],
                "flag": p["flag"],
                "rank": p["rank"],
                "points": p["points"],
                "exact": p["exact"],
                "grand_total": parsed["grand_total"],
                "matchdays": parsed["matchdays"],
                "bonus": parsed["bonus"],
            }
        except Exception as exc:
            print(f"  ! player {tid} ({p['name']}) failed: {exc}", file=sys.stderr)
            # Keep previous data for this player rather than wiping it.
            if tid in prev_predictions:
                kept = dict(prev_predictions[tid])
                kept.update(
                    {"rank": p["rank"], "points": p["points"], "exact": p["exact"], "name": p["name"], "flag": p["flag"]}
                )
                predictions[tid] = kept
    return players, predictions


def build_rankings(players: list[dict]) -> dict:
    return {
        "updated": now_iso(),
        "pool": {"id": POOL_ID, "slug": POOL_SLUG, "name": POOL_SLUG.replace("-", " ").title()},
        "players": [
            {
                "team_id": p["team_id"],
                "slug": p["slug"],
                "name": p["name"],
                "flag": p["flag"],
                "rank": p["rank"],
                "points": p["points"],
                "exact": p["exact"],
            }
            for p in players
        ],
    }


def snapshot_signature(players: list[dict]) -> str:
    """Stable signature of a leaderboard snapshot for dedup (ignores timestamp)."""
    rows = sorted(
        ((p["team_id"], p["rank"], p["points"], p["exact"]) for p in players),
        key=lambda r: r[0],
    )
    return json.dumps(rows)


def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return default
    return default


def write_json(path: Path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def run(out_dir: Path, samples: bool):
    out_dir.mkdir(parents=True, exist_ok=True)
    rankings_path = out_dir / "rankings.json"
    history_path = out_dir / "history.json"
    predictions_path = out_dir / "predictions.json"

    prev_predictions = load_json(predictions_path, {}).get("players", {})

    if samples:
        here = Path(__file__).resolve().parent
        lb_html = (here / "sample-leaderboard.html").read_text()
        player_html = (here / "sample-player-page.html").read_text()
        get_lb = lambda: lb_html
        # Only JJ (247528) has a sample page; others fall back gracefully.
        get_player = lambda p: player_html if p["team_id"] == "247528" else "<html></html>"
        attempts = 1
    else:
        get_lb = lambda: fetch(LEADERBOARD_URL)
        get_player = lambda p: fetch(PLAYER_URL_TMPL.format(team_id=p["team_id"], slug=p["slug"]))
        attempts = MAX_ATTEMPTS

    players, predictions = [], {}
    last_err = None
    for attempt in range(1, attempts + 1):
        try:
            print(f"Attempt {attempt}/{attempts} …", file=sys.stderr)
            players, predictions = scrape_once(get_lb, get_player, prev_predictions)
            print(f"  ok: {len(players)} players, {len(predictions)} prediction pages", file=sys.stderr)
            break
        except Exception as exc:
            last_err = exc
            print(f"  attempt failed: {exc}", file=sys.stderr)
            if attempt < attempts:
                print(f"  waiting {RETRY_GAP_SECONDS}s before retry …", file=sys.stderr)
                time.sleep(RETRY_GAP_SECONDS)

    if not players:
        print(f"FATAL: all attempts failed; not writing files. Last error: {last_err}", file=sys.stderr)
        return 1

    # rankings.json
    rankings = build_rankings(players)
    write_json(rankings_path, rankings)

    # history.json — append only if the snapshot actually changed.
    history = load_json(history_path, [])
    sig = snapshot_signature(players)
    last_sig = snapshot_signature(history[-1]["players"]) if history else None
    if sig != last_sig:
        history.append(
            {
                "timestamp": rankings["updated"],
                "players": [
                    {
                        "team_id": p["team_id"],
                        "name": p["name"],
                        "slug": p["slug"],
                        "flag": p["flag"],
                        "rank": p["rank"],
                        "points": p["points"],
                        "exact": p["exact"],
                    }
                    for p in players
                ],
            }
        )
        write_json(history_path, history)
        print(f"  history: appended snapshot ({len(history)} total)", file=sys.stderr)
    else:
        print("  history: unchanged since last snapshot, not appending", file=sys.stderr)

    # predictions.json
    write_json(predictions_path, {"updated": rankings["updated"], "players": predictions})

    print("Done.", file=sys.stderr)
    return 0


def main():
    ap = argparse.ArgumentParser(description="Scrape the fantasy soccer pool.")
    ap.add_argument("--samples", action="store_true", help="parse local sample HTML (offline test)")
    ap.add_argument("--out", default=".", help="output directory for JSON files (default: repo root)")
    args = ap.parse_args()
    sys.exit(run(Path(args.out).resolve(), args.samples))


if __name__ == "__main__":
    main()
