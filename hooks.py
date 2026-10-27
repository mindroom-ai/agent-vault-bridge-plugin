"""Route selected GitHub shell calls through the local Agent Vault proxy."""

from __future__ import annotations

import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
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
)

from .vault_client import BootstrapSettings, SessionToken, VaultSettings, mint_proxy_session_token

PLUGIN_NAME = "agent-vault-bridge"
TOKEN_ENV_NAME = "AGENT_VAULT_PROXY_TOKEN"
CA_ENV_NAME = "AGENT_VAULT_CA_PATH"
AUDIT_LOG_NAME = "agent-vault-bridge.log"
AUDIT_HOST_ARGUMENT = "_agent_vault_bridge_upstream_host"
AUDIT_ROUTED_ARGUMENT = "_agent_vault_bridge_routed"
SECRET_ENV_NAMES = ("GITHUB_TOKEN", "GH_TOKEN")
_TOKEN_REFRESH_SKEW_SECONDS = 60
_GITHUB_TOKEN_PATTERNS = (re.compile(r"github_pat_[A-Za-z0-9_]+"), re.compile(r"ghp_[A-Za-z0-9_]+"))
_GH_API_PATTERN = re.compile(r"(?:^|[;&|()\s])(?:gh|gh-via-broker)\s+api(?:\s|$)")
_DEFAULT_BOOTSTRAP_SETTINGS = BootstrapSettings()
_DEFAULT_VAULT_SETTINGS = VaultSettings()


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    """How one tool call should be adjusted."""

    upstream_host: str | None
    env_overlay: dict[str, str]

    @property
    def routed(self) -> bool:
        """Return whether the call should use the Agent Vault proxy."""
        return self.upstream_host is not None


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


def execution_plan_for_call(
    tool_name: str,
    arguments: Mapping[str, object],
    settings: VaultSettings,
) -> ExecutionPlan:
    """Return the env and audit plan for one tool call."""
    if not _is_gated_tool(tool_name, settings.gated_tools):
        return ExecutionPlan(upstream_host=None, env_overlay={})

    upstream_host = _upstream_host_for_arguments(arguments, settings.brokered_hosts)
    if upstream_host is None:
        return ExecutionPlan(upstream_host=None, env_overlay={})

    token = ensure_session_token(settings)
    return ExecutionPlan(upstream_host=upstream_host, env_overlay=_proxy_env(settings, token))


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
    """Mark one brokered shell call for the audit log, or decline it when no proxy session is available."""
    settings = resolve_settings(ctx.settings)
    try:
        plan = execution_plan_for_call(ctx.tool_name, ctx.arguments, settings)
    except Exception:
        if _is_gated_tool(ctx.tool_name, settings.gated_tools) and _upstream_host_for_arguments(
            ctx.arguments,
            settings.brokered_hosts,
        ):
            ctx.decline("Agent Vault proxy session is unavailable for this GitHub call.")
            return
        raise

    for name in SECRET_ENV_NAMES:
        ctx.arguments.pop(name, None)
    if plan.routed:
        ctx.arguments[AUDIT_HOST_ARGUMENT] = plan.upstream_host
        ctx.arguments[AUDIT_ROUTED_ARGUMENT] = True
        if ctx.correlation_id:
            _audit_host_by_correlation[ctx.correlation_id] = plan.upstream_host


@hook(EVENT_TOOL_ENVIRONMENT, name="agent-vault-bridge-environment", priority=20, timeout_ms=5000)
async def set_shell_command_environment(ctx: ToolEnvironmentContext) -> None:
    """Keep GitHub tokens from every shell command, and send a brokered one through the Agent Vault proxy."""
    if ctx.registered_toolkit != "shell":
        return
    for name in SECRET_ENV_NAMES:
        ctx.unset_env(name)
    try:
        plan = execution_plan_for_call(ctx.function_name, ctx.arguments, resolve_settings(ctx.settings))
    except Exception:
        # Raising would also drop the removals above. The before hook declines a call it cannot mint a session for,
        # so this fails only when the session expired while the call waited, such as for approval.
        ctx.logger.exception("agent-vault-bridge could not mint a proxy session for this shell call")
        return
    for name, value in plan.env_overlay.items():
        ctx.set_env(name, value)


@hook(EVENT_TOOL_AFTER_CALL, name="agent-vault-bridge-after", priority=20, timeout_ms=3000)
async def audit_brokered_tool_call(ctx: ToolAfterCallContext) -> None:
    """Append one sanitized audit record for a brokered tool call."""
    host = _audit_host(ctx.arguments) or _audit_host_by_correlation.pop(ctx.correlation_id, None)
    if host is None:
        return

    status = "success" if _tool_call_succeeded(ctx) else "fail"
    log_path = _audit_log_path(ctx.runtime_paths.storage_root, ctx.agent_name)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    line = f"{timestamp} shell {status} {host}\n"
    if _contains_github_token(line):
        msg = "agent-vault-bridge refused to write an audit line containing a GitHub token"
        raise RuntimeError(msg)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(line)


def _proxy_env(settings: VaultSettings, token: SessionToken) -> dict[str, str]:
    return {
        "HTTPS_PROXY": settings.vault_proxy,
        "REQUESTS_CA_BUNDLE": settings.ca_path,
        "CURL_CA_BUNDLE": settings.ca_path,
        "SSL_CERT_FILE": settings.ca_path,
        TOKEN_ENV_NAME: token.value,
        CA_ENV_NAME: settings.ca_path,
        "PATH": _path_with_plugin_bin(os.environ.get("PATH", "")),
    }


def _path_with_plugin_bin(current_path: str) -> str:
    plugin_bin = str(Path(__file__).resolve().parent / "bin")
    entries = [entry for entry in current_path.split(os.pathsep) if entry]
    return os.pathsep.join([plugin_bin, *[entry for entry in entries if entry != plugin_bin]])


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


def _audit_host(arguments: Mapping[str, object]) -> str | None:
    host = arguments.get(AUDIT_HOST_ARGUMENT)
    return host if isinstance(host, str) and host else None


def _audit_log_path(storage_root: Path, agent_name: str) -> Path:
    return storage_root / "agents" / agent_name / "workspace" / AUDIT_LOG_NAME


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
