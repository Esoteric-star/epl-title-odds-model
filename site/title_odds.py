# Published copy of src/title_odds.py from EPL Tally (commit ca1dcb7).
# Only change from production: the CACHE_DIR default path.

"""Title-odds model + shared data helpers.

Single source of truth for "settled by end of matchday k" / gap / n_remaining
so the audit script (scripts/title_odds_test.py) and the app's leaderboard
chart never compute it two different ways.

Matches are bucketed by their EFFECTIVE matchday (see effective_matchday()
below), not blindly by the `matchday` column -- a postponed-and-replayed
fixture is credited to whichever matchday's time window its actual kickoff
falls into, not its original nominal slot. See is_matchday_complete() for
how a matchday is judged "done" in the presence of a still-pending
postponement.
"""
import hashlib
import json
import os
from pathlib import Path

import numpy as np

import sqlite3

from db import CURRENT_SEASON, DB_PATH
from players import parse_utc
from settle import score

MASON_ID, KATIE_ID = 1, 2

# ---------------------------------------------------------------------------
# Model: symmetric-prior Dirichlet-multinomial title-odds simulation
# ---------------------------------------------------------------------------
CATS = [(m, k) for m in (0, 1, 3) for k in (0, 1, 3)]
GAP = np.array([m - k for m, k in CATS])
EXACT = np.array([(m == 3) - (k == 3) for m, k in CATS])

BASE_MARGINAL = {3: 0.105, 1: 0.305, 0: 0.59}
BASE = 5 * np.array([BASE_MARGINAL[m] * BASE_MARGINAL[k] for m, k in CATS])

SIMS = 10_000
# implied_gap_sd(200, var_per_match_diff, 380) ~= 27.9 points, using this
# season's actual per-match (mason-katie) variance so far (var ~= 1.087
# over the 90 matches settled, ties included) -- i.e. at strength=200 the
# prior alone implies a season-end points gap with ~28-point standard
# deviation, before any real results are mixed in. See implied_gap_sd()
# below; recompute if BASE_MARGINAL or STRENGTH changes.
STRENGTH = 200
MODEL_VERSION = "joint-dirichlet-s200-v1"


def symmetric_prior(counts, strength):
    joint = (counts + BASE).reshape(3, 3)
    sym = (joint + joint.T) / 2
    return strength * (sym / sym.sum()).ravel()


def mason_title_odds(settled, gap_pts, gap_exact, n_remaining, strength=STRENGTH, sims=SIMS, seed=0):
    if n_remaining == 0:
        return float(gap_pts > 0 or (gap_pts == 0 and gap_exact > 0))
    counts = np.array([settled.count(c) for c in CATS])
    alpha = symmetric_prior(counts, strength) + counts
    rng = np.random.default_rng(seed)
    p = rng.dirichlet(alpha, size=sims)
    n = rng.multinomial(n_remaining, p)
    final_gap = gap_pts + n @ GAP
    final_exact = gap_exact + n @ EXACT
    return ((final_gap > 0) | ((final_gap == 0) & (final_exact > 0))).mean()


def implied_gap_sd(strength, var_per_match_diff, n_matches=380):
    """How strong is `strength`, in plain terms? The standard deviation of
    the final season-end points gap that the prior alone implies, if
    per-match (mason-katie) point differences had variance
    `var_per_match_diff` and nothing else were known. A documentation/
    calibration aid -- not used by the model at runtime."""
    return n_matches * np.sqrt(var_per_match_diff / (strength + 1))


# ---------------------------------------------------------------------------
# Data access (read-only queries; caller owns the connection)
# ---------------------------------------------------------------------------
def load_matches(conn, season=CURRENT_SEASON):
    return conn.execute(
        """SELECT id, matchday, utc_date, home, away, home_score, away_score, status
           FROM matches
           WHERE season = ? AND competition = 'PL'
           ORDER BY matchday, utc_date""",
        (season,),
    ).fetchall()


def load_predictions(conn, player_id):
    rows = conn.execute(
        "SELECT match_id, pred_home, pred_away FROM predictions WHERE player_id = ?",
        (player_id,),
    ).fetchall()
    return {r["match_id"]: (r["pred_home"], r["pred_away"]) for r in rows}


def pts_for(preds_map, match):
    pred = preds_map.get(match["id"])
    if not pred or pred[0] is None or pred[1] is None:
        return 0, 0  # missing prediction = 0
    return score(pred[0], pred[1], match["home_score"], match["away_score"])


# ---------------------------------------------------------------------------
# Matchday completion + postponement handling
# ---------------------------------------------------------------------------
def _matchday_windows(matches):
    """Each nominal matchday's window is [its earliest non-POSTPONED
    kickoff, the next matchday's earliest non-POSTPONED kickoff). Built
    only from non-postponed matches so a postponed fixture's stale
    original date can never skew its own matchday's window. The last
    matchday's window is open-ended (None)."""
    starts = {}
    for m in matches:
        if m["status"] == "POSTPONED":
            continue
        md = m["matchday"]
        dt = parse_utc(m["utc_date"])
        if md not in starts or dt < starts[md]:
            starts[md] = dt

    ordered = sorted(starts.items(), key=lambda kv: kv[1])
    windows = {}
    for i, (md, start) in enumerate(ordered):
        end = ordered[i + 1][1] if i + 1 < len(ordered) else None
        windows[md] = (start, end)
    return windows


def effective_matchday(match, windows):
    """Which matchday's window actually contains this match's kickoff --
    its own nominal matchday for a normally-scheduled fixture, or whichever
    later matchday's window the kickoff now falls into for a postponed one
    that's since been rescheduled and played."""
    dt = parse_utc(match["utc_date"])
    nominal = match["matchday"]

    start, end = windows.get(nominal, (dt, None))
    if start <= dt and (end is None or dt < end):
        return nominal

    for md, (start, end) in windows.items():
        if start <= dt and (end is None or dt < end):
            return md
    return nominal  # no window claims it (e.g. still-POSTPONED, no real kickoff yet)


def _all_non_postponed_finished(md, matches):
    """The 'normal' rule only, no escape hatch: every non-postponed
    fixture nominally in this matchday has finished."""
    nominal_matches = [m for m in matches if m["matchday"] == md]
    non_postponed = [m for m in nominal_matches if m["status"] != "POSTPONED"]
    return bool(non_postponed) and all(m["status"] == "FINISHED" for m in non_postponed)


def is_matchday_complete(md, matches):
    """A matchday is done when every one of its non-postponed fixtures has
    finished, OR when some STRICTLY LATER matchday is itself complete
    under that normal rule (real time has moved past md and finished a
    whole later matchday, postponed fixture or not -- we don't let one
    stuck fixture block the whole season's progression). A single fixture
    from a later matchday merely being brought forward and played early
    does NOT trip this: that later matchday must be fully done on its own
    terms, not just contain one finished match."""
    if _all_non_postponed_finished(md, matches):
        return True
    later_mds = {m["matchday"] for m in matches if m["matchday"] > md}
    return any(_all_non_postponed_finished(later_md, matches) for later_md in later_mds)


# ---------------------------------------------------------------------------
# Shared GW-by-GW breakdown -- the single definition of "settled by end of k"
# ---------------------------------------------------------------------------
def gameweek_breakdown(matches, mason_preds, katie_preds):
    """Yield one dict per completed matchday (see is_matchday_complete), in
    ascending nominal-matchday order, with cumulative totals as of the end
    of that matchday (i.e. "settled by end of k"). A match's points are
    credited to its EFFECTIVE matchday (see effective_matchday), not
    necessarily the matchday being yielded."""
    total_season_matches = len(matches)
    windows = _matchday_windows(matches)

    finished_by_effective_md = {}
    for m in matches:
        if m["status"] == "FINISHED":
            emd = effective_matchday(m, windows)
            finished_by_effective_md.setdefault(emd, []).append(m)

    nominal_mds = sorted({m["matchday"] for m in matches})

    mason_cum = katie_cum = 0
    mason_exact_cum = katie_exact_cum = 0
    finished_count_cum = 0
    settled = []
    settled_matches = []  # (id, home_score, away_score) for every settled match so far

    for md in nominal_mds:
        if not is_matchday_complete(md, matches):
            break

        gw_matches = finished_by_effective_md.get(md, [])
        if not gw_matches:
            continue  # nothing newly settled in this matchday's bucket

        for m in gw_matches:
            mp, me = pts_for(mason_preds, m)
            kp, ke = pts_for(katie_preds, m)
            mason_cum += mp
            katie_cum += kp
            mason_exact_cum += me
            katie_exact_cum += ke
            settled.append((mp, kp))
            settled_matches.append((m["id"], m["home_score"], m["away_score"]))

        finished_count_cum += len(gw_matches)
        n_remaining = total_season_matches - finished_count_cum

        yield {
            "matchday": md,
            "gw_matches": gw_matches,
            "settled": list(settled),
            "settled_matches": list(settled_matches),
            "mason_cum": mason_cum,
            "katie_cum": katie_cum,
            "mason_exact_cum": mason_exact_cum,
            "katie_exact_cum": katie_exact_cum,
            "gap_pts": mason_cum - katie_cum,
            "gap_exact": mason_exact_cum - katie_exact_cum,
            "n_remaining": n_remaining,
            "first_kickoff": min(m["utc_date"] for m in gw_matches),
        }


# ---------------------------------------------------------------------------
# Cache: one JSON file per season, keyed by matchday
# ---------------------------------------------------------------------------
def _cache_dir():
    return Path(os.environ.get("CACHE_DIR", "cache"))


def _cache_path(season):
    d = _cache_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d / f"title_odds_{season.replace('/', '-')}.json"


def _settled_hash(settled, settled_matches=()):
    """Cache entries are keyed by matchday alone, so the hash is what
    actually guards against staleness. Hashing just the derived (mason_pts,
    katie_pts) pairs misses a corrected result that happens not to change
    either player's points (e.g. 2-0 corrected to 3-0 is still a "home
    win" for both predictions) -- folding in the raw (id, home_score,
    away_score) tuples for every settled match means ANY score correction
    forces a recompute, not just ones that happen to move the points."""
    payload = (tuple(settled), tuple(settled_matches))
    return hashlib.sha256(repr(payload).encode()).hexdigest()


def _load_cache(path):
    if path.is_file():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_cache(path, data):
    """Atomic write: build the file fully in a temp file in the SAME
    directory, then os.replace() it into place -- a reader can never see a
    half-written cache file, even if two writers race."""
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------
def odds_by_matchday(conn, season=CURRENT_SEASON):
    """Returns a list of dicts: matchday (0 = a 50/50-ish point before GW1),
    mason_pct, katie_pct, gap_pts, gap_exact, mason_cum, first_kickoff.
    Computes (and updates the cache) for any matchday whose settled-match
    hash or MODEL_VERSION doesn't match what's cached."""
    matches = load_matches(conn, season)
    mason_preds = load_predictions(conn, MASON_ID)
    katie_preds = load_predictions(conn, KATIE_ID)

    cache_path = _cache_path(season)
    cache = _load_cache(cache_path)
    entries_cache = cache.get("entries", {}) if cache.get("model_version") == MODEL_VERSION else {}
    dirty = cache.get("model_version") != MODEL_VERSION

    def resolve(key, settled, settled_matches, gap_pts, gap_exact, n_remaining, seed, first_kickoff, mason_cum):
        nonlocal dirty
        h = _settled_hash(settled, settled_matches)
        cached = entries_cache.get(key)
        if cached and cached.get("hash") == h:
            mason_pct = cached["mason_pct"]
        else:
            mason_pct = mason_title_odds(settled, gap_pts, gap_exact, n_remaining, seed=seed)
            entries_cache[key] = {
                "hash": h,
                "mason_pct": mason_pct,
                "gap_pts": gap_pts,
                "gap_exact": gap_exact,
                "mason_cum": mason_cum,
                "first_kickoff": first_kickoff,
            }
            dirty = True
        return mason_pct

    results = []

    # matchday 0: the pre-season point, before any match is settled
    pre_n_remaining = len(matches)
    pre_first_kickoff = min((m["utc_date"] for m in matches), default=None)
    mason_pct0 = (
        resolve("0", [], [], 0, 0, pre_n_remaining, 0, pre_first_kickoff, 0)
        if pre_n_remaining
        else 0.5
    )
    results.append(
        {
            "matchday": 0,
            "mason_pct": mason_pct0,
            "katie_pct": 1 - mason_pct0,
            "gap_pts": 0,
            "gap_exact": 0,
            "mason_cum": 0,
            "first_kickoff": pre_first_kickoff,
        }
    )

    for entry in gameweek_breakdown(matches, mason_preds, katie_preds):
        k = entry["matchday"]
        mason_pct = resolve(
            str(k),
            entry["settled"],
            entry["settled_matches"],
            entry["gap_pts"],
            entry["gap_exact"],
            entry["n_remaining"],
            k,
            entry["first_kickoff"],
            entry["mason_cum"],
        )
        results.append(
            {
                "matchday": k,
                "mason_pct": mason_pct,
                "katie_pct": 1 - mason_pct,
                "gap_pts": entry["gap_pts"],
                "gap_exact": entry["gap_exact"],
                "mason_cum": entry["mason_cum"],
                "first_kickoff": entry["first_kickoff"],
            }
        )

    if dirty:
        _save_cache(cache_path, {"model_version": MODEL_VERSION, "entries": entries_cache})

    return results


def refresh_odds_cache(season=CURRENT_SEASON):
    """Entry point for the fetch cron: recompute (and atomically write) the
    odds cache right after new results land, so the page itself only ever
    needs to read the cache -- see load_cached_odds(). Opens the DB
    read-only (no schema init, no migrate, no write capability at all) --
    this function has no business touching the DB file, only reading it."""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return odds_by_matchday(conn, season)
    finally:
        conn.close()


def load_cached_odds(season=CURRENT_SEASON):
    """Pure cache read -- no DB connection at all. Returns the same list of
    dicts odds_by_matchday() would return, or None if there's no usable
    cache yet (first run before the cron has ever populated it), in which
    case the caller should fall back to odds_by_matchday()."""
    cache = _load_cache(_cache_path(season))
    if cache.get("model_version") != MODEL_VERSION:
        return None
    entries = cache.get("entries", {})
    if not entries:
        return None

    results = []
    for key in sorted(entries, key=int):
        e = entries[key]
        mason_pct = e["mason_pct"]
        results.append(
            {
                "matchday": int(key),
                "mason_pct": mason_pct,
                "katie_pct": 1 - mason_pct,
                "gap_pts": e["gap_pts"],
                "gap_exact": e["gap_exact"],
                "mason_cum": e.get("mason_cum", 0),
                "first_kickoff": e["first_kickoff"],
            }
        )
    return results
