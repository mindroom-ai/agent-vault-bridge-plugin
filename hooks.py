"""Route selected GitHub shell calls through the local Agent Vault proxy."""

from __future__ import annotations

import os
import re
import stat
import time
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlparse

from mindroom.plugin_api import (
    EVENT_BOT_READY,
    EVENT_TOOL_AFTER_CALL,
    EVENT_TOOL_BEFORE_CALL,
    EVENT_TOOL_ENVIRONMENT,
    AgentLifecycleContext,
    ToolAfterCallContext,
    ToolBeforeCallContext,
    ToolEnvironmentContext,
    hook,
    open_directory_within_root,
)

from .vault_client import BootstrapSettings, SessionToken, VaultSettings, mint_proxy_session_token

PLUGIN_NAME = "agent-vault-bridge"
TOKEN_ENV_NAME = "AGENT_VAULT_PROXY_TOKEN"
CA_ENV_NAME = "AGENT_VAULT_CA_PATH"
AUDIT_LOG_NAME = "agent-vault-bridge.log"
# Worker code can write the agent's workspace, so the log is opened without following a link, and without blocking on a
# planted FIFO.
_AUDIT_LOG_FLAGS = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
SECRET_ENV_NAMES = ("GITHUB_TOKEN", "GH_TOKEN")
# The shims a brokered command finds first on PATH, so `gh` and `curl` send the proxy session.
SHIM_DIR = str(Path(__file__).resolve().parent / "bin")
SESSION_UNAVAILABLE = "Agent Vault proxy session is unavailable for this GitHub call."
_TOKEN_REFRESH_SKEW_SECONDS = 60
_GITHUB_TOKEN_PATTERNS = (re.compile(r"github_pat_[A-Za-z0-9_]+"), re.compile(r"ghp_[A-Za-z0-9_]+"))
_GH_API_PATTERN = re.compile(r"(?:^|[;&|()\s])(?:gh|gh-via-broker)\s+api(?:\s|$)")
_DEFAULT_BOOTSTRAP_SETTINGS = BootstrapSettings()
_DEFAULT_VAULT_SETTINGS = VaultSettings()


_session_token: SessionToken | None = None
_audit_host_by_correlation: dict[str, str] = {}


def resolve_settings(raw_settings: Mapping[str, object] | None) -> VaultSettings:
    """Return plugin settings with defaults applied."""
    raw = dict(raw_settings or {})
    bootstrap_raw = raw.get("bootstrap")
    bootstrap_map = bootstrap_raw if isinstance(bootstrap_raw, Mapping) else {}

    return VaultSettings(
        vault_api=_string_setting(raw, "vault_api", _DEFAULT_VAULT_SETTINGS.vault_api),
        vault_proxy=_string_setting(raw, "vault_proxy", _DEFAULT_VAULT_SETTINGS.vault_proxy),
        ca_path=_string_setting(raw, "ca_path", _DEFAULT_VAULT_SETTINGS.ca_path),
        session_ttl_seconds=_int_setting(
            raw,
            "session_ttl_seconds",
            _DEFAULT_VAULT_SETTINGS.session_ttl_seconds,
        ),
        brokered_hosts=_string_tuple_setting(raw, "brokered_hosts", _DEFAULT_VAULT_SETTINGS.brokered_hosts),
        gated_tools=_string_tuple_setting(raw, "gated_tools", _DEFAULT_VAULT_SETTINGS.gated_tools),
        bootstrap=BootstrapSettings(
            method=_string_setting(bootstrap_map, "method", _DEFAULT_BOOTSTRAP_SETTINGS.method),
            container=_string_setting(bootstrap_map, "container", _DEFAULT_BOOTSTRAP_SETTINGS.container),
            token_file=_string_setting(bootstrap_map, "token_file", _DEFAULT_BOOTSTRAP_SETTINGS.token_file),
        ),
    )


def ensure_session_token(settings: VaultSettings) -> SessionToken:
    """Return a cached proxy session token, minting when missing or near expiry."""
    global _session_token

    now = time.time()
    if _session_token is not None and _session_token.is_valid(now + _TOKEN_REFRESH_SKEW_SECONDS):
        return _session_token

    _session_token = mint_proxy_session_token(settings, now=now)
    return _session_token


def brokered_host_for_call(tool_name: str, arguments: Mapping[str, object], settings: VaultSettings) -> str | None:
    """Return the brokered upstream host one tool call targets, or None when the call is not brokered."""
    if not _is_gated_tool(tool_name, settings.gated_tools):
        return None
    return _upstream_host_for_arguments(arguments, settings.brokered_hosts)


@hook(EVENT_BOT_READY, name="agent-vault-bridge-ready", priority=50, timeout_ms=30000)
async def prime_agent_vault_session(ctx: AgentLifecycleContext) -> None:
    """Mint the shared proxy session token after the bot is ready."""
    ensure_session_token(resolve_settings(ctx.settings))
    ctx.logger.info(
        "agent-vault-bridge session token ready",
        entity_name=ctx.entity_name,
        expires_at=int(_session_token.expires_at) if _session_token is not None else None,
    )


@hook(EVENT_TOOL_BEFORE_CALL, name="agent-vault-bridge-before", priority=20, timeout_ms=5000)
async def prepare_brokered_tool_call(ctx: ToolBeforeCallContext) -> None:
    """Decline a brokered shell call when no proxy session is available, and otherwise mark it for the audit log.

    A background script's call asks for approval after this hook, so it is refused before anyone is asked; a call in a
    reply that needs approval runs its hooks only once it is approved.
    """
    settings = resolve_settings(ctx.settings)
    host = brokered_host_for_call(ctx.tool_name, ctx.arguments, settings)
    if host is None:
        return
    if _session_token_or_decline(ctx, settings) is None:
        return
    if ctx.correlation_id:
        _audit_host_by_correlation[ctx.correlation_id] = host


# Its own hook, so a failure of the broker hook below can never hand a command the tokens.
@hook(EVENT_TOOL_ENVIRONMENT, name="agent-vault-bridge-strip-tokens", priority=20)
async def strip_github_tokens(ctx: ToolEnvironmentContext) -> None:
    """Keep GitHub tokens from every shell command."""
    if ctx.registered_toolkit != "shell":
        return
    for name in SECRET_ENV_NAMES:
        ctx.unset_env(name)


@hook(EVENT_TOOL_ENVIRONMENT, name="agent-vault-bridge-proxy", priority=20, timeout_ms=5000)
async def route_brokered_shell_command(ctx: ToolEnvironmentContext) -> None:
    """Send a brokered shell command through the Agent Vault proxy, or decline it when no proxy session is available.

    The session is checked again here because it can expire while the call waits, for example for approval.
    """
    if ctx.registered_toolkit != "shell":
        return
    settings = resolve_settings(ctx.settings)
    if brokered_host_for_call(ctx.function_name, ctx.arguments, settings) is None:
        return
    token = _session_token_or_decline(ctx, settings)
    if token is None:
        return
    for name, value in _proxy_env(settings, token).items():
        ctx.set_env(name, value)
    ctx.prepend_path(SHIM_DIR)


@hook(EVENT_TOOL_AFTER_CALL, name="agent-vault-bridge-after", priority=20, timeout_ms=3000)
async def audit_brokered_tool_call(ctx: ToolAfterCallContext) -> None:
    """Append one sanitized audit record for a brokered tool call."""
    host = _audit_host_by_correlation.pop(ctx.correlation_id, None)
    if host is None:
        return

    status = "success" if _tool_call_succeeded(ctx) else "fail"
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    line = f"{timestamp} shell {status} {host}\n"
    if _contains_github_token(line):
        msg = "agent-vault-bridge refused to write an audit line containing a GitHub token"
        raise RuntimeError(msg)
    _append_audit_line(ctx.runtime_paths.storage_root, ctx.agent_name, line)


def _session_token_or_decline(
    ctx: ToolBeforeCallContext | ToolEnvironmentContext,
    settings: VaultSettings,
) -> SessionToken | None:
    """Return the proxy session for a brokered call, or decline the call when Agent Vault cannot provide one."""
    try:
        return ensure_session_token(settings)
    except Exception:
        ctx.logger.exception("agent-vault-bridge could not mint a proxy session")
        ctx.decline(SESSION_UNAVAILABLE)
        return None


def _proxy_env(settings: VaultSettings, token: SessionToken) -> dict[str, str]:
    return {
        "HTTPS_PROXY": settings.vault_proxy,
        "REQUESTS_CA_BUNDLE": settings.ca_path,
        "CURL_CA_BUNDLE": settings.ca_path,
        "SSL_CERT_FILE": settings.ca_path,
        TOKEN_ENV_NAME: token.value,
        CA_ENV_NAME: settings.ca_path,
    }


def _is_gated_tool(tool_name: str, gated_tools: tuple[str, ...]) -> bool:
    allowed = {name.strip() for name in gated_tools if name.strip()}
    return tool_name in allowed or ("shell" in allowed and tool_name == "run_shell_command")


def _upstream_host_for_arguments(arguments: Mapping[str, object], brokered_hosts: tuple[str, ...]) -> str | None:
    brokered = set(brokered_hosts)
    args = _shell_args(arguments)
    if _is_gh_api_call(args) and "api.github.com" in brokered:
        return "api.github.com"

    for arg in args:
        parsed = urlparse(arg)
        if parsed.scheme in {"http", "https"} and parsed.hostname in brokered:
            return parsed.hostname
        for host in brokered:
            if host in arg:
                return host
    return None


def _shell_args(arguments: Mapping[str, object]) -> tuple[str, ...]:
    raw_args = arguments.get("args")
    if isinstance(raw_args, str):
        return (raw_args,)
    if not isinstance(raw_args, list):
        return ()
    return tuple(item for item in raw_args if isinstance(item, str))


def _is_gh_api_call(args: tuple[str, ...]) -> bool:
    if len(args) >= 2:
        executable = Path(args[0]).name
        if executable in {"gh", "gh-via-broker"} and args[1] == "api":
            return True
    return any(_GH_API_PATTERN.search(arg) for arg in args)


def _append_audit_line(storage_root: Path, agent_name: str, line: str) -> None:
    """Append one line to the agent's audit log, raising when the log or a directory on its path is a link.

    The log must be a regular file with no other hard link, as MindRoom requires of workspace files the primary writes,
    so the append never changes a file outside the workspace. MindRoom logs a refused append as a failed hook.
    """
    with open_directory_within_root(storage_root, Path("agents", agent_name, "workspace"), create=True) as workspace_fd:
        descriptor = os.open(AUDIT_LOG_NAME, _AUDIT_LOG_FLAGS, 0o666, dir_fd=workspace_fd)
    file_status = os.fstat(descriptor)
    hard_linked = file_status.st_nlink > 1
    if not stat.S_ISREG(file_status.st_mode) or hard_linked:
        os.close(descriptor)
        reason = "has another hard link" if hard_linked else "is not a regular file"
        msg = f"agent-vault-bridge refused to append to {AUDIT_LOG_NAME}, which {reason}"
        raise RuntimeError(msg)
    with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
        handle.write(line)


def _tool_call_succeeded(ctx: ToolAfterCallContext) -> bool:
    if ctx.blocked or ctx.error is not None:
        return False
    if isinstance(ctx.result, str) and ctx.result.startswith("Error:"):
        return False
    return True


def _contains_github_token(text: str) -> bool:
    return any(pattern.search(text) for pattern in _GITHUB_TOKEN_PATTERNS)


def _string_setting(settings: Mapping[str, object], name: str, default: str) -> str:
    value = settings.get(name)
    return value if isinstance(value, str) and value else default


def _int_setting(settings: Mapping[str, object], name: str, default: int) -> int:
    value = settings.get(name)
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    return default


def _string_tuple_setting(settings: Mapping[str, object], name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    value = settings.get(name)
    if isinstance(value, list | tuple):
        return tuple(item for item in value if isinstance(item, str) and item)
    return default
