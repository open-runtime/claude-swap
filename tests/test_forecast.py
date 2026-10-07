"""Forecast strategy: burn-rate leave, Fable-first landing."""

from __future__ import annotations

from claude_swap.forecast import (
    LOUD_FLOOR_PCT,
    QUIET_CEILING_PCT,
    AccountSnapshot,
    Sample,
    Spend,
    Window,
    credits_billed,
    decide,
    record_sample,
    samples_from,
    session_burn,
    snapshot,
)


def _account(
    number: str,
    session: float,
    weekly: float = 0.0,
    fable: float | None = None,
    spend: float | None = None,
) -> AccountSnapshot:
    return AccountSnapshot(
        number=number,
        five_hour=Window(session),
        seven_day=Window(weekly),
        fable=None if fable is None else Window(fable),
        spend=None if spend is None else Spend(spend),
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
        # 70% -> 76% in 60s leaves 24 points, about 240s, outside the 150s window.
        decision = _decide(
            [_account("1", 76, fable=10), _account("2", 0, fable=0)],
            [Sample(0, 70), Sample(60, 76)],
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

    def test_fable_landing_needs_shared_windows_under_the_leave_line(self):
        # Slot 5: Fable 76% but weekly 99%. Landing there means leaving next tick.
        decision = _decide(
            [
                _account("6", 20, weekly=58, fable=100),
                _account("5", 31, weekly=99, fable=76),
            ],
            [Sample(0, 20), Sample(120, 20)],
            current="6",
        )
        assert decision.switch_to is None
        assert decision.reason == "fable-unavailable"

    def test_one_percent_is_not_a_landing_when_it_lasts_under_ten_minutes(self):
        # Slot 4 has 20% headroom but a measured 3 minutes at the current
        # pace; slot 6 has less headroom and 40 minutes. Rank by minutes.
        minutes = {("4", "opus"): 3.0, ("6", "opus"): 40.0}
        decision = decide(
            [
                _account("1", 100, fable=100),
                _account("4", 80, weekly=54, fable=97),
                _account("6", 85, weekly=58, fable=100),
            ],
            current="1",
            samples=[Sample(0, 100), Sample(120, 100)],
            hysteresis_pct=10.0,
            prefer_fable=True,
            minutes_for=lambda number, model: minutes.get((number, model)),
        )
        assert decision.switch_to == "6"
        assert "about 40 minutes" in decision.detail

    def test_short_landing_still_beats_a_dead_account(self):
        decision = decide(
            [_account("1", 100, fable=100), _account("4", 80, weekly=54, fable=97)],
            current="1",
            samples=[Sample(0, 100), Sample(120, 100)],
            hysteresis_pct=10.0,
            prefer_fable=True,
            minutes_for=lambda number, model: 2.0,
        )
        assert decision.switch_to == "4"

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


class TestUsageCredits:
    """Seat 13 on Oct 6: five hours at 100% with extra usage on, $1,547 of
    credits, no limit message anywhere. Credits are the last resort, used
    only when every other account is out of plan room."""

    def test_snapshot_reads_spend_only_when_extra_usage_is_on(self):
        with_credits = snapshot("13", {"five_hour": {"pct": 100}, "seven_day": {"pct": 86}, "spend": {"used": 1547.27, "currency": "USD"}})
        without = snapshot("1", {"five_hour": {"pct": 50}, "seven_day": {"pct": 10}})
        assert with_credits.spend == Spend(used=1547.27)
        assert without.spend is None

    def test_record_sample_keeps_spend_and_a_spend_change_is_a_new_sample(self):
        row = record_sample([], at=0, session_pct=100, spend_used=10.0)
        # Same percentages seconds later, but the bill moved: that is news.
        row = record_sample(row, at=20, session_pct=100, spend_used=12.5)
        assert [item["spend"] for item in row] == [10.0, 12.5]
        samples = samples_from(row)
        assert samples[-1].spend_used == 12.5
        assert credits_billed(samples) == 2.5
        assert credits_billed([Sample(0, 50), Sample(60, 55)]) is None

    def test_billing_seat_leaves_for_any_account_with_plan_room(self):
        # The reading still says 97%, but the bill rose between readings:
        # the seat is past its plan. Slot 2 has Opus room only; take it.
        decision = _decide(
            [_account("13", 97, 86, fable=100, spend=1550.0), _account("2", 40, 74, fable=100)],
            [Sample(0, 97, 100, 1547.27), Sample(120, 97, 100, 1550.0)],
            current="13",
        )
        assert decision.switch_to == "2"
        assert decision.escaping_limit is True
        assert "billing usage credits" in decision.detail

    def test_billing_seat_prefers_fable_room_over_opus_room(self):
        decision = _decide(
            [
                _account("13", 97, 86, fable=100, spend=1550.0),
                _account("2", 40, 74, fable=100),
                _account("17", 25, 34, fable=13),
            ],
            [Sample(0, 97, 100, 1547.27), Sample(120, 97, 100, 1550.0)],
            current="13",
        )
        assert decision.switch_to == "17"
        assert "most Fable room" in decision.detail

    def test_billing_seat_stays_when_every_other_account_is_exhausted(self):
        decision = _decide(
            [_account("13", 97, 86, fable=100, spend=1550.0), _account("2", 100, 74), _account("4", 10, 100)],
            [Sample(0, 97, 100, 1547.27), Sample(120, 97, 100, 1550.0)],
            current="13",
        )
        assert decision.switch_to is None
        assert decision.reason == "credits-backstop"
        assert "usage credits are carrying the work" in decision.detail

    def test_a_flat_bill_is_not_a_reason_to_leave(self):
        decision = _decide(
            [_account("13", 50, 40, fable=20, spend=1547.27), _account("2", 0, 0, fable=0)],
            [Sample(0, 50, 20, 1547.27), Sample(120, 50, 20, 1547.27)],
            current="13",
        )
        assert decision.switch_to is None
        assert decision.reason == "holding"
        assert "$1,547.27 of usage credits this month" in decision.detail

