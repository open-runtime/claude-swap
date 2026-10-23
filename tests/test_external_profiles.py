"""An externally owned Claude profile must never acquire a second refresh consumer."""
import json
from unittest.mock import patch

import pytest

from claude_swap.exceptions import ConfigError, CredentialReadError
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.session import SessionManager
from claude_swap import oauth

EMAIL = "person@example.com"
ORG = "org-personal"


def credential(token="access-original", expires=99999999999999):
    return json.dumps({"claudeAiOauth": {"accessToken": token,
        "refreshToken": "one-time-refresh", "expiresAt": expires}})


def profile(path, email=EMAIL, org=ORG, creds=None, default=False):
    path.mkdir(parents=True, exist_ok=True)
    config = path.parent / ".claude.json" if default else path / ".claude.json"
    config.write_text(json.dumps({"oauthAccount": {"emailAddress": email,
        "organizationUuid": org, "accountUuid": "user-1"}}))
    (path / ".credentials.json").write_text(creds or credential())
    return str(path)


def switcher(temp_home, bound=True, default=False):
    s = ClaudeAccountSwitcher()
    s._setup_directories()
    s._init_sequence_file()
    path = profile(temp_home / (".claude" if default else "external"), default=default)
    source = "default" if default else path
    record = {"email": EMAIL, "organizationUuid": ORG, "uuid": "user-1",
        "alias": "keep-me", "disabled": True}
    if bound:
        record["sourceProfile"] = source
    data = {"accounts": {"1": record}, "sequence": [1], "activeAccountNumber": 9}
    s._write_json(s.sequence_file, data)
    return s, path, source


def test_binding_preserves_metadata_and_does_not_copy_credentials(temp_home):
    s, path, source = switcher(temp_home, bound=False)
    with patch("claude_swap.oauth.fetch_oauth_profile", return_value={
        "uuid": "user-1", "email": EMAIL, "organizationUuid": ORG}), \
         patch.object(s, "_write_account_credentials") as write:
        s.add_account(source_profile=source)
    data = s._get_sequence_data()
    assert data["activeAccountNumber"] == 9
    assert data["accounts"]["1"]["disabled"] is True
    assert data["accounts"]["1"]["alias"] == "keep-me"
    assert data["accounts"]["1"]["sourceProfile"] == source
    write.assert_not_called()


def test_new_binding_starts_disabled_and_preserves_active_pointer(temp_home):
    s, path, _ = switcher(temp_home, bound=False)
    new = profile(temp_home / "new", email="new@example.com", org="org-new")
    with patch("claude_swap.oauth.fetch_oauth_profile", return_value={
        "uuid": "user-1", "email": "new@example.com", "organizationUuid": "org-new"}):
        s.add_account(source_profile=new)
    data = s._get_sequence_data()
    assert data["accounts"]["2"]["disabled"] is True
    assert data["activeAccountNumber"] == 9


def test_live_source_replaces_stale_backup_on_each_collection(temp_home):
    s, path, _ = switcher(temp_home)
    with patch.object(s._store, "_read_account_credentials", return_value=credential("stale")), \
         patch.object(s, "_read_active_credentials") as active:
        first = s._build_accounts_info()[0]
        from pathlib import Path
        (Path(path) / ".credentials.json").write_text(credential("rotated"))
        second = s._build_accounts_info()[0]
    assert oauth.extract_access_token(first[5]) == "access-original"
    assert oauth.extract_access_token(second[5]) == "rotated"
    active.assert_not_called()


@pytest.mark.parametrize("failure,expected", [
    ("expired", "token expired"), ("missing", "no credentials"),
    ("unreadable", "keychain unavailable"), ("wrong-email", "foreign credential"),
    ("wrong-org", "foreign credential"),
])
def test_source_failure_never_uses_backup_or_refreshes(temp_home, failure, expected):
    s, path, _ = switcher(temp_home)
    from pathlib import Path
    if failure == "expired":
        (Path(path) / ".credentials.json").write_text(credential(expires=1))
    elif failure == "missing":
        (Path(path) / ".credentials.json").unlink()
    elif failure.startswith("wrong"):
        profile(Path(path), email="wrong@example.com" if failure == "wrong-email" else EMAIL,
                org="wrong-org" if failure == "wrong-org" else ORG)
    reader = patch("claude_swap.session.read_config_dir_credentials",
                   side_effect=CredentialReadError("locked")) if failure == "unreadable" else patch.object(s, "_live_session_pids", return_value=[])
    with reader, patch.object(s, "consume_backup_grant") as consume, \
         patch("claude_swap.oauth.try_refresh_oauth_credentials") as refresh, \
         patch.object(s, "_write_account_credentials") as write, \
         patch.object(s._store, "_read_account_credentials", return_value=credential("stale")) as backup:
        info = s._build_accounts_info()[0]
        assert s._static_usage_sentinel(info) == expected
        assert s._fetch_account_usage(info).sentinel == expected
    consume.assert_not_called()
    refresh.assert_not_called()
    write.assert_not_called()
    backup.assert_not_called()


def test_bound_even_active_fetch_is_read_only(temp_home):
    s, _, _ = switcher(temp_home, default=True)
    with patch("claude_swap.oauth.try_fetch_usage_for_account",
               return_value=oauth.UsageOutcome(usage={"five_hour": {"utilization": 1}})) as fetch, \
         patch.object(s, "_fetch_active_usage") as active:
        info = s._build_accounts_info()[0]
        assert info[4] is True
        s._fetch_account_usage(info)
    assert fetch.call_args.kwargs == {"is_active": True}
    active.assert_not_called()


def test_default_source_uses_unsuffixed_keychain_and_home_identity(temp_home):
    s, path, _ = switcher(temp_home, default=True)
    with patch("claude_swap.session.read_config_dir_credentials", return_value=credential()) as read:
        info = s._build_accounts_info()[0]
    assert info[1] == EMAIL
    assert read.call_args.kwargs["strict_keychain"] is True
    assert read.call_args.kwargs["keychain_service"] == "Claude Code-credentials"
    assert read.call_args.args == (path,)


@pytest.mark.parametrize("action", ["switch", "run", "setup", "refresh", "write"])
def test_bound_account_refuses_duplicate_consumers(temp_home, action):
    s, _, _ = switcher(temp_home)
    with patch("shutil.which", return_value="/fake/claude"), \
         patch("claude_swap.oauth.try_refresh_oauth_credentials") as refresh:
        with pytest.raises(ConfigError, match="source profile"):
            if action == "switch":
                s.switch_to("1", force=True)
            elif action == "run":
                SessionManager(s).run("1", [])
            elif action == "setup":
                SessionManager(s).setup_session("1", share=False)
            elif action == "refresh":
                s.consume_backup_grant("1", EMAIL, credential())
            else:
                s._write_account_credentials("1", EMAIL, credential())
    refresh.assert_not_called()
    assert not s._account_is_switchable("1")


def test_registration_rejects_unverified_token_identity(temp_home):
    s, _, source = switcher(temp_home, bound=False)
    original = s.sequence_file.read_bytes()
    with patch("claude_swap.oauth.fetch_oauth_profile", return_value={
        "uuid": "user-1", "email": EMAIL, "organizationUuid": "wrong-org"}), \
         pytest.raises(ConfigError, match="identity"):
        s.add_account(source_profile=source)
    assert s.sequence_file.read_bytes() == original


def test_registration_refuses_api_key_override(temp_home, monkeypatch):
    s, _, source = switcher(temp_home, bound=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-api-key")
    with pytest.raises(ConfigError, match="API"):
        s.add_account(source_profile=source)


def test_http_401_does_not_refresh_or_persist_source(temp_home):
    s, _, _ = switcher(temp_home)
    with patch("claude_swap.oauth.try_fetch_usage_for_account",
               return_value=oauth.UsageOutcome(None, error="http-401")) as fetch, \
         patch("claude_swap.oauth.try_refresh_oauth_credentials") as refresh, \
         patch.object(s, "_write_credentials") as write:
        result = s._fetch_account_usage(s._build_accounts_info()[0])
    assert result.sentinel == "token expired"
    assert fetch.call_args.kwargs == {"is_active": True}
    refresh.assert_not_called()
    write.assert_not_called()


def test_switch_away_from_bound_default_refused_before_writes(temp_home):
    s, _, _ = switcher(temp_home, default=True)
    data = s._get_sequence_data()
    data["accounts"]["2"] = {"email": "other@example.com", "organizationUuid": ""}
    data["sequence"].append(2)
    s._write_json(s.sequence_file, data)
    with patch.object(s, "_write_credentials") as write, pytest.raises(ConfigError, match="source profile"):
        s._perform_switch("2", force_activate=True)
    write.assert_not_called()


def test_registration_between_consume_precheck_and_lock_never_posts(temp_home):
    s, _, source = switcher(temp_home, bound=False)
    original = s._consume_backup_grant_locked
    def interleave(*args):
        with patch("claude_swap.oauth.fetch_oauth_profile", return_value={
            "uuid": "user-1", "email": EMAIL, "organizationUuid": ORG}):
            # Simulates the registration winning before the consumer's lock:
            # install its completed metadata without acquiring the already-held lock.
            data = s._get_sequence_data()
            data["accounts"]["1"]["sourceProfile"] = source
            s._write_json(s.sequence_file, data)
        return original(*args)
    with patch.object(s, "_consume_backup_grant_locked", side_effect=interleave), \
         patch("claude_swap.oauth.try_refresh_oauth_credentials", return_value=oauth.RefreshOutcome(None, "transient")) as refresh:
        result = s.consume_backup_grant("1", EMAIL, credential())
    assert result.error == "source-profile"
    refresh.assert_not_called()


def test_direct_active_usage_cannot_refresh_bound_default(temp_home):
    s, _, _ = switcher(temp_home, default=True)
    with patch("claude_swap.oauth.try_fetch_usage_for_account",
               return_value=oauth.UsageOutcome(None, error="http-401")), \
         patch("claude_swap.oauth.try_refresh_oauth_credentials") as refresh, \
         patch.object(s, "_write_credentials") as write:
        result = s._fetch_active_usage("1", EMAIL, credential(expires=1), ORG)
    assert result.sentinel == "token expired"
    refresh.assert_not_called()
    write.assert_not_called()


@pytest.mark.parametrize("action", ["add-token", "move", "swap"])
def test_bound_replacement_refused_before_any_store_change(temp_home, action):
    s, _, _ = switcher(temp_home)
    data = s._get_sequence_data()
    data["accounts"]["2"] = {"email": "other@example.com", "organizationUuid": ""}
    data["sequence"].append(2)
    s._write_json(s.sequence_file, data)
    original = s.sequence_file.read_bytes()
    with pytest.raises(ConfigError, match="source profile"):
        if action == "add-token":
            s.add_account_from_token("sk-ant-oat01-fake", slot=1, email="different@example.com", assume_yes=True)
        elif action == "move":
            s.move_account("1", "3")
        else:
            s.swap_accounts("1", "2")
    assert s.sequence_file.read_bytes() == original


def test_named_raw_path_and_credentials_ignore_ambient_redirect(temp_home, monkeypatch):
    s, path, _ = switcher(temp_home)
    data = s._get_sequence_data()
    data["accounts"]["1"]["sourceProfile"] = path + "/"
    s._write_json(s.sequence_file, data)
    monkeypatch.setenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", "/wrong-store")
    with patch("claude_swap.session.read_config_dir_credentials", return_value=credential()) as read:
        s._build_accounts_info()
    assert read.call_args.args == (path + "/",)
    assert read.call_args.kwargs == {"strict_keychain": True, "keychain_service": None}


def test_cached_status_uses_fresh_bound_source_not_expired_ambient(temp_home):
    s, _, _ = switcher(temp_home)
    from claude_swap.credentials import ActiveCredentials
    with patch.object(s, "_read_active_credentials", return_value=ActiveCredentials(credential("ambient", expires=1), False)), \
         patch.object(s, "_collect_usage_entries", return_value={"1": None}) as collect:
        s._active_account_usage("1", EMAIL, ORG)
    assert oauth.extract_access_token(collect.call_args.args[0][0][5]) == "access-original"


def test_bound_default_store_is_protected_even_after_logout(temp_home):
    s, _, _ = switcher(temp_home, default=True)
    (temp_home / ".claude.json").unlink()
    with patch.object(s._store, "_write_credentials") as write, pytest.raises(ConfigError, match="source profile"):
        s._write_credentials(credential("replacement"))
    write.assert_not_called()


def test_current_source_state_overrides_cached_healthy_usage(temp_home):
    s, path, _ = switcher(temp_home)
    from pathlib import Path
    from claude_swap.usage_store import UsageEntry
    info = s._build_accounts_info()
    (Path(path) / ".credentials.json").unlink()
    cached = UsageEntry(last_good={"five_hour": {"pct": 12}})
    with patch.object(s._usage_store, "entries", return_value={"1": cached}), \
         patch.object(s._usage_store, "reserve", return_value={}), \
         patch.object(s, "_run_usage_fetches") as fetch:
        entry = s._collect_usage_entries(info)["1"]
    assert entry.decision_value() == "no credentials"
    fetch.assert_not_called()
