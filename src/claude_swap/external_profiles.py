"""Read-only bindings to Claude profiles owned by another launcher.

A binding is a credential SOURCE, never a copied refresh-token family. Only
Claude launched against that exact profile may refresh it. The raw path is
preserved because Claude hashes the literal CLAUDE_CONFIG_DIR for Keychain.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from claude_swap import oauth
from claude_swap.credentials import CLAUDE_CODE_KEYCHAIN_SERVICE, looks_like_api_key
from claude_swap.exceptions import ConfigError, CredentialReadError
from claude_swap.json_output import (
    USAGE_FOREIGN_CREDENTIAL, USAGE_KEYCHAIN_UNAVAILABLE,
    USAGE_NO_CREDENTIALS, USAGE_TOKEN_EXPIRED,
)
from claude_swap.paths import get_default_claude_config_home, get_default_global_config_path


@dataclass(frozen=True)
class ProfileRead:
    credentials: str = ""
    identity: dict | None = None
    state: str | None = None


def profile_paths(source: str) -> tuple[str, Path, str | None]:
    if source == "default":
        return (str(get_default_claude_config_home()), get_default_global_config_path(),
                CLAUDE_CODE_KEYCHAIN_SERVICE)
    if not isinstance(source, str) or not Path(source).is_absolute():
        raise ConfigError("A source profile must be an exact absolute path or 'default'.")
    directory = Path(source)
    legacy = directory / ".config.json"
    return source, legacy if legacy.exists() else directory / ".claude.json", None


def read_profile(source: str, expected: dict | None = None) -> ProfileRead:
    """Read strictly from the selected profile; never refresh or fall back to a backup."""
    from claude_swap.session import read_config_dir_credentials

    try:
        directory, config_path, service = profile_paths(source)
        config = json.loads(config_path.read_text(encoding="utf-8"))
        identity = config.get("oauthAccount") if isinstance(config, dict) else None
        if not isinstance(identity, dict) or not identity.get("emailAddress"):
            return ProfileRead(state=USAGE_NO_CREDENTIALS)
        if config.get("primaryApiKey") or config.get("apiKeyHelper"):
            return ProfileRead(identity=identity, state=USAGE_FOREIGN_CREDENTIAL)
        if expected is not None and (
            identity.get("emailAddress") != expected.get("email")
            or (identity.get("organizationUuid") or "") != (expected.get("organizationUuid") or "")
            or (expected.get("uuid") and identity.get("accountUuid") != expected["uuid"])
        ):
            return ProfileRead(identity=identity, state=USAGE_FOREIGN_CREDENTIAL)
        # Explicit service choice ignores ambient CLAUDE_SECURESTORAGE_CONFIG_DIR.
        creds = read_config_dir_credentials(directory, strict_keychain=True, keychain_service=service)
        # /login can replace config and credential independently. Refuse a moving identity.
        if json.loads(config_path.read_text(encoding="utf-8")) != config:
            return ProfileRead(identity=identity, state=USAGE_FOREIGN_CREDENTIAL)
    except CredentialReadError:
        return ProfileRead(state=USAGE_KEYCHAIN_UNAVAILABLE)
    except (OSError, ValueError, ConfigError):
        return ProfileRead(state=USAGE_NO_CREDENTIALS)
    if not creds or looks_like_api_key(creds) or not oauth.extract_access_token(creds):
        return ProfileRead(identity=identity, state=USAGE_NO_CREDENTIALS)
    data = oauth.extract_oauth_data(creds) or {}
    state = USAGE_TOKEN_EXPIRED if oauth.is_oauth_token_expired(data.get("expiresAt")) else None
    return ProfileRead(creds, identity, state)


def verified_profile(source: str) -> ProfileRead:
    """Bind only a currently usable token whose server identity matches its profile."""
    profile_paths(source)  # validate the raw path before reading
    from claude_swap.session import AUTH_OVERRIDE_ENV_VARS

    if any(os.environ.get(key) for key in AUTH_OVERRIDE_ENV_VARS):
        raise ConfigError("Unset API/token authentication overrides before binding a source profile.")
    result = read_profile(source)
    if result.state:
        raise ConfigError(f"Cannot bind source profile {source}: {result.state}; sign in through its owner.")
    identity = result.identity or {}
    remote = oauth.fetch_oauth_profile(oauth.extract_access_token(result.credentials))
    if not remote or (
        remote.get("email") != identity.get("emailAddress")
        or (remote.get("organizationUuid") or "") != (identity.get("organizationUuid") or "")
        or (identity.get("accountUuid") and remote.get("uuid") != identity["accountUuid"])
    ):
        raise ConfigError(f"Cannot verify source profile identity for {source}; nothing changed.")
    after = read_profile(source)
    if after != result:
        raise ConfigError("The source profile changed during identity verification; retry binding.")
    return result
