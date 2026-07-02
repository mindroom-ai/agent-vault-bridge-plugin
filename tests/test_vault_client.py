"""Tests for the vault_client bootstrap methods."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_vault_bridge_test_import.vault_client import (
    BootstrapSettings,
    VaultSettings,
    mint_proxy_session_token,
)


def _token_file_settings(token_file: str, ttl: int = 3600) -> VaultSettings:
    return VaultSettings(
        session_ttl_seconds=ttl,
        bootstrap=BootstrapSettings(method="token_file", token_file=token_file),
    )


def test_token_file_returns_trimmed_token_with_refresh_expiry(tmp_path: Path) -> None:
    token_path = tmp_path / "proxy-token"
    token_path.write_text("  \nav_agent_abc123\n\n", encoding="utf-8")

    token = mint_proxy_session_token(_token_file_settings(str(token_path)), now=1000.0)

    assert token.value == "av_agent_abc123"
    assert token.expires_at == 1000.0 + 3600


def test_token_file_rereads_rotated_token(tmp_path: Path) -> None:
    token_path = tmp_path / "proxy-token"
    token_path.write_text("first-token\n", encoding="utf-8")
    settings = _token_file_settings(str(token_path))

    assert mint_proxy_session_token(settings).value == "first-token"
    token_path.write_text("rotated-token\n", encoding="utf-8")
    assert mint_proxy_session_token(settings).value == "rotated-token"


def test_token_file_empty_file_raises(tmp_path: Path) -> None:
    token_path = tmp_path / "proxy-token"
    token_path.write_text("", encoding="utf-8")

    with pytest.raises(RuntimeError, match="token file is empty"):
        mint_proxy_session_token(_token_file_settings(str(token_path)))


def test_token_file_missing_path_setting_raises() -> None:
    settings = VaultSettings(bootstrap=BootstrapSettings(method="token_file"))

    with pytest.raises(ValueError, match="token_file is required"):
        mint_proxy_session_token(settings)


def test_token_file_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(OSError, match=""):
        mint_proxy_session_token(_token_file_settings(str(tmp_path / "absent")))


def test_unknown_bootstrap_method_raises() -> None:
    settings = VaultSettings(bootstrap=BootstrapSettings(method="carrier_pigeon"))

    with pytest.raises(ValueError, match="Unsupported agent-vault bootstrap method"):
        mint_proxy_session_token(settings)
