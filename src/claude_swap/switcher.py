"""Core account switcher logic for Claude Code."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib import error, request

# Only import keyring on non-Linux platforms
if sys.platform != "linux":
    import keyring

from claude_swap.exceptions import (
    AccountNotFoundError,
    ConfigError,
    CredentialReadError,
    CredentialWriteError,
    SwitchError,
    ValidationError,
)
from claude_swap.locking import FileLock
from claude_swap.logging_config import setup_logging
from claude_swap.models import Platform, SwitchTransaction, get_timestamp

# Service name for keyring storage
KEYRING_SERVICE = "claude-code"
KEYRING_ACTIVE_USERNAME = "active-credentials"
DEFAULT_STATUS_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
DEFAULT_STATUS_USER_AGENT = "claude-code/2.1.77"
DEFAULT_STATUS_BETA_HEADER = "oauth-2025-04-20"
DEFAULT_STATUS_TIMEOUT_SECONDS = 20.0

OAUTH_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
OAUTH_EXPIRY_BUFFER_MS = 5 * 60 * 1000


class ClaudeAccountSwitcher:
    """Multi-account switcher for Claude Code."""

    def __init__(self, debug: bool = False):
        self.home = Path.home()
        self.backup_dir = self.home / ".claude-swap-backup"
        self.sequence_file = self.backup_dir / "sequence.json"
        self.configs_dir = self.backup_dir / "configs"
        self.credentials_dir = self.backup_dir / "credentials"
        self.lock_file = self.backup_dir / ".lock"
        self.platform = Platform.detect()
        self._logger = setup_logging(self.backup_dir, debug=debug)
        self._logger.debug(
            "Initialized switcher platform=%s home=%s backup_dir=%s sequence_file=%s",
            self.platform.name,
            self.home,
            self.backup_dir,
            self.sequence_file,
        )

    def _is_running_in_container(self) -> bool:
        """Check if running inside a container."""
        # Check environment variables (works on all platforms)
        if os.environ.get("CONTAINER") or os.environ.get("container"):
            return True

        # Windows doesn't have the same container indicators
        if self.platform == Platform.WINDOWS:
            return False

        # Check for Docker environment file (Linux/macOS)
        if Path("/.dockerenv").exists():
            return True

        # Check cgroup for container indicators (Linux)
        cgroup_path = Path("/proc/1/cgroup")
        if cgroup_path.exists():
            try:
                content = cgroup_path.read_text()
                if any(
                    x in content
                    for x in ["docker", "lxc", "containerd", "kubepods"]
                ):
                    return True
            except PermissionError:
                pass

        # Check mount info (Linux)
        mountinfo_path = Path("/proc/self/mountinfo")
        if mountinfo_path.exists():
            try:
                content = mountinfo_path.read_text()
                if any(x in content for x in ["docker", "overlay"]):
                    return True
            except PermissionError:
                pass

        return False

    def _get_claude_config_path(self) -> Path:
        """Get Claude configuration file path with fallback."""
        primary_config = self.home / ".claude" / ".claude.json"
        fallback_config = self.home / ".claude.json"

        if primary_config.exists():
            try:
                data = json.loads(primary_config.read_text())
                if "oauthAccount" in data:
                    self._logger.debug(
                        "Using primary Claude config path: %s", primary_config
                    )
                    return primary_config
            except (json.JSONDecodeError, KeyError):
                self._logger.debug(
                    "Primary Claude config exists but is not usable, falling back: %s",
                    primary_config,
                )
                pass

        self._logger.debug("Using fallback Claude config path: %s", fallback_config)
        return fallback_config

    def _validate_email(self, email: str) -> bool:
        """Validate email format."""
        pattern = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
        return bool(re.match(pattern, email))

    def _setup_directories(self) -> None:
        """Create backup directories with proper permissions."""
        for directory in [self.backup_dir, self.configs_dir, self.credentials_dir]:
            directory.mkdir(parents=True, exist_ok=True)
            if sys.platform != "win32":
                os.chmod(directory, 0o700)

    def _read_json(self, path: Path) -> dict | None:
        """Read and parse JSON file."""
        if not path.exists():
            self._logger.debug("JSON file does not exist: %s", path)
            return None
        try:
            data = json.loads(path.read_text())
            if isinstance(data, dict):
                self._logger.debug(
                    "Read JSON file %s with top-level keys=%s",
                    path,
                    sorted(data.keys()),
                )
            else:
                self._logger.debug(
                    "Read JSON file %s with top-level type=%s",
                    path,
                    type(data).__name__,
                )
            return data
        except json.JSONDecodeError:
            self._logger.warning(f"Invalid JSON in {path}")
            return None

    def _write_json(self, path: Path, data: dict) -> None:
        """Write JSON file with validation."""
        content = json.dumps(data, indent=2)
        self._logger.debug(
            "Writing JSON file %s with top-level keys=%s",
            path,
            sorted(data.keys()),
        )

        # Write to temp file first
        temp_path = path.with_suffix(f".{os.getpid()}.tmp")
        temp_path.write_text(content)

        # Validate written content
        try:
            json.loads(temp_path.read_text())
        except json.JSONDecodeError:
            temp_path.unlink()
            raise ConfigError("Generated invalid JSON")

        # Move to final location
        shutil.move(str(temp_path), str(path))
        if sys.platform != "win32":
            os.chmod(path, 0o600)
        self._logger.debug("Wrote JSON file %s (%s bytes)", path, len(content))

    def _normalize_email(self, email: str) -> str:
        """Normalize email for comparisons."""
        return email.strip().lower()

    def _describe_secret_payload(self, payload: str | None) -> str:
        """Summarize sensitive payloads without logging secret values."""
        if payload is None:
            return "state=error"
        if not payload:
            return "state=empty"

        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            return f"state=text length={len(payload)}"

        summary = [f"state=json type={type(parsed).__name__}", f"length={len(payload)}"]
        if isinstance(parsed, dict):
            summary.append(f"json_keys={sorted(parsed.keys())}")
            oauth = parsed.get("claudeAiOauth")
            if isinstance(oauth, dict):
                summary.append(f"claudeAiOauth_keys={sorted(oauth.keys())}")
                access_token = oauth.get("accessToken")
                refresh_token = oauth.get("refreshToken")
                if isinstance(access_token, str):
                    summary.append(f"accessToken_length={len(access_token)}")
                if isinstance(refresh_token, str):
                    summary.append(f"refreshToken_length={len(refresh_token)}")

        return " ".join(summary)

    def _extract_identity(self, data: dict | None) -> dict[str, str] | None:
        """Extract stable account identity fields from Claude metadata."""
        if not isinstance(data, dict):
            return None

        identity = {
            "email": str(data.get("email") or data.get("emailAddress") or "").strip(),
            "uuid": str(data.get("uuid") or data.get("accountUuid") or "").strip(),
            "organizationUuid": str(data.get("organizationUuid") or "").strip(),
            "displayName": str(data.get("displayName") or "").strip(),
            "organizationName": str(data.get("organizationName") or "").strip(),
            "billingType": str(data.get("billingType") or "").strip(),
        }

        if not any(identity.values()):
            return None
        return identity

    def _format_identity(self, identity: dict[str, str] | None) -> str:
        """Format identity fields for debug logging."""
        if not identity:
            return "identity=<none>"

        return (
            f"email={identity.get('email') or '-'} "
            f"uuid={identity.get('uuid') or '-'} "
            f"organizationUuid={identity.get('organizationUuid') or '-'} "
            f"displayName={identity.get('displayName') or '-'} "
            f"organizationName={identity.get('organizationName') or '-'} "
            f"billingType={identity.get('billingType') or '-'}"
        )

    def _account_label(self, identity: dict[str, str] | None) -> str | None:
        """Derive a short human-readable account label."""
        if not identity:
            return None

        organization_name = str(identity.get("organizationName") or "").strip()
        if organization_name:
            return organization_name

        display_name = str(identity.get("displayName") or "").strip()
        if display_name:
            return display_name

        organization_uuid = str(identity.get("organizationUuid") or "").strip()
        if organization_uuid:
            return organization_uuid.split("-", maxsplit=1)[0]

        return None

    def _format_account_display(self, identity: dict | None) -> str:
        """Format account identity for user-facing output."""
        extracted_identity = self._extract_identity(identity)
        if not extracted_identity:
            return "unknown"

        email = extracted_identity.get("email") or "unknown"
        label = self._account_label(extracted_identity)
        if label:
            return f"{email} [{label}]"
        return email

    def _parse_status_datetime(self, value: str | None) -> datetime | None:
        """Parse status timestamps and normalize naive datetimes to UTC."""
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed

    def _parse_credential_payload(self, credentials: str | None) -> dict[str, Any] | None:
        """Parse Claude credential JSON and return oauth payload if present."""
        if not credentials:
            return None
        try:
            payload = json.loads(credentials)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None

        oauth = payload.get("claudeAiOauth")
        if isinstance(oauth, dict):
            return oauth
        return payload

    def _extract_access_token(self, credentials: str | None) -> str | None:
        """Extract access token from Claude credentials."""
        oauth = self._parse_credential_payload(credentials)
        if not oauth:
            return None
        token = oauth.get("accessToken")
        return token if isinstance(token, str) and token else None

    def _is_oauth_token_expired(self, credentials: str | None) -> bool:
        """Check if the OAuth token in credentials is expired or about to expire."""
        oauth = self._parse_credential_payload(credentials)
        if not oauth:
            return False
        expires_at = oauth.get("expiresAt")
        if not isinstance(expires_at, (int, float)):
            return False
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        return now_ms + OAUTH_EXPIRY_BUFFER_MS >= int(expires_at)

    def _refresh_oauth_credentials(self, credentials: str) -> str | None:
        """Refresh an OAuth access token via the platform token endpoint."""
        try:
            data = json.loads(credentials)
            oauth = data.get("claudeAiOauth")
            if not isinstance(oauth, dict):
                return None

            refresh_token = oauth.get("refreshToken")
            if not refresh_token:
                return None

            scopes = oauth.get("scopes")
            if not isinstance(scopes, list) or not scopes:
                self._logger.debug("OAuth refresh skipped: scopes missing")
                return None

            body = json.dumps({
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": OAUTH_CLIENT_ID,
                "scope": " ".join(scopes),
            }).encode()

            req = request.Request(
                OAUTH_TOKEN_URL,
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with request.urlopen(req, timeout=10) as resp:
                resp_data = json.loads(resp.read().decode())

            now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
            oauth["accessToken"] = resp_data["access_token"]
            oauth["expiresAt"] = now_ms + resp_data["expires_in"] * 1000
            if resp_data.get("refresh_token"):
                oauth["refreshToken"] = resp_data["refresh_token"]
            if resp_data.get("scope"):
                oauth["scopes"] = resp_data["scope"].split()

            data["claudeAiOauth"] = oauth
            return json.dumps(data)
        except Exception as exc:
            self._logger.debug("OAuth refresh failed: %r", exc)
            return None

    def _ensure_fresh_credentials(
        self, account_num: str, email: str, credentials: str
    ) -> tuple[str, str | None]:
        """Return (credentials, access_token) after refreshing if expired.

        If the token is expired and refresh succeeds, the refreshed credentials
        are persisted to backup storage. Returns the working credentials and
        the extracted access token (or None if no usable token).
        """
        access_token = self._extract_access_token(credentials)

        if access_token and not self._is_oauth_token_expired(credentials):
            return credentials, access_token

        if not self._is_oauth_token_expired(credentials):
            return credentials, access_token

        self._logger.debug(
            "OAuth token expired for Account-%s, attempting refresh", account_num
        )
        refreshed = self._refresh_oauth_credentials(credentials)
        if refreshed:
            self._write_account_credentials(account_num, email, refreshed)
            self._logger.info(
                "Refreshed OAuth token for Account-%s", account_num
            )
            return refreshed, self._extract_access_token(refreshed)

        return credentials, access_token

    def _normalize_expires_at(self, value: Any) -> str | None:
        """Convert credential expiry values to ISO timestamps when possible."""
        if not isinstance(value, (int, float)):
            return None

        raw = float(value)
        if raw > 1_000_000_000_000:
            raw = raw / 1000.0
        try:
            return datetime.fromtimestamp(raw, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return None

    def _credential_metadata(self, credentials: str | None) -> dict[str, Any]:
        """Build safe credential metadata for status output."""
        oauth = self._parse_credential_payload(credentials) or {}
        token = oauth.get("accessToken")
        scopes_raw = oauth.get("scopes")
        scopes = (
            [scope for scope in scopes_raw if isinstance(scope, str)]
            if isinstance(scopes_raw, list)
            else []
        )
        return {
            "access_token_present": isinstance(token, str) and bool(token),
            "access_token_suffix": token[-6:] if isinstance(token, str) and token else None,
            "subscription_type": (
                str(oauth.get("subscriptionType")) if oauth.get("subscriptionType") else None
            ),
            "scopes": scopes,
            "expires_at": self._normalize_expires_at(oauth.get("expiresAt")),
        }

    def _human_window_name(self, name: str) -> str:
        """Format usage window names for human output."""
        mapping = {
            "extra_usage": "extra usage",
            "five_hour": "5h",
            "seven_day": "7d",
            "seven_day_cowork": "7d cowork",
            "seven_day_sonnet": "7d sonnet",
            "seven_day_opus": "7d opus",
            "seven_day_oauth_apps": "7d oauth apps",
        }
        return mapping.get(name, name.replace("_", " "))

    def _format_timestamp(self, value: str | None) -> str | None:
        """Render ISO timestamps in UTC for user-facing output."""
        if not value:
            return None
        dt = self._parse_status_datetime(value)
        if dt is None:
            return value
        dt = dt.astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M UTC")

    def _format_duration(self, seconds: int) -> str:
        """Render durations like 5h 12m."""
        if seconds <= 0:
            return "now"
        minutes, _ = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        days, hours = divmod(hours, 24)
        parts = []
        if days:
            parts.append(f"{days}d")
        if hours:
            parts.append(f"{hours}h")
        if minutes:
            parts.append(f"{minutes}m")
        if not parts:
            return "<1m"
        return " ".join(parts[:2])

    def _format_reusable_in(self, reset_at: str | None) -> str | None:
        """Render relative time until an account becomes reusable."""
        reset_dt = self._parse_status_datetime(reset_at)
        if reset_dt is None:
            return None
        remaining = int((reset_dt - datetime.now(timezone.utc)).total_seconds())
        if remaining <= 0:
            return "now"
        return self._format_duration(remaining)

    def _render_windows_line(self, windows: dict[str, Any]) -> str | None:
        """Render a compact human summary of usage windows."""
        if not windows:
            return None
        parts = []
        for key, value in windows.items():
            if not isinstance(value, dict):
                continue
            utilization = value.get("utilization")
            if utilization is None or not isinstance(utilization, (int, float)):
                continue
            piece = f"{self._human_window_name(key)} {utilization:.0f}%"
            resets_at = self._format_timestamp(value.get("resets_at"))
            if resets_at:
                piece += f" (resets {resets_at})"
            parts.append(piece)
        return ", ".join(parts) if parts else None

    def _normalize_usage_windows(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Keep only recognizable usage window payloads."""
        windows: dict[str, Any] = {}
        for key, value in payload.items():
            if isinstance(value, dict) and "utilization" in value:
                windows[key] = {
                    "utilization": value.get("utilization"),
                    "resets_at": value.get("resets_at") or value.get("resetsAt"),
                }
        return windows

    def _earliest_limit_reset(self, windows: dict[str, Any]) -> str | None:
        """Get the earliest limit-reset timestamp for a limited account."""
        limited_resets: list[tuple[datetime, str]] = []
        for window in windows.values():
            if not isinstance(window, dict):
                continue
            utilization = window.get("utilization")
            reset_value = window.get("resets_at")
            if not isinstance(utilization, (int, float)) or float(utilization) < 100.0:
                continue
            reset_dt = self._parse_status_datetime(reset_value)
            if reset_dt is None or not isinstance(reset_value, str):
                continue
            limited_resets.append((reset_dt, reset_value))
        if not limited_resets:
            return None
        return min(limited_resets, key=lambda item: item[0])[1]

    def _classify_usage_windows(
        self, windows: dict[str, Any]
    ) -> tuple[str, str, str | None]:
        """Classify usage payload into a high-level status."""
        reset_at = self._earliest_limit_reset(windows)
        if reset_at is not None:
            return ("limited", "usage limit reached", reset_at)
        return ("ready", "OK", None)

    def _fetch_usage_for_token(self, token: str) -> dict[str, Any]:
        """Fetch usage data from Anthropic for a specific OAuth token."""
        usage_url = os.environ.get("CSWAP_STATUS_USAGE_URL", DEFAULT_STATUS_USAGE_URL)
        timeout_seconds = float(
            os.environ.get(
                "CSWAP_STATUS_TIMEOUT_SECONDS", str(DEFAULT_STATUS_TIMEOUT_SECONDS)
            )
        )
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "User-Agent": os.environ.get(
                "CSWAP_STATUS_USER_AGENT", DEFAULT_STATUS_USER_AGENT
            ),
            "Authorization": f"Bearer {token}",
            "anthropic-beta": os.environ.get(
                "CSWAP_STATUS_BETA_HEADER", DEFAULT_STATUS_BETA_HEADER
            ),
        }
        req = request.Request(usage_url, headers=headers, method="GET")
        try:
            with request.urlopen(req, timeout=timeout_seconds) as response:
                body = response.read().decode("utf-8")
                payload = json.loads(body) if body else {}
                payload = payload if isinstance(payload, dict) else {}
                windows = self._normalize_usage_windows(payload)
                state, detail, rate_limited_until = self._classify_usage_windows(
                    windows
                )
                return {
                    "state": state,
                    "detail": detail,
                    "rate_limited_until": rate_limited_until,
                    "windows": windows,
                    "source": "usage_api",
                    "raw": payload,
                }
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace").strip()
            if exc.code in {401, 403}:
                return {
                    "state": "auth",
                    "detail": body or f"http {exc.code}",
                    "rate_limited_until": None,
                    "windows": {},
                    "source": "usage_api",
                    "raw": {},
                }
            if exc.code == 429:
                retry_after = exc.headers.get("Retry-After")
                reset_at = None
                if retry_after and retry_after.isdigit():
                    reset_at = (
                        datetime.now(timezone.utc)
                        + timedelta(seconds=int(retry_after))
                    ).isoformat()
                return {
                    "state": "limited",
                    "detail": body or "rate limit",
                    "rate_limited_until": reset_at,
                    "windows": {},
                    "source": "usage_api",
                    "raw": {},
                }
            return {
                "state": "other",
                "detail": body or f"http {exc.code}",
                "rate_limited_until": None,
                "windows": {},
                "source": "usage_api",
                "raw": {},
            }
        except error.URLError as exc:
            return {
                "state": "probe_error",
                "detail": str(exc.reason),
                "rate_limited_until": None,
                "windows": {},
                "source": "usage_api",
                "raw": {},
            }
        except TimeoutError:
            return {
                "state": "timeout",
                "detail": "request timed out",
                "rate_limited_until": None,
                "windows": {},
                "source": "usage_api",
                "raw": {},
            }
        except json.JSONDecodeError:
            return {
                "state": "other",
                "detail": "invalid JSON from usage endpoint",
                "rate_limited_until": None,
                "windows": {},
                "source": "usage_api",
                "raw": {},
            }

    def _read_identity_from_config_path(
        self, config_path: Path, source: str
    ) -> dict[str, str] | None:
        """Read Claude oauth identity metadata from a config file."""
        data = self._read_json(config_path)
        if not data:
            self._logger.debug(
                "No readable JSON data for %s config path=%s", source, config_path
            )
            return None

        identity = self._extract_identity(data.get("oauthAccount"))
        self._logger.debug(
            "Resolved %s identity from %s: %s",
            source,
            config_path,
            self._format_identity(identity),
        )
        return identity

    def _get_current_account_identity(self) -> dict[str, str] | None:
        """Read identity metadata for the currently active Claude account."""
        config_path = self._get_claude_config_path()
        if not config_path.exists():
            self._logger.debug(
                "Current Claude config path does not exist: %s", config_path
            )
            return None
        return self._read_identity_from_config_path(config_path, "current")

    def _read_backup_account_identity(
        self, account_num: str, email: str
    ) -> dict[str, str] | None:
        """Read identity metadata for a backed-up managed account."""
        config_file = self.configs_dir / f".claude-config-{account_num}-{email}.json"
        if not config_file.exists():
            self._logger.debug(
                "Backup config missing for Account-%s path=%s",
                account_num,
                config_file,
            )
            return None
        return self._read_identity_from_config_path(
            config_file, f"backup Account-{account_num}"
        )

    def _backfill_sequence_metadata(self, data: dict) -> bool:
        """Backfill missing account metadata from stored backup configs."""
        changed = False
        for account_num, account in data.get("accounts", {}).items():
            missing_fields = [
                field
                for field in (
                    "uuid",
                    "organizationUuid",
                    "displayName",
                    "organizationName",
                    "billingType",
                )
                if not account.get(field)
            ]
            if not missing_fields:
                continue

            email = str(account.get("email") or "").strip()
            if not email:
                self._logger.debug(
                    "Cannot backfill Account-%s because email is missing", account_num
                )
                continue

            backup_identity = self._read_backup_account_identity(account_num, email)
            if not backup_identity:
                self._logger.debug(
                    "No backup identity available for Account-%s missing_fields=%s",
                    account_num,
                    missing_fields,
                )
                continue

            updates = {}
            for field in missing_fields:
                value = backup_identity.get(field)
                if value:
                    updates[field] = value

            if updates:
                account.update(updates)
                changed = True
                self._logger.debug(
                    "Backfilled Account-%s metadata from backup: %s",
                    account_num,
                    updates,
                )

        if changed:
            self._write_json(self.sequence_file, data)
            self._logger.info("Persisted backfilled managed-account metadata")
        return changed

    def _identity_match_reason(
        self, left: dict[str, str] | None, right: dict[str, str] | None
    ) -> tuple[bool, str]:
        """Compare two account identities and explain the decision."""
        if not left or not right:
            return False, "missing identity"

        left_org = left.get("organizationUuid") or ""
        right_org = right.get("organizationUuid") or ""
        if left_org and right_org:
            if left_org == right_org:
                return True, "organizationUuid matched"
            return False, "organizationUuid mismatched"
        if left_org or right_org:
            return False, "organizationUuid only present on one side"

        left_uuid = left.get("uuid") or ""
        right_uuid = right.get("uuid") or ""
        if left_uuid and right_uuid:
            if left_uuid == right_uuid:
                return True, "uuid matched"
            return False, "uuid mismatched"
        if left_uuid or right_uuid:
            return False, "uuid only present on one side"

        left_email = self._normalize_email(left.get("email") or "")
        right_email = self._normalize_email(right.get("email") or "")
        if left_email and right_email:
            if left_email == right_email:
                return True, "email fallback matched"
            return False, "email mismatched"

        return False, "insufficient identity fields"

    def _find_matching_account_number(
        self, identity: dict[str, str] | None, data: dict | None
    ) -> str | None:
        """Find the managed account matching a live/current identity."""
        if not identity:
            self._logger.debug("Managed-account lookup skipped: no identity provided")
            return None
        if not data:
            self._logger.debug(
                "Managed-account lookup skipped: no sequence data for target=%s",
                self._format_identity(identity),
            )
            return None

        self._logger.debug(
            "Searching managed accounts for target identity: %s",
            self._format_identity(identity),
        )
        for num, account in data.get("accounts", {}).items():
            account_identity = self._extract_identity(account)
            matched, reason = self._identity_match_reason(identity, account_identity)
            self._logger.debug(
                "Identity compare target vs Account-%s %s => matched=%s reason=%s",
                num,
                self._format_identity(account_identity),
                matched,
                reason,
            )
            if matched:
                return num

        self._logger.debug(
            "No managed account matched target identity: %s",
            self._format_identity(identity),
        )
        return None

    def _sync_active_account_number(self, data: dict, current_account: str) -> None:
        """Persist the live active account number when sequence state is stale."""
        stored_active = data.get("activeAccountNumber")
        if str(stored_active) == current_account:
            self._logger.debug(
                "Sequence activeAccountNumber already matches Account-%s",
                current_account,
            )
            return

        self._logger.warning(
            "Correcting stale activeAccountNumber from %s to %s",
            stored_active,
            current_account,
        )
        data["activeAccountNumber"] = int(current_account)
        data["lastUpdated"] = get_timestamp()
        self._write_json(self.sequence_file, data)

    def _capture_active_state_snapshot(self, data: dict) -> dict[str, Any]:
        """Capture the currently active Claude auth state for later restoration."""
        current_identity = self._get_current_account_identity()
        current_email = current_identity.get("email") if current_identity else None
        if not current_email:
            raise ConfigError("No active Claude account found")

        current_credentials = self._read_credentials()
        if current_credentials is None:
            raise CredentialReadError("Failed to read current credentials")
        if not current_credentials:
            raise CredentialReadError("No credentials found for current account")

        config_path = self._get_claude_config_path()
        try:
            current_config = config_path.read_text()
        except FileNotFoundError:
            raise ConfigError("Claude config file not found")
        except PermissionError:
            raise ConfigError("Permission denied reading Claude config")

        current_account = self._find_matching_account_number(current_identity, data)
        if current_account is not None:
            self._sync_active_account_number(data, current_account)

        snapshot = {
            "credentials": current_credentials,
            "config": current_config,
            "config_path": config_path,
            "identity": current_identity,
            "managed_account_number": current_account,
            "stored_active_account_number": (
                str(data.get("activeAccountNumber"))
                if data.get("activeAccountNumber") is not None
                else None
            ),
            "display": self._format_account_display(current_identity),
        }
        self._logger.debug(
            "Captured active status snapshot managed_account=%s display=%s",
            current_account,
            snapshot["display"],
        )
        return snapshot

    def _restore_active_state_snapshot(
        self, snapshot: dict[str, Any], data: dict
    ) -> tuple[str | None, str]:
        """Restore the active Claude auth state after a status sweep."""
        self._write_credentials(snapshot["credentials"])

        config_path = snapshot["config_path"]
        config_path.write_text(snapshot["config"])
        if sys.platform != "win32":
            os.chmod(config_path, 0o600)

        managed_account_number = snapshot.get("managed_account_number")
        if managed_account_number is not None:
            data["activeAccountNumber"] = int(managed_account_number)
            restored_account_number = str(managed_account_number)
        else:
            data["activeAccountNumber"] = None
            restored_account_number = None

        data["lastUpdated"] = get_timestamp()
        self._write_json(self.sequence_file, data)

        restored_identity = self._get_current_account_identity()
        restored_display = (
            self._format_account_display(restored_identity)
            if restored_identity
            else str(snapshot.get("display") or "unknown")
        )
        self._logger.debug(
            "Restored active status snapshot restored_account=%s display=%s",
            restored_account_number,
            restored_display,
        )
        return restored_account_number, restored_display

    def _activate_account_from_backup(self, target_account: str, data: dict) -> dict[str, str] | None:
        """Activate a managed account from stored backup state without user-facing output."""
        target_info = data.get("accounts", {}).get(target_account)
        if not isinstance(target_info, dict):
            raise AccountNotFoundError(f"Account-{target_account} does not exist")

        target_email = str(target_info.get("email") or "")
        target_credentials = self._read_account_credentials(target_account, target_email)
        target_config = self._read_account_config(target_account, target_email)
        self._logger.debug(
            "Status sweep loading Account-%s credentials summary=%s config_size=%s",
            target_account,
            self._describe_secret_payload(target_credentials),
            len(target_config),
        )

        if not target_credentials or not target_config:
            raise SwitchError(f"Missing backup data for Account-{target_account}")

        self._write_credentials(target_credentials)

        try:
            target_config_data = json.loads(target_config)
        except json.JSONDecodeError as exc:
            raise SwitchError(f"Invalid backup config for Account-{target_account}: {exc}")
        oauth_section = target_config_data.get("oauthAccount")
        if not isinstance(oauth_section, dict):
            raise SwitchError(f"Invalid oauthAccount in backup for Account-{target_account}")

        config_path = self._get_claude_config_path()
        current_config_data = self._read_json(config_path)
        if current_config_data is None:
            raise ConfigError("Claude config file not found")
        current_config_data["oauthAccount"] = oauth_section
        self._write_json(config_path, current_config_data)

        data["activeAccountNumber"] = int(target_account)
        data["lastUpdated"] = get_timestamp()
        self._write_json(self.sequence_file, data)

        live_identity = self._get_current_account_identity()
        merged_identity = self._extract_identity(target_info) or {}
        if live_identity:
            for key, value in live_identity.items():
                if value:
                    merged_identity[key] = value
        self._logger.debug(
            "Activated Account-%s for status sweep identity=%s",
            target_account,
            self._format_identity(merged_identity),
        )
        return merged_identity or None

    def _find_ready_account(
        self, candidates: list[str], data: dict
    ) -> tuple[str | None, str | None]:
        """Probe candidate accounts and return the first with available capacity.

        Returns (account_number, reason) where reason is a human message when
        no ready account is found.  The caller must already hold the file lock
        and must restore the active state afterwards.
        """
        best_limited: tuple[str, datetime | None] | None = None

        print("Checking account capacity...")

        for account_num in candidates:
            account_record = data.get("accounts", {}).get(account_num)
            if not isinstance(account_record, dict):
                self._logger.warning(
                    "Capacity probe skipping Account-%s: missing record", account_num
                )
                continue

            display = self._format_account_display(account_record)

            try:
                account_email = str(account_record.get("email", ""))
                backup_credentials = self._read_account_credentials(
                    account_num, account_email
                )
                if not backup_credentials:
                    self._logger.warning(
                        "Capacity probe Account-%s: cannot read credentials",
                        account_num,
                    )
                    print(f"  Account-{account_num}: {display} — error (no credentials)")
                    continue

                backup_credentials, access_token = self._ensure_fresh_credentials(
                    account_num, account_email, backup_credentials
                )
                if not access_token:
                    self._logger.warning(
                        "Capacity probe Account-%s: no access token", account_num
                    )
                    print(f"  Account-{account_num}: {display} — error (no token)")
                    continue

                usage = self._fetch_usage_for_token(access_token)
                state = usage.get("state")
                windows = usage.get("windows", {})
                windows_line = self._render_windows_line(windows)
                self._logger.debug(
                    "Capacity probe Account-%s state=%s", account_num, state
                )

                if state == "auth":
                    self._logger.debug(
                        "Capacity probe Account-%s got auth error, attempting refresh",
                        account_num,
                    )
                    refreshed = self._refresh_oauth_credentials(backup_credentials)
                    if refreshed:
                        self._write_account_credentials(
                            account_num, account_email, refreshed
                        )
                        retry_token = self._extract_access_token(refreshed)
                        if retry_token:
                            usage = self._fetch_usage_for_token(retry_token)
                            state = usage.get("state")
                            windows = usage.get("windows", {})
                            windows_line = self._render_windows_line(windows)

                if state == "ready":
                    status_detail = "ready"
                    if windows_line:
                        status_detail += f" ({windows_line})"
                    print(f"  Account-{account_num}: {display} — {status_detail}")
                    return account_num, None

                if state == "limited":
                    reset_at = usage.get("rate_limited_until")
                    reset_dt = self._parse_status_datetime(reset_at) if reset_at else None
                    status_detail = "limited"
                    if reset_dt is not None:
                        remaining = int(
                            (reset_dt - datetime.now(timezone.utc)).total_seconds()
                        )
                        eta = self._format_duration(max(remaining, 0))
                        status_detail += f" (resets in {eta})"
                    if windows_line:
                        status_detail += f" — {windows_line}"
                    print(f"  Account-{account_num}: {display} — {status_detail}")

                    if best_limited is None or (
                        reset_dt is not None
                        and (
                            best_limited[1] is None or reset_dt < best_limited[1]
                        )
                    ):
                        best_limited = (account_num, reset_dt)
                else:
                    detail = usage.get("detail", state)
                    print(f"  Account-{account_num}: {display} — {detail}")

            except Exception as exc:
                self._logger.warning(
                    "Capacity probe failed for Account-%s: %s", account_num, exc
                )
                print(f"  Account-{account_num}: {display} — error ({exc})")
                continue

        print()

        if best_limited is not None:
            acct, reset_dt = best_limited
            if reset_dt is not None:
                remaining = int(
                    (reset_dt - datetime.now(timezone.utc)).total_seconds()
                )
                eta = self._format_duration(max(remaining, 0))
                return acct, (
                    f"No accounts with capacity. Switching to Account-{acct} "
                    f"(earliest reset in {eta})."
                )
            return acct, f"No accounts with capacity. Switching to Account-{acct} (soonest to reset)."

        return None, "All accounts are at capacity and none could be probed."

    def _build_status_counts(self, accounts: list[dict[str, Any]]) -> dict[str, int]:
        """Count usage states across swept accounts."""
        counts = {"ready": 0, "limited": 0, "auth": 0, "other": 0}
        for account in accounts:
            usage = account.get("usage", {})
            state = usage.get("state") if isinstance(usage, dict) else None
            key = state if state in counts else "other"
            counts[key] += 1
        return counts

    def _render_status_human(self, payload: dict[str, Any]) -> str:
        """Render status sweep payload as human-readable text."""
        state = payload.get("state")
        if state == "no_active_account":
            return "Status: No active Claude account"
        if state == "no_managed_accounts":
            return "Status: No managed Claude accounts"

        lines = ["Status sweep:"]
        original_num = payload.get("original_active_account_number")
        original_display = payload.get("original_active_display") or "unknown"
        restored_num = payload.get("restored_active_account_number")
        restored_display = payload.get("restored_active_display") or "unknown"
        counts = payload.get("counts", {})
        swept_at = self._format_timestamp(payload.get("swept_at"))

        if original_num:
            lines.append(f"  Original active: Account-{original_num} ({original_display})")
        else:
            lines.append(f"  Original active: unmanaged ({original_display})")
        if restored_num:
            lines.append(f"  Restored active: Account-{restored_num} ({restored_display})")
        else:
            lines.append(f"  Restored active: unmanaged ({restored_display})")
        lines.append(f"  Total managed accounts: {len(payload.get('accounts', []))}")
        lines.append(
            "  Summary: "
            f"{counts.get('ready', 0)} ready, "
            f"{counts.get('limited', 0)} limited, "
            f"{counts.get('auth', 0)} auth, "
            f"{counts.get('other', 0)} other"
        )
        if swept_at:
            lines.append(f"  Swept at: {swept_at}")
        lines.append("")

        for account in payload.get("accounts", []):
            lines.append(
                f"Account-{account['account_number']}: {account['display']}"
            )
            if account.get("account_uuid"):
                lines.append(f"  Account UUID: {account['account_uuid']}")
            if account.get("organization_uuid"):
                lines.append(f"  Organization UUID: {account['organization_uuid']}")
            if account.get("organization_name"):
                lines.append(f"  Organization: {account['organization_name']}")
            if account.get("billing_type"):
                lines.append(f"  Billing type: {account['billing_type']}")

            token = account.get("token", {})
            if isinstance(token, dict):
                if token.get("subscription_type"):
                    lines.append(
                        f"  Subscription type: {token['subscription_type']}"
                    )
                scopes = token.get("scopes") or []
                if scopes:
                    lines.append(f"  Token scopes: {', '.join(scopes)}")
                if token.get("expires_at"):
                    expires_at = self._format_timestamp(token["expires_at"])
                    lines.append(f"  Token expires: {expires_at or token['expires_at']}")

            usage = account.get("usage", {})
            if isinstance(usage, dict):
                lines.append(f"  Usage state: {usage.get('state', 'unknown')}")
                if usage.get("detail") and usage.get("detail") != "OK":
                    lines.append(f"  Usage detail: {usage['detail']}")
                if usage.get("rate_limited_until"):
                    reusable_in = self._format_reusable_in(usage["rate_limited_until"])
                    reusable_at = self._format_timestamp(usage["rate_limited_until"])
                    if reusable_in and reusable_at:
                        lines.append(
                            f"  Reusable in: {reusable_in} (at {reusable_at})"
                        )
                    elif reusable_at:
                        lines.append(f"  Reusable at: {reusable_at}")
                windows_line = self._render_windows_line(usage.get("windows", {}))
                if windows_line:
                    lines.append(f"  Windows: {windows_line}")
            lines.append("")

        return "\n".join(lines).rstrip()

    def _collect_status_snapshot(self) -> dict[str, Any]:
        """Collect a full multi-account usage snapshot and restore the original state."""
        data = self._get_sequence_data()
        if not data or not data.get("accounts"):
            current_identity = self._get_current_account_identity()
            current_display = self._format_account_display(current_identity)
            current_email = current_identity.get("email") if current_identity else None
            if not current_email:
                return {
                    "state": "no_active_account",
                    "message": "No active Claude account",
                    "swept_at": get_timestamp(),
                    "original_active_account_number": None,
                    "original_active_display": None,
                    "restored_active_account_number": None,
                    "restored_active_display": None,
                    "counts": {"ready": 0, "limited": 0, "auth": 0, "other": 0},
                    "accounts": [],
                }
            return {
                "state": "no_managed_accounts",
                "message": "No managed Claude accounts",
                "swept_at": get_timestamp(),
                "original_active_account_number": None,
                "original_active_display": current_display,
                "restored_active_account_number": None,
                "restored_active_display": current_display,
                "counts": {"ready": 0, "limited": 0, "auth": 0, "other": 0},
                "accounts": [],
            }

        with FileLock(self.lock_file):
            snapshot = self._capture_active_state_snapshot(data)
            accounts_payload: list[dict[str, Any]] = []
            sequence = [
                str(account_num)
                for account_num in data.get("sequence", [])
                if str(account_num) in data.get("accounts", {})
            ]
            remaining_accounts = sorted(
                (
                    str(account_num)
                    for account_num in data.get("accounts", {})
                    if str(account_num) not in sequence
                ),
                key=lambda account_num: int(account_num)
                if account_num.isdigit()
                else account_num,
            )
            sequence.extend(remaining_accounts)
            self._logger.debug(
                "Collecting status snapshot sequence=%s original_active=%s",
                sequence,
                snapshot.get("managed_account_number"),
            )

            try:
                for account_num in sequence:
                    account_record = data["accounts"][account_num]
                    display = self._format_account_display(account_record)
                    self._logger.info(
                        "Sweeping usage for Account-%s %s", account_num, display
                    )
                    try:
                        merged_identity = self._activate_account_from_backup(
                            account_num, data
                        )
                        active_credentials = self._read_credentials()
                        if active_credentials is None:
                            raise CredentialReadError(
                                "Failed to read active credentials after activation"
                            )

                        account_email = str(account_record.get("email", ""))
                        is_original = account_num == snapshot.get(
                            "managed_account_number"
                        )
                        if not is_original:
                            active_credentials, _ = self._ensure_fresh_credentials(
                                account_num, account_email, active_credentials
                            )

                        token_metadata = self._credential_metadata(active_credentials)
                        access_token = self._extract_access_token(active_credentials)
                        if not access_token:
                            usage = {
                                "state": "auth",
                                "detail": "missing access token",
                                "rate_limited_until": None,
                                "windows": {},
                                "source": "status_sweep",
                                "raw": {},
                            }
                        else:
                            usage = self._fetch_usage_for_token(access_token)
                            if usage.get("state") == "auth" and not is_original:
                                refreshed = self._refresh_oauth_credentials(
                                    active_credentials
                                )
                                if refreshed:
                                    self._write_account_credentials(
                                        account_num, account_email, refreshed
                                    )
                                    retry_token = self._extract_access_token(refreshed)
                                    if retry_token:
                                        usage = self._fetch_usage_for_token(retry_token)
                                        token_metadata = self._credential_metadata(
                                            refreshed
                                        )

                        accounts_payload.append(
                            {
                                "account_number": account_num,
                                "display": self._format_account_display(
                                    merged_identity or account_record
                                ),
                                "email": (
                                    (merged_identity or {}).get("email")
                                    or account_record.get("email")
                                ),
                                "account_uuid": (
                                    (merged_identity or {}).get("uuid")
                                    or account_record.get("uuid")
                                ),
                                "organization_uuid": (
                                    (merged_identity or {}).get("organizationUuid")
                                    or account_record.get("organizationUuid")
                                ),
                                "display_name": (
                                    (merged_identity or {}).get("displayName")
                                    or account_record.get("displayName")
                                ),
                                "organization_name": (
                                    (merged_identity or {}).get("organizationName")
                                    or account_record.get("organizationName")
                                ),
                                "billing_type": (
                                    (merged_identity or {}).get("billingType")
                                    or account_record.get("billingType")
                                ),
                                "token": token_metadata,
                                "usage": usage,
                                "was_original_active": account_num
                                == snapshot.get("managed_account_number"),
                            }
                        )
                    except Exception as exc:
                        self._logger.error(
                            "Status sweep failed for Account-%s: %s",
                            account_num,
                            exc,
                        )
                        identity = self._extract_identity(account_record) or {}
                        accounts_payload.append(
                            {
                                "account_number": account_num,
                                "display": self._format_account_display(identity),
                                "email": identity.get("email"),
                                "account_uuid": identity.get("uuid"),
                                "organization_uuid": identity.get("organizationUuid"),
                                "display_name": identity.get("displayName"),
                                "organization_name": identity.get("organizationName"),
                                "billing_type": identity.get("billingType"),
                                "token": {
                                    "access_token_present": False,
                                    "access_token_suffix": None,
                                    "subscription_type": None,
                                    "scopes": [],
                                    "expires_at": None,
                                },
                                "usage": {
                                    "state": "switch_error",
                                    "detail": str(exc),
                                    "rate_limited_until": None,
                                    "windows": {},
                                    "source": "status_sweep",
                                    "raw": {},
                                },
                                "was_original_active": account_num
                                == snapshot.get("managed_account_number"),
                            }
                        )
            finally:
                try:
                    restored_account_number, restored_display = (
                        self._restore_active_state_snapshot(snapshot, data)
                    )
                except Exception as exc:
                    self._logger.error(
                        "Status sweep failed to restore original state: %s", exc
                    )
                    raise SwitchError(
                        f"Status sweep failed to restore the original account: {exc}"
                    ) from exc

            counts = self._build_status_counts(accounts_payload)
            return {
                "state": "ok",
                "swept_at": get_timestamp(),
                "original_active_account_number": snapshot.get("managed_account_number"),
                "original_active_display": snapshot.get("display"),
                "restored_active_account_number": restored_account_number,
                "restored_active_display": restored_display,
                "counts": counts,
                "accounts": accounts_payload,
            }

    def _read_credentials(self) -> str | None:
        """Read credentials from Claude Code's storage.

        Claude Code stores credentials in:
        - macOS: Keychain with service "Claude Code-credentials"
        - Linux/WSL/Windows: File at ~/.claude/.credentials.json

        Returns:
            Credentials string if found, empty string if not found, None on error.
        """
        self._logger.debug(
            "Reading active Claude credentials for platform=%s", self.platform.name
        )
        if self.platform == Platform.MACOS:
            try:
                result = subprocess.run(
                    [
                        "security",
                        "find-generic-password",
                        "-s",
                        "Claude Code-credentials",
                        "-w",
                    ],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                credentials = result.stdout.strip()
                self._logger.debug(
                    "Read active macOS keychain credentials summary: %s",
                    self._describe_secret_payload(credentials),
                )
                return credentials
            except subprocess.CalledProcessError as e:
                if e.returncode == 44:  # Item not found
                    self._logger.debug("Active macOS keychain credentials were not found")
                    return ""
                self._logger.error(f"Failed to read credentials: {e}")
                return None
            except Exception as e:
                self._logger.error(f"Unexpected error reading credentials: {e}")
                return None
        else:  # Linux/WSL/Windows - credentials stored in file
            cred_file = self.home / ".claude" / ".credentials.json"
            if cred_file.exists():
                try:
                    credentials = cred_file.read_text()
                    self._logger.debug(
                        "Read active credentials file %s summary: %s",
                        cred_file,
                        self._describe_secret_payload(credentials),
                    )
                    return credentials
                except Exception as e:
                    self._logger.error(f"Failed to read credentials file: {e}")
                    return None
            self._logger.debug("Active credentials file not found: %s", cred_file)
            return ""

    def _write_credentials(self, credentials: str) -> None:
        """Write credentials to Claude Code's storage.

        Claude Code stores credentials in:
        - macOS: Keychain with service "Claude Code-credentials"
        - Linux/WSL/Windows: File at ~/.claude/.credentials.json

        Raises:
            CredentialWriteError: If writing credentials fails.
        """
        self._logger.debug(
            "Writing active Claude credentials summary: %s",
            self._describe_secret_payload(credentials),
        )
        if self.platform == Platform.MACOS:
            result = subprocess.run(
                [
                    "security",
                    "add-generic-password",
                    "-U",
                    "-s",
                    "Claude Code-credentials",
                    "-a",
                    os.environ.get("USER", "user"),
                    "-w",
                    credentials,
                ],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                raise CredentialWriteError(
                    f"Failed to write credentials: {result.stderr}"
                )
            self._logger.debug("Wrote active credentials to macOS keychain")
        else:  # Linux/WSL/Windows - credentials stored in file
            cred_dir = self.home / ".claude"
            cred_dir.mkdir(parents=True, exist_ok=True)
            cred_file = cred_dir / ".credentials.json"
            try:
                cred_file.write_text(credentials)
                if sys.platform != "win32":
                    os.chmod(cred_file, 0o600)
                self._logger.debug(
                    "Wrote active credentials file %s summary: %s",
                    cred_file,
                    self._describe_secret_payload(credentials),
                )
            except Exception as e:
                raise CredentialWriteError(f"Failed to write credentials: {e}")

    def _read_account_credentials(self, account_num: str, email: str) -> str:
        """Read account credentials from backup.

        On Linux/WSL: Uses file-based storage to avoid keyring backend issues.
        On macOS/Windows: Uses system keyring.
        """
        if self.platform in (Platform.LINUX, Platform.WSL):
            cred_file = self.credentials_dir / f".creds-{account_num}-{email}.enc"
            if cred_file.exists():
                try:
                    encoded = cred_file.read_text()
                    credentials = base64.b64decode(encoded).decode("utf-8")
                    self._logger.debug(
                        "Read backup credentials file %s for Account-%s summary: %s",
                        cred_file,
                        account_num,
                        self._describe_secret_payload(credentials),
                    )
                    return credentials
                except Exception as e:
                    self._logger.warning(f"Failed to read credentials file: {e}")
                    return ""
            self._logger.debug(
                "Backup credentials file missing for Account-%s path=%s",
                account_num,
                cred_file,
            )
            return ""
        else:
            # Use keyring for macOS/Windows
            username = f"account-{account_num}-{email}"
            try:
                creds = keyring.get_password(KEYRING_SERVICE, username)
                self._logger.debug(
                    "Read backup keyring credentials username=%s summary: %s",
                    username,
                    self._describe_secret_payload(creds),
                )
                return creds if creds else ""
            except Exception as e:
                self._logger.warning(f"Failed to read credentials from keyring: {e}")
                return ""

    def _write_account_credentials(
        self, account_num: str, email: str, credentials: str
    ) -> None:
        """Write account credentials to backup.

        On Linux/WSL: Uses file-based storage to avoid keyring backend issues.
        On macOS/Windows: Uses system keyring.
        """
        self._logger.debug(
            "Writing backup credentials for Account-%s email=%s summary: %s",
            account_num,
            email,
            self._describe_secret_payload(credentials),
        )
        if self.platform in (Platform.LINUX, Platform.WSL):
            cred_file = self.credentials_dir / f".creds-{account_num}-{email}.enc"
            try:
                encoded = base64.b64encode(credentials.encode("utf-8")).decode("utf-8")
                cred_file.write_text(encoded)
                os.chmod(cred_file, 0o600)
                self._logger.debug(
                    "Wrote backup credentials file %s for Account-%s",
                    cred_file,
                    account_num,
                )
            except Exception as e:
                self._logger.warning(f"Failed to write credentials file: {e}")
        else:
            # Use keyring for macOS/Windows
            username = f"account-{account_num}-{email}"
            try:
                keyring.set_password(KEYRING_SERVICE, username, credentials)
                self._logger.debug(
                    "Wrote backup keyring credentials username=%s", username
                )
            except Exception as e:
                self._logger.warning(f"Failed to write credentials to keyring: {e}")

    def _delete_account_credentials(self, account_num: str, email: str) -> None:
        """Delete account credentials from backup.

        On Linux/WSL: Deletes file-based credential storage.
        On macOS/Windows: Removes from system keyring.
        """
        if self.platform in (Platform.LINUX, Platform.WSL):
            cred_file = self.credentials_dir / f".creds-{account_num}-{email}.enc"
            try:
                if cred_file.exists():
                    cred_file.unlink()
                    self._logger.debug(
                        "Deleted backup credentials file %s for Account-%s",
                        cred_file,
                        account_num,
                    )
            except Exception as e:
                self._logger.warning(f"Failed to delete credentials file: {e}")
        else:
            # Use keyring for macOS/Windows
            username = f"account-{account_num}-{email}"
            try:
                keyring.delete_password(KEYRING_SERVICE, username)
                self._logger.debug(
                    "Deleted backup keyring credentials username=%s", username
                )
            except keyring.errors.PasswordDeleteError:
                pass  # Credential doesn't exist, that's fine
            except Exception as e:
                self._logger.warning(f"Failed to delete credentials from keyring: {e}")

    def _read_account_config(self, account_num: str, email: str) -> str:
        """Read account config from backup."""
        config_file = self.configs_dir / f".claude-config-{account_num}-{email}.json"
        if config_file.exists():
            content = config_file.read_text()
            self._logger.debug(
                "Read backup config %s for Account-%s (%s bytes)",
                config_file,
                account_num,
                len(content),
            )
            return content
        self._logger.debug(
            "Backup config missing for Account-%s path=%s", account_num, config_file
        )
        return ""

    def _write_account_config(
        self, account_num: str, email: str, config: str
    ) -> None:
        """Write account config to backup."""
        config_file = self.configs_dir / f".claude-config-{account_num}-{email}.json"
        config_file.write_text(config)
        if sys.platform != "win32":
            os.chmod(config_file, 0o600)
        self._logger.debug(
            "Wrote backup config %s for Account-%s (%s bytes)",
            config_file,
            account_num,
            len(config),
        )

    def _init_sequence_file(self) -> None:
        """Initialize sequence.json if it doesn't exist."""
        if not self.sequence_file.exists():
            init_data = {
                "activeAccountNumber": None,
                "lastUpdated": get_timestamp(),
                "sequence": [],
                "accounts": {},
            }
            self._write_json(self.sequence_file, init_data)

    def _get_sequence_data(self) -> dict | None:
        """Get sequence data."""
        data = self._read_json(self.sequence_file)
        if not data:
            self._logger.debug("No sequence data available at %s", self.sequence_file)
            return None

        self._logger.debug(
            "Loaded sequence data activeAccountNumber=%s sequence=%s managed_accounts=%s",
            data.get("activeAccountNumber"),
            data.get("sequence", []),
            sorted(data.get("accounts", {}).keys()),
        )
        self._backfill_sequence_metadata(data)
        return data

    def _get_next_account_number(self) -> int:
        """Get next account number."""
        data = self._get_sequence_data()
        if not data or not data.get("accounts"):
            return 1

        account_nums = [int(k) for k in data["accounts"].keys()]
        return max(account_nums, default=0) + 1

    def _get_current_account(self) -> str | None:
        """Get current account email from .claude.json.

        Returns:
            Email address if found, None otherwise.
        """
        identity = self._get_current_account_identity()
        email = identity.get("email") if identity else None
        self._logger.debug("Current account email lookup returned: %s", email)
        return email or None

    def _account_exists(self, target: str | dict[str, str]) -> bool:
        """Check if account exists by identity or email."""
        data = self._get_sequence_data()
        if not data:
            return False

        if isinstance(target, str):
            normalized_target = self._normalize_email(target)
            exists = any(
                self._normalize_email(str(account.get("email") or ""))
                == normalized_target
                for account in data.get("accounts", {}).values()
            )
            self._logger.debug(
                "Account existence email lookup target=%s exists=%s",
                target,
                exists,
            )
            return exists

        target_identity = target
        exists = self._find_matching_account_number(target_identity, data) is not None
        self._logger.debug(
            "Account existence target=%s exists=%s",
            self._format_identity(self._extract_identity(target_identity)),
            exists,
        )
        return exists

    def _resolve_account_identifier(self, identifier: str) -> str | None:
        """Resolve account identifier (number or email) to account number."""
        if identifier.isdigit():
            self._logger.debug(
                "Resolved numeric account identifier directly: %s", identifier
            )
            return identifier

        data = self._get_sequence_data()
        if not data:
            return None

        matches = []
        for num, account in data.get("accounts", {}).items():
            if self._normalize_email(account.get("email", "")) == self._normalize_email(
                identifier
            ):
                matches.append(num)

        self._logger.debug(
            "Resolved email identifier=%s matches=%s", identifier, matches
        )
        if len(matches) > 1:
            raise ValidationError(
                f"Email '{identifier}' matches multiple managed accounts. "
                "Use the account number instead."
            )
        if matches:
            return matches[0]
        return None

    def add_account(self) -> None:
        """Add current account to managed accounts."""
        self._setup_directories()
        self._init_sequence_file()

        current_identity = self._get_current_account_identity()
        current_email = current_identity.get("email") if current_identity else None
        if not current_email:
            raise ConfigError("No active Claude account found. Please log in first.")

        data = self._get_sequence_data()
        existing_account = self._find_matching_account_number(current_identity, data)
        if existing_account:
            self._logger.info(
                "Refreshing credentials for Account-%s: %s",
                existing_account,
                self._format_identity(current_identity),
            )
            current_creds = self._read_credentials()
            if current_creds is None:
                raise CredentialReadError("Failed to read credentials for current account")
            if not current_creds:
                raise CredentialReadError("No credentials found for current account")

            config_path = self._get_claude_config_path()
            try:
                current_config = config_path.read_text()
            except FileNotFoundError:
                raise ConfigError("Claude config file not found")
            except PermissionError:
                raise ConfigError("Permission denied reading Claude config")

            self._write_account_credentials(existing_account, current_email, current_creds)
            self._write_account_config(existing_account, current_email, current_config)

            data["activeAccountNumber"] = int(existing_account)
            data["lastUpdated"] = get_timestamp()
            self._write_json(self.sequence_file, data)

            self._logger.info(f"Updated credentials for Account-{existing_account}: {current_email}")
            print(
                f"Updated credentials for Account-{existing_account}: "
                f"{self._format_account_display(current_identity)}"
            )
            return

        account_num = str(self._get_next_account_number())
        self._logger.debug(
            "Adding new managed account Account-%s identity=%s",
            account_num,
            self._format_identity(current_identity),
        )

        # Backup current credentials and config
        current_creds = self._read_credentials()
        if current_creds is None:
            raise CredentialReadError("Failed to read credentials for current account")
        if not current_creds:
            raise CredentialReadError("No credentials found for current account")
        self._logger.debug(
            "Current credential snapshot summary before add: %s",
            self._describe_secret_payload(current_creds),
        )

        config_path = self._get_claude_config_path()
        try:
            current_config = config_path.read_text()
        except FileNotFoundError:
            raise ConfigError("Claude config file not found")
        except PermissionError:
            raise ConfigError("Permission denied reading Claude config")
        self._logger.debug(
            "Read current config for add from %s (%s bytes)", config_path, len(current_config)
        )

        # Store backups
        self._write_account_credentials(account_num, current_email, current_creds)
        self._write_account_config(account_num, current_email, current_config)

        # Update sequence.json
        data = self._get_sequence_data()
        account_record = {
            "email": current_email,
            "added": get_timestamp(),
        }
        if current_identity:
            if current_identity.get("uuid"):
                account_record["uuid"] = current_identity["uuid"]
            if current_identity.get("organizationUuid"):
                account_record["organizationUuid"] = current_identity["organizationUuid"]
            if current_identity.get("displayName"):
                account_record["displayName"] = current_identity["displayName"]
            if current_identity.get("organizationName"):
                account_record["organizationName"] = current_identity["organizationName"]
            if current_identity.get("billingType"):
                account_record["billingType"] = current_identity["billingType"]

        self._logger.debug(
            "Persisting managed account record Account-%s: %s",
            account_num,
            account_record,
        )
        data["accounts"][account_num] = account_record
        data["sequence"].append(int(account_num))
        data["activeAccountNumber"] = int(account_num)
        data["lastUpdated"] = get_timestamp()

        self._write_json(self.sequence_file, data)
        self._logger.info(f"Added account {account_num}: {current_email}")
        print(
            f"Added Account {account_num}: "
            f"{self._format_account_display(account_record)}"
        )

    def remove_account(self, identifier: str) -> None:
        """Remove account from managed accounts."""
        if not self.sequence_file.exists():
            raise ConfigError("No accounts are managed yet")

        # Resolve identifier
        if not identifier.isdigit():
            if not self._validate_email(identifier):
                raise ValidationError(f"Invalid email format: {identifier}")

        account_num = self._resolve_account_identifier(identifier)
        if not account_num:
            raise AccountNotFoundError(
                f"No account found with identifier: {identifier}"
            )

        data = self._get_sequence_data()
        account_info = data.get("accounts", {}).get(account_num)

        if not account_info:
            raise AccountNotFoundError(f"Account-{account_num} does not exist")

        email = account_info.get("email")
        active_account = data.get("activeAccountNumber")
        self._logger.debug(
            "Removing Account-%s info=%s activeAccountNumber=%s",
            account_num,
            account_info,
            active_account,
        )

        if str(active_account) == account_num:
            print(f"Warning: Account-{account_num} ({email}) is currently active")

        confirm = input(
            f"Are you sure you want to permanently remove "
            f"Account-{account_num} ({email})? [y/N] "
        )
        if confirm.lower() != "y":
            print("Cancelled")
            return

        # Remove backup files
        self._delete_account_credentials(account_num, email)
        config_file = self.configs_dir / f".claude-config-{account_num}-{email}.json"
        if config_file.exists():
            config_file.unlink()

        # Update sequence.json
        del data["accounts"][account_num]
        data["sequence"] = [n for n in data["sequence"] if n != int(account_num)]
        data["lastUpdated"] = get_timestamp()

        self._write_json(self.sequence_file, data)
        self._logger.info(f"Removed account {account_num}: {email}")
        print(f"Account-{account_num} ({email}) has been removed")

    def list_accounts(self) -> None:
        """List all managed accounts."""
        if not self.sequence_file.exists():
            print("No accounts are managed yet.")
            self._first_run_setup()
            return

        data = self._get_sequence_data()
        current_identity = self._get_current_account_identity()

        active_num = self._find_matching_account_number(current_identity, data)
        if active_num is not None:
            self._sync_active_account_number(data, active_num)

        self._logger.debug(
            "Listing managed accounts active_num=%s current_identity=%s",
            active_num,
            self._format_identity(current_identity),
        )

        print("Accounts:")
        for num in data.get("sequence", []):
            account = data.get("accounts", {}).get(str(num), {})
            display = self._format_account_display(account)
            self._logger.debug(
                "List entry Account-%s record=%s", num, self._format_identity(self._extract_identity(account))
            )
            if str(num) == active_num:
                print(f"  {num}: {display} (active)")
            else:
                print(f"  {num}: {display}")

    def status(self, as_json: bool = False) -> None:
        """Display current account status or a full managed-account usage sweep."""
        payload = self._collect_status_snapshot()
        if as_json:
            json.dump(payload, sys.stdout, indent=2, sort_keys=True)
            sys.stdout.write("\n")
            return

        print(self._render_status_human(payload))

    def _first_run_setup(self) -> None:
        """First-run setup workflow."""
        current_identity = self._get_current_account_identity()
        current_email = current_identity.get("email") if current_identity else None

        if not current_email:
            print("No active Claude account found. Please log in first.")
            return

        response = input(
            f"No managed accounts found. Add current account "
            f"({current_email}) to managed list? [Y/n] "
        )
        if response.lower() == "n":
            print("Setup cancelled. You can run 'cswap --add-account' later.")
            return

        self.add_account()

    def switch(self) -> None:
        """Switch to next account in sequence."""
        if not self.sequence_file.exists():
            raise ConfigError("No accounts are managed yet")

        current_identity = self._get_current_account_identity()
        current_email = current_identity.get("email") if current_identity else None
        if not current_email:
            raise ConfigError("No active Claude account found")

        data = self._get_sequence_data()
        current_account = self._find_matching_account_number(current_identity, data)

        # Check if current account is managed
        if current_account is None:
            self._logger.info(
                "Current identity is not managed before switch: %s",
                self._format_identity(current_identity),
            )
            print(f"Notice: Active account '{current_email}' was not managed.")
            self.add_account()
            data = self._get_sequence_data()
            account_num = data.get("activeAccountNumber")
            print(f"It has been automatically added as Account-{account_num}.")
            print("Please run the switch command again to switch to the next account.")
            return

        self._sync_active_account_number(data, current_account)
        data = self._get_sequence_data()
        sequence = data.get("sequence", [])

        if len(sequence) < 2:
            print("Only one account is managed. Add more accounts to switch between.")
            return

        # Find current index and build ordered candidate list
        try:
            current_index = sequence.index(int(current_account))
        except ValueError:
            current_index = 0
            self._logger.warning(
                "Current Account-%s was missing from sequence ordering=%s",
                current_account,
                sequence,
            )

        candidates = [
            str(sequence[(current_index + offset) % len(sequence)])
            for offset in range(1, len(sequence))
        ]

        self._logger.debug(
            "Switch rotation current_account=%s candidates=%s sequence=%s",
            current_account,
            candidates,
            sequence,
        )

        # Probe candidates for one with available capacity
        best_account, reason = self._find_ready_account(candidates, data)

        if best_account is None:
            print("All accounts are at capacity. No switch performed.")
            print("Run 'cswap --status' to see reset times.")
            return

        if reason:
            print(reason)

        self._perform_switch(best_account)

    def switch_to(self, identifier: str) -> None:
        """Switch to specific account."""
        if not self.sequence_file.exists():
            raise ConfigError("No accounts are managed yet")

        # Resolve identifier
        if not identifier.isdigit():
            if not self._validate_email(identifier):
                raise ValidationError(f"Invalid email format: {identifier}")

        data = self._get_sequence_data()
        current_identity = self._get_current_account_identity()
        current_email = current_identity.get("email") if current_identity else None
        if not current_email:
            raise ConfigError("No active Claude account found")

        current_account = self._find_matching_account_number(current_identity, data)
        if current_account is None:
            self._logger.info(
                "Current identity is not managed before switch-to %s: %s",
                identifier,
                self._format_identity(current_identity),
            )
            print(f"Notice: Active account '{current_email}' was not managed.")
            self.add_account()
            data = self._get_sequence_data()
            account_num = data.get("activeAccountNumber")
            print(f"It has been automatically added as Account-{account_num}.")
            print(
                "Please run the switch command again to switch to the requested account."
            )
            return

        self._sync_active_account_number(data, current_account)
        target_account = self._resolve_account_identifier(identifier)
        if not target_account:
            raise AccountNotFoundError(
                f"No account found with identifier: {identifier}"
            )

        data = self._get_sequence_data()
        if target_account not in data.get("accounts", {}):
            raise AccountNotFoundError(f"Account-{target_account} does not exist")

        self._logger.debug(
            "Switch-to requested identifier=%s resolved_target=%s current_account=%s",
            identifier,
            target_account,
            current_account,
        )

        # Probe the target account's capacity before switching
        target_record = data.get("accounts", {}).get(target_account, {})
        target_email = str(target_record.get("email", ""))
        display = self._format_account_display(target_record)
        try:
            backup_credentials = self._read_account_credentials(
                target_account, target_email
            )
            if backup_credentials:
                backup_credentials, access_token = self._ensure_fresh_credentials(
                    target_account, target_email, backup_credentials
                )
                if access_token:
                    usage = self._fetch_usage_for_token(access_token)
                    state = usage.get("state")
                    windows = usage.get("windows", {})
                    windows_line = self._render_windows_line(windows)

                    if state == "auth":
                        refreshed = self._refresh_oauth_credentials(backup_credentials)
                        if refreshed:
                            self._write_account_credentials(
                                target_account, target_email, refreshed
                            )
                            retry_token = self._extract_access_token(refreshed)
                            if retry_token:
                                usage = self._fetch_usage_for_token(retry_token)
                                state = usage.get("state")
                                windows = usage.get("windows", {})
                                windows_line = self._render_windows_line(windows)

                    if state == "ready":
                        status_detail = "ready"
                        if windows_line:
                            status_detail += f" ({windows_line})"
                        print(f"Account-{target_account}: {display} — {status_detail}")
                    elif state == "limited":
                        reset_at = usage.get("rate_limited_until")
                        reset_dt = (
                            self._parse_status_datetime(reset_at) if reset_at else None
                        )
                        status_detail = "limited"
                        if reset_dt is not None:
                            remaining = int(
                                (
                                    reset_dt - datetime.now(timezone.utc)
                                ).total_seconds()
                            )
                            eta = self._format_duration(max(remaining, 0))
                            status_detail += f" (resets in {eta})"
                        if windows_line:
                            status_detail += f" — {windows_line}"
                        print(
                            f"Warning: Account-{target_account}: {display} — {status_detail}"
                        )
                    else:
                        detail = usage.get("detail", state)
                        print(f"Account-{target_account}: {display} — {detail}")
        except Exception as exc:
            self._logger.warning(
                "Capacity probe failed for Account-%s: %s", target_account, exc
            )

        self._perform_switch(target_account)

    def _perform_switch(self, target_account: str) -> None:
        """Perform the actual account switch with transaction support."""
        with FileLock(self.lock_file):
            data = self._get_sequence_data()
            target_email = data["accounts"][target_account]["email"]
            target_identity = self._extract_identity(data["accounts"][target_account])
            current_identity = self._get_current_account_identity()
            current_email = current_identity.get("email") if current_identity else None

            if not current_email:
                raise SwitchError("No current account to switch from")

            current_account = self._find_matching_account_number(current_identity, data)
            if current_account is None:
                raise SwitchError(
                    "Active Claude account is not managed. Add it with --add-account "
                    "before switching."
                )
            self._sync_active_account_number(data, current_account)

            self._logger.debug(
                "Beginning switch current_account=%s current_identity=%s "
                "target_account=%s target_identity=%s",
                current_account,
                self._format_identity(current_identity),
                target_account,
                self._format_identity(target_identity),
            )

            if current_account == target_account:
                self._logger.info(
                    "Requested switch target Account-%s is already active",
                    target_account,
                )
                print(f"Account-{target_account} ({target_email}) is already active")
                self.list_accounts()
                print()
                print("Please restart Claude Code to use the current authentication.")
                print()
                return

            config_path = self._get_claude_config_path()

            # Create transaction for rollback capability
            try:
                original_creds = self._read_credentials()
                if original_creds is None:
                    raise CredentialReadError("Failed to read current credentials")
                original_config = config_path.read_text()
                self._logger.debug(
                    "Captured pre-switch current credentials summary: %s",
                    self._describe_secret_payload(original_creds),
                )
                self._logger.debug(
                    "Captured pre-switch current config from %s (%s bytes)",
                    config_path,
                    len(original_config),
                )
            except FileNotFoundError:
                raise ConfigError("Claude config file not found")
            except PermissionError:
                raise ConfigError("Permission denied reading Claude config")

            transaction = SwitchTransaction(
                original_credentials=original_creds,
                original_config=original_config,
                original_account_num=current_account,
                original_email=current_email,
                config_path=config_path,
            )

            try:
                # Step 1: Backup current account
                self._write_account_credentials(
                    current_account, current_email, original_creds
                )
                self._write_account_config(
                    current_account, current_email, original_config
                )
                self._logger.info(f"Backed up account {current_account}")

                # Step 2: Retrieve target account
                target_creds = self._read_account_credentials(
                    target_account, target_email
                )
                target_config = self._read_account_config(target_account, target_email)
                self._logger.debug(
                    "Loaded target credential snapshot summary: %s",
                    self._describe_secret_payload(target_creds),
                )
                self._logger.debug(
                    "Loaded target config snapshot size=%s bytes",
                    len(target_config),
                )

                if not target_creds or not target_config:
                    raise SwitchError(
                        f"Missing backup data for Account-{target_account}"
                    )

                # Step 3: Activate target account - credentials
                self._write_credentials(target_creds)
                transaction.record_step("credentials_written")
                self._logger.info("Wrote target credentials")

                # Step 4: Update config with target oauthAccount
                target_config_data = json.loads(target_config)
                oauth_section = target_config_data.get("oauthAccount")

                if not oauth_section:
                    raise SwitchError("Invalid oauthAccount in backup")
                self._logger.debug(
                    "Applying target oauth identity for Account-%s: %s",
                    target_account,
                    self._format_identity(self._extract_identity(oauth_section)),
                )

                current_config_data = self._read_json(config_path)
                current_config_data["oauthAccount"] = oauth_section

                self._write_json(config_path, current_config_data)
                transaction.record_step("config_written")
                self._logger.info("Updated config file")

                # Step 5: Update sequence state
                data["activeAccountNumber"] = int(target_account)
                data["lastUpdated"] = get_timestamp()
                self._write_json(self.sequence_file, data)
                transaction.record_step("sequence_updated")

                self._logger.info(
                    f"Switched from account {current_account} to {target_account}"
                )
                print(f"Switched to Account-{target_account} ({target_email})")
                self.list_accounts()
                print()
                print("Please restart Claude Code to use the new authentication.")
                print()

            except Exception as e:
                self._logger.error(f"Switch failed: {e}, attempting rollback")
                if transaction.completed_steps:
                    success = transaction.rollback(self)
                    if success:
                        self._logger.info("Rollback successful")
                        raise SwitchError(
                            f"Switch failed and was rolled back: {e}"
                        )
                    else:
                        self._logger.error("Rollback failed!")
                        raise SwitchError(
                            f"Switch failed and rollback also failed: {e}. "
                            f"Manual recovery may be needed."
                        )
                raise

    def purge(self) -> None:
        """Remove all traces of claude-swap from the system.

        This removes:
        - All stored account credentials (files on Linux, keyring on macOS/Windows)
        - The ~/.claude-swap-backup directory and all its contents
        """
        print("This will remove ALL claude-swap data from your system:")
        print(f"  - Backup directory: {self.backup_dir}")
        if self.platform in (Platform.LINUX, Platform.WSL):
            print("  - All stored account credential files")
        else:
            print("  - All stored account credentials from the system keyring")
        print()
        print("Note: This does NOT affect your current Claude Code login.")
        print()

        confirm = input("Are you sure you want to purge all data? [y/N] ")
        if confirm.lower() != "y":
            print("Cancelled")
            return

        removed_items = []

        # Remove credentials
        data = self._get_sequence_data()
        if data:
            for account_num, account_info in data.get("accounts", {}).items():
                email = account_info.get("email", "")
                if self.platform in (Platform.LINUX, Platform.WSL):
                    # Remove credential files on Linux
                    cred_file = (
                        self.credentials_dir / f".creds-{account_num}-{email}.enc"
                    )
                    try:
                        if cred_file.exists():
                            cred_file.unlink()
                            removed_items.append(f"Credential file: {cred_file.name}")
                    except Exception:
                        pass  # Ignore errors during purge
                else:
                    # Remove from keyring on macOS/Windows
                    username = f"account-{account_num}-{email}"
                    try:
                        keyring.delete_password(KEYRING_SERVICE, username)
                        removed_items.append(f"Credential: {username}")
                    except keyring.errors.PasswordDeleteError:
                        pass  # Credential doesn't exist
                    except Exception:
                        pass  # Ignore other errors during purge

        # Remove backup directory
        if self.backup_dir.exists():
            # Close log handlers before deleting (required on Windows)
            for handler in self._logger.handlers[:]:
                handler.close()
                self._logger.removeHandler(handler)

            shutil.rmtree(self.backup_dir)
            removed_items.append(f"Directory: {self.backup_dir}")

        if removed_items:
            print("\nRemoved:")
            for item in removed_items:
                print(f"  - {item}")
        else:
            print("\nNo claude-swap data found to remove.")

        print("\nPurge complete.")
