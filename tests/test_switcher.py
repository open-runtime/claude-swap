"""Tests for the ClaudeAccountSwitcher class."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from claude_swap.exceptions import (
    AccountNotFoundError,
    ConfigError,
    CredentialReadError,
    ValidationError,
)
from claude_swap.models import Platform
from claude_swap.switcher import ClaudeAccountSwitcher


def configure_status_sweep_mocks(
    switcher: ClaudeAccountSwitcher,
    current_account: str,
    credentials_by_account: dict[str, str],
) -> dict[str, str]:
    """Configure in-memory credential + usage mocks for status sweep tests."""
    active_credentials = {"value": credentials_by_account[current_account]}
    switcher._read_credentials = MagicMock(
        side_effect=lambda: active_credentials["value"]
    )
    switcher._write_credentials = MagicMock(
        side_effect=lambda value: active_credentials.__setitem__("value", value)
    )
    switcher._read_account_credentials = MagicMock(
        side_effect=lambda num, email: credentials_by_account[num]
    )
    switcher._fetch_usage_for_token = MagicMock(
        side_effect=lambda token: {
            "state": "ready",
            "detail": "OK",
            "rate_limited_until": None,
            "windows": {
                "five_hour": {
                    "utilization": 10.0,
                    "resets_at": "2026-03-18T00:00:00+00:00",
                }
            },
            "source": "usage_api",
            "raw": {
                "five_hour": {
                    "utilization": 10.0,
                    "resets_at": "2026-03-18T00:00:00+00:00",
                }
            },
        }
    )
    return active_credentials


class TestEmailValidation:
    """Test email validation."""

    def test_valid_emails(self, temp_home: Path):
        """Test that valid emails pass validation."""
        switcher = ClaudeAccountSwitcher()
        valid_emails = [
            "user@example.com",
            "user.name@example.co.uk",
            "user+tag@example.org",
            "user123@test.io",
        ]
        for email in valid_emails:
            assert switcher._validate_email(email), f"Expected {email} to be valid"

    def test_invalid_emails(self, temp_home: Path):
        """Test that invalid emails fail validation."""
        switcher = ClaudeAccountSwitcher()
        invalid_emails = [
            "not-an-email",
            "@example.com",
            "user@",
            "user@.com",
            "",
            "user@com",
        ]
        for email in invalid_emails:
            assert not switcher._validate_email(email), f"Expected {email} to be invalid"


class TestPlatformDetection:
    """Test platform detection."""

    @patch("platform.system", return_value="Darwin")
    def test_macos_detection(self, mock_system, temp_home: Path):
        """Test macOS platform detection."""
        assert Platform.detect() == Platform.MACOS

    @patch("platform.system", return_value="Linux")
    @patch.dict(os.environ, {}, clear=False)
    def test_linux_detection(self, mock_system, temp_home: Path):
        """Test Linux platform detection."""
        # Ensure WSL_DISTRO_NAME is not set
        env = os.environ.copy()
        env.pop("WSL_DISTRO_NAME", None)
        with patch.dict(os.environ, env, clear=True):
            assert Platform.detect() == Platform.LINUX

    @patch("platform.system", return_value="Linux")
    @patch.dict(os.environ, {"WSL_DISTRO_NAME": "Ubuntu"})
    def test_wsl_detection(self, mock_system, temp_home: Path):
        """Test WSL platform detection."""
        assert Platform.detect() == Platform.WSL

    @patch("platform.system", return_value="Windows")
    def test_windows_detection(self, mock_system, temp_home: Path):
        """Test Windows platform detection."""
        assert Platform.detect() == Platform.WINDOWS

    @patch("platform.system", return_value="FreeBSD")
    def test_unknown_platform(self, mock_system, temp_home: Path):
        """Test unknown platform detection."""
        assert Platform.detect() == Platform.UNKNOWN


class TestJsonOperations:
    """Test JSON read/write operations."""

    def test_write_and_read_json(self, temp_home: Path):
        """Test writing and reading JSON files."""
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()

        test_path = switcher.backup_dir / "test.json"
        test_data = {"key": "value", "number": 42, "nested": {"a": 1}}

        switcher._write_json(test_path, test_data)
        result = switcher._read_json(test_path)

        assert result == test_data

    def test_read_nonexistent_json(self, temp_home: Path):
        """Test reading non-existent JSON file returns None."""
        switcher = ClaudeAccountSwitcher()
        result = switcher._read_json(Path("/nonexistent/path.json"))
        assert result is None

    def test_read_invalid_json(self, temp_home: Path):
        """Test reading invalid JSON file returns None."""
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()

        test_path = switcher.backup_dir / "invalid.json"
        test_path.write_text("not valid json {{{")

        result = switcher._read_json(test_path)
        assert result is None

    @pytest.mark.skipif(sys.platform == "win32", reason="File permissions work differently on Windows")
    def test_json_file_permissions(self, temp_home: Path):
        """Test that JSON files are written with correct permissions."""
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()

        test_path = switcher.backup_dir / "secure.json"
        switcher._write_json(test_path, {"secret": "data"})

        # Check file permissions (0o600 = owner read/write only)
        stat = test_path.stat()
        assert stat.st_mode & 0o777 == 0o600


class TestGetCurrentAccount:
    """Test getting current account."""

    def test_no_config_file(self, temp_home: Path):
        """Test when no config file exists."""
        switcher = ClaudeAccountSwitcher()
        assert switcher._get_current_account() is None

    def test_with_valid_config(self, temp_home: Path, mock_claude_config: Path):
        """Test reading email from valid config."""
        switcher = ClaudeAccountSwitcher()
        assert switcher._get_current_account() == "test@example.com"

    def test_config_without_oauth(self, temp_home: Path):
        """Test config file without oauthAccount."""
        config_path = temp_home / ".claude" / ".claude.json"
        config_path.write_text(json.dumps({"other": "data"}))

        switcher = ClaudeAccountSwitcher()
        assert switcher._get_current_account() is None

    def test_config_with_empty_email(self, temp_home: Path):
        """Test config with empty email address."""
        config_path = temp_home / ".claude" / ".claude.json"
        config_path.write_text(
            json.dumps({"oauthAccount": {"emailAddress": "", "accountUuid": "uuid"}})
        )

        switcher = ClaudeAccountSwitcher()
        assert switcher._get_current_account() is None


class TestAccountExists:
    """Test account existence checking."""

    def test_account_exists(self, temp_home: Path, sample_sequence_data: dict):
        """Test checking if account exists."""
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        assert switcher._account_exists("account1@example.com") is True
        assert switcher._account_exists("nonexistent@example.com") is False

    def test_no_sequence_file(self, temp_home: Path):
        """Test account exists when no sequence file."""
        switcher = ClaudeAccountSwitcher()
        assert switcher._account_exists("any@example.com") is False


class TestResolveAccountIdentifier:
    """Test resolving account identifiers."""

    def test_resolve_by_number(self, temp_home: Path, sample_sequence_data: dict):
        """Test resolving account by number."""
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        assert switcher._resolve_account_identifier("1") == "1"
        assert switcher._resolve_account_identifier("2") == "2"

    def test_resolve_by_email(self, temp_home: Path, sample_sequence_data: dict):
        """Test resolving account by email."""
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        assert switcher._resolve_account_identifier("account1@example.com") == "1"
        assert switcher._resolve_account_identifier("account2@example.com") == "2"

    def test_resolve_nonexistent(self, temp_home: Path, sample_sequence_data: dict):
        """Test resolving non-existent account."""
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        assert switcher._resolve_account_identifier("nonexistent@example.com") is None
        assert switcher._resolve_account_identifier("999") == "999"  # Numbers pass through

    def test_resolve_by_email_requires_number_when_same_email_is_ambiguous(
        self, temp_home: Path
    ):
        """Test same-email accounts require account number selection."""
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(
            switcher.sequence_file,
            {
                "activeAccountNumber": 1,
                "lastUpdated": "2024-01-01T00:00:00Z",
                "sequence": [1, 2],
                "accounts": {
                    "1": {
                        "email": "shared@example.com",
                        "uuid": "uuid-a",
                        "organizationUuid": "org-a",
                        "added": "2024-01-01T00:00:00Z",
                    },
                    "2": {
                        "email": "shared@example.com",
                        "uuid": "uuid-b",
                        "organizationUuid": "org-b",
                        "added": "2024-01-02T00:00:00Z",
                    },
                },
            },
        )

        with pytest.raises(ValidationError, match="Use the account number"):
            switcher._resolve_account_identifier("shared@example.com")


class TestDirectorySetup:
    """Test directory setup."""

    def test_creates_directories(self, temp_home: Path):
        """Test that setup creates required directories."""
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()

        assert switcher.backup_dir.exists()
        assert switcher.configs_dir.exists()
        assert switcher.credentials_dir.exists()

    @pytest.mark.skipif(sys.platform == "win32", reason="File permissions work differently on Windows")
    def test_directory_permissions(self, temp_home: Path):
        """Test that directories have correct permissions."""
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()

        for directory in [switcher.backup_dir, switcher.configs_dir, switcher.credentials_dir]:
            stat = directory.stat()
            assert stat.st_mode & 0o777 == 0o700


class TestGetNextAccountNumber:
    """Test getting next account number."""

    def test_first_account(self, temp_home: Path):
        """Test first account number is 1."""
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._init_sequence_file()

        assert switcher._get_next_account_number() == 1

    def test_with_existing_accounts(self, temp_home: Path, sample_sequence_data: dict):
        """Test next number after existing accounts."""
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        assert switcher._get_next_account_number() == 3


class TestAddAccount:
    """Test adding managed accounts."""

    def test_add_account_allows_same_email_when_org_uuid_differs(
        self, temp_home: Path
    ):
        """Test same-email accounts can coexist when Claude identity differs."""
        config_path = temp_home / ".claude" / ".claude.json"
        config_path.write_text(
            json.dumps(
                {
                    "oauthAccount": {
                        "emailAddress": "shared@example.com",
                        "accountUuid": "uuid-b",
                        "organizationUuid": "org-b",
                    }
                }
            )
        )

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(
            switcher.sequence_file,
            {
                "activeAccountNumber": 1,
                "lastUpdated": "2024-01-01T00:00:00Z",
                "sequence": [1],
                "accounts": {
                    "1": {
                        "email": "shared@example.com",
                        "uuid": "uuid-a",
                        "organizationUuid": "org-a",
                        "added": "2024-01-01T00:00:00Z",
                    }
                },
            },
        )

        switcher._read_credentials = MagicMock(return_value='{"accessToken":"test"}')
        switcher._write_account_credentials = MagicMock()
        switcher._write_account_config = MagicMock()

        switcher.add_account()

        data = switcher._get_sequence_data()
        assert list(data["accounts"].keys()) == ["1", "2"]
        assert data["accounts"]["2"]["email"] == "shared@example.com"
        assert data["accounts"]["2"]["uuid"] == "uuid-b"
        assert data["accounts"]["2"]["organizationUuid"] == "org-b"

    def test_add_account_prints_organization_name_label(
        self, temp_home: Path, capsys: pytest.CaptureFixture[str]
    ):
        """Test added account output includes a differentiating label."""
        config_path = temp_home / ".claude" / ".claude.json"
        config_path.write_text(
            json.dumps(
                {
                    "oauthAccount": {
                        "emailAddress": "shared@example.com",
                        "accountUuid": "uuid-b",
                        "organizationUuid": "org-b",
                        "organizationName": "Personal",
                        "displayName": "Tsavo",
                    }
                }
            )
        )

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(
            switcher.sequence_file,
            {
                "activeAccountNumber": 1,
                "lastUpdated": "2024-01-01T00:00:00Z",
                "sequence": [1],
                "accounts": {
                    "1": {
                        "email": "shared@example.com",
                        "uuid": "uuid-a",
                        "organizationUuid": "org-a",
                        "organizationName": "Work",
                        "added": "2024-01-01T00:00:00Z",
                    }
                },
            },
        )

        switcher._read_credentials = MagicMock(return_value='{"accessToken":"test"}')
        switcher._write_account_credentials = MagicMock()
        switcher._write_account_config = MagicMock()

        switcher.add_account()
        captured = capsys.readouterr()

        assert "Added Account 2: shared@example.com [Personal]" in captured.out


class TestStatus:
    """Test status command."""

    def test_status_no_account(self, temp_home: Path):
        """Test status when no account is logged in."""
        switcher = ClaudeAccountSwitcher()
        # Should not raise, just print
        switcher.status()

    def test_status_unmanaged_account(
        self, temp_home: Path, mock_claude_config: Path
    ):
        """Test status with unmanaged account."""
        switcher = ClaudeAccountSwitcher()
        switcher.status()

    def test_status_managed_account(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        """Test status with managed account."""
        # Update sequence data to match mock config email
        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)
        (switcher.configs_dir / ".claude-config-1-test@example.com.json").write_text(
            json.dumps(
                {
                    "oauthAccount": {
                        "emailAddress": "test@example.com",
                        "accountUuid": "uuid-1",
                    }
                }
            )
        )
        (
            switcher.configs_dir / ".claude-config-2-account2@example.com.json"
        ).write_text(
            json.dumps(
                {
                    "oauthAccount": {
                        "emailAddress": "account2@example.com",
                        "accountUuid": "uuid-2",
                    }
                }
            )
        )
        configure_status_sweep_mocks(
            switcher,
            "1",
            {
                "1": json.dumps(
                    {
                        "claudeAiOauth": {
                            "accessToken": "token-1",
                            "subscriptionType": "pro",
                            "scopes": ["user:profile"],
                        }
                    }
                ),
                "2": json.dumps(
                    {
                        "claudeAiOauth": {
                            "accessToken": "token-2",
                            "subscriptionType": "pro",
                            "scopes": ["user:profile"],
                        }
                    }
                ),
            },
        )

        switcher.status()

    def test_status_matches_same_email_account_by_organization_uuid(
        self, temp_home: Path, capsys: pytest.CaptureFixture[str]
    ):
        """Test status chooses the right same-email managed account."""
        config_path = temp_home / ".claude" / ".claude.json"
        config_path.write_text(
            json.dumps(
                {
                    "oauthAccount": {
                        "emailAddress": "shared@example.com",
                        "accountUuid": "uuid-b",
                        "organizationUuid": "org-b",
                    }
                }
            )
        )

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(
            switcher.sequence_file,
            {
                "activeAccountNumber": 1,
                "lastUpdated": "2024-01-01T00:00:00Z",
                "sequence": [1, 2],
                "accounts": {
                    "1": {
                        "email": "shared@example.com",
                        "uuid": "uuid-a",
                        "organizationUuid": "org-a",
                        "added": "2024-01-01T00:00:00Z",
                    },
                    "2": {
                        "email": "shared@example.com",
                        "uuid": "uuid-b",
                        "organizationUuid": "org-b",
                        "added": "2024-01-02T00:00:00Z",
                    },
                },
            },
        )
        (switcher.configs_dir / ".claude-config-1-shared@example.com.json").write_text(
            json.dumps(
                {
                    "oauthAccount": {
                        "emailAddress": "shared@example.com",
                        "accountUuid": "uuid-a",
                        "organizationUuid": "org-a",
                    }
                }
            )
        )
        (switcher.configs_dir / ".claude-config-2-shared@example.com.json").write_text(
            json.dumps(
                {
                    "oauthAccount": {
                        "emailAddress": "shared@example.com",
                        "accountUuid": "uuid-b",
                        "organizationUuid": "org-b",
                    }
                }
            )
        )
        configure_status_sweep_mocks(
            switcher,
            "2",
            {
                "1": json.dumps(
                    {
                        "claudeAiOauth": {
                            "accessToken": "token-1",
                            "subscriptionType": "pro",
                            "scopes": ["user:profile"],
                        }
                    }
                ),
                "2": json.dumps(
                    {
                        "claudeAiOauth": {
                            "accessToken": "token-2",
                            "subscriptionType": "pro",
                            "scopes": ["user:profile"],
                        }
                    }
                ),
            },
        )

        switcher.status()
        captured = capsys.readouterr()

        assert "Original active: Account-2" in captured.out
        assert "Account-2: shared@example.com" in captured.out

    def test_status_displays_organization_name_label_for_same_email_account(
        self, temp_home: Path, capsys: pytest.CaptureFixture[str]
    ):
        """Test status output includes a differentiating organization label."""
        config_path = temp_home / ".claude" / ".claude.json"
        config_path.write_text(
            json.dumps(
                {
                    "oauthAccount": {
                        "emailAddress": "shared@example.com",
                        "accountUuid": "uuid-b",
                        "organizationUuid": "org-b",
                        "organizationName": "Personal",
                    }
                }
            )
        )

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(
            switcher.sequence_file,
            {
                "activeAccountNumber": 2,
                "lastUpdated": "2024-01-01T00:00:00Z",
                "sequence": [1, 2],
                "accounts": {
                    "1": {
                        "email": "shared@example.com",
                        "uuid": "uuid-a",
                        "organizationUuid": "org-a",
                        "organizationName": "Work",
                        "added": "2024-01-01T00:00:00Z",
                    },
                    "2": {
                        "email": "shared@example.com",
                        "uuid": "uuid-b",
                        "organizationUuid": "org-b",
                        "organizationName": "Personal",
                        "added": "2024-01-02T00:00:00Z",
                    },
                },
            },
        )
        (switcher.configs_dir / ".claude-config-1-shared@example.com.json").write_text(
            json.dumps(
                {
                    "oauthAccount": {
                        "emailAddress": "shared@example.com",
                        "accountUuid": "uuid-a",
                        "organizationUuid": "org-a",
                        "organizationName": "Work",
                    }
                }
            )
        )
        (switcher.configs_dir / ".claude-config-2-shared@example.com.json").write_text(
            json.dumps(
                {
                    "oauthAccount": {
                        "emailAddress": "shared@example.com",
                        "accountUuid": "uuid-b",
                        "organizationUuid": "org-b",
                        "organizationName": "Personal",
                    }
                }
            )
        )
        configure_status_sweep_mocks(
            switcher,
            "2",
            {
                "1": json.dumps(
                    {
                        "claudeAiOauth": {
                            "accessToken": "token-1",
                            "subscriptionType": "pro",
                            "scopes": ["user:profile"],
                        }
                    }
                ),
                "2": json.dumps(
                    {
                        "claudeAiOauth": {
                            "accessToken": "token-2",
                            "subscriptionType": "max",
                            "scopes": ["user:profile"],
                        }
                    }
                ),
            },
        )

        switcher.status()
        captured = capsys.readouterr()

        assert "Original active: Account-2 (shared@example.com [Personal])" in captured.out
        assert "Account-1: shared@example.com [Work]" in captured.out
        assert "Account-2: shared@example.com [Personal]" in captured.out

    def test_status_does_not_fallback_to_email_when_live_org_is_missing_in_backup(
        self, temp_home: Path, capsys: pytest.CaptureFixture[str]
    ):
        """Test missing backup org metadata does not falsely match by email."""
        config_path = temp_home / ".claude" / ".claude.json"
        config_path.write_text(
            json.dumps(
                {
                    "oauthAccount": {
                        "emailAddress": "shared@example.com",
                        "accountUuid": "uuid-b",
                        "organizationUuid": "org-b",
                    }
                }
            )
        )

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(
            switcher.sequence_file,
            {
                "activeAccountNumber": 1,
                "lastUpdated": "2024-01-01T00:00:00Z",
                "sequence": [1],
                "accounts": {
                    "1": {
                        "email": "shared@example.com",
                        "added": "2024-01-01T00:00:00Z",
                    }
                },
            },
        )
        (switcher.configs_dir / ".claude-config-1-shared@example.com.json").write_text(
            json.dumps(
                {
                    "oauthAccount": {
                        "emailAddress": "shared@example.com",
                    }
                }
            )
        )
        configure_status_sweep_mocks(
            switcher,
            "1",
            {
                "1": json.dumps(
                    {
                        "claudeAiOauth": {
                            "accessToken": "token-1",
                            "subscriptionType": "pro",
                            "scopes": ["user:profile"],
                        }
                    }
                )
            },
        )

        switcher.status()
        captured = capsys.readouterr()

        assert "Original active: unmanaged (shared@example.com" in captured.out
        assert "Account-1: shared@example.com" in captured.out


class TestListAccounts:
    """Test list output."""

    def test_list_accounts_displays_organization_name_labels_for_same_email_accounts(
        self, temp_home: Path, capsys: pytest.CaptureFixture[str]
    ):
        """Test list output differentiates same-email accounts."""
        config_path = temp_home / ".claude" / ".claude.json"
        config_path.write_text(
            json.dumps(
                {
                    "oauthAccount": {
                        "emailAddress": "shared@example.com",
                        "accountUuid": "uuid-b",
                        "organizationUuid": "org-b",
                        "organizationName": "Personal",
                    }
                }
            )
        )

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(
            switcher.sequence_file,
            {
                "activeAccountNumber": 2,
                "lastUpdated": "2024-01-01T00:00:00Z",
                "sequence": [1, 2],
                "accounts": {
                    "1": {
                        "email": "shared@example.com",
                        "uuid": "uuid-a",
                        "organizationUuid": "org-a",
                        "organizationName": "Work",
                        "added": "2024-01-01T00:00:00Z",
                    },
                    "2": {
                        "email": "shared@example.com",
                        "uuid": "uuid-b",
                        "organizationUuid": "org-b",
                        "organizationName": "Personal",
                        "added": "2024-01-02T00:00:00Z",
                    },
                },
            },
        )

        switcher.list_accounts()
        captured = capsys.readouterr()

        assert "1: shared@example.com [Work]" in captured.out
        assert "2: shared@example.com [Personal] (active)" in captured.out


class TestStatusSweep:
    """Test multi-account status sweep behavior."""

    def test_format_reusable_in_handles_naive_iso_timestamps(
        self, temp_home: Path
    ):
        """Test naive reset timestamps do not crash human formatting."""
        switcher = ClaudeAccountSwitcher()
        assert switcher._format_reusable_in("2099-03-18T02:00:00") is not None

    def test_normalize_usage_windows_supports_resets_at_and_resets_at_camel_case(
        self, temp_home: Path
    ):
        """Test usage windows accept both resets_at and resetsAt fields."""
        switcher = ClaudeAccountSwitcher()

        windows = switcher._normalize_usage_windows(
            {
                "five_hour": {
                    "utilization": 11.0,
                    "resetsAt": "2026-03-18T01:00:00+00:00",
                },
                "seven_day": {
                    "utilization": 22.0,
                    "resets_at": "2026-03-19T01:00:00+00:00",
                },
            }
        )

        assert windows["five_hour"]["resets_at"] == "2026-03-18T01:00:00+00:00"
        assert windows["seven_day"]["resets_at"] == "2026-03-19T01:00:00+00:00"

    def test_collect_status_snapshot_sweeps_all_accounts_and_restores_original_account(
        self, temp_home: Path
    ):
        """Test status snapshot sweeps each managed account and restores original."""
        config_path = temp_home / ".claude" / ".claude.json"
        config_path.write_text(
            json.dumps(
                {
                    "oauthAccount": {
                        "emailAddress": "second@example.com",
                        "accountUuid": "uuid-2",
                        "organizationUuid": "org-2",
                        "organizationName": "Second",
                    }
                }
            )
        )

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(
            switcher.sequence_file,
            {
                "activeAccountNumber": 2,
                "lastUpdated": "2024-01-01T00:00:00Z",
                "sequence": [1, 2],
                "accounts": {
                    "1": {
                        "email": "first@example.com",
                        "uuid": "uuid-1",
                        "organizationUuid": "org-1",
                        "organizationName": "First",
                        "added": "2024-01-01T00:00:00Z",
                    },
                    "2": {
                        "email": "second@example.com",
                        "uuid": "uuid-2",
                        "organizationUuid": "org-2",
                        "organizationName": "Second",
                        "added": "2024-01-02T00:00:00Z",
                    },
                },
            },
        )
        (switcher.configs_dir / ".claude-config-1-first@example.com.json").write_text(
            json.dumps(
                {
                    "oauthAccount": {
                        "emailAddress": "first@example.com",
                        "accountUuid": "uuid-1",
                        "organizationUuid": "org-1",
                        "organizationName": "First",
                    }
                }
            )
        )
        (switcher.configs_dir / ".claude-config-2-second@example.com.json").write_text(
            json.dumps(
                {
                    "oauthAccount": {
                        "emailAddress": "second@example.com",
                        "accountUuid": "uuid-2",
                        "organizationUuid": "org-2",
                        "organizationName": "Second",
                    }
                }
            )
        )

        credentials_by_account = {
            "1": json.dumps(
                {
                    "claudeAiOauth": {
                        "accessToken": "token-1",
                        "subscriptionType": "pro",
                        "scopes": ["user:profile"],
                    }
                }
            ),
            "2": json.dumps(
                {
                    "claudeAiOauth": {
                        "accessToken": "token-2",
                        "subscriptionType": "max",
                        "scopes": ["user:profile"],
                    }
                }
            ),
        }
        active_credentials = {"value": credentials_by_account["2"]}
        fetch_order: list[str] = []

        switcher._read_credentials = MagicMock(
            side_effect=lambda: active_credentials["value"]
        )
        switcher._write_credentials = MagicMock(
            side_effect=lambda value: active_credentials.__setitem__("value", value)
        )
        switcher._read_account_credentials = MagicMock(
            side_effect=lambda num, email: credentials_by_account[num]
        )
        switcher._fetch_usage_for_token = MagicMock(
            side_effect=lambda token: (
                fetch_order.append(token)
                or {
                    "state": "ready",
                    "detail": "OK",
                    "rate_limited_until": None,
                    "windows": {
                        "five_hour": {
                            "utilization": 10.0 if token == "token-1" else 20.0,
                            "resets_at": "2026-03-18T00:00:00+00:00",
                        }
                    },
                    "source": "usage_api",
                    "raw": {
                        "five_hour": {
                            "utilization": 10.0 if token == "token-1" else 20.0,
                            "resets_at": "2026-03-18T00:00:00+00:00",
                        }
                    },
                }
            )
        )

        payload = switcher._collect_status_snapshot()

        assert fetch_order == ["token-1", "token-2"]
        assert payload["original_active_account_number"] == "2"
        assert payload["restored_active_account_number"] == "2"
        assert [account["account_number"] for account in payload["accounts"]] == [
            "1",
            "2",
        ]
        assert payload["accounts"][0]["usage"]["windows"]["five_hour"]["utilization"] == 10.0
        assert payload["accounts"][1]["usage"]["windows"]["five_hour"]["utilization"] == 20.0
        assert active_credentials["value"] == credentials_by_account["2"]
        restored_config = json.loads(config_path.read_text())
        assert restored_config["oauthAccount"]["organizationUuid"] == "org-2"

    def test_collect_status_snapshot_includes_accounts_missing_from_sequence(
        self, temp_home: Path
    ):
        """Test accounts present in storage but missing from sequence are still swept."""
        config_path = temp_home / ".claude" / ".claude.json"
        config_path.write_text(
            json.dumps(
                {
                    "oauthAccount": {
                        "emailAddress": "second@example.com",
                        "accountUuid": "uuid-2",
                        "organizationUuid": "org-2",
                    }
                }
            )
        )

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(
            switcher.sequence_file,
            {
                "activeAccountNumber": 2,
                "lastUpdated": "2024-01-01T00:00:00Z",
                "sequence": [2],
                "accounts": {
                    "1": {
                        "email": "first@example.com",
                        "uuid": "uuid-1",
                        "organizationUuid": "org-1",
                        "added": "2024-01-01T00:00:00Z",
                    },
                    "2": {
                        "email": "second@example.com",
                        "uuid": "uuid-2",
                        "organizationUuid": "org-2",
                        "added": "2024-01-02T00:00:00Z",
                    },
                },
            },
        )
        (switcher.configs_dir / ".claude-config-1-first@example.com.json").write_text(
            json.dumps(
                {
                    "oauthAccount": {
                        "emailAddress": "first@example.com",
                        "accountUuid": "uuid-1",
                        "organizationUuid": "org-1",
                    }
                }
            )
        )
        (switcher.configs_dir / ".claude-config-2-second@example.com.json").write_text(
            json.dumps(
                {
                    "oauthAccount": {
                        "emailAddress": "second@example.com",
                        "accountUuid": "uuid-2",
                        "organizationUuid": "org-2",
                    }
                }
            )
        )

        configure_status_sweep_mocks(
            switcher,
            "2",
            {
                "1": json.dumps(
                    {
                        "claudeAiOauth": {
                            "accessToken": "token-1",
                            "subscriptionType": "pro",
                            "scopes": ["user:profile"],
                        }
                    }
                ),
                "2": json.dumps(
                    {
                        "claudeAiOauth": {
                            "accessToken": "token-2",
                            "subscriptionType": "max",
                            "scopes": ["user:profile"],
                        }
                    }
                ),
            },
        )

        payload = switcher._collect_status_snapshot()

        assert [account["account_number"] for account in payload["accounts"]] == [
            "2",
            "1",
        ]

    def test_collect_status_snapshot_restores_unmanaged_original_without_stale_active_account_number(
        self, temp_home: Path
    ):
        """Test restoring an unmanaged original account clears activeAccountNumber."""
        config_path = temp_home / ".claude" / ".claude.json"
        config_path.write_text(
            json.dumps(
                {
                    "oauthAccount": {
                        "emailAddress": "shared@example.com",
                        "accountUuid": "uuid-b",
                        "organizationUuid": "org-b",
                    }
                }
            )
        )

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(
            switcher.sequence_file,
            {
                "activeAccountNumber": 1,
                "lastUpdated": "2024-01-01T00:00:00Z",
                "sequence": [1],
                "accounts": {
                    "1": {
                        "email": "shared@example.com",
                        "added": "2024-01-01T00:00:00Z",
                    }
                },
            },
        )
        (switcher.configs_dir / ".claude-config-1-shared@example.com.json").write_text(
            json.dumps(
                {
                    "oauthAccount": {
                        "emailAddress": "shared@example.com",
                    }
                }
            )
        )

        original_credentials = json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "token-original",
                    "subscriptionType": "pro",
                    "scopes": ["user:profile"],
                }
            }
        )
        managed_credentials = json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "token-managed",
                    "subscriptionType": "pro",
                    "scopes": ["user:profile"],
                }
            }
        )
        active_credentials = {"value": original_credentials}

        switcher._read_credentials = MagicMock(
            side_effect=lambda: active_credentials["value"]
        )
        switcher._write_credentials = MagicMock(
            side_effect=lambda value: active_credentials.__setitem__("value", value)
        )
        switcher._read_account_credentials = MagicMock(
            side_effect=lambda num, email: managed_credentials
        )
        switcher._fetch_usage_for_token = MagicMock(
            return_value={
                "state": "ready",
                "detail": "OK",
                "rate_limited_until": None,
                "windows": {
                    "five_hour": {
                        "utilization": 5.0,
                        "resets_at": "2026-03-18T00:00:00+00:00",
                    }
                },
                "source": "usage_api",
                "raw": {},
            }
        )

        payload = switcher._collect_status_snapshot()
        stored_sequence = switcher._read_json(switcher.sequence_file)

        assert payload["original_active_account_number"] is None
        assert payload["restored_active_account_number"] is None
        assert stored_sequence["activeAccountNumber"] is None
        assert active_credentials["value"] == original_credentials
        restored_config = json.loads(config_path.read_text())
        assert restored_config["oauthAccount"]["organizationUuid"] == "org-b"

    def test_status_json_outputs_sweep_payload(
        self, temp_home: Path, capsys: pytest.CaptureFixture[str]
    ):
        """Test JSON mode prints the sweep payload."""
        switcher = ClaudeAccountSwitcher()
        switcher._collect_status_snapshot = MagicMock(
            return_value={
                "swept_at": "2026-03-17T18:00:00Z",
                "original_active_account_number": "2",
                "restored_active_account_number": "2",
                "accounts": [
                    {
                        "account_number": "2",
                        "display": "second@example.com [Second]",
                        "usage": {"state": "ready", "windows": {}},
                    }
                ],
            }
        )

        switcher.status(as_json=True)
        captured = capsys.readouterr()
        payload = json.loads(captured.out)

        assert payload["original_active_account_number"] == "2"
        assert payload["accounts"][0]["display"] == "second@example.com [Second]"

    def test_status_human_output_includes_usage_details_for_each_account(
        self, temp_home: Path, capsys: pytest.CaptureFixture[str]
    ):
        """Test human status output includes per-account usage detail."""
        switcher = ClaudeAccountSwitcher()
        switcher._collect_status_snapshot = MagicMock(
            return_value={
                "swept_at": "2026-03-17T18:00:00Z",
                "original_active_account_number": "3",
                "original_active_display": "tsavo@pieces.app [Tsavo Knott]",
                "restored_active_account_number": "3",
                "restored_active_display": "tsavo@pieces.app [Tsavo Knott]",
                "accounts": [
                    {
                        "account_number": "2",
                        "display": "tsavo@pieces.app [Pieces]",
                        "usage": {
                            "state": "ready",
                            "detail": "OK",
                            "windows": {
                                "five_hour": {
                                    "utilization": 12.0,
                                    "resets_at": "2026-03-18T00:00:00+00:00",
                                }
                            },
                        },
                    },
                    {
                        "account_number": "3",
                        "display": "tsavo@pieces.app [Tsavo Knott]",
                        "usage": {
                            "state": "limited",
                            "detail": "rate limit",
                            "rate_limited_until": "2026-03-18T02:00:00+00:00",
                            "windows": {
                                "seven_day": {
                                    "utilization": 100.0,
                                    "resets_at": "2026-03-18T02:00:00+00:00",
                                }
                            },
                        },
                    },
                ],
            }
        )

        switcher.status()
        captured = capsys.readouterr()

        assert "Original active: Account-3 (tsavo@pieces.app [Tsavo Knott])" in captured.out
        assert "Restored active: Account-3 (tsavo@pieces.app [Tsavo Knott])" in captured.out
        assert "Account-2: tsavo@pieces.app [Pieces]" in captured.out
        assert "Account-3: tsavo@pieces.app [Tsavo Knott]" in captured.out
        assert "Windows: 5h 12%" in captured.out
        assert "Usage state: limited" in captured.out

    def test_list_accounts_backfills_organization_names_from_backup_configs(
        self, temp_home: Path, capsys: pytest.CaptureFixture[str]
    ):
        """Test legacy managed accounts get readable labels from backup config."""
        config_path = temp_home / ".claude" / ".claude.json"
        config_path.write_text(
            json.dumps(
                {
                    "oauthAccount": {
                        "emailAddress": "shared@example.com",
                        "accountUuid": "uuid-b",
                        "organizationUuid": "org-b",
                        "organizationName": "Personal",
                    }
                }
            )
        )

        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(
            switcher.sequence_file,
            {
                "activeAccountNumber": 2,
                "lastUpdated": "2024-01-01T00:00:00Z",
                "sequence": [1, 2],
                "accounts": {
                    "1": {
                        "email": "shared@example.com",
                        "uuid": "uuid-a",
                        "organizationUuid": "org-a",
                        "added": "2024-01-01T00:00:00Z",
                    },
                    "2": {
                        "email": "shared@example.com",
                        "uuid": "uuid-b",
                        "organizationUuid": "org-b",
                        "added": "2024-01-02T00:00:00Z",
                    },
                },
            },
        )
        (switcher.configs_dir / ".claude-config-1-shared@example.com.json").write_text(
            json.dumps(
                {
                    "oauthAccount": {
                        "emailAddress": "shared@example.com",
                        "accountUuid": "uuid-a",
                        "organizationUuid": "org-a",
                        "organizationName": "Work",
                    }
                }
            )
        )
        (switcher.configs_dir / ".claude-config-2-shared@example.com.json").write_text(
            json.dumps(
                {
                    "oauthAccount": {
                        "emailAddress": "shared@example.com",
                        "accountUuid": "uuid-b",
                        "organizationUuid": "org-b",
                        "organizationName": "Personal",
                    }
                }
            )
        )

        switcher.list_accounts()
        captured = capsys.readouterr()

        assert "1: shared@example.com [Work]" in captured.out
        assert "2: shared@example.com [Personal] (active)" in captured.out
