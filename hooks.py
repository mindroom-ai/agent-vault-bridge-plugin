"""Route selected GitHub shell calls through the local Agent Vault proxy."""

from __future__ import annotations

import contextlib
import os
import re
import time
from collections.abc import Iterator, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from mindroom.hooks import (
    EVENT_BOT_READY,
    EVENT_TOOL_AFTER_CALL,
    EVENT_TOOL_BEFORE_CALL,
    AgentLifecycleContext,
    ToolAfterCallContext,
    ToolBeforeCallContext,
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

    gated: bool
    upstream_host: str | None
    env_overlay: dict[str, str]
    extra_passthrough: tuple[str, ...]

    @property
    def routed(self) -> bool:
        """Return whether the call should use the Agent Vault proxy."""
        return self.upstream_host is not None


_session_token: SessionToken | None = None
_settings = VaultSettings()
_local_shell_plan: ContextVar[ExecutionPlan | None] = ContextVar("agent_vault_bridge_plan", default=None)
_patches_installed = False
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
    *,
    function_name: str | None = None,
) -> ExecutionPlan:
    """Return the env and audit plan for one tool call."""
    gated = _is_gated_tool(tool_name, function_name, settings.gated_tools)
    if not gated:
        return ExecutionPlan(gated=False, upstream_host=None, env_overlay={}, extra_passthrough=())

    upstream_host = _upstream_host_for_arguments(arguments, settings.brokered_hosts)
    if upstream_host is None:
        return ExecutionPlan(gated=True, upstream_host=None, env_overlay={}, extra_passthrough=())

    token = ensure_session_token(settings)
    env_overlay = _proxy_env(settings, token)
    return ExecutionPlan(
        gated=True,
        upstream_host=upstream_host,
        env_overlay=env_overlay,
        extra_passthrough=tuple(env_overlay),
    )


def env_with_plan(base_env: Mapping[str, str] | None, plan: ExecutionPlan) -> dict[str, str]:
    """Apply secret stripping and any broker overlay to an execution env."""
    env = dict(base_env or {})
    for name in SECRET_ENV_NAMES:
        env.pop(name, None)
    if plan.routed:
        env.update(plan.env_overlay)
    return env


def extra_passthrough_with_plan(existing: str | None, plan: ExecutionPlan) -> str | None:
    """Return shell extra passthrough names needed for sandboxed broker env."""
    if not plan.routed:
        return existing

    names: list[str] = []
    for chunk in (existing or "").replace("\n", ",").split(","):
        name = chunk.strip()
        if name:
            names.append(name)
    names.extend(name for name in plan.extra_passthrough if name not in names)
    return ",".join(names)


@hook(EVENT_BOT_READY, name="agent-vault-bridge-ready", priority=50, timeout_ms=30000)
async def prime_agent_vault_session(ctx: AgentLifecycleContext) -> None:
    """Mint the shared proxy session token after the bot is ready."""
    settings = _activate_settings(ctx.settings)
    ensure_session_token(settings)
    ctx.logger.info(
        "agent-vault-bridge session token ready",
        entity_name=ctx.entity_name,
        expires_at=int(_session_token.expires_at) if _session_token is not None else None,
    )


@hook(EVENT_TOOL_BEFORE_CALL, name="agent-vault-bridge-before", priority=20, timeout_ms=5000)
async def prepare_brokered_tool_call(ctx: ToolBeforeCallContext) -> None:
    """Prepare one shell call for Agent Vault routing when it targets GitHub."""
    settings = _activate_settings(ctx.settings)
    try:
        plan = execution_plan_for_call(ctx.tool_name, ctx.arguments, settings)
    except Exception:
        if _is_gated_tool(ctx.tool_name, None, settings.gated_tools) and _upstream_host_for_arguments(
            ctx.arguments,
            settings.brokered_hosts,
        ):
            ctx.decline("Agent Vault proxy session is unavailable for this GitHub call.")
            return
        raise

    _local_shell_plan.set(plan)
    for name in SECRET_ENV_NAMES:
        ctx.arguments.pop(name, None)
    if plan.routed:
        ctx.arguments[AUDIT_HOST_ARGUMENT] = plan.upstream_host
        ctx.arguments[AUDIT_ROUTED_ARGUMENT] = True
        if ctx.correlation_id:
            _audit_host_by_correlation[ctx.correlation_id] = plan.upstream_host


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


def _activate_settings(raw_settings: Mapping[str, object] | None) -> VaultSettings:
    global _settings
    _settings = resolve_settings(raw_settings)
    return _settings


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


def _is_gated_tool(tool_name: str, function_name: str | None, gated_tools: tuple[str, ...]) -> bool:
    allowed = {name.strip() for name in gated_tools if name.strip()}
    if tool_name in allowed or (function_name is not None and function_name in allowed):
        return True
    if "shell" in allowed and (tool_name == "run_shell_command" or function_name == "run_shell_command"):
        return True
    return False


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


@contextlib.contextmanager
def _scoped_local_shell_plan(plan: ExecutionPlan) -> Iterator[None]:
    token = _local_shell_plan.set(plan)
    previous_values = {name: os.environ.get(name) for name in (*SECRET_ENV_NAMES, *plan.env_overlay)}
    try:
        for name in SECRET_ENV_NAMES:
            os.environ.pop(name, None)
        if plan.routed:
            os.environ.update(plan.env_overlay)
        yield
    finally:
        for name, value in previous_values.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        _local_shell_plan.reset(token)


def _install_runtime_patches() -> None:
    global _patches_installed
    if _patches_installed:
        return
    _patch_tool_call_env()
    _patch_sandbox_proxy_env()
    _patch_shell_subprocess_env()
    _patches_installed = True


def _patch_tool_call_env() -> None:
    from mindroom.tool_system import tool_hooks

    if getattr(tool_hooks._call_tool, "_agent_vault_bridge_patched", False):
        return
    original_call_tool = tool_hooks._call_tool

    async def call_tool_with_agent_vault_env(
        func: object,
        args: dict[str, Any],
        *,
        tool_name: str,
        agent_name: str | None,
    ) -> object:
        plan = execution_plan_for_call(tool_name, args, _settings)
        with _scoped_local_shell_plan(plan):
            return await original_call_tool(func, args, tool_name=tool_name, agent_name=agent_name)

    call_tool_with_agent_vault_env._agent_vault_bridge_patched = True
    tool_hooks._call_tool = call_tool_with_agent_vault_env


def _patch_sandbox_proxy_env() -> None:
    from mindroom.tool_system import sandbox_proxy

    if getattr(sandbox_proxy._call_proxy_sync, "_agent_vault_bridge_patched", False):
        return
    original_call_proxy_sync = sandbox_proxy._call_proxy_sync

    def call_proxy_sync_with_agent_vault_env(
        *,
        tool_name: str,
        function_name: str,
        kwargs: dict[str, object],
        execution_env: dict[str, str] | None = None,
        extra_env_passthrough: str | None = None,
        **call_kwargs: Any,
    ) -> object:
        plan = execution_plan_for_call(tool_name, kwargs, _settings, function_name=function_name)
        return original_call_proxy_sync(
            tool_name=tool_name,
            function_name=function_name,
            kwargs=kwargs,
            execution_env=env_with_plan(execution_env, plan) if plan.gated else execution_env,
            extra_env_passthrough=extra_passthrough_with_plan(extra_env_passthrough, plan),
            **call_kwargs,
        )

    call_proxy_sync_with_agent_vault_env._agent_vault_bridge_patched = True
    sandbox_proxy._call_proxy_sync = call_proxy_sync_with_agent_vault_env


def _patch_shell_subprocess_env() -> None:
    from mindroom.tools import shell

    if getattr(shell._shell_subprocess_env, "_agent_vault_bridge_patched", False):
        return
    original_shell_subprocess_env = shell._shell_subprocess_env

    def shell_subprocess_env_with_agent_vault(
        runtime_env: dict[str, str],
        **kwargs: Any,
    ) -> dict[str, str]:
        env = original_shell_subprocess_env(runtime_env, **kwargs)
        plan = _local_shell_plan.get()
        if plan is None:
            for name in SECRET_ENV_NAMES:
                env.pop(name, None)
            return env
        return env_with_plan(env, plan)

    shell_subprocess_env_with_agent_vault._agent_vault_bridge_patched = True
    shell._shell_subprocess_env = shell_subprocess_env_with_agent_vault


_install_runtime_patches()
