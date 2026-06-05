# Agent Vault Bridge

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Docs](https://img.shields.io/badge/docs-plugins-blue)](https://docs.mindroom.chat/plugins/)

<img src="https://media.githubusercontent.com/media/mindroom-ai/mindroom/refs/heads/main/frontend/public/logo.png" alt="MindRoom Logo" align="right" width="120" />

Route MindRoom worker egress through [Infisical Agent Vault](https://github.com/Infisical/agent-vault) without handing the upstream API credential to the agent, shell process, Docker runner, or Kubernetes worker pod.

## Model

The recommended setup uses MindRoom's native `worker_egress_brokers` config:

1. MindRoom routes `shell` or `python` to a worker.
2. MindRoom injects only proxy and CA env into that worker request.
3. The worker sends HTTP(S) traffic to this bridge adapter.
4. The adapter adds `Proxy-Authorization` for Agent Vault.
5. Agent Vault injects the real upstream credential server-side and forwards the request.

There is no command-line URL matching. If a URL is hidden inside a bash script, Python package, or subprocess, the process still uses the proxy env.

## Components

| Component | Purpose |
|-----------|---------|
| `adapter.py` | Small forward-proxy adapter that adds the Agent Vault proxy session header. Supports normal proxy requests and `CONNECT`. |
| `local/broker_smoke/` | Docker smoke harness with fake Agent Vault and fake upstream. |

## MindRoom Config

Use a MindRoom version that supports `worker_egress_brokers`.

```yaml
worker_egress_brokers:
  agent_vault:
    proxy_url: http://agent-vault-bridge-adapter:18080
    ca_bundle: /etc/ssl/agent-vault-ca.pem
    no_proxy: localhost,127.0.0.1,.svc

defaults:
  worker_tools: [shell, python]
  worker_scope: user_agent
  worker_egress_broker: agent_vault

agents:
  code:
    display_name: Code
    tools: [shell, python]
```

Disable the inherited broker for one agent with `worker_egress_broker: false`.

## Adapter

```bash
python -m adapter \
  --host 0.0.0.0 \
  --port 18080 \
  --upstream-proxy-url http://agent-vault:14322 \
  --session-token "$AGENT_VAULT_PROXY_SESSION_TOKEN"
```

Provide `AGENT_VAULT_PROXY_SESSION_TOKEN` through your process manager, Docker secret, or Kubernetes Secret. Token minting stays outside this adapter.

## Docker

Example shape:

```yaml
services:
  agent-vault:
    image: infisical/agent-vault:latest
    container_name: agent-vault
    volumes:
      - agent-vault-data:/data
    environment:
      - AGENT_VAULT_MASTER_PASSWORD=${AGENT_VAULT_MASTER_PASSWORD}

  agent-vault-bridge-adapter:
    image: python:3.13-alpine
    working_dir: /app
    command:
      - python
      - -m
      - adapter
      - --host
      - 0.0.0.0
      - --port
      - "18080"
      - --upstream-proxy-url
      - http://agent-vault:14322
      - --session-token
      - ${AGENT_VAULT_PROXY_SESSION_TOKEN}
    volumes:
      - ./agent-vault-bridge-plugin:/app:ro

  mindroom:
    image: ghcr.io/mindroom-ai/mindroom:latest
    env_file: .env
    volumes:
      - ./config.yaml:/app/config.yaml:ro
      - ./agent-vault-ca.pem:/etc/ssl/agent-vault-ca.pem:ro
    environment:
      - MINDROOM_WORKER_BACKEND=static_runner
      - MINDROOM_SANDBOX_PROXY_URL=http://sandbox-runner:8766
      - MINDROOM_SANDBOX_PROXY_TOKEN=${MINDROOM_SANDBOX_PROXY_TOKEN}
      - MINDROOM_SANDBOX_EXECUTION_MODE=selective
      - MINDROOM_SANDBOX_PROXY_TOOLS=shell,python

  sandbox-runner:
    image: ghcr.io/mindroom-ai/mindroom:latest
    command: ["/app/run-sandbox-runner.sh"]
    volumes:
      - ./config.yaml:/app/config.yaml:ro
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

The worker only needs network access to `agent-vault-bridge-adapter:18080` and the CA path configured in MindRoom. It does not need the Agent Vault session token or upstream API token.

## Kubernetes

Run Agent Vault and the adapter as private services:

- Agent Vault `Deployment` or `StatefulSet` with persistent `/data`.
- Agent Vault `ClusterIP` service exposing management/API and proxy ports.
- Adapter `Deployment` exposing port `18080`.
- MindRoom worker pods receive `HTTP_PROXY`, `HTTPS_PROXY`, and CA env through `worker_egress_brokers`.
- NetworkPolicy lets workers reach only the adapter; adapter reaches Agent Vault; Agent Vault reaches approved upstreams.

MindRoom config usually points at:

```yaml
worker_egress_brokers:
  agent_vault:
    proxy_url: http://agent-vault-bridge-adapter.agent-vault.svc.cluster.local:18080
    ca_bundle: /etc/ssl/agent-vault-ca.pem
    no_proxy: localhost,127.0.0.1,.svc,.cluster.local
```

For production Kubernetes, give the adapter a short-lived session token through a Secret and rotate it outside this process.

## Local Validation

Run unit tests:

```bash
PYTHONPATH=/path/to/mindroom/src pytest tests -q
```

Run Docker smoke:

```bash
./local/broker_smoke/smoke.sh
```

The smoke starts a runner, adapter, fake Agent Vault, and fake upstream. It validates:

- hidden URLs inside scripts still route through the proxy env
- upstream receives the injected fake credential
- `Proxy-Authorization` does not reach upstream
- runner env does not contain broker or service tokens
- direct runner egress fails when proxy env is removed
