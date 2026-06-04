# Agent Vault Bridge

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Docs](https://img.shields.io/badge/docs-plugins-blue)](https://docs.mindroom.chat/plugins/)
[![Hooks](https://img.shields.io/badge/docs-hooks-blue)](https://docs.mindroom.chat/hooks/)

<img src="https://media.githubusercontent.com/media/mindroom-ai/mindroom/refs/heads/main/frontend/public/logo.png" alt="MindRoom Logo" align="right" width="120" />

Route selected [MindRoom](https://github.com/mindroom-ai/mindroom) shell calls through [Infisical Agent Vault](https://github.com/Infisical/agent-vault) so agents can use brokered API access without receiving the real credential.

Agent Vault runs as a credential broker and forward proxy. This plugin detects configured upstream hosts, strips selected secret environment variables from the shell subprocess, injects proxy settings, and records a short audit line after each brokered call.

## Features

- Intercepts `run_shell_command` calls that target brokered upstream hosts
- Routes `gh api ...` and `curl ...api.github.com...` calls through Agent Vault
- Strips `GITHUB_TOKEN` and `GH_TOKEN` from the shell environment before brokered calls run
- Mints and reuses a proxy-role Agent Vault session token
- Prepends plugin shims for `gh` and `curl` so common GitHub calls use the broker
- Writes one sanitized audit line per brokered call to `<workspace>/agent-vault-bridge.log`
- Keeps hooks idempotent so MindRoom hot reloads do not stack duplicate patches

## How It Works

1. An agent invokes `run_shell_command`.
2. The `agent-vault-bridge-before` hook checks whether the shell call targets a configured host such as `api.github.com`.
3. For matching calls, the plugin removes GitHub token variables, injects proxy and CA environment variables, and prepends this plugin's `bin/` directory to `PATH`.
4. The `gh` or `curl` shim sends the request through Agent Vault with `Proxy-Authorization: Bearer <session-token>`.
5. Agent Vault validates the session, attaches the real credential server-side, and forwards the request upstream.
6. The `agent-vault-bridge-after` hook writes a sanitized audit entry with timestamp, shell status, and upstream host.

## Hooks

| Hook | Event | Purpose |
|------|-------|---------|
| `agent-vault-bridge-ready` | `bot:ready` | Mint and cache a proxy session token when MindRoom starts |
| `agent-vault-bridge-before` | `tool:before_call` | Detect brokered shell calls, strip secrets, and inject proxy settings |
| `agent-vault-bridge-after` | `tool:after_call` | Append a sanitized audit line for brokered calls |

## Configuration

Plugin settings in `config.yaml`:

| Setting | Required | Description |
|---------|----------|-------------|
| `vault_api` | No | Agent Vault management API. Defaults to `http://127.0.0.1:14321` |
| `vault_proxy` | No | Agent Vault forward proxy. Defaults to `https://127.0.0.1:14322` |
| `ca_path` | No | CA bundle path trusted by brokered clients. Defaults to `/etc/ssl/agent-vault-ca.pem` |
| `session_ttl_seconds` | No | Proxy session TTL requested from Agent Vault. Defaults to `86400` |
| `brokered_hosts` | No | Upstream hosts that should be brokered. Defaults to `api.github.com` |
| `gated_tools` | No | Tool names eligible for brokering. Defaults to `shell` |
| `bootstrap.method` | No | Session-token bootstrap method. Currently only `docker_exec` |
| `bootstrap.container` | No | Agent Vault container name for `docker exec`. Defaults to `agent-vault` |

Example:

```yaml
plugins:
  - path: plugins/agent-vault-bridge
    settings:
      vault_api: http://127.0.0.1:14321
      vault_proxy: https://127.0.0.1:14322
      ca_path: /etc/ssl/agent-vault-ca.pem
      brokered_hosts:
        - api.github.com
      gated_tools:
        - shell
      bootstrap:
        method: docker_exec
        container: agent-vault
```

## Setup

1. Run Agent Vault where MindRoom can reach it.
2. Register the upstream service and credential in Agent Vault.
3. Fetch the Agent Vault CA certificate and place it at the configured `ca_path`.
4. Copy this plugin to the MindRoom profile's plugin directory, for example `~/.mindroom/plugins/agent-vault-bridge`.
5. Add the plugin to `config.yaml`.
6. Restart MindRoom, or rely on hot reload if the active runtime supports it.

For MindRoom NixOS hosts, Agent Vault service wiring lives outside this plugin repo. In the current MindRoom infrastructure, that module is in `~/dotfiles/configs/nixos/optional/agent-vault.nix`.

## Docker Sandbox Runner

MindRoom can route code-execution tools to a Docker-hosted sandbox runner with `MINDROOM_WORKER_BACKEND=static_runner`. In that mode the primary MindRoom process decides whether a shell call should be brokered, then forwards the tool call to the runner over the sandbox-runner API. The runner executes the shell command in the container.

For Agent Vault Bridge, Docker sandboxing works when all of these are true:

- the primary MindRoom process can mint an Agent Vault proxy session token
- the sandbox runner can reach the Agent Vault proxy listener
- the Agent Vault CA certificate is mounted inside the sandbox runner at `ca_path`
- the plugin's `bin/` directory is visible inside the runner at the same path used by the primary process
- `shell` is routed through the sandbox proxy for the agents that should use Docker execution

Docker Compose shape:

```yaml
services:
  agent-vault:
    image: infisical/agent-vault:latest
    container_name: agent-vault
    ports:
      - "127.0.0.1:14321:14321"
      - "127.0.0.1:14322:14322"
    volumes:
      - agent-vault-data:/data
    environment:
      - AGENT_VAULT_MASTER_PASSWORD=${AGENT_VAULT_MASTER_PASSWORD}
      - AGENT_VAULT_ADDR=http://agent-vault:14321

  mindroom:
    image: ghcr.io/mindroom-ai/mindroom:latest
    env_file: .env
    volumes:
      - ./config.yaml:/app/config.yaml:ro
      - ./plugins:/app/plugins:ro
      - ./mindroom_data:/app/mindroom_data
      - ./agent-vault-ca.pem:/etc/ssl/agent-vault-ca.pem:ro
      - /var/run/docker.sock:/var/run/docker.sock
    environment:
      - MINDROOM_WORKER_BACKEND=static_runner
      - MINDROOM_SANDBOX_PROXY_URL=http://sandbox-runner:8766
      - MINDROOM_SANDBOX_PROXY_TOKEN=${MINDROOM_SANDBOX_PROXY_TOKEN}
      - MINDROOM_SANDBOX_EXECUTION_MODE=selective
      - MINDROOM_SANDBOX_PROXY_TOOLS=shell,file,python

  sandbox-runner:
    image: ghcr.io/mindroom-ai/mindroom:latest
    command: ["/app/run-sandbox-runner.sh"]
    user: "1000:1000"
    volumes:
      - ./config.yaml:/app/config.yaml:ro
      - ./plugins:/app/plugins:ro
      - ./agent-vault-ca.pem:/etc/ssl/agent-vault-ca.pem:ro
      - sandbox-workspace:/app/workspace
    environment:
      - MINDROOM_SANDBOX_RUNNER_MODE=true
      - MINDROOM_SANDBOX_PROXY_TOKEN=${MINDROOM_SANDBOX_PROXY_TOKEN}
      - MINDROOM_CONFIG_PATH=/app/config.yaml
      - MINDROOM_STORAGE_PATH=/app/workspace/.mindroom

volumes:
  agent-vault-data:
  sandbox-workspace:
```

Plugin config for that Compose topology:

```yaml
plugins:
  - path: plugins/agent-vault-bridge
    settings:
      vault_api: http://agent-vault:14321
      vault_proxy: http://agent-vault:14322
      ca_path: /etc/ssl/agent-vault-ca.pem
      brokered_hosts:
        - api.github.com
      gated_tools:
        - shell
      bootstrap:
        method: docker_exec
        container: agent-vault
```

Fetch the CA once after Agent Vault is running:

```bash
curl -fsSL http://127.0.0.1:14321/v1/mitm/ca.pem > agent-vault-ca.pem
```

The current `docker_exec` bootstrap method runs `docker exec agent-vault ...` from the primary MindRoom runtime. If the primary runtime is itself a container, it needs both a Docker CLI binary and access to the host Docker socket. If the MindRoom image you run does not include the Docker CLI, build a small derived image that adds it, or use a future API/file/env bootstrap method instead of `docker_exec`.

For host-native MindRoom plus a Docker sandbox runner, keep the same `static_runner` environment variables from the MindRoom sandbox-proxy guide. Set `vault_proxy` to an address reachable from inside the sandbox runner, such as `http://host.docker.internal:14322` on Docker Desktop or an address on a shared Docker network.

Do not mount the primary MindRoom storage tree into the sandbox runner. The runner should get its own scratch workspace and only the config/plugin files needed to register tools and expose this plugin's shell shims.

### Local Smoke Test

This repo includes a self-contained Docker smoke test for the simplified brokered-worker model:

```bash
./local/broker_smoke/smoke.sh
```

The smoke test starts four containers:

- `runner` on an internal worker network with only proxy env
- `adapter` on both worker and egress networks
- `fake-agent-vault` on the egress network, acting like an Agent Vault proxy
- `upstream` on the egress network, echoing request headers

It proves:

- a URL hidden inside `script-with-hidden-url.sh` is brokered without command-line URL matching
- the upstream receives `Authorization: Bearer fake-secret`
- `Proxy-Authorization` does not reach upstream
- runner env does not contain broker or service tokens
- direct runner egress to upstream fails when proxy env is removed

This validates the topology and failure modes before wiring the same adapter pattern to real Agent Vault.

## Kubernetes

Agent Vault can run in Kubernetes as a private service with persistent storage. The [Agent Vault Docker guide](https://docs.agent-vault.dev/self-hosting/docker) documents the image, `/data` volume, `14321` API port, `14322` proxy port, and `/health` endpoint. The shape is:

- `Deployment` or `StatefulSet` running `infisical/agent-vault`
- `PersistentVolumeClaim` mounted at `/data`
- `Secret` holding `AGENT_VAULT_MASTER_PASSWORD`
- `ClusterIP` service exposing `14321` for the management API and `14322` for the proxy
- optional private ingress, VPN, or port-forward access to `14321` for operators
- network policy that lets MindRoom pods reach `14322` and keeps the management API private
- CA certificate mounted into the MindRoom pod at the plugin's `ca_path`

For Kubernetes, the proxy URL will usually be `http://agent-vault.agent-vault.svc.cluster.local:14322`. The `HTTPS_PROXY` environment variable routes HTTPS upstream traffic through Agent Vault; the proxy listener itself is a forward proxy.

Starter manifest:

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: agent-vault
---
apiVersion: v1
kind: Secret
metadata:
  name: agent-vault-master-password
  namespace: agent-vault
type: Opaque
stringData:
  AGENT_VAULT_MASTER_PASSWORD: change-me
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: agent-vault-data
  namespace: agent-vault
spec:
  accessModes: ["ReadWriteOnce"]
  resources:
    requests:
      storage: 2Gi
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: agent-vault
  namespace: agent-vault
spec:
  replicas: 1
  selector:
    matchLabels:
      app: agent-vault
  template:
    metadata:
      labels:
        app: agent-vault
    spec:
      containers:
        - name: agent-vault
          image: infisical/agent-vault:latest
          ports:
            - name: api
              containerPort: 14321
            - name: proxy
              containerPort: 14322
          env:
            - name: AGENT_VAULT_MASTER_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: agent-vault-master-password
                  key: AGENT_VAULT_MASTER_PASSWORD
            - name: AGENT_VAULT_ADDR
              value: http://agent-vault.agent-vault.svc.cluster.local:14321
          readinessProbe:
            httpGet:
              path: /health
              port: api
          volumeMounts:
            - name: data
              mountPath: /data
      volumes:
        - name: data
          persistentVolumeClaim:
            claimName: agent-vault-data
---
apiVersion: v1
kind: Service
metadata:
  name: agent-vault
  namespace: agent-vault
spec:
  type: ClusterIP
  selector:
    app: agent-vault
  ports:
    - name: api
      port: 14321
      targetPort: api
    - name: proxy
      port: 14322
      targetPort: proxy
```

Current plugin limitation: Kubernetes is not drop-in yet because the plugin mints proxy sessions with `docker exec agent-vault ...`. A MindRoom pod normally cannot run `docker exec` against another pod. Pick one of these bootstrap designs before using this plugin in Kubernetes:

1. Add an API bootstrap method that uses an Agent Vault agent token and vault name to mint proxy sessions from the management API.
2. Add a file or environment bootstrap method and rotate the proxy session token with a CronJob or sidecar.
3. Run MindRoom under `agent-vault vault run` instead of using this plugin's bootstrap path, then keep this plugin only for targeted shell shims and audit behavior.

The first option is the clean Kubernetes target because Agent Vault already supports named remote agents and token-based remote runtimes.

When one of those bootstrap paths exists, the MindRoom-side plugin config should point at the service DNS name:

```yaml
plugins:
  - path: plugins/agent-vault-bridge
    settings:
      vault_api: http://agent-vault.agent-vault.svc.cluster.local:14321
      vault_proxy: http://agent-vault.agent-vault.svc.cluster.local:14322
      ca_path: /etc/ssl/agent-vault-ca.pem
      brokered_hosts:
        - api.github.com
```

Pin the Agent Vault image tag, replace the placeholder master password with a real Kubernetes secret, and add network policies before production use.

## Limitations

- The plugin currently monkey-patches private MindRoom internals because `ToolBeforeCallContext` does not expose an env-injection API.
- The bootstrap path only supports `docker_exec`, which requires Docker CLI access from the primary runtime and is not suitable for Kubernetes without an additional bootstrap method.
- The `gh` shim only brokers `gh api <endpoint>`.
- The `curl` shim only brokers calls that include a configured brokered host.
- Hosts not listed in `brokered_hosts` are not routed through Agent Vault.
- Agent Vault protects credentials stored in the vault. Credentials still present in readable files or unrelated environment variables remain outside this plugin's protection.

## License

[MIT](LICENSE) - Bas Nijholt.
