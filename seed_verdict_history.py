#!/usr/bin/env python3
"""
seed_verdict_history.py — one-time historical backfill of
cache["VERDICT_HISTORY"] from backtest_indicators.py's already-generated,
already-reviewed daily-matrix CSVs (backtest_output/daily_indicator_matrix_
*.csv). Not part of the daily automated pipeline, not run automatically by
update_dashboard.py's main() — a deliberate, one-time, human-triggered
backfill, same "reuse the real artifacts, don't reconstruct" discipline as
everything else in this project.

Each CSV already has the exact walk-forward "Cycle Bottom/Momentum Shift/
Cycle Top Weighted %" columns build_verdict()/_top()/_momentum() themselves
produced (see backtest_indicators.py's own "Walk-forward weighted verdict
percentages" block) — this script only parses those three columns back out
and appends them via ud.verdict_history_append(), tagged source=
"backtest_seed" so the provenance is honestly distinguishable from a
genuinely live day's own real run, even though the chart itself doesn't
visually distinguish the two (per spec).

Usage:
    python seed_verdict_history.py backtest_output/daily_indicator_matrix_2026-06-01_to_2026-06-30.csv \
                                    backtest_output/daily_indicator_matrix_2026-07-01_to_2026-07-31.csv
"""
import csv
import sys
from datetime import date

import update_dashboard as ud


def _parse_pct(cell):
    """"38.9%" -> 38.9, "N/A" -> None, "" -> None. Never fabricates a
    number for a cell that wasn't one."""
    cell = (cell or "").strip()
    if not cell or cell in ("N/A", "—"):
        return None
    if cell.endswith("%"):
        cell = cell[:-1]
    try:
        return float(cell)
    except ValueError:
        return None


def seed_from_csv(cache, csv_path):
    """Returns the number of day-rows appended from this file."""
    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        rows = list(csv.reader(f))
    header = rows[1]  # row 0 is the table-group super-header, row 1 is real column names
    idx = {h: i for i, h in enumerate(header)}
    date_i = idx["Date"]
    cb_i, ms_i, ct_i = (idx["Cycle Bottom Weighted %"], idx["Momentum Shift Weighted %"],
                        idx["Cycle Top Weighted %"])
    n = 0
    for row in rows[2:]:
        if not row or not row[date_i] or row[date_i].startswith("TOTAL"):
            continue  # the trailing totals row, or a stray blank line
        d = date.fromisoformat(row[date_i])
        cb, ms, ct = _parse_pct(row[cb_i]), _parse_pct(row[ms_i]), _parse_pct(row[ct_i])
        ud.verdict_history_append(cache, d, cb, ms, ct, source="backtest_seed")
        n += 1
    return n


def main():
    csv_paths = sys.argv[1:]
    if not csv_paths:
        print(__doc__)
        sys.exit(1)

    cache = ud.load_cache()
    before = len(cache.get("VERDICT_HISTORY", []))

    total_appended = 0
    for path in csv_paths:
        n = seed_from_csv(cache, path)
        print(f"  {path}: {n} day-rows appended (source=backtest_seed)")
        total_appended += n

    after = len(cache.get("VERDICT_HISTORY", []))
    ud.save_cache(cache)
    print(f"\nVERDICT_HISTORY: {before} -> {after} entries ({total_appended} rows processed across "
          f"{len(csv_paths)} file(s))")
    print(f"Saved to {ud.CACHE_FILE}")


if __name__ == "__main__":
    main()
