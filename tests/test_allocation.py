"""Room in minutes and tokens from readings plus the token ledger."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from claude_swap.allocation import (
    Allocation,
    Reading,
    readings_from_auto_log,
    readings_from_history,
)
from claude_swap.tokens import Call

NOW = 1_791_230_400.0  # 2026-10-05T20:00:00Z


def _call(at: float, organization: str, model: str = "claude-opus-5-5", total: int = 1_000_000) -> Call:
    return Call(at=at, organization=organization, model=model, input_tokens=0, output_tokens=total, cache_read_tokens=0, cache_creation_tokens=0)


def test_tokens_per_point_measures_tokens_spent_while_the_window_rose():
    readings = [
        Reading(NOW - 1800, "5", "org-a", {"h5": 40.0}),
        Reading(NOW - 1200, "5", "org-a", {"h5": 50.0}),
        Reading(NOW - 600, "5", "org-a", {"h5": 60.0}),
    ]
    calls = [_call(NOW - 1500, "org-a"), _call(NOW - 900, "org-a"), _call(NOW - 100, "org-a")]
    allocation = Allocation(readings, calls, slot_organizations={"5": "org-a"}, tiers={"5": "max_20x"}, now=NOW)
    ratio, tokens, points = allocation.tokens_per_point("5", "h5", 3600)
    # Two calls fell inside the rise from 40 to 60; the one after the last reading does not count.
    assert points == 20.0
    assert tokens == 2_000_000
    assert ratio == 100_000


def test_a_reset_starts_a_new_segment():
    readings = [
        Reading(NOW - 3000, "5", "org-a", {"h5": 90.0}),
        Reading(NOW - 2400, "5", "org-a", {"h5": 100.0}),
        Reading(NOW - 1800, "5", "org-a", {"h5": 0.0}),
        Reading(NOW - 1200, "5", "org-a", {"h5": 10.0}),
    ]
    calls = [_call(NOW - 2700, "org-a"), _call(NOW - 1500, "org-a")]
    allocation = Allocation(readings, calls, slot_organizations={"5": "org-a"}, tiers={}, now=NOW)
    ratio, tokens, points = allocation.tokens_per_point("5", "h5", 3600)
    assert points == 20.0
    assert tokens == 2_000_000


def test_sibling_seat_spend_is_left_out():
    # Slots 7 and 9 share the Pieces organization. Only slot 7 rose; slot 9's
    # spend during its own rise is subtracted from slot 7's segment.
    readings = [
        Reading(NOW - 1800, "7", "org-p", {"h5": 10.0}),
        Reading(NOW - 600, "7", "org-p", {"h5": 20.0}),
        Reading(NOW - 1500, "9", "org-p", {"h5": 50.0}),
        Reading(NOW - 1200, "9", "org-p", {"h5": 60.0}),
    ]
    calls = [_call(NOW - 1400, "org-p"), _call(NOW - 900, "org-p")]
    allocation = Allocation(readings, calls, slot_organizations={"7": "org-p", "9": "org-p"}, tiers={}, now=NOW)
    ratio, tokens, points = allocation.tokens_per_point("7", "h5", 3600)
    assert tokens == 1_000_000
    assert points == 10.0


def test_summary_estimates_minutes_from_current_demand():
    readings = [
        Reading(NOW - 1800, "5", "org-a", {"h5": 40.0, "fable": 10.0}),
        Reading(NOW - 600, "5", "org-a", {"h5": 60.0, "fable": 20.0}),
    ]
    # 2M tokens in the rise, 1M in the last 30 minutes.
    calls = [_call(NOW - 1500, "org-a", total=1_000_000), _call(NOW - 900, "org-a", total=1_000_000), _call(NOW - 60, "org-a", model="claude-fable-5-1", total=1_000_000)]
    allocation = Allocation(readings, calls, slot_organizations={"5": "org-a", "6": "org-b"}, tiers={"5": "max_20x", "6": "max_20x"}, now=NOW)
    latest = {
        "5": {"h5": {"pct": 60.0, "resetsAt": None}, "fable": {"pct": 20.0}},
        "6": {"h5": {"pct": 90.0}},
    }
    rooms = allocation.summarize(latest, 3600)
    session = rooms["5"].windows["h5"]
    # Two calls fell inside the rise from 40 to 60; the Fable call came after the last reading.
    assert session.tokens_per_point == 100_000
    assert session.tokens_left == 40 * session.tokens_per_point
    # Demand over the last 30 minutes is 3M tokens / 30 min.
    assert round(session.minutes_left, 2) == round(session.tokens_left / (3_000_000 / 30), 2)
    # Slot 6 never rose; it borrows the same-tier ratio and says so.
    borrowed = rooms["6"].windows["h5"]
    assert borrowed.tokens_per_point_source == "same-tier"
    assert borrowed.tokens_left == 10 * session.tokens_per_point
    assert rooms["5"].minutes_for("opus") == session.minutes_left


def test_idle_stretch_ends_a_segment_where_it_went_flat():
    # Rise 10 -> 30 by NOW-3000, flat for 20 minutes, then a new rise. Tokens
    # spent during the flat stretch belong to nobody's rise.
    readings = [
        Reading(NOW - 3600, "5", "org-a", {"h5": 10.0}),
        Reading(NOW - 3000, "5", "org-a", {"h5": 30.0}),
        Reading(NOW - 2400, "5", "org-a", {"h5": 30.0}),
        Reading(NOW - 1800, "5", "org-a", {"h5": 30.0}),
        Reading(NOW - 1200, "5", "org-a", {"h5": 40.0}),
    ]
    calls = [_call(NOW - 3300, "org-a"), _call(NOW - 2000, "org-a"), _call(NOW - 1500, "org-a")]
    allocation = Allocation(readings, calls, slot_organizations={"5": "org-a"}, tiers={}, now=NOW)
    ratio, tokens, points = allocation.tokens_per_point("5", "h5", 7200)
    assert points == 30.0
    assert tokens == 2_000_000


def test_history_rows_become_readings():
    rows = [{"at": 1.0, "slot": "5", "org": "org-a", "h5": 12.0, "d7": 50.0, "scoped": {"Fable": 76.0}}]
    readings = readings_from_history(rows)
    assert readings[0].pct == {"h5": 12.0, "d7": 50.0, "fable": 76.0}


def test_auto_log_backfill_infers_dates_walking_backwards(tmp_path: Path):
    log = tmp_path / "auto.stdout.log"
    log.write_text(
        "23:58:00  Account-1 (a@x): 10% used (switch at 80%) | others: #2: 5h 10% · 7d 20% · Fable 30%\n"
        "00:02:00  Account-1 (a@x): 10% used (switch at 80%) | others: #2: 5h 12% · 7d 20% · Fable 30%\n"
        "00:03:00  no switch: holding\n"
    )
    end = datetime(2026, 10, 6, 0, 10, tzinfo=timezone.utc)
    readings = readings_from_auto_log(log, end, {"2": "org-b"})
    assert [r.pct["h5"] for r in readings] == [10.0, 12.0]
    assert datetime.fromtimestamp(readings[0].at, timezone.utc).day == 5
    assert datetime.fromtimestamp(readings[1].at, timezone.utc).day == 6
    assert readings[0].organization == "org-b"
