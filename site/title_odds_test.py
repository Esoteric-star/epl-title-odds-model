#!/usr/bin/env python
"""READ-ONLY test script: GW-by-GW title-odds trace under several prior
strengths, plus a pure-luck (equal-skill) baseline.

Rules this script follows:
  - opens its target DB read-only (file:...?mode=ro), never writes to it
  - never touches app code, never restarts anything
  - imports the repo's own scoring logic (src/settle.score) and the shared
    model/data helpers (src/title_odds.py) instead of reimplementing them,
    and calls the repo's own leaderboard() route function
    (src/app.leaderboard) to cross-check final totals

This worktree has no epl.db of its own (gitignored, and this is a preview
checkout) -- DATABASE_PATH picks the DB to read, same env var db.py itself
honours, defaulting to the preview copy at /tmp/epl-preview.db rather than
to a real epl.db that doesn't exist here.

Run with: venv/bin/python scripts/title_odds_test.py
Or:       DATABASE_PATH=/path/to/some.db venv/bin/python scripts/title_odds_test.py
"""
import os
import sqlite3
import sys
from pathlib import Path

import numpy as np
from scipy.stats import norm

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

DB_FILE = Path(os.environ.get("DATABASE_PATH", "/tmp/epl-preview.db"))
DB_URI = f"file:{DB_FILE}?mode=ro"


def ro_connect():
    conn = sqlite3.connect(DB_URI, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# Scoring + model + "settled by end of k" helpers are the repo's real,
# shared implementations -- used as-is, not reimplemented here.
from title_odds import (  # noqa: E402
    MASON_ID,
    KATIE_ID,
    mason_title_odds,
    load_matches,
    load_predictions,
    pts_for,
    gameweek_breakdown,
)

STRENGTHS = (20, 50, 100, 200)


# ---------------------------------------------------------------------------
# Step 1: inspect schema
# ---------------------------------------------------------------------------
def print_schema():
    conn = ro_connect()
    for table in ("matches", "predictions"):
        print(f"=== schema: {table} ===")
        for r in conn.execute(f"PRAGMA table_info({table})"):
            print(f"  {r['cid']:>2}  {r['name']:<12} {r['type']}")
    conn.close()
    print()


# ---------------------------------------------------------------------------
# Call the real leaderboard() route function, guarded so that importing
# app.py (which seeds players on module load) can never write to epl.db.
# Every write statement becomes a no-op; SELECTs pass through to a genuine
# read-only connection.
# ---------------------------------------------------------------------------
def get_route_leaderboard():
    os.environ.setdefault("APP_PASSCODE", "unused-for-this-readonly-check")
    os.environ.setdefault("FLASK_SECRET_KEY", "unused-for-this-readonly-check")

    import db as db_module

    class _NoopCursor:
        def fetchall(self):
            return []

        def fetchone(self):
            return None

    class _GuardedConn:
        def __init__(self):
            self._inner = ro_connect()

        def execute(self, sql, params=()):
            first_word = sql.strip().split(None, 1)[0].upper()
            if first_word in ("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP"):
                return _NoopCursor()
            return self._inner.execute(sql, params)

        def executescript(self, *_a, **_kw):
            pass

        def commit(self):
            pass

        def close(self):
            self._inner.close()

    db_module.get_conn = lambda: _GuardedConn()

    import app as app_module  # imported only after get_conn is guarded

    return app_module.leaderboard()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    print_schema()

    conn = ro_connect()
    matches = load_matches(conn)
    mason_preds = load_predictions(conn, MASON_ID)
    katie_preds = load_predictions(conn, KATIE_ID)
    conn.close()

    entries = list(gameweek_breakdown(matches, mason_preds, katie_preds))

    # Fixed "pure luck" sigma: per-match (mason - katie) point diff over
    # ALL settled matches in the season, including ties (diff = 0). This is
    # held constant across every GW row -- it's the equal-skill baseline,
    # not a running estimate, and it's test-script-only: it must never be
    # ported into the app's title-odds model (which only ever sees matches
    # settled by end of k).
    all_diffs = [mp - kp for mp, kp in (entries[-1]["settled"] if entries else [])]
    sigma_luck = float(np.std(all_diffs)) if all_diffs else 0.0

    header = (
        f"{'GW':>3} | {'gap':>5} | {'n_remaining':>11} | {'luck':>8} | "
        + " | ".join(f"{'s' + str(s):>8}" for s in STRENGTHS)
    )
    print(header)
    print("-" * len(header))

    oracle_rows = []  # (gw, model_s200, luck) for the oracle check below

    for entry in entries:
        gw = entry["matchday"]
        gap_pts = entry["gap_pts"]
        n_remaining = entry["n_remaining"]

        luck = None
        if n_remaining == 0:
            luck_str = "n/a"
        elif sigma_luck == 0:
            luck_str = "skip(sigma=0)"
        else:
            luck = norm.cdf(gap_pts / (sigma_luck * np.sqrt(n_remaining)))
            luck_str = f"{luck:.4f}"

        s_vals = [
            mason_title_odds(
                entry["settled"], gap_pts, entry["gap_exact"], n_remaining, strength=s, seed=gw
            )
            for s in STRENGTHS
        ]
        if luck is not None:
            model_s200 = s_vals[STRENGTHS.index(200)]
            oracle_rows.append((gw, model_s200, luck))

        print(
            f"{gw:>3} | {gap_pts:>5} | {n_remaining:>11} | {luck_str:>8} | "
            + " | ".join(f"{v:>8.4f}" for v in s_vals)
        )

    mason_cum = entries[-1]["mason_cum"] if entries else 0
    katie_cum = entries[-1]["katie_cum"] if entries else 0
    mason_exact_cum = entries[-1]["mason_exact_cum"] if entries else 0
    katie_exact_cum = entries[-1]["katie_exact_cum"] if entries else 0

    print()
    print(f"Final cumulative -- Mason: {mason_cum} pts ({mason_exact_cum} exact)   "
          f"Katie: {katie_cum} pts ({katie_exact_cum} exact)")
    print(f"sigma_luck (ties included, n={len(all_diffs)}): {sigma_luck:.4f}")

    # ------------------------------------------------------------------
    # Cross-check against the real leaderboard() route function
    # ------------------------------------------------------------------
    route_board = {row["name"]: row for row in get_route_leaderboard()}
    route_mason = route_board.get("Mason", {})
    route_katie = route_board.get("Katie", {})

    mason_match = (
        route_mason.get("points") == mason_cum and route_mason.get("exact") == mason_exact_cum
    )
    katie_match = (
        route_katie.get("points") == katie_cum and route_katie.get("exact") == katie_exact_cum
    )

    print()
    print("Leaderboard cross-check (via app.leaderboard()):")
    print(f"  Mason: route={route_mason.get('points')}pts/{route_mason.get('exact')}exact  "
          f"computed={mason_cum}pts/{mason_exact_cum}exact  -> {'PASS' if mason_match else 'FAIL'}")
    print(f"  Katie: route={route_katie.get('points')}pts/{route_katie.get('exact')}exact  "
          f"computed={katie_cum}pts/{katie_exact_cum}exact  -> {'PASS' if katie_match else 'FAIL'}")
    print(f"  OVERALL: {'PASS' if (mason_match and katie_match) else 'FAIL'}")

    # ------------------------------------------------------------------
    # Joint 3x3 counts + correlation coefficient
    # ------------------------------------------------------------------
    settled = entries[-1]["settled"] if entries else []
    print()
    print("Joint outcome counts (rows = Mason pts, cols = Katie pts):")
    cats = (0, 1, 3)
    col_header = "        " + "".join(f"{k:>8}" for k in cats)
    print(col_header)
    for m in cats:
        row = "".join(f"{settled.count((m, k)):>8}" for k in cats)
        print(f"Mason={m:<2} {row}")

    mason_pts_list = [mp for mp, _ in settled]
    katie_pts_list = [kp for _, kp in settled]
    corr = float(np.corrcoef(mason_pts_list, katie_pts_list)[0, 1]) if settled else float("nan")

    print()
    print(f"Total settled matches: {len(settled)}")
    print(f"Correlation(Mason pts, Katie pts) = {corr:.4f}")

    # ------------------------------------------------------------------
    # Oracle check: model (strength=200, matching production STRENGTH)
    # vs. the equal-skill luck formula, for every finished matchday.
    # ------------------------------------------------------------------
    print()
    print("Oracle check: model (s200) vs luck formula, per finished matchday")
    print(f"{'GW':>3} | {'model_s200':>10} | {'luck':>8} | {'|diff|':>8}")
    max_diff = 0.0
    all_within = True
    for gw, model_val, luck_val in oracle_rows:
        diff = abs(model_val - luck_val)
        max_diff = max(max_diff, diff)
        within = diff < 0.15
        all_within &= within
        print(f"{gw:>3} | {model_val:>10.4f} | {luck_val:>8.4f} | {diff:>8.4f}{'' if within else '  FAIL'}")
    print(f"\nmax |model_s200 - luck| = {max_diff:.4f}  (threshold 0.15)  -> {'PASS' if all_within else 'FAIL'}")
    assert all_within, f"model vs luck formula diverged by {max_diff:.4f} (> 0.15) on at least one matchday"


if __name__ == "__main__":
    main()
