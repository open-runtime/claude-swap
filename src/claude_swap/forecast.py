"""Forecast strategy: leave an account when its session will fill before a
switch can land, and land on the account with the most Fable room.

The usage endpoint cannot be polled faster than ``poll_policy`` already
allows. This module only interprets samples the engine has already stored.
A flat account stays until ``QUIET_CEILING_PCT``. A fast one can leave once
it is past ``LOUD_FLOOR_PCT`` and projected to fill inside ``PICKUP_S``,
which covers the macOS keychain pickup plus one more sample.
"""

from __future__ import annotations

from dataclasses import dataclass

PICKUP_S = 90.0
QUIET_CEILING_PCT = 92.0
LOUD_FLOOR_PCT = 70.0
FABLE_REMAINING_FLOOR = 5.0
MIN_SAMPLE_SPAN_S = 45.0
MAX_SAMPLES = 8


@dataclass(frozen=True)
class Window:
    pct: float


@dataclass(frozen=True)
class AccountSnapshot:
    number: str
    five_hour: Window | None
    seven_day: Window | None
    fable: Window | None


@dataclass(frozen=True)
class Sample:
    at: float
    session_pct: float


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


def snapshot(number: str, usage: dict | str | None) -> AccountSnapshot | None:
    if not isinstance(usage, dict):
        return None
    account = AccountSnapshot(
        number=str(number),
        five_hour=window_from(usage, "five_hour"),
        seven_day=window_from(usage, "seven_day"),
        fable=fable_window(usage),
    )
    if account.five_hour is None and account.seven_day is None and account.fable is None:
        return None
    return account


def record_sample(existing: list, *, at: float, session_pct: float) -> list[dict]:
    """Append one session reading, dropping duplicates and keeping the tail."""
    row: list[dict] = []
    for item in existing:
        if (
            isinstance(item, dict)
            and isinstance(item.get("at"), (int, float))
            and isinstance(item.get("pct"), (int, float))
        ):
            row.append({"at": float(item["at"]), "pct": float(item["pct"])})
    if row and at <= row[-1]["at"]:
        return row[-MAX_SAMPLES:]
    if (
        row
        and abs(row[-1]["pct"] - session_pct) < 0.05
        and at - row[-1]["at"] < MIN_SAMPLE_SPAN_S
    ):
        return row[-MAX_SAMPLES:]
    row.append({"at": at, "pct": session_pct})
    return row[-MAX_SAMPLES:]


def samples_from(raw: list) -> list[Sample]:
    return [
        Sample(at=float(item["at"]), session_pct=float(item["pct"]))
        for item in raw
        if isinstance(item, dict)
        and isinstance(item.get("at"), (int, float))
        and isinstance(item.get("pct"), (int, float))
    ]


def session_burn(samples: list[Sample]) -> float | None:
    """Session-percent per second, or None until two readings span the minimum."""
    if len(samples) < 2:
        return None
    first, last = samples[0], samples[-1]
    span = last.at - first.at
    if span < MIN_SAMPLE_SPAN_S:
        return None
    return max(0.0, (last.session_pct - first.session_pct) / span)


def _fable_usable(account: AccountSnapshot) -> bool:
    if account.fable is None or account.fable.pct > 100.0 - FABLE_REMAINING_FLOOR:
        return False
    if account.five_hour is not None and account.five_hour.pct >= 100.0:
        return False
    if account.seven_day is not None and account.seven_day.pct >= 100.0:
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


def _should_leave(active: AccountSnapshot, samples: list[Sample]) -> tuple[bool, bool, str]:
    """Whether to leave, whether this is a hard limit, and why."""
    session = active.five_hour.pct if active.five_hour is not None else None
    weekly = active.seven_day.pct if active.seven_day is not None else None
    if (session is not None and session >= 100.0) or (weekly is not None and weekly >= 100.0):
        return True, True, "at-limit"
    if (
        (session is not None and session >= QUIET_CEILING_PCT)
        or (weekly is not None and weekly >= QUIET_CEILING_PCT)
    ):
        return True, False, "ceiling"
    if session is None:
        return False, False, "holding"
    burn = session_burn(samples)
    if burn is None or burn <= 0.0 or session < LOUD_FLOOR_PCT:
        return False, False, "holding"
    remaining = 100.0 - session
    if remaining / burn < PICKUP_S:
        return True, False, "burn"
    return False, False, "holding"


def _fill_phrase(active: AccountSnapshot, samples: list[Sample]) -> str:
    session = active.five_hour
    if session is None:
        return "session unread"
    burn = session_burn(samples)
    if burn is None or burn <= 0.0:
        return f"session {session.pct:.0f}%"
    seconds = (100.0 - session.pct) / burn
    if seconds >= 3600.0:
        return f"session {session.pct:.0f}%, about {seconds / 3600.0:.1f}h of runway"
    return f"session {session.pct:.0f}%, about {max(1, round(seconds / 60.0)):.0f}m of runway"


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


def decide(
    accounts: list[AccountSnapshot],
    *,
    current: str,
    samples: list[Sample],
    hysteresis_pct: float,
    prefer_fable: bool,
) -> Decision:
    """Pick a landing account, or stay.

    Fable changes who we land on. It does not by itself force a leave while
    the session and the shared weekly window still have runway, because that
    would abandon an account Opus can still use.
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
    fable_available = prefer_fable and any(_fable_usable(account) for account in accounts)
    leave, escaping, _because = _should_leave(active, samples)
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
        peers = [account for account in others if _fable_usable(account)]
        landing_fable = bool(peers)
        if not peers:
            peers = [account for account in others if _opus_usable(account)]
    else:
        peers = [account for account in others if _opus_usable(account)]
    if not peers:
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
    if not qualified and escaping:
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
    qualified.sort(key=lambda account: (-peer_headroom(account), int(account.number) if account.number.isdigit() else account.number))
    chosen = qualified[0]
    phrase = _fill_phrase(active, samples)
    if landing_fable:
        detail = f"{phrase}; switching to account {chosen.number}, which has the most Fable room"
    elif prefer_fable:
        detail = (
            "Fable is unavailable on every other account; only session and weekly remain. "
            f"{phrase}; switching to account {chosen.number}"
        )
    else:
        detail = f"{phrase}; switching to account {chosen.number}"
    return Decision(
        switch_to=chosen.number,
        reason="forecast",
        detail=detail,
        fable_available=landing_fable,
        escaping_limit=escaping,
    )
