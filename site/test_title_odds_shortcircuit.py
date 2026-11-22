#!/usr/bin/env python3
"""Self-contained grader for the mason_title_odds decided-season short-circuit.

epl-title-odds-model is a published copy: title_odds.py imports db/players/
settle, which are NOT shipped here. We stub them in sys.modules before import,
so this grader runs on numpy alone -- no DB, no app.py, no preview .db.
"""
import sys
import types
from datetime import datetime
from pathlib import Path

_db = types.ModuleType("db")
_db.CURRENT_SEASON = "2026/27"
_db.DB_PATH = ":memory:"
sys.modules["db"] = _db

_players = types.ModuleType("players")
_players.parse_utc = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00"))
sys.modules["players"] = _players

_settle = types.ModuleType("settle")
_settle.score = lambda ph, pa, hs, a_s: (0, 0)
sys.modules["settle"] = _settle

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np  # noqa: E402
import title_odds as T  # noqa: E402


class _NoSim(Exception):
    pass


class _guard:
    """Inside this block, building an RNG raises, so a call that returns
    without raising provably did not simulate."""
    def __enter__(self):
        self._orig = T.np.random.default_rng
        def _boom(*a, **k):
            raise _NoSim()
        T.np.random.default_rng = _boom
        return self

    def __exit__(self, *a):
        T.np.random.default_rng = self._orig
        return False


def f(gap_pts, gap_exact, n_remaining, seed=0):
    return T.mason_title_odds([], gap_pts, gap_exact, n_remaining, seed=seed)


def main():
    # 1. Strictly decided seasons must short-circuit (no sim) and be exact.
    for n in range(1, 21):
        with _guard():
            assert f(3 * n + 1, 0, n) == 1.0, f"not clinched at gap={3*n+1}, n={n}"
            assert f(-3 * n - 1, 0, n) == 0.0, f"not lost at gap={-3*n-1}, n={n}"

    # 2. Boundary gap == +/-3n must NOT short-circuit: a final gap of 0 is
    #    reachable and the exact-score tiebreak then decides, so it must sim.
    for n in (1, 2, 3):
        for gap in (3 * n, -3 * n):
            with _guard():
                try:
                    f(gap, 0, n)
                except _NoSim:
                    pass
                else:
                    raise AssertionError(f"wrongly short-circuited boundary gap={gap}, n={n}")
            v = f(gap, 0, n)
            assert 0.0 < v < 1.0, f"boundary gap={gap}, n={n} gave {v}"

    # 3. Finished season (n_remaining == 0) unchanged: pure tiebreak, ties to Katie.
    assert f(1, 0, 0) == 1.0
    assert f(0, 1, 0) == 1.0
    assert f(0, 0, 0) == 0.0
    assert f(-1, 0, 0) == 0.0

    # 4. Determinism preserved for a normal simulated case.
    assert f(2, 0, 50, seed=7) == f(2, 0, 50, seed=7)

    print("OK: all short-circuit checks passed")


if __name__ == "__main__":
    main()
