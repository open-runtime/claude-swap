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
    session_burn,
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

    def test_latest_rise_leaves_when_the_longer_slope_still_looks_safe(self):
        # Slot 10 on Oct 5: a slow climb, then 79% to 89% in about a minute.
        # The line across the whole ring still had about two minutes left.
        # The latest rise had about 70 seconds, inside the pickup window.
        samples = [
            Sample(0, 59),
            Sample(62, 59),
            Sample(121, 73),
            Sample(183, 79),
            Sample(241, 79),
            Sample(305, 89),
        ]
        assert session_burn(samples) == (10.0 / 64.0)
        decision = _decide(
            [_account("1", 89, fable=10), _account("2", 5, fable=0)],
            samples,
        )
        assert decision.switch_to == "2"

    def test_repeated_reading_keeps_the_latest_rise(self):
        samples = [Sample(0, 79), Sample(63, 89), Sample(126, 89)]
        assert session_burn(samples) == (10.0 / 63.0)
        decision = _decide(
            [_account("1", 89, fable=10), _account("2", 5, fable=0)],
            samples,
        )
        assert decision.switch_to == "2"

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
        assert decision.reason == "fable-unavailable"
        assert decision.fable_available is False
        assert "no account with Fable room has an open session" in decision.detail

    def test_full_fable_week_leaves_a_quiet_session(self):
        # Slot 6: session 17%, Fable already at 95%. The session runway is
        # hours. Fable work is already failing.
        decision = _decide(
            [_account("6", 17, weekly=55, fable=95), _account("8", 20, weekly=29, fable=40)],
            [Sample(0, 17), Sample(120, 17)],
            current="6",
        )
        assert decision.switch_to == "8"
        assert "most Fable room" in decision.detail

    def test_full_fable_week_stays_when_every_open_session_is_out_of_fable(self):
        decision = _decide(
            [
                _account("6", 17, weekly=55, fable=100),
                _account("4", 21, weekly=54, fable=97),
                _account("7", 100, weekly=18, fable=33),
            ],
            [Sample(0, 17), Sample(120, 17)],
            current="6",
        )
        assert decision.switch_to is None
        assert decision.reason == "fable-unavailable"

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

