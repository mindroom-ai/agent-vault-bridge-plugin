# Agent Vault Bridge

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Docs](https://img.shields.io/badge/docs-plugins-blue)](https://docs.mindroom.chat/plugins/)
[![Hooks](https://img.shields.io/badge/docs-hooks-blue)](https://docs.mindroom.chat/hooks/)

<img src="https://media.githubusercontent.com/media/mindroom-ai/mindroom/refs/heads/main/frontend/public/logo.png" alt="MindRoom Logo" align="right" width="120" />

Route outbound API calls from [MindRoom](https://github.com/mindroom-ai/mindroom) agent shell tools through a local [Infisical Agent Vault](https://github.com/Infisical/agent-vault) so secrets never enter the agent process.

The plugin gates `run_shell_command` invocations that target brokered hosts (e.g. `api.github.com`), strips known credential env vars from the subprocess, sets up a `Proxy-Authorization` session token, and proxies the call via Agent Vault. The agent sees the response but never the credential. A short audit line is appended per call: `<ISO-timestamp> shell <success|fail> <upstream-host>` — token values are never logged.

## Features

- Intercepts `run_shell_command` calls that resolve to a brokered upstream host (default: `api.github.com`)
- Strips `GITHUB_TOKEN`, `GH_TOKEN`, and similar secrets from the subprocess env
- Mints short-TTL `proxy`-role session tokens from Agent Vault and supplies them via `Proxy-Authorization` to the local HTTPS proxy
- Ships a tiny `gh-via-broker` shell wrapper that adapts `gh api <endpoint>` to the proxy + auth header convention
- Writes a one-line audit record per brokered call to `<workspace>/agent-vault-bridge.log` (no tokens, no bodies)
- Idempotent — safe to hot-reload while MindRoom is running

## Architecture

```
agent's run_shell_command(args=["gh","api","/user"])
        │
        ▼
agent-vault-bridge (tool:before_call)
   ├── strip GITHUB_TOKEN / GH_TOKEN from env
   ├── inject HTTPS_PROXY=https://127.0.0.1:14322
   ├── inject CA_BUNDLE → /etc/ssl/agent-vault-ca.pem
   ├── inject PATH prefix with gh-via-broker shim
   └── inject AGENT_VAULT_PROXY_TOKEN=<minted session>
        │
        ▼
gh-via-broker shim → curl --proxy-header "Proxy-Authorization: Bearer …"
        │
        ▼
Agent Vault HTTPS proxy (127.0.0.1:14322)
   ├── verifies session token
   ├── attaches Authorization: Bearer <real GITHUB_TOKEN from vault>
   └── forwards to api.github.com
        │
        ▼
agent receives response; agent-vault-bridge (tool:after_call) appends audit line
```

## Hooks

| Hook | Event | Purpose |
|------|-------|---------|
| `prime_agent_vault_session` | `bot:ready` | Mint a long-TTL proxy session token at startup; cache and reuse |
| `prepare_brokered_tool_call` | `tool:before_call` | Detect brokered host, strip secrets, set proxy + token env, prepend shim PATH |
| `audit_brokered_tool_call` | `tool:after_call` | Append one sanitized audit line to `<agent-workspace>/agent-vault-bridge.log` |

## Configuration

`plugin.yaml`:

```yaml
name: agent-vault-bridge
enabled: true
settings:
  vault_api: http://127.0.0.1:14321         # Agent Vault management API
  vault_proxy: https://127.0.0.1:14322      # Agent Vault HTTPS proxy
  ca_path: /etc/ssl/agent-vault-ca.pem      # Trust anchor for the proxy
  session_ttl_seconds: 86400                # 24 h proxy session
  brokered_hosts: [api.github.com]          # Upstream hosts gated by the plugin
  gated_tools: [shell]                      # Tool names whose calls may be brokered
  bootstrap:
    method: docker_exec                     # How to obtain a proxy session token
    container: agent-vault                  # Docker container name running Agent Vault
```

Two bootstrap methods are supported:

- `docker_exec` (default) — mint short-TTL session tokens via `docker exec <container> agent-vault vault token --role proxy`. Requires the Agent Vault container to run on the same host as MindRoom.
- `token_file` — read a long-lived **proxy-role agent token** from a local file. Use this when Agent Vault runs on another machine: create the agent once with `agent-vault agent create <name> --vault <vault>:proxy --token-only` and store the printed token (mode 0600). The file is re-read every `session_ttl_seconds`, so rotating the token on disk needs no restart. A proxy-role token can exercise credentials through the proxy but can never read them.

```yaml
  bootstrap:
    method: token_file
    token_file: ~/.config/agent-vault/proxy-token
```

To broker additional services, add the host to `brokered_hosts` and add the corresponding service + credential to your Agent Vault instance.

## Prerequisites

1. **A reachable Agent Vault** (local or remote) with the target service registered and a credential set. The management API and HTTPS proxy ports must be directly reachable from the MindRoom host — the proxy speaks HTTP CONNECT, which cannot sit behind a normal reverse proxy.
2. **CA cert** fetched once (`agent-vault ca fetch --address <api-url>`) and placed at the configured `ca_path` (mode 0644).
3. **A token source**: either a local Docker container named `agent-vault` (`bootstrap.method: docker_exec`), or a proxy-role agent token file (`bootstrap.method: token_file`) for remote vaults.

A NixOS module that wires all three is available at `mindroom/configs/nixos/optional/agent-vault.nix`.

## Install

Vendor this plugin with the MindRoom CLI:

```bash
mindroom plugins install agent-vault-bridge-plugin
```

Then reference it from `config.yaml`:

```yaml
plugins:
  - path: plugins/agent-vault-bridge-plugin
```

Update to the latest commit later with:

```bash
mindroom plugins update agent-vault-bridge-plugin
```

The command pins the exact installed commit in `.mindroom-plugin.lock.json` and strictly validates the plugin before activating it.
It requires a MindRoom release newer than v2026.7.175.
For a manual checkout instead, see Setup below.

## Setup

1. Copy this plugin to `~/.mindroom-chat/plugins/agent-vault-bridge`.
2. Add the plugin to `config.yaml`:
   ```yaml
   plugins:
     - path: plugins/agent-vault-bridge
   ```
3. MindRoom hot-reloads on config change — no restart required.

## Limitations and Known Trade-offs

- The plugin currently monkey-patches private MindRoom internals (`mindroom.tool_system.tool_hooks._call_tool`, `mindroom.tool_system.sandbox_proxy._call_proxy_sync`, `mindroom.tools.shell._shell_subprocess_env`) because `ToolBeforeCallContext` does not yet expose an env-injection surface. Patches are sentinel-guarded for idempotency.
- The `gh-via-broker` shim only handles the `gh api <endpoint>` form. Other `gh` subcommands fall through with a clear stderr message.
- Hosts not on `brokered_hosts` are not gated; agents can still call them directly without the proxy.
- Bootstrap uses `docker exec` to mint session tokens — this requires the MindRoom process to have rights to run `docker exec` against the `agent-vault` container.

## License

[MIT](LICENSE) — Bas Nijholt.