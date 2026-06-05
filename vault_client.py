"""Minimal Agent Vault session-token client."""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BootstrapSettings:
    """How this plugin obtains a proxy session token."""

    method: str = "docker_exec"
    container: str = "agent-vault"


@dataclass(frozen=True, slots=True)
class VaultSettings:
    """Agent Vault connection and session settings."""

    mode: str = "native_worker_egress"
    vault_api: str = "http://127.0.0.1:14321"
    vault_proxy: str = "https://127.0.0.1:14322"
    ca_path: str = "/etc/ssl/agent-vault-ca.pem"
    session_ttl_seconds: int = 86400
    brokered_hosts: tuple[str, ...] = ("api.github.com",)
    gated_tools: tuple[str, ...] = ("shell",)
    bootstrap: BootstrapSettings = BootstrapSettings()


@dataclass(frozen=True, slots=True)
class SessionToken:
    """One minted Agent Vault proxy token with local expiry time."""

    value: str
    expires_at: float

    def is_valid(self, now: float | None = None) -> bool:
        """Return whether the token should still be used."""
        resolved_now = time.time() if now is None else now
        return bool(self.value) and resolved_now < self.expires_at


def mint_proxy_session_token(settings: VaultSettings, *, now: float | None = None) -> SessionToken:
    """Mint one proxy session token using the configured bootstrap method."""
    if settings.bootstrap.method != "docker_exec":
        msg = f"Unsupported agent-vault bootstrap method: {settings.bootstrap.method}"
        raise ValueError(msg)

    ttl_seconds = max(1, int(settings.session_ttl_seconds))
    result = subprocess.run(
        [
            "docker",
            "exec",
            settings.bootstrap.container,
            "agent-vault",
            "vault",
            "token",
            "--role",
            "proxy",
            "--ttl",
            str(ttl_seconds),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    token = _last_nonempty_line(result.stdout)
    if not token:
        msg = "agent-vault token command returned no token"
        raise RuntimeError(msg)

    resolved_now = time.time() if now is None else now
    return SessionToken(value=token, expires_at=resolved_now + ttl_seconds)


def _last_nonempty_line(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else ""
