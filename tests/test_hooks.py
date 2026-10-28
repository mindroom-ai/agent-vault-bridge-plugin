# ruff: noqa: SLF001
"""Tests for the agent-vault-bridge plugin hooks."""

from __future__ import annotations

import importlib.util
import re
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from mindroom import plugin_api
from mindroom.constants import RuntimePaths
from mindroom.hooks import HookRegistry, emit_collect_environment
from mindroom.plugin_api import ToolAfterCallContext, ToolBeforeCallContext, ToolEnvironmentContext

from agent_vault_bridge_test_import import hooks
from agent_vault_bridge_test_import.vault_client import SessionToken

if TYPE_CHECKING:
    from collections.abc import Callable

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


@pytest.mark.asyncio
async def test_github_tokens_are_stripped_even_when_the_broker_hook_fails(
    monkeypatch: pytest.MonkeyPatch,
    plugin_registry: HookRegistry,
) -> None:
    """Stripping is a hook of its own, so the removals survive a broker hook that raises."""
    _seed_token(monkeypatch)

    def broken_proxy_env(*_args: object) -> dict[str, str]:
        msg = "broker hook bug"
        raise RuntimeError(msg)

    monkeypatch.setattr(hooks, "_proxy_env", broken_proxy_env)

    changes = await _collect_environment(plugin_registry, {"args": ["gh", "api", "/user"]})

    assert changes == ({"GITHUB_TOKEN": None, "GH_TOKEN": None}, (), None)


@pytest.mark.parametrize(
    "make_older",
    [
        pytest.param(lambda mp: mp.delattr(ToolEnvironmentContext, "decline"), id="no-decline"),
        pytest.param(lambda mp: mp.delattr(ToolEnvironmentContext, "prepend_path"), id="no-prepend-path"),
        pytest.param(lambda mp: mp.delattr(plugin_api, "EVENT_TOOL_ENVIRONMENT"), id="no-environment-hook"),
        pytest.param(lambda mp: mp.setitem(sys.modules, "mindroom.plugin_api", None), id="no-plugin-api"),
    ],
)
def test_plugin_refuses_to_load_on_an_older_mindroom(
    monkeypatch: pytest.MonkeyPatch,
    make_older: Callable[[pytest.MonkeyPatch], None],
) -> None:
    """Every older MindRoom gets the minimum release, instead of a bare import error or brokered calls run unproxied."""
    make_older(monkeypatch)
    module_name = f"{hooks.__package__}.hooks_on_older_mindroom"
    spec = importlib.util.spec_from_file_location(module_name, PLUGIN_ROOT / "hooks.py")
    assert spec is not None
    assert spec.loader is not None

    minimum = re.escape(f"needs MindRoom {hooks.MINIMUM_MINDROOM_RELEASE} or later")
    with pytest.raises(ImportError, match=f"^agent-vault-bridge {minimum}, "):
        spec.loader.exec_module(importlib.util.module_from_spec(spec))


def _before_call_context(correlation_id: str) -> ToolBeforeCallContext:
    return ToolBeforeCallContext(
        tool_name="run_shell_command",
        arguments={"args": ["gh", "api", "/user"]},
        agent_name="code",
        room_id=None,
        thread_id=None,
        requester_id=None,
        session_id=None,
        correlation_id=correlation_id,
    )


@pytest.mark.asyncio
async def test_before_hook_marks_brokered_call_for_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_token(monkeypatch)
    monkeypatch.setattr(hooks, "_audit_host_by_correlation", {})
    ctx = _before_call_context("call-1")

    await hooks.prepare_brokered_tool_call(ctx)

    assert hooks._audit_host_by_correlation == {"call-1": "api.github.com"}
    assert not ctx.declined


@pytest.mark.asyncio
async def test_before_hook_declines_brokered_call_when_no_session_can_be_minted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _vault_is_down(monkeypatch)
    monkeypatch.setattr(hooks, "_audit_host_by_correlation", {})
    ctx = _before_call_context("call-1")

    await hooks.prepare_brokered_tool_call(ctx)

    assert (ctx.declined, ctx.decline_reason) == (True, hooks.SESSION_UNAVAILABLE)
    assert hooks._audit_host_by_correlation == {}


def _after_call_context(storage_root: Path, correlation_id: str) -> ToolAfterCallContext:
    runtime_paths = RuntimePaths(
        config_path=storage_root.parent / "config.yaml",
        config_dir=storage_root.parent,
        env_path=storage_root.parent / ".env",
        storage_root=storage_root,
        process_env={},
        env_file_values={},
    )
    return ToolAfterCallContext(
        tool_name="run_shell_command",
        arguments={"args": ["gh", "api", "/user"]},
        agent_name="code",
        room_id=None,
        thread_id=None,
        requester_id=None,
        session_id=None,
        result='{"login":"example"}',
        error=None,
        blocked=False,
        duration_ms=1.0,
        runtime_paths=runtime_paths,
        correlation_id=correlation_id,
    )


@pytest.mark.asyncio
async def test_after_hook_writes_sanitized_audit_line(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    storage_root = tmp_path / "mindroom_data"
    storage_root.mkdir()
    log_path = storage_root / "agents" / "code" / "workspace" / hooks.AUDIT_LOG_NAME
    monkeypatch.setattr(hooks, "_audit_host_by_correlation", {"call-2": "api.github.com", "call-3": "api.github.com"})

    await hooks.audit_brokered_tool_call(_after_call_context(storage_root, "call-2"))
    await hooks.audit_brokered_tool_call(_after_call_context(storage_root, "call-3"))

    lines = log_path.read_text(encoding="utf-8").splitlines(keepends=True)
    assert len(lines) == 2
    for line in lines:
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z shell success api\.github\.com\n", line)
        assert "github_pat_" not in line
        assert "ghp_" not in line
        assert "session-token" not in line
    assert hooks._audit_host_by_correlation == {}


def _link_audit_log(storage_root: Path, outside: Path) -> None:
    workspace = storage_root / "agents" / "code" / "workspace"
    workspace.mkdir(parents=True)
    (workspace / hooks.AUDIT_LOG_NAME).symlink_to(outside / "target.txt")


def _link_workspace(storage_root: Path, outside: Path) -> None:
    (storage_root / "agents" / "code").mkdir(parents=True)
    (storage_root / "agents" / "code" / "workspace").symlink_to(outside, target_is_directory=True)


def _link_agent_directory(storage_root: Path, outside: Path) -> None:
    (storage_root / "agents").mkdir()
    (storage_root / "agents" / "code").symlink_to(outside, target_is_directory=True)


@pytest.mark.parametrize(
    "plant_link",
    [
        pytest.param(_link_audit_log, id="linked-log"),
        pytest.param(_link_workspace, id="linked-workspace"),
        pytest.param(_link_agent_directory, id="linked-agent-directory"),
    ],
)
@pytest.mark.asyncio
async def test_after_hook_never_appends_through_a_link_worker_code_planted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    plant_link: Callable[[Path, Path], None],
) -> None:
    """Worker code can write the agent's workspace, so a link there must not redirect the primary's append."""
    storage_root = tmp_path / "mindroom_data"
    storage_root.mkdir()
    outside = tmp_path / "outside"
    (outside / "workspace").mkdir(parents=True)
    # Files the primary can write that each link would reach: a named file, and the log name through a linked directory.
    for target in (
        outside / "target.txt",
        outside / hooks.AUDIT_LOG_NAME,
        outside / "workspace" / hooks.AUDIT_LOG_NAME,
    ):
        target.write_text("primary-owned\n", encoding="utf-8")
    before = {path: path.read_bytes() for path in outside.rglob("*") if path.is_file()}
    plant_link(storage_root, outside)
    monkeypatch.setattr(hooks, "_audit_host_by_correlation", {"call-4": "api.github.com"})

    await hooks.audit_brokered_tool_call(_after_call_context(storage_root, "call-4"))

    assert {path: path.read_bytes() for path in outside.rglob("*") if path.is_file()} == before
    assert not [path for path in storage_root.rglob("*") if path.is_file() and not path.is_symlink()]
    assert hooks._audit_host_by_correlation == {}
