# Data

## points_by_match.csv

One row per finished 2026-27 Premier League match, up to the end of gameweek 9. 90 rows.

| Column | Type | What it is |
|---|---|---|
| `gameweek` | integer | Premier League matchday, 1 to 9 |
| `match_id` | integer | football-data.org match ID |
| `mason_pts` | 0, 1 or 3 | Mason's points for the match: 3 exact score, 1 right result, 0 wrong |
| `katie_pts` | 0, 1 or 3 | Katie's points, same rules |
| `mason_exact` | 0 or 1 | 1 if Mason got the exact score |
| `katie_exact` | 0 or 1 | 1 if Katie got the exact score |

Rows are sorted by gameweek, then match ID.

## Checks

These totals match the app's leaderboard after gameweek 9:

| | Points | Exact scores |
|---|---|---|
| Mason | 54 | 9 |
| Katie | 58 | 10 |

Other numbers used in the write-up, all reproducible from this file:

- Both of us scored the same points in 61 of the 90 matches.
- Variance of the per-match difference (`mason_pts - katie_pts`) is 1.087, using population variance.

## Source

Points were worked out from our predictions in EPL Tally and final scores from the football-data.org free tier. No raw scores or fixtures are in this file. Predictions are published with both players' consent.
