# ruff: noqa: SLF001
"""Tests for the agent-vault-bridge plugin hooks."""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = Path("/srv/mindroom/src")
for path in (PLUGIN_ROOT, SRC_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from mindroom.constants import RuntimePaths

from agent_vault_bridge_test_import import hooks
from agent_vault_bridge_test_import.vault_client import SessionToken


def _settings() -> hooks.VaultSettings:
    return hooks.VaultSettings(session_ttl_seconds=600)


def _seed_token(monkeypatch: pytest.MonkeyPatch, value: str = "session-token") -> None:
    monkeypatch.setattr(hooks, "_session_token", SessionToken(value=value, expires_at=time.time() + 600))


def test_gh_api_call_builds_proxy_env_and_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_token(monkeypatch)
    monkeypatch.setenv("PATH", "/usr/bin")

    plan = hooks.execution_plan_for_call("run_shell_command", {"args": ["gh", "api", "/user"]}, _settings())

    assert plan.routed
    assert plan.upstream_host == "api.github.com"
    assert plan.env_overlay["HTTPS_PROXY"] == "https://127.0.0.1:14322"
    assert plan.env_overlay["REQUESTS_CA_BUNDLE"] == "/etc/ssl/agent-vault-ca.pem"
    assert plan.env_overlay["CURL_CA_BUNDLE"] == "/etc/ssl/agent-vault-ca.pem"
    assert plan.env_overlay["SSL_CERT_FILE"] == "/etc/ssl/agent-vault-ca.pem"
    assert plan.env_overlay["AGENT_VAULT_PROXY_TOKEN"] == "session-token"
    assert plan.env_overlay["PATH"].split(":")[0] == str(PLUGIN_ROOT / "bin")


def test_gh_api_inside_bash_command_is_routed(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_token(monkeypatch)

    plan = hooks.execution_plan_for_call(
        "run_shell_command",
        {"args": ["bash", "-lc", "echo GH_USER_START; gh api /user | jq -c '{login,id}'"]},
        _settings(),
    )

    assert plan.routed
    assert plan.upstream_host == "api.github.com"


def test_secret_env_is_stripped_without_routing(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_token(monkeypatch)
    plan = hooks.execution_plan_for_call(
        "run_shell_command",
        {"args": ["curl", "https://example.com/"]},
        _settings(),
    )

    env = hooks.env_with_plan({"GITHUB_TOKEN": "secret", "GH_TOKEN": "secret", "PATH": "/usr/bin"}, plan)

    assert plan.gated
    assert not plan.routed
    assert "GITHUB_TOKEN" not in env
    assert "GH_TOKEN" not in env
    assert env["PATH"] == "/usr/bin"


def test_curl_to_github_is_routed(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_token(monkeypatch)
    plan = hooks.execution_plan_for_call(
        "run_shell_command",
        {"args": ["curl", "https://api.github.com/user"]},
        _settings(),
    )

    assert plan.routed
    assert plan.upstream_host == "api.github.com"
    assert "AGENT_VAULT_PROXY_TOKEN" in hooks.extra_passthrough_with_plan(None, plan)


@pytest.mark.asyncio
async def test_before_hook_marks_brokered_call_for_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_token(monkeypatch)
    ctx = SimpleNamespace(
        tool_name="run_shell_command",
        arguments={"args": ["gh", "api", "/user"], "GITHUB_TOKEN": "secret"},
        settings={},
        correlation_id="call-1",
        decline=MagicMock(),
    )

    await hooks.prepare_brokered_tool_call(ctx)

    assert ctx.arguments[hooks.AUDIT_HOST_ARGUMENT] == "api.github.com"
    assert ctx.arguments[hooks.AUDIT_ROUTED_ARGUMENT] is True
    assert hooks._audit_host_by_correlation["call-1"] == "api.github.com"
    assert "GITHUB_TOKEN" not in ctx.arguments
    ctx.decline.assert_not_called()


@pytest.mark.asyncio
async def test_after_hook_writes_sanitized_audit_line(tmp_path: Path) -> None:
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
        arguments={hooks.AUDIT_HOST_ARGUMENT: "api.github.com"},
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


def test_shell_subprocess_env_patch_strips_github_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    from mindroom.tools import shell

    _seed_token(monkeypatch)
    monkeypatch.setenv("PATH", "/usr/bin")
    plan = hooks.execution_plan_for_call("run_shell_command", {"args": ["gh", "api", "/user"]}, _settings())

    with hooks._scoped_local_shell_plan(plan):
        env = shell._shell_subprocess_env(
            {"GITHUB_TOKEN": "secret", "GH_TOKEN": "secret", "PATH": "/usr/bin"},
            base_process_env={},
        )

    assert env["AGENT_VAULT_PROXY_TOKEN"] == "session-token"
    assert env["PATH"].split(":")[0] == str(PLUGIN_ROOT / "bin")
    assert "GITHUB_TOKEN" not in env
    assert "GH_TOKEN" not in env
