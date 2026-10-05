"""Forecast strategy: burn-rate leave, Fable-first landing."""

from __future__ import annotations

from claude_swap.forecast import (
    LOUD_FLOOR_PCT,
    QUIET_CEILING_PCT,
    AccountSnapshot,
    Sample,
    Window,
    decide,
    record_sample,
)


def _account(number: str, session: float, weekly: float = 0.0, fable: float | None = None) -> AccountSnapshot:
    return AccountSnapshot(
        number=number,
        five_hour=Window(session),
        seven_day=Window(weekly),
        fable=None if fable is None else Window(fable),
    )


def _decide(accounts, samples, *, current="1", hysteresis=10.0, prefer_fable=True):
    return decide(
        accounts,
        current=current,
        samples=samples,
        hysteresis_pct=hysteresis,
        prefer_fable=prefer_fable,
    )


class TestForecastPolicy:
    def test_flat_account_stays_below_the_ceiling(self):
        decision = _decide(
            [_account("1", 50, fable=10), _account("2", 0, fable=0)],
            [Sample(0, 50), Sample(120, 50)],
        )
        assert decision.switch_to is None
        assert decision.reason == "holding"
        assert decision.fable_available is True
        assert "holding" in decision.detail

    def test_ceiling_moves_to_the_account_with_fable_room(self):
        decision = _decide(
            [_account("1", QUIET_CEILING_PCT, fable=20), _account("2", 10, fable=0), _account("3", 40, fable=5)],
            [Sample(0, 80), Sample(120, QUIET_CEILING_PCT)],
        )
        assert decision.switch_to == "2"
        assert "most Fable room" in decision.detail

    def test_fast_burn_leaves_before_the_ceiling(self):
        # 70% -> 88% in 60s leaves 12 points, about 40s of runway, inside the
        # 90s pickup window, and the session is already past the loud floor.
        decision = _decide(
            [_account("1", 88, fable=10), _account("2", 5, fable=0)],
            [Sample(0, 70), Sample(60, 88)],
        )
        assert decision.switch_to == "2"
        assert decision.escaping_limit is False

    def test_burn_below_the_loud_floor_stays(self):
        assert LOUD_FLOOR_PCT == 70.0
        decision = _decide(
            [_account("1", 60, fable=10), _account("2", 0, fable=0)],
            [Sample(0, 20), Sample(60, 60)],
        )
        assert decision.switch_to is None
        assert decision.reason == "holding"

    def test_projected_fill_beyond_pickup_stays(self):
        # 60% -> 75% in 60s leaves 25 points, about 100s, outside the 90s window.
        decision = _decide(
            [_account("1", 75, fable=10), _account("2", 0, fable=0)],
            [Sample(0, 60), Sample(60, 75)],
        )
        assert decision.switch_to is None

    def test_reports_when_no_account_has_fable_room(self):
        decision = _decide(
            [_account("1", 40, fable=100), _account("2", 10, fable=100)],
            [Sample(0, 40), Sample(120, 40)],
        )
        assert decision.switch_to is None
        assert decision.fable_available is False
        assert "Fable is unavailable" in decision.detail
        assert "session and weekly" in decision.detail

    def test_lands_on_opus_when_fable_peers_are_gone(self):
        decision = _decide(
            [_account("1", 96, fable=100), _account("2", 15, fable=100)],
            [Sample(0, 90), Sample(120, 96)],
        )
        assert decision.switch_to == "2"
        assert decision.fable_available is False
        assert "every other account" in decision.detail

    def test_small_gap_does_not_switch(self):
        decision = _decide(
            [_account("1", 93, fable=0), _account("2", 88, fable=0)],
            [Sample(0, 93), Sample(120, 93)],
        )
        assert decision.switch_to is None
        assert decision.reason == "no-qualifying-candidate"

    def test_hard_limit_escapes_to_any_open_account(self):
        decision = _decide(
            [_account("1", 100, fable=10), _account("2", 95, fable=0)],
            [Sample(0, 100), Sample(120, 100)],
        )
        assert decision.switch_to == "2"
        assert decision.escaping_limit is True

    def test_record_sample_keeps_a_short_tail_and_drops_duplicates(self):
        row = record_sample([], at=0, session_pct=10)
        row = record_sample(row, at=10, session_pct=10)
        assert len(row) == 1
        row = record_sample(row, at=60, session_pct=12)
        assert [item["pct"] for item in row] == [10, 12]
        for index in range(10):
            row = record_sample(row, at=120 + index * 60, session_pct=20 + index)
        assert len(row) == 8
        assert row[-1]["pct"] == 29

