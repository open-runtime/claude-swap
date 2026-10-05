"""Room in minutes and tokens, per account and window.

A percent is not a quantity. One point of a Max 20x week is four times the
tokens of one point on a team seat, and one point lasts seconds under a
dozen agents and hours under one. Two local records make the conversion:

- ``usage_history.jsonl``: every reading of every account's windows.
- Claude Code's session logs: every model call with its token counts and
  the organization signed in (see ``tokens.py``).

For each account and window, tokens per percent point is measured over a
lookback as tokens spent while that window rose, divided by how far it rose.
Remaining tokens are the remaining points times that ratio. Remaining minutes
divide those tokens by this machine's recent token demand, so a candidate
account is judged by how long *your* current load would last there, not by
its own idle history.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from claude_swap.tokens import Call

WINDOWS = ("h5", "d7", "fable")
WINDOW_LABELS = {"h5": "session", "d7": "weekly", "fable": "Fable"}
FLAT_BREAK_S = 15 * 60.0
DEMAND_LOOKBACK_S = 30 * 60.0
MIN_RISE_PCT = 1.0


@dataclass(frozen=True)
class Reading:
    at: float
    slot: str
    organization: str
    pct: dict[str, float]
    # Reset time the provider published for each window at this reading.
    scheduled: dict[str, str] = field(default_factory=dict)


@dataclass
class WindowRoom:
    window: str
    used_pct: float | None = None
    remaining_pct: float | None = None
    burn_pct_per_hour: float | None = None
    tokens_per_point: float | None = None
    tokens_per_point_source: str = "none"
    tokens_spent: float = 0.0
    points_risen: float = 0.0
    tokens_left: float | None = None
    minutes_left: float | None = None
    resets_at: str | None = None

    def as_dict(self) -> dict:
        return {
            "window": self.window,
            "usedPct": self.used_pct,
            "remainingPct": self.remaining_pct,
            "burnPctPerHour": self.burn_pct_per_hour,
            "tokensPerPoint": self.tokens_per_point,
            "tokensPerPointSource": self.tokens_per_point_source,
            "tokensSpent": self.tokens_spent,
            "pointsRisen": self.points_risen,
            "tokensLeft": self.tokens_left,
            "minutesLeft": self.minutes_left,
            "resetsAt": self.resets_at,
        }


@dataclass
class AccountRoom:
    slot: str
    organization: str
    tier: str | None
    windows: dict[str, WindowRoom] = field(default_factory=dict)

    def minutes_for(self, model: str) -> float | None:
        """Minutes your current demand would last here for ``model``.

        Opus draws on session and weekly. Fable also needs its own week.
        The answer is the smallest known window; None when nothing is known.
        """
        keys = ("h5", "d7", "fable") if model == "fable" else ("h5", "d7")
        known = [
            self.windows[key].minutes_left
            for key in keys
            if key in self.windows and self.windows[key].minutes_left is not None
        ]
        return min(known) if known else None

    def as_dict(self) -> dict:
        return {
            "slot": self.slot,
            "organization": self.organization,
            "tier": self.tier,
            "windows": {key: room.as_dict() for key, room in self.windows.items()},
            "minutesForOpus": self.minutes_for("opus"),
            "minutesForFable": self.minutes_for("fable"),
        }


def readings_from_history(rows: list[dict]) -> list[Reading]:
    out: list[Reading] = []
    for row in rows:
        pct: dict[str, float] = {}
        scheduled: dict[str, str] = {}
        for key in ("h5", "d7"):
            if isinstance(row.get(key), (int, float)):
                pct[key] = float(row[key])
            if isinstance(row.get(key + "Reset"), str):
                scheduled[key] = row[key + "Reset"]
        scoped = row.get("scoped")
        if isinstance(scoped, dict):
            for name, value in scoped.items():
                if isinstance(name, str) and name.lower() == "fable" and isinstance(value, (int, float)):
                    pct["fable"] = float(value)
                    scoped_reset = row.get("scopedReset")
                    if isinstance(scoped_reset, dict) and isinstance(scoped_reset.get(name), str):
                        scheduled["fable"] = scoped_reset[name]
        if not pct:
            continue
        out.append(Reading(at=float(row["at"]), slot=str(row.get("slot", "")), organization=str(row.get("org", "")), pct=pct, scheduled=scheduled))
    out.sort(key=lambda reading: reading.at)
    return out


_POLL_LINE = re.compile(r"^(\d{2}):(\d{2}):(\d{2})\s+Account-(\d+) \([^)]*\): .*?\| others: (.*)$")
_OTHER = re.compile(r"#(\d+): ((?:[^,#]|,(?! #))*)")
_WINDOW = re.compile(r"(5h|7d|Fable) (\d+)%")


def readings_from_auto_log(path: Path, end: datetime, organizations: dict[str, str]) -> list[Reading]:
    """Backfill from the rotator's human log.

    Each poll line lists every non-active account's windows. Lines carry a
    time but no date, so dates are inferred walking backwards from ``end``:
    whenever the clock time increases while walking back, the day moves back
    one. The active account's own windows are not in these lines.
    """
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out: list[Reading] = []
    day = end.date()
    previous_seconds: int | None = None
    for line in reversed(lines):
        match = _POLL_LINE.match(line)
        if not match:
            continue
        hour, minute, second, _active, others = match.groups()
        seconds = int(hour) * 3600 + int(minute) * 60 + int(second)
        if previous_seconds is not None and seconds > previous_seconds:
            day = day - timedelta(days=1)
        previous_seconds = seconds
        stamp = datetime.combine(day, datetime.min.time(), tzinfo=end.tzinfo) + timedelta(seconds=seconds)
        if stamp > end:
            continue
        for other in _OTHER.finditer(others):
            slot, body = other.groups()
            pct: dict[str, float] = {}
            for name, value in _WINDOW.findall(body):
                key = {"5h": "h5", "7d": "d7", "Fable": "fable"}[name]
                pct[key] = float(value)
            if not pct:
                continue
            out.append(Reading(at=stamp.timestamp(), slot=slot, organization=organizations.get(slot, ""), pct=pct))
    out.sort(key=lambda reading: reading.at)
    return out


def _series(readings: list[Reading], slot: str, window: str) -> list[tuple[float, float]]:
    """Every reading, repeats included. A repeated value is how idle time shows."""
    return [(r.at, r.pct[window]) for r in readings if r.slot == slot and window in r.pct]


def _segments(series: list[tuple[float, float]], since: float) -> list[tuple[float, float, float]]:
    """(start, end, rise) runs after ``since`` with no reset and no long idle stretch.

    A reset is a drop. Idle is the same value repeated for ``FLAT_BREAK_S``;
    a sparse gap with a rise across it is still a rise. A segment that goes
    idle ends where it went flat, so idle time contributes no tokens to it.
    """
    out: list[tuple[float, float, float]] = []
    start: tuple[float, float] | None = None
    last: tuple[float, float] | None = None
    flat_since: float | None = None

    def close(end_at: float, end_pct: float) -> None:
        if start is not None and end_pct - start[1] >= MIN_RISE_PCT:
            out.append((start[0], end_at, end_pct - start[1]))

    for at, pct in series:
        if last is None:
            if at >= since:
                start = (at, pct)
            last = (at, pct)
            flat_since = None
            continue
        if pct < last[1]:
            if start is not None:
                close(flat_since if flat_since is not None else last[0], last[1])
            start = (at, pct) if at >= since else None
            flat_since = None
        elif pct == last[1]:
            if flat_since is None:
                flat_since = last[0]
            if start is not None and at - flat_since >= FLAT_BREAK_S:
                close(flat_since, pct)
                start = None
        else:
            if start is None and at >= since:
                start = last if (flat_since is None or at - flat_since < FLAT_BREAK_S) else last
            flat_since = None
        last = (at, pct)
    if start is not None and last is not None:
        close(flat_since if flat_since is not None else last[0], last[1])
    return out


def _is_fable(model: str) -> bool:
    return "fable" in model.lower()


RESET_DROP_PCT = 20.0


def resets(readings: list[Reading], since: float) -> list[dict]:
    """Window resets seen in the history: a drop of ``RESET_DROP_PCT`` or more
    between consecutive readings of one account's window. The provider also
    publishes a reset time per window; a drop well before that time is an
    early reset or a quota grant, which is worth seeing."""
    out: list[dict] = []
    previous: dict[tuple[str, str], tuple[float, float, str | None]] = {}
    for reading in readings:
        for window, pct in reading.pct.items():
            key = (reading.slot, window)
            last = previous.get(key)
            if last is not None and last[1] - pct >= RESET_DROP_PCT and reading.at >= since:
                out.append({
                    "slot": reading.slot,
                    "window": window,
                    "at": reading.at,
                    "fromPct": last[1],
                    "toPct": pct,
                    # The reset the provider had published before the drop.
                    # Known only for readings recorded by the store itself.
                    "scheduledAt": last[2],
                })
            previous[key] = (reading.at, pct, reading.scheduled.get(window))
    out.sort(key=lambda item: item["at"], reverse=True)
    return out


def series_for(readings: list[Reading], slot: str, since: float) -> list[dict]:
    """Readings of one account after ``since`` as chart points."""
    return [
        {"at": reading.at, **{window: pct for window, pct in reading.pct.items()}}
        for reading in readings
        if reading.slot == slot and reading.at >= since
    ]


class Allocation:
    def __init__(
        self,
        readings: list[Reading],
        calls: list[Call],
        *,
        slot_organizations: dict[str, str],
        tiers: dict[str, str | None],
        now: float,
    ):
        self.readings = readings
        self.calls = sorted(calls, key=lambda call: call.at)
        self.slot_organizations = slot_organizations
        self.tiers = tiers
        self.now = now

    # -- demand ---------------------------------------------------------------

    def demand_tokens_per_minute(self, *, fable_only: bool, lookback_s: float = DEMAND_LOOKBACK_S) -> float:
        """Cost-weighted tokens per minute over the lookback (see ``Call.weighted``)."""
        since = self.now - lookback_s
        total = sum(
            call.weighted for call in self.calls
            if call.at >= since and (not fable_only or _is_fable(call.model))
        )
        return total / (lookback_s / 60.0)

    # -- measurement ----------------------------------------------------------

    def _tokens_between(self, organization: str, start: float, end: float, *, fable_only: bool) -> float:
        """Cost-weighted tokens the organization spent in (start, end]."""
        return sum(
            call.weighted for call in self.calls
            if call.organization == organization
            and start < call.at <= end
            and (not fable_only or _is_fable(call.model))
        )

    def _rising_intervals(self, slot: str, window: str, since: float) -> list[tuple[float, float]]:
        series = _series(self.readings, slot, window)
        out: list[tuple[float, float]] = []
        previous: tuple[float, float] | None = None
        for at, pct in series:
            if previous is not None and at >= since and pct > previous[1]:
                out.append((previous[0], at))
            previous = (at, pct)
        return out

    def tokens_per_point(self, slot: str, window: str, lookback_s: float) -> tuple[float | None, float, float]:
        """(ratio, weighted tokens, points) measured for one slot and window.

        Tokens spent by another seat of the same organization while its own
        window rose are left out, so team seats sharing one organization do
        not inherit each other's spend.
        """
        organization = self.slot_organizations.get(slot, "")
        since = self.now - lookback_s
        series = _series(self.readings, slot, window)
        fable_only = window == "fable"
        siblings = [other for other, org in self.slot_organizations.items() if org == organization and other != slot]
        sibling_rises = [interval for other in siblings for interval in self._rising_intervals(other, window, since)]
        tokens = 0.0
        points = 0.0
        for start, end, rise in _segments(series, since):
            spent = self._tokens_between(organization, start, end, fable_only=fable_only)
            for rise_start, rise_end in sibling_rises:
                low, high = max(start, rise_start), min(end, rise_end)
                if low < high:
                    spent -= self._tokens_between(organization, low, high, fable_only=fable_only)
            if spent <= 0:
                continue
            tokens += spent
            points += rise
        if points < MIN_RISE_PCT:
            return None, tokens, points
        return tokens / points, tokens, points

    def burn_pct_per_hour(self, slot: str, window: str, lookback_s: float) -> float | None:
        since = self.now - lookback_s
        series = [point for point in _series(self.readings, slot, window) if point[0] >= since]
        if len(series) < 2:
            return None
        rise = sum(max(0.0, b[1] - a[1]) for a, b in zip(series, series[1:]))
        span = series[-1][0] - series[0][0]
        if span <= 0:
            return None
        return rise / span * 3600.0

    # -- summary --------------------------------------------------------------

    def summarize(self, latest: dict[str, dict], lookback_s: float) -> dict[str, AccountRoom]:
        """``latest`` maps slot -> {window: {"pct": p, "resetsAt": iso}} from the store."""
        rooms: dict[str, AccountRoom] = {}
        measured: dict[str, list[tuple[str | None, float]]] = {key: [] for key in WINDOWS}
        for slot, windows in latest.items():
            room = AccountRoom(slot=slot, organization=self.slot_organizations.get(slot, ""), tier=self.tiers.get(slot))
            for key in WINDOWS:
                window = windows.get(key)
                if not isinstance(window, dict) or window.get("pct") is None:
                    continue
                used = float(window["pct"])
                ratio, tokens, points = self.tokens_per_point(slot, key, lookback_s)
                entry = WindowRoom(
                    window=key,
                    used_pct=used,
                    remaining_pct=max(0.0, 100.0 - used),
                    burn_pct_per_hour=self.burn_pct_per_hour(slot, key, lookback_s),
                    tokens_per_point=ratio,
                    tokens_per_point_source="measured" if ratio is not None else "none",
                    tokens_spent=tokens,
                    points_risen=points,
                    resets_at=window.get("resetsAt"),
                )
                if ratio is not None:
                    measured[key].append((room.tier, ratio))
                room.windows[key] = entry
            rooms[slot] = room
        # Borrow a ratio for accounts with no measurement: same tier first,
        # then any account. The source says which, so the page can say "est."
        demand_all = self.demand_tokens_per_minute(fable_only=False)
        demand_fable = self.demand_tokens_per_minute(fable_only=True) or demand_all
        for room in rooms.values():
            for key, entry in room.windows.items():
                if entry.tokens_per_point is None:
                    same_tier = [ratio for tier, ratio in measured[key] if tier == room.tier and tier is not None]
                    pool = same_tier or [ratio for _tier, ratio in measured[key]]
                    if pool:
                        pool.sort()
                        entry.tokens_per_point = pool[len(pool) // 2]
                        entry.tokens_per_point_source = "same-tier" if same_tier else "any-account"
                if entry.tokens_per_point is not None and entry.remaining_pct is not None:
                    entry.tokens_left = entry.remaining_pct * entry.tokens_per_point
                    demand = demand_fable if key == "fable" else demand_all
                    entry.minutes_left = (entry.tokens_left / demand) if demand > 0 else None
        return rooms
