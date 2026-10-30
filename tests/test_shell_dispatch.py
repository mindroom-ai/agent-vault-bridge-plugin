"""Brokered and plain shell calls through MindRoom's real shell tool and tool-call policy, locally and in a worker."""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import nullcontext
from typing import TYPE_CHECKING

import pytest
import structlog
from mindroom.approval.tool_approval import BackgroundScriptToolOrigin, ToolApprovalDecision
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.hooks import HookBindings, HookRegistry
from mindroom.llm.request_context import bind_llm_request_log_context
from mindroom.tool_system.invocation import ToolCallContext, execute_tool_call
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.registry import builtin_tool_registry
from mindroom.tool_system.runtime_context import detached_tool_scope
from mindroom.tool_system.tool_hooks import build_tool_hook_middleware
from tests.test_sandbox_proxy import _MIND_EXECUTION_IDENTITY
from tests.test_tool_environment_hook import _function, _Shell, _worker_tool

from agent_vault_bridge_test_import import hooks
from agent_vault_bridge_test_import.vault_client import SessionToken

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.tool_function import ToolOutcome

    _ApprovalGate = Callable[[BackgroundScriptToolOrigin, str, dict[str, object]], Awaitable[ToolApprovalDecision]]

# MindRoom's own GitHub tokens, which the shell tool's `extra_env_passthrough` would hand every command.
_GITHUB_TOKENS = {"GITHUB_TOKEN": "github-token", "GH_TOKEN": "gh-token"}
_PRINT_TOKENS = 'printf "%s|%s|%s|" "${GITHUB_TOKEN-unset}" "${GH_TOKEN-unset}" "${AGENT_VAULT_PROXY_TOKEN-unset}"; '


@pytest.fixture
def runtime_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> RuntimePaths:
    """MindRoom's runtime paths for the test, with its GitHub tokens in MindRoom's process environment."""
    for name, value in _GITHUB_TOKENS.items():
        monkeypatch.setenv(name, value)
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}\n", encoding="utf-8")
    return resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "storage",
        process_env=dict(os.environ),
    )


def _local_shell(runtime_paths: RuntimePaths, **shell_settings: str) -> _Shell:
    toolkit = get_tool_by_name(
        "shell",
        runtime_paths,
        runs_in_worker=False,
        tool_config_overrides=shell_settings,
        worker_target=None,
        tools=builtin_tool_registry(),
    )
    return _Shell(_function(toolkit, "shell", "run_shell_command"), sent=[], runtime=nullcontext)


@pytest.fixture(params=["local", "worker"])
def make_shell(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    runtime_paths: RuntimePaths,
) -> Callable[..., _Shell]:
    """Build the shell tool, with any shell tool settings, to run commands in MindRoom's process or in a worker."""
    passthrough = ",".join(_GITHUB_TOKENS)

    def make(**shell_settings: str) -> _Shell:
        if request.param == "local":
            return _local_shell(runtime_paths, extra_env_passthrough=passthrough, **shell_settings)
        return _worker_tool(
            monkeypatch,
            tmp_path,
            "shell",
            "run_shell_command",
            extra_env_passthrough=passthrough,
            **shell_settings,
        )

    return make


@pytest.fixture(autouse=True)
def session(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test with a valid proxy session."""
    monkeypatch.setattr(hooks, "_session_token", SessionToken(value="session-token", expires_at=time.time() + 600))


async def _call(
    shell: _Shell,
    registry: HookRegistry,
    command: str,
    runtime_paths: RuntimePaths,
    *,
    approval_gate: _ApprovalGate | None = None,
) -> ToolOutcome:
    """Run ``command`` as one shell call, through ``approval_gate`` as a background script's call when given."""
    (outcome,) = await _calls(shell, registry, [command], runtime_paths, approval_gate=approval_gate)
    return outcome


async def _calls(
    shell: _Shell,
    registry: HookRegistry,
    commands: list[str],
    runtime_paths: RuntimePaths,
    *,
    approval_gate: _ApprovalGate | None = None,
) -> list[ToolOutcome]:
    """Run each of ``commands`` as one shell call, all at once, through ``approval_gate`` when given.

    Fail when any hook fails, since MindRoom only logs a failing observer hook such as the audit.
    """
    identity = _MIND_EXECUTION_IDENTITY
    origin = (
        None
        if approval_gate is None
        else BackgroundScriptToolOrigin(
            run_id="script-run",
            call_id="call-1",
            requester_id=identity.requester_id,
            toolkit_name="shell",
            function_name="run_shell_command",
        )
    )
    policy = build_tool_hook_middleware(
        HookBindings(registry=registry),
        Config(),
        runtime_paths,
        origin=origin,
        approval_gate=approval_gate,
    )

    async def call(command: str) -> ToolOutcome:
        with shell.runtime(), detached_tool_scope(identity):
            return await execute_tool_call(
                shell.function,
                {"args": ["bash", "-c", command]},
                ToolCallContext(run_id="run-1", session_id=identity.session_id, user_id=None),
                middleware=(policy,),
            )

    with structlog.testing.capture_logs() as logs:
        outcomes = await asyncio.gather(*(call(command) for command in commands))
    assert [log for log in logs if log["event"] == "Hook execution failed"] == []
    return outcomes


async def _run(shell: _Shell, registry: HookRegistry, command: str, runtime_paths: RuntimePaths) -> str:
    """Run ``command`` as one shell call and return the last line it printed."""
    outcome = await _call(shell, registry, command, runtime_paths)
    assert not outcome.is_error, outcome.content
    return outcome.content.rsplit("\n", 1)[-1]


def _audit_records(runtime_paths: RuntimePaths) -> list[str]:
    """Return the records of the agent's audit log, without their timestamps."""
    log_path = runtime_paths.storage_root / "agents" / "mind" / "workspace" / hooks.AUDIT_LOG_NAME
    if not log_path.exists():
        return []
    return [line.split(" ", 1)[1] for line in log_path.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def configured_bin(tmp_path: Path) -> Path:
    """A `shell_path_prepend` directory with a `gh` and a `curl` that only print, so no test reaches GitHub."""
    directory = tmp_path / "configured-bin"
    directory.mkdir()
    for name, body in (("gh", "echo configured-gh"), ("curl", 'echo "curl $*"')):
        executable = directory / name
        executable.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        executable.chmod(0o755)
    return directory


@pytest.mark.asyncio
async def test_a_plain_command_loses_the_github_tokens(
    make_shell: Callable[..., _Shell],
    plugin_registry: HookRegistry,
    runtime_paths: RuntimePaths,
) -> None:
    """A command that is not brokered gets neither MindRoom's GitHub tokens nor the proxy session, nor an audit line."""
    shell = make_shell()

    without_plugin = await _run(shell, HookRegistry.empty(), f"{_PRINT_TOKENS}echo plain", runtime_paths)
    with_plugin = await _run(shell, plugin_registry, f"{_PRINT_TOKENS}echo plain", runtime_paths)

    assert without_plugin == "github-token|gh-token|unset|plain"
    assert with_plugin == "unset|unset|unset|plain"
    assert _audit_records(runtime_paths) == []


@pytest.mark.asyncio
async def test_a_brokered_gh_call_runs_the_shim_ahead_of_shell_path_prepend(
    make_shell: Callable[..., _Shell],
    plugin_registry: HookRegistry,
    configured_bin: Path,
    runtime_paths: RuntimePaths,
) -> None:
    """`gh` resolves to the plugin's shim even when a `shell_path_prepend` directory holds another `gh`.

    The shim then calls `curl` through the proxy with the session, the command gets no GitHub token, and the audit log
    records the call.
    """
    shell = make_shell(shell_path_prepend=str(configured_bin))

    without_plugin = await _run(shell, HookRegistry.empty(), "gh api /user", runtime_paths)
    with_plugin = await _run(shell, plugin_registry, f"{_PRINT_TOKENS}gh api /user", runtime_paths)
    github_token, gh_token, session, curl = with_plugin.split("|")

    assert without_plugin == "configured-gh"
    assert (github_token, gh_token, session) == ("unset", "unset", "session-token")
    assert curl.startswith("curl ")
    assert "--proxy https://127.0.0.1:14322 " in curl
    assert "--proxy-header Proxy-Authorization: Bearer session-token " in curl
    assert curl.endswith(" https://api.github.com/user")
    assert _audit_records(runtime_paths) == ["shell success api.github.com"]


@pytest.mark.asyncio
async def test_a_session_that_cannot_be_refreshed_after_approval_declines_the_command(
    make_shell: Callable[..., _Shell],
    plugin_registry: HookRegistry,
    configured_bin: Path,
    runtime_paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The session expires while the call waits for approval and Agent Vault cannot mint a new one.

    The call is refused with the plugin's reason instead of running without the proxy: the command never runs, no
    worker request is sent, and the audit log records nothing, since no call went through the proxy.
    """
    shell = make_shell(shell_path_prepend=str(configured_bin))
    marker = tmp_path / "command-ran"
    asked: list[str] = []

    def vault_is_down(*_args: object, **_kwargs: object) -> SessionToken:
        msg = "agent-vault is down"
        raise RuntimeError(msg)

    async def approve_once_the_session_expired(
        _origin: BackgroundScriptToolOrigin,
        tool_name: str,
        _arguments: dict[str, object],
    ) -> ToolApprovalDecision:
        asked.append(tool_name)
        monkeypatch.setattr(hooks, "_session_token", SessionToken(value="session-token", expires_at=time.time()))
        monkeypatch.setattr(hooks, "mint_proxy_session_token", vault_is_down)
        return ToolApprovalDecision(approved=True)

    outcome = await _call(
        shell,
        plugin_registry,
        f"touch {marker}; gh api /user",
        runtime_paths,
        approval_gate=approve_once_the_session_expired,
    )

    assert asked == ["run_shell_command"]
    assert outcome.is_error
    assert outcome.content.startswith("[TOOL CALL DECLINED]\n")
    assert f"\nReason: {hooks.SESSION_UNAVAILABLE}\n" in outcome.content
    assert not marker.exists()
    assert shell.sent == []
    assert _audit_records(runtime_paths) == []


@pytest.mark.asyncio
async def test_concurrent_brokered_calls_of_one_turn_each_record_their_own_host(
    make_shell: Callable[..., _Shell],
    make_plugin_registry: Callable[..., HookRegistry],
    configured_bin: Path,
    runtime_paths: RuntimePaths,
    tmp_path: Path,
) -> None:
    """Two brokered calls of one turn share its correlation id, and each one's audit line still names its own host.

    The first command waits until the second has started, so both calls have run their hooks before either finishes.
    """
    shell = make_shell(shell_path_prepend=str(configured_bin))
    registry = make_plugin_registry(brokered_hosts=["api.github.com", "uploads.github.com"])
    second_started = tmp_path / "second-started"
    os.mkfifo(second_started)
    # Held open by the test, so neither command blocks opening the FIFO and the signal waits until it is read.
    held = os.open(second_started, os.O_RDWR)
    try:
        with bind_llm_request_log_context(correlation_id="turn-1"):
            first, second = await _calls(
                shell,
                registry,
                [
                    f"read -r -t 20 _ < {second_started} || exit 1; gh api /user",
                    f"echo started > {second_started}; curl https://uploads.github.com/repos",
                ],
                runtime_paths,
            )
    finally:
        os.close(held)

    assert not first.is_error, first.content
    assert not second.is_error, second.content
    assert sorted(_audit_records(runtime_paths)) == [
        "shell success api.github.com",
        "shell success uploads.github.com",
    ]
