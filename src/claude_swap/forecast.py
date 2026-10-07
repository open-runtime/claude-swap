"""Forecast strategy: leave an account when its session will fill before a
switch can land, and land on the account with the most Fable room.

The usage endpoint cannot be polled faster than ``poll_policy`` already
allows. This module only interprets samples the engine has already stored.
A flat account stays until ``QUIET_CEILING_PCT``. A fast one can leave once
it is past ``LOUD_FLOOR_PCT`` and projected to fill inside ``PICKUP_S``,
which covers the macOS keychain pickup plus one more sample.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

# Time a switch needs to land before the window fills: about 30s for a
# running Claude Code to pick up the new login, plus the poll interval, since
# the next reading is up to 60s away and a burst in that gap is invisible.
# 0% to 16% in three minutes was observed on a Max 20x account; 90s was tight.
PICKUP_S = 150.0
QUIET_CEILING_PCT = 92.0
LOUD_FLOOR_PCT = 70.0
# An account above this Fable utilization is not a place to send Fable work.
# Five percent left still returns "You've reached your Fable limit" within a
# minute of real traffic, which is what happened on slot 6.
FABLE_USABLE_MAX_PCT = 80.0
MIN_SAMPLE_SPAN_S = 45.0
MAX_SAMPLES = 8
# A landing must be worth at least this long at the machine's current token
# demand, when that can be measured. One percent of a team seat's week under
# a dozen agents is gone before the switch has finished landing.
MIN_LANDING_MINUTES = 10.0

MinutesFor = Callable[[str, str], float | None]


@dataclass(frozen=True)
class Window:
    pct: float


@dataclass(frozen=True)
class Spend:
    """Usage credits billed so far this month, on a seat with extra usage on.

    Such a seat is never refused: when a window fills the API keeps serving
    and bills credits instead. Seat 13 did that for five hours on Oct 6,
    $1,547 of it, with no limit message anywhere. Credits are an acceptable
    backstop when every account is exhausted, and the wrong place to sit
    while another account still has plan room.
    """

    used: float


@dataclass(frozen=True)
class AccountSnapshot:
    number: str
    five_hour: Window | None
    seven_day: Window | None
    fable: Window | None
    spend: Spend | None = None


@dataclass(frozen=True)
class Sample:
    at: float
    session_pct: float
    fable_pct: float | None = None
    spend_used: float | None = None


@dataclass(frozen=True)
class Decision:
    switch_to: str | None
    reason: str
    detail: str
    fable_available: bool
    escaping_limit: bool


def window_from(usage: dict | str | None, key: str) -> Window | None:
    if not isinstance(usage, dict):
        return None
    raw = usage.get(key)
    if not isinstance(raw, dict):
        return None
    pct = raw.get("pct")
    if not isinstance(pct, (int, float)):
        return None
    return Window(pct=float(pct))


def fable_window(usage: dict | str | None) -> Window | None:
    if not isinstance(usage, dict):
        return None
    scoped = usage.get("scoped")
    if not isinstance(scoped, list):
        return None
    for item in scoped:
        if (
            isinstance(item, dict)
            and isinstance(item.get("name"), str)
            and item["name"].lower() == "fable"
            and isinstance(item.get("pct"), (int, float))
        ):
            return Window(pct=float(item["pct"]))
    return None


def spend_from(usage: dict | str | None) -> Spend | None:
    if not isinstance(usage, dict):
        return None
    raw = usage.get("spend")
    if not isinstance(raw, dict) or not isinstance(raw.get("used"), (int, float)):
        return None
    return Spend(used=float(raw["used"]))


def snapshot(number: str, usage: dict | str | None) -> AccountSnapshot | None:
    if not isinstance(usage, dict):
        return None
    account = AccountSnapshot(
        number=str(number),
        five_hour=window_from(usage, "five_hour"),
        seven_day=window_from(usage, "seven_day"),
        fable=fable_window(usage),
        spend=spend_from(usage),
    )
    if account.five_hour is None and account.seven_day is None and account.fable is None:
        return None
    return account


def record_sample(
    existing: list,
    *,
    at: float,
    session_pct: float,
    fable_pct: float | None = None,
    spend_used: float | None = None,
) -> list[dict]:
    """Append one reading, dropping duplicates and keeping the tail."""
    row: list[dict] = []
    for item in existing:
        if (
            isinstance(item, dict)
            and isinstance(item.get("at"), (int, float))
            and isinstance(item.get("pct"), (int, float))
        ):
            kept = {"at": float(item["at"]), "pct": float(item["pct"])}
            if isinstance(item.get("fable"), (int, float)):
                kept["fable"] = float(item["fable"])
            if isinstance(item.get("spend"), (int, float)):
                kept["spend"] = float(item["spend"])
            row.append(kept)
    if row and at <= row[-1]["at"]:
        return row[-MAX_SAMPLES:]
    last_fable = row[-1].get("fable") if row else None
    fable_unchanged = (
        fable_pct is None
        or not isinstance(last_fable, float)
        or abs(last_fable - fable_pct) < 0.05
    )
    last_spend = row[-1].get("spend") if row else None
    spend_unchanged = (
        spend_used is None
        or not isinstance(last_spend, float)
        or abs(last_spend - spend_used) < 0.005
    )
    if (
        row
        and abs(row[-1]["pct"] - session_pct) < 0.05
        and fable_unchanged
        and spend_unchanged
        and at - row[-1]["at"] < MIN_SAMPLE_SPAN_S
    ):
        return row[-MAX_SAMPLES:]
    stored = {"at": at, "pct": session_pct}
    if fable_pct is not None:
        stored["fable"] = fable_pct
    if spend_used is not None:
        stored["spend"] = spend_used
    row.append(stored)
    return row[-MAX_SAMPLES:]


def samples_from(raw: list) -> list[Sample]:
    samples: list[Sample] = []
    for item in raw:
        if not (
            isinstance(item, dict)
            and isinstance(item.get("at"), (int, float))
            and isinstance(item.get("pct"), (int, float))
        ):
            continue
        fable = item.get("fable")
        spend = item.get("spend")
        samples.append(
            Sample(
                at=float(item["at"]),
                session_pct=float(item["pct"]),
                fable_pct=float(fable) if isinstance(fable, (int, float)) else None,
                spend_used=float(spend) if isinstance(spend, (int, float)) else None,
            )
        )
    return samples


def session_burn(samples: list[Sample]) -> float | None:
    """Session-percent per second of the latest rise.

    The slope from the oldest retained reading to the newest is one line
    across the whole ring (about seven minutes). A slow start hides a spike
    at the end, and a repeated reading stretches that line so the runway
    looks longer. The latest step whose session percent rose is the burn.
    A flat or lower newest reading keeps that rise instead of clearing it.
    """
    if len(samples) < 2:
        return None
    saw_span = False
    for index in range(len(samples) - 1, 0, -1):
        current = samples[index]
        previous = samples[index - 1]
        span = current.at - previous.at
        if span < MIN_SAMPLE_SPAN_S:
            continue
        saw_span = True
        delta = current.session_pct - previous.session_pct
        if delta > 0.0:
            return delta / span
    return 0.0 if saw_span else None


def fable_burn(samples: list[Sample]) -> float | None:
    """Fable-percent per second of the latest rise. Same rule as the session."""
    usable = [
        Sample(at=sample.at, session_pct=sample.fable_pct)
        for sample in samples
        if sample.fable_pct is not None
    ]
    return session_burn(usable)


def with_latest_session(samples: list[Sample], session_pct: float | None) -> list[Sample]:
    """Replace the newest sample's percent when a fresher reading arrived.

    The timestamp stays. A refetch is a correction of the reading just taken,
    not a new interval, so it must not invent a burn over a fraction of a second.
    """
    if session_pct is None or not samples:
        return samples
    last = samples[-1]
    if abs(last.session_pct - session_pct) < 0.05:
        return samples
    corrected = list(samples)
    corrected[-1] = Sample(at=last.at, session_pct=session_pct)
    return corrected


def _fable_usable(account: AccountSnapshot) -> bool:
    """A place Fable work can actually run for a while.

    The shared session and weekly windows must be under the leave line, not
    just under 100%. Slot 5 at weekly 99% counted as Fable room once, the
    engine landed there, and the next tick left again.
    """
    if account.fable is None or account.fable.pct > FABLE_USABLE_MAX_PCT:
        return False
    if account.five_hour is not None and account.five_hour.pct >= QUIET_CEILING_PCT:
        return False
    if account.seven_day is not None and account.seven_day.pct >= QUIET_CEILING_PCT:
        return False
    return True


def _opus_usable(account: AccountSnapshot) -> bool:
    if account.five_hour is not None and account.five_hour.pct >= 100.0:
        return False
    if account.seven_day is not None and account.seven_day.pct >= 100.0:
        return False
    return account.five_hour is not None or account.seven_day is not None


def _headroom(account: AccountSnapshot, *, fable: bool) -> float:
    pcts: list[float] = []
    if account.five_hour is not None:
        pcts.append(account.five_hour.pct)
    if account.seven_day is not None:
        pcts.append(account.seven_day.pct)
    if fable and account.fable is not None:
        pcts.append(account.fable.pct)
    if not pcts:
        return 0.0
    return 100.0 - max(pcts)


def credits_billed(samples: list[Sample]) -> float | None:
    """Credits billed between the last two readings that carried a spend
    figure, or None when there are not two such readings.

    Positive means the seat was billing in that interval: its plan windows
    are full and extra usage is covering the requests. Nothing else in the
    usage reading says so, since the percentages stop at 100.
    """
    with_spend = [sample for sample in samples if sample.spend_used is not None]
    if len(with_spend) < 2:
        return None
    return with_spend[-1].spend_used - with_spend[-2].spend_used


def _should_leave(
    active: AccountSnapshot, samples: list[Sample], *, prefer_fable: bool
) -> tuple[bool, bool, str]:
    """Whether to leave, whether this is a hard session/weekly limit, and why.

    The reason starts with ``fable`` when Fable is the only window forcing the
    move. A full Fable week used to be ignored while the session still had
    hours left, which is how slot 6 sat at Fable 100% and session 17%.

    ``credit-spend`` is a hard limit too: the seat is past its plan and
    billing usage credits. Credits are the last resort, so any account with
    plan room is a better place to be; ``decide`` stays only when there is
    none.
    """
    session = active.five_hour.pct if active.five_hour is not None else None
    weekly = active.seven_day.pct if active.seven_day is not None else None
    fable = active.fable.pct if prefer_fable and active.fable is not None else None
    if (session is not None and session >= 100.0) or (weekly is not None and weekly >= 100.0):
        return True, True, "session-limit"
    billed = credits_billed(samples)
    if active.spend is not None and billed is not None and billed > 0.0:
        return True, True, "credit-spend"
    if (
        (session is not None and session >= QUIET_CEILING_PCT)
        or (weekly is not None and weekly >= QUIET_CEILING_PCT)
    ):
        return True, False, "session-ceiling"
    if session is not None and session >= LOUD_FLOOR_PCT:
        burn = session_burn(samples)
        if burn is not None and burn > 0.0 and (100.0 - session) / burn < PICKUP_S:
            return True, False, "session-burn"
    if fable is not None and fable >= 100.0:
        return True, True, "fable-limit"
    if fable is not None and fable > FABLE_USABLE_MAX_PCT:
        return True, False, "fable-ceiling"
    if fable is not None and fable >= LOUD_FLOOR_PCT:
        burn = fable_burn(samples)
        if burn is not None and burn > 0.0 and (100.0 - fable) / burn < PICKUP_S:
            return True, False, "fable-burn"
    return False, False, "holding"


def _runway_phrase(label: str, pct: float, burn: float | None) -> str:
    if burn is None or burn <= 0.0:
        return f"{label} {pct:.0f}%"
    seconds = (100.0 - pct) / burn
    if seconds >= 3600.0:
        return f"{label} {pct:.0f}%, about {seconds / 3600.0:.1f}h of runway"
    return f"{label} {pct:.0f}%, about {max(1, round(seconds / 60.0)):.0f}m of runway"


def _fill_phrase(active: AccountSnapshot, samples: list[Sample]) -> str:
    parts: list[str] = []
    if active.five_hour is None:
        parts.append("session unread")
    else:
        parts.append(_runway_phrase("session", active.five_hour.pct, session_burn(samples)))
    if active.fable is not None:
        parts.append(_runway_phrase("Fable", active.fable.pct, fable_burn(samples)))
    if active.spend is not None:
        billed = credits_billed(samples)
        if billed is not None and billed > 0.0:
            parts.append(f"billing usage credits (${billed:,.2f} since the last reading, ${active.spend.used:,.2f} this month)")
        else:
            parts.append(f"${active.spend.used:,.2f} of usage credits this month")
    return "; ".join(parts)


def _hold_detail(
    active: AccountSnapshot,
    samples: list[Sample],
    *,
    prefer_fable: bool,
    fable_available: bool,
) -> str:
    phrase = _fill_phrase(active, samples)
    hold = (
        f"holding until {QUIET_CEILING_PCT:.0f}% or a projected fill "
        f"inside {PICKUP_S:.0f}s"
    )
    if prefer_fable and not fable_available:
        return f"Fable is unavailable; only session and weekly remain. {phrase}; {hold}"
    return f"{phrase}; {hold}"


def _worth_landing(account: AccountSnapshot, model: str, minutes_for: MinutesFor | None) -> bool:
    if minutes_for is None:
        return True
    minutes = minutes_for(account.number, model)
    return minutes is None or minutes >= MIN_LANDING_MINUTES


def decide(
    accounts: list[AccountSnapshot],
    *,
    current: str,
    samples: list[Sample],
    hysteresis_pct: float,
    prefer_fable: bool,
    minutes_for: MinutesFor | None = None,
) -> Decision:
    """Pick a landing account, or stay.

    When Fable is the model being protected, a Fable week past
    ``FABLE_USABLE_MAX_PCT`` forces a leave even if the session is quiet.
    If no other account has real Fable room and an open session, stay and
    say so, instead of moving to another account that is also out of Fable.
    """
    active = next((account for account in accounts if account.number == current), None)
    if active is None:
        return Decision(
            switch_to=None,
            reason="holding",
            detail="active account has no session reading yet",
            fable_available=False,
            escaping_limit=False,
        )
    def fable_peer(account: AccountSnapshot) -> bool:
        return _fable_usable(account) and _worth_landing(account, "fable", minutes_for)

    def opus_peer(account: AccountSnapshot) -> bool:
        return _opus_usable(account) and _worth_landing(account, "opus", minutes_for)

    fable_available = prefer_fable and any(fable_peer(account) for account in accounts)
    leave, escaping, because = _should_leave(active, samples, prefer_fable=prefer_fable)
    fable_only = because.startswith("fable")
    short_landing = False
    if fable_only and not any(fable_peer(account) for account in accounts if account.number != current):
        # No peer lasts the full ten minutes. A short Fable landing still beats
        # staying where Fable has already failed, so take the longest one.
        short = [account for account in accounts if account.number != current and _fable_usable(account)]
        if short:
            short_landing = True
            fable_available = True

            def fable_peer(account: AccountSnapshot, _short=frozenset(a.number for a in short)) -> bool:  # noqa: F811
                return account.number in _short
    if fable_only and not any(fable_peer(account) for account in accounts if account.number != current):
        phrase = _fill_phrase(active, samples)
        return Decision(
            switch_to=None,
            reason="fable-unavailable",
            detail=(
                "Fable is full, and no account with Fable room has an open session. "
                f"{phrase}"
            ),
            fable_available=False,
            escaping_limit=False,
        )
    if not leave:
        return Decision(
            switch_to=None,
            reason="holding",
            detail=_hold_detail(
                active,
                samples,
                prefer_fable=prefer_fable,
                fable_available=fable_available,
            ),
            fable_available=fable_available,
            escaping_limit=False,
        )

    others = [account for account in accounts if account.number != current]
    landing_fable = False
    if fable_available:
        peers = [account for account in others if fable_peer(account)]
        landing_fable = bool(peers)
        if not peers:
            peers = [account for account in others if opus_peer(account)]
    else:
        peers = [account for account in others if opus_peer(account)]
    if not peers and escaping and not fable_only:
        # Leaving a dead account: a short landing still beats no landing.
        peers = [account for account in others if _opus_usable(account)]
    if not peers:
        if because == "credit-spend" and all(not _opus_usable(account) for account in others):
            # Credits are the backstop: every other account is out of plan
            # room, so staying here and paying is the only way to keep going.
            return Decision(
                switch_to=None,
                reason="credits-backstop",
                detail=(
                    f"{_fill_phrase(active, samples)}; every other account is at a "
                    "session or weekly limit, so usage credits are carrying the work"
                ),
                fable_available=False,
                escaping_limit=True,
            )
        if not _opus_usable(active) and all(not _opus_usable(account) for account in others):
            return Decision(
                switch_to=None,
                reason="all-exhausted",
                detail="every account is at a session or weekly limit",
                fable_available=False,
                escaping_limit=True,
            )
        phrase = _fill_phrase(active, samples)
        return Decision(
            switch_to=None,
            reason="no-qualifying-candidate",
            detail=f"{phrase}; no other account has enough extra room",
            fable_available=fable_available,
            escaping_limit=escaping,
        )

    active_headroom = _headroom(active, fable=landing_fable and _fable_usable(active))

    def peer_headroom(account: AccountSnapshot) -> float:
        return _headroom(account, fable=landing_fable and _fable_usable(account))

    qualified = [
        account
        for account in peers
        if (
            peer_headroom(account) > active_headroom
            if escaping
            else peer_headroom(account) >= active_headroom + hysteresis_pct
        )
        and peer_headroom(account) > 0.0
    ]
    if not qualified and escaping and landing_fable:
        qualified = [account for account in peers if peer_headroom(account) > 0.0]
    elif not qualified and escaping and not fable_only:
        qualified = [account for account in peers if peer_headroom(account) > 0.0]
    if not qualified:
        phrase = _fill_phrase(active, samples)
        return Decision(
            switch_to=None,
            reason="no-qualifying-candidate",
            detail=f"{phrase}; no other account has enough extra room",
            fable_available=fable_available,
            escaping_limit=escaping,
        )
    model = "fable" if landing_fable else "opus"

    def rank(account: AccountSnapshot) -> tuple:
        # Minutes at the current pace first, when measured; a 20x plan at 30%
        # lasts longer than a team seat at 10% left. Headroom breaks ties.
        minutes = minutes_for(account.number, model) if minutes_for else None
        return (
            0 if minutes is not None else 1,
            -(minutes or 0.0),
            -peer_headroom(account),
            int(account.number) if account.number.isdigit() else 0,
        )

    qualified.sort(key=rank)
    chosen = qualified[0]
    phrase = _fill_phrase(active, samples)
    minutes = minutes_for(chosen.number, model) if minutes_for else None
    lasting = f", about {minutes:.0f} minutes at the current pace" if minutes is not None else ""
    if short_landing:
        lasting += "; no account lasts longer, so this is a short landing"
    if landing_fable:
        detail = f"{phrase}; switching to account {chosen.number}, which has the most Fable room{lasting}"
    elif prefer_fable:
        detail = (
            "Fable is unavailable on every other account; only session and weekly remain. "
            f"{phrase}; switching to account {chosen.number}{lasting}"
        )
    else:
        detail = f"{phrase}; switching to account {chosen.number}{lasting}"
    return Decision(
        switch_to=chosen.number,
        reason="forecast",
        detail=detail,
        fable_available=landing_fable,
        escaping_limit=escaping,
    )
