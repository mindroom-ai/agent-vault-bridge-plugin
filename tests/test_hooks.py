# ruff: noqa: SLF001
"""Tests for the agent-vault-bridge plugin hooks."""

from __future__ import annotations

import importlib.util
import re
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from mindroom.constants import RuntimePaths
from mindroom.hooks import HookRegistry, emit_collect_environment
from mindroom.plugin_api import ToolEnvironmentContext

from agent_vault_bridge_test_import import hooks
from agent_vault_bridge_test_import.vault_client import SessionToken

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_CA_PATH = "/etc/ssl/agent-vault-ca.pem"


def _settings() -> hooks.VaultSettings:
    return hooks.VaultSettings(session_ttl_seconds=600)


def _seed_token(monkeypatch: pytest.MonkeyPatch, value: str = "session-token") -> None:
    monkeypatch.setattr(hooks, "_session_token", SessionToken(value=value, expires_at=time.time() + 600))


def _vault_is_down(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_to_mint(*_args: object, **_kwargs: object) -> SessionToken:
        msg = "agent-vault is down"
        raise RuntimeError(msg)

    monkeypatch.setattr(hooks, "_session_token", None)
    monkeypatch.setattr(hooks, "mint_proxy_session_token", fail_to_mint)


async def _collect_environment(
    registry: HookRegistry,
    arguments: dict[str, object],
    *,
    function_name: str = "run_shell_command",
    registered_toolkit: str | None = "shell",
) -> tuple[dict[str, str | None], tuple[str, ...], str | None]:
    """Run the plugin's environment hooks for one call, as MindRoom collects them before the call runs.

    Return the variables they set (None for a removal), the directories they put first on PATH, and the reason they
    declined the call, if they did.
    """
    ctx = ToolEnvironmentContext(
        function_name=function_name,
        registered_toolkit=registered_toolkit,
        arguments=arguments,
        agent_name="code",
        room_id=None,
        requester_id=None,
    )
    environment = await emit_collect_environment(registry, ctx)
    return dict(environment.values), environment.path_prepend, ctx.decline_reason if ctx.declined else None


@pytest.mark.parametrize(
    "raw_args",
    [
        ["gh", "api", "/user"],
        "gh api /user",
        ["bash", "-lc", "echo GH_USER_START; gh api /user | jq -c '{login,id}'"],
        ["curl", "https://api.github.com/user"],
    ],
)
def test_github_api_calls_are_brokered(raw_args: list[str] | str) -> None:
    assert hooks.brokered_host_for_call("run_shell_command", {"args": raw_args}, _settings()) == "api.github.com"


def test_other_calls_are_not_brokered() -> None:
    settings = _settings()
    plain_curl = {"args": ["curl", "https://example.com/"]}
    gh_api = {"args": ["gh", "api", "/user"]}

    assert hooks.brokered_host_for_call("run_shell_command", plain_curl, settings) is None
    assert hooks.brokered_host_for_call("run_python_code", gh_api, settings) is None


@pytest.mark.asyncio
async def test_brokered_shell_command_gets_proxy_env_and_shims_without_github_tokens(
    monkeypatch: pytest.MonkeyPatch,
    plugin_registry: HookRegistry,
) -> None:
    _seed_token(monkeypatch)

    values, path_prepend, declined = await _collect_environment(plugin_registry, {"args": ["gh", "api", "/user"]})

    assert values == {
        "GITHUB_TOKEN": None,
        "GH_TOKEN": None,
        "HTTPS_PROXY": "https://127.0.0.1:14322",
        "REQUESTS_CA_BUNDLE": _CA_PATH,
        "CURL_CA_BUNDLE": _CA_PATH,
        "SSL_CERT_FILE": _CA_PATH,
        "AGENT_VAULT_PROXY_TOKEN": "session-token",
        "AGENT_VAULT_CA_PATH": _CA_PATH,
    }
    assert path_prepend == (str(PLUGIN_ROOT / "bin"),)
    assert declined is None


@pytest.mark.asyncio
async def test_every_shell_command_loses_github_tokens_without_routing(
    monkeypatch: pytest.MonkeyPatch,
    plugin_registry: HookRegistry,
) -> None:
    _seed_token(monkeypatch)

    changes = await _collect_environment(plugin_registry, {"args": ["curl", "https://example.com/"]})

    assert changes == ({"GITHUB_TOKEN": None, "GH_TOKEN": None}, (), None)


@pytest.mark.asyncio
async def test_brokered_shell_command_is_declined_when_no_session_can_be_minted(
    monkeypatch: pytest.MonkeyPatch,
    plugin_registry: HookRegistry,
) -> None:
    """The call is refused, and the tokens stay removed, since stripping them is a hook of its own."""
    _vault_is_down(monkeypatch)

    changes = await _collect_environment(plugin_registry, {"args": ["gh", "api", "/user"]})

    assert changes == ({"GITHUB_TOKEN": None, "GH_TOKEN": None}, (), hooks.SESSION_UNAVAILABLE)


@pytest.mark.asyncio
async def test_other_tools_get_no_environment_changes(
    monkeypatch: pytest.MonkeyPatch,
    plugin_registry: HookRegistry,
) -> None:
    _seed_token(monkeypatch)

    changes = await _collect_environment(
        plugin_registry,
        {"args": ["gh", "api", "/user"]},
        function_name="run_python_code",
        registered_toolkit="python",
    )

    assert changes == ({}, (), None)


@pytest.mark.parametrize("method", ["decline", "prepend_path"])
def test_plugin_refuses_to_load_on_a_mindroom_without_the_hook_methods(
    monkeypatch: pytest.MonkeyPatch,
    method: str,
) -> None:
    """On an older MindRoom, loading fails with the minimum release instead of brokered calls running unproxied."""
    monkeypatch.delattr(ToolEnvironmentContext, method)
    module_name = f"{hooks.__package__}.hooks_on_older_mindroom"
    spec = importlib.util.spec_from_file_location(module_name, PLUGIN_ROOT / "hooks.py")
    assert spec is not None
    assert spec.loader is not None

    with pytest.raises(ImportError, match=r"needs the first MindRoom release after v2026\.10\.614 or newer"):
        spec.loader.exec_module(importlib.util.module_from_spec(spec))


def _before_call_context(correlation_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        tool_name="run_shell_command",
        arguments={"args": ["gh", "api", "/user"]},
        settings={},
        correlation_id=correlation_id,
        decline=MagicMock(),
        logger=MagicMock(),
    )


@pytest.mark.asyncio
async def test_before_hook_marks_brokered_call_for_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_token(monkeypatch)
    monkeypatch.setattr(hooks, "_audit_host_by_correlation", {})
    ctx = _before_call_context("call-1")

    await hooks.prepare_brokered_tool_call(ctx)

    assert hooks._audit_host_by_correlation == {"call-1": "api.github.com"}
    ctx.decline.assert_not_called()


@pytest.mark.asyncio
async def test_before_hook_declines_brokered_call_when_no_session_can_be_minted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _vault_is_down(monkeypatch)
    monkeypatch.setattr(hooks, "_audit_host_by_correlation", {})
    ctx = _before_call_context("call-1")

    await hooks.prepare_brokered_tool_call(ctx)

    ctx.decline.assert_called_once_with(hooks.SESSION_UNAVAILABLE)
    assert hooks._audit_host_by_correlation == {}


@pytest.mark.asyncio
async def test_after_hook_writes_sanitized_audit_line(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(hooks, "_audit_host_by_correlation", {"call-2": "api.github.com"})
    runtime_paths = RuntimePaths(
        config_path=tmp_path / "config.yaml",
        config_dir=tmp_path,
        env_path=tmp_path / ".env",
        storage_root=tmp_path / "mindroom_data",
        process_env={},
        env_file_values={},
    )
    ctx = SimpleNamespace(
        tool_name="run_shell_command",
        arguments={"args": ["gh", "api", "/user"]},
        agent_name="code",
        runtime_paths=runtime_paths,
        result='{"login":"example"}',
        error=None,
        blocked=False,
        correlation_id="call-2",
    )

    await hooks.audit_brokered_tool_call(ctx)

    log_path = runtime_paths.storage_root / "agents" / "code" / "workspace" / hooks.AUDIT_LOG_NAME
    line = log_path.read_text(encoding="utf-8")
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z shell success api\.github\.com\n", line)
    assert "github_pat_" not in line
    assert "ghp_" not in line
    assert "session-token" not in line
    assert hooks._audit_host_by_correlation == {}
