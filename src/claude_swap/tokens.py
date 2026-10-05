"""Token totals from Claude Code's own session logs.

Claude Code appends one JSON line per event to
``~/.claude/projects/<project>/<session>.jsonl``. Each ``assistant`` line
carries ``message.model`` and ``message.usage`` with input, output, cache
read, and cache creation token counts. A ``credential_org`` attachment near
the top of each session names the organization that was signed in, and a
later one marks a switch mid-session. That is enough to total tokens per
organization, per model, and per hour without calling Anthropic.

Files are read incrementally: the byte offset and the organization in force
are kept per file, so a refresh only parses what was appended since last time.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

TOKEN_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


@dataclass
class Call:
    at: float
    organization: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int

    @property
    def total(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_creation_tokens
        )

    @property
    def weighted(self) -> float:
        """Tokens weighted by their relative cost, in input-token units.

        Anthropic prices output at 5x input, a cache write at 1.25x, and a
        cache read at 0.1x, and usage limits track cost rather than raw
        count. Raw totals are 96% cache reads here, so a shift toward or
        away from cached context would swing a raw ratio by an order of
        magnitude while the real spend barely moved.
        """
        return (
            self.input_tokens
            + 5.0 * self.output_tokens
            + 0.1 * self.cache_read_tokens
            + 1.25 * self.cache_creation_tokens
        )


@dataclass(frozen=True)
class Refusal:
    """The API refused a model on this organization.

    Recorded from ``assistant`` rows whose ``apiError`` names a usage-credit
    refusal: an answer the server actually gave. Claude Code also writes
    ``model_consent_fallback`` system rows when it declines a model from the
    profile it holds in memory, without calling the API; on Oct 5 two
    long-running sessions did that for an hour on accounts whose Fable was
    fine, because they had loaded a standard seat's profile during a login.
    Those rows say nothing about the account and are not counted.
    """

    at: float
    organization: str
    model: str
    fallback_model: str


@dataclass
class _FileCursor:
    offset: int = 0
    organization: str = ""
    partial: str = ""


@dataclass
class TokenLedger:
    """Incremental reader over the session logs under ``root``."""

    root: Path
    horizon_s: float = 7 * 24 * 3600.0
    cursors: dict[str, _FileCursor] = field(default_factory=dict)
    calls: list[Call] = field(default_factory=list)
    refusals: list[Refusal] = field(default_factory=list)
    last_scan_at: float = 0.0

    def refused_since(self, organization: str, since: float, model_contains: str = "fable") -> float | None:
        """Latest refusal time for ``model_contains`` on the organization after ``since``."""
        times = [
            refusal.at for refusal in self.refusals
            if refusal.organization == organization
            and refusal.at >= since
            and model_contains in refusal.model.lower()
        ]
        return max(times) if times else None

    def refresh(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        cutoff = now - self.horizon_s
        seen: set[str] = set()
        if self.root.is_dir():
            for path in self.root.rglob("*.jsonl"):
                try:
                    stat = path.stat()
                except OSError:
                    continue
                if stat.st_mtime < cutoff:
                    continue
                key = str(path)
                seen.add(key)
                cursor = self.cursors.setdefault(key, _FileCursor())
                if stat.st_size < cursor.offset:
                    cursor.offset = 0
                    cursor.partial = ""
                if stat.st_size == cursor.offset:
                    continue
                self._read(path, cursor)
        self.calls = [call for call in self.calls if call.at >= cutoff]
        self.refusals = [refusal for refusal in self.refusals if refusal.at >= cutoff]
        for key in list(self.cursors):
            if key not in seen:
                del self.cursors[key]
        self.last_scan_at = now

    def _read(self, path: Path, cursor: _FileCursor) -> None:
        try:
            with path.open("rb") as handle:
                handle.seek(cursor.offset)
                data = handle.read()
                cursor.offset = handle.tell()
        except OSError:
            return
        text = cursor.partial + data.decode("utf-8", "replace")
        lines = text.split("\n")
        cursor.partial = lines.pop() if not text.endswith("\n") else ""
        for line in lines:
            if not line:
                continue
            if '"credential_org"' in line:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                organization = (row.get("attachment") or {}).get("organizationUuid")
                if isinstance(organization, str):
                    cursor.organization = organization
                continue
            if '"usage"' not in line or '"assistant"' not in line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("type") != "assistant":
                continue
            message = row.get("message") or {}
            api_error = row.get("apiError")
            if isinstance(api_error, str) and "usage_credit" in api_error:
                stamp = _parse_timestamp(row.get("timestamp"))
                if stamp is not None:
                    self.refusals.append(
                        Refusal(
                            at=stamp,
                            organization=cursor.organization,
                            model=str(message.get("model") or ""),
                            fallback_model="",
                        )
                    )
                continue
            usage = message.get("usage") or {}
            if not isinstance(usage, dict):
                continue
            stamp = _parse_timestamp(row.get("timestamp"))
            if stamp is None:
                continue
            model = message.get("model") or "unknown"
            if model == "<synthetic>":
                continue

            def count(key: str) -> int:
                value = usage.get(key)
                return int(value) if isinstance(value, (int, float)) else 0

            self.calls.append(
                Call(
                    at=stamp,
                    organization=cursor.organization,
                    model=str(model),
                    input_tokens=count("input_tokens"),
                    output_tokens=count("output_tokens"),
                    cache_read_tokens=count("cache_read_input_tokens"),
                    cache_creation_tokens=count("cache_creation_input_tokens"),
                )
            )

    def summary(self, since_s: float, now: float | None = None) -> dict:
        """Totals since ``now - since_s``, by organization, model, and hour."""
        now = time.time() if now is None else now
        cutoff = now - since_s
        by_organization: dict[str, dict] = {}
        by_model: dict[str, dict] = {}
        hours: dict[int, dict] = {}
        total = _empty()
        calls = 0
        for call in self.calls:
            if call.at < cutoff:
                continue
            calls += 1
            _add(total, call)
            _add(by_organization.setdefault(call.organization or "", _empty()), call)
            _add(by_model.setdefault(call.model, _empty()), call)
            bucket = int(call.at // 3600) * 3600
            hour = hours.setdefault(bucket, {"at": bucket, "models": {}})
            _add(hour["models"].setdefault(call.model, _empty()), call)
        return {
            "sinceSeconds": since_s,
            "calls": calls,
            "total": total,
            "byOrganization": by_organization,
            "byModel": by_model,
            "hours": [hours[key] for key in sorted(hours)],
        }


def _empty() -> dict:
    return {
        "calls": 0,
        "input": 0,
        "output": 0,
        "cacheRead": 0,
        "cacheCreation": 0,
        "total": 0,
    }


def _add(bucket: dict, call: Call) -> None:
    bucket["calls"] += 1
    bucket["input"] += call.input_tokens
    bucket["output"] += call.output_tokens
    bucket["cacheRead"] += call.cache_read_tokens
    bucket["cacheCreation"] += call.cache_creation_tokens
    bucket["total"] += call.total


def _parse_timestamp(raw: object) -> float | None:
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
    except ValueError:
        return None


def default_root() -> Path:
    return Path.home() / ".claude" / "projects"
