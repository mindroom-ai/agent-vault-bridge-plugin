#!/bin/sh
set -eu

cd "$(dirname "$0")"

export COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-agent-vault-bridge-smoke}"
TMP_DIR="$(mktemp -d 2>/dev/null || mktemp -d -t 'agent-vault-bridge-smoke')"

cleanup() {
  docker compose -f compose.yaml down -v --remove-orphans >/dev/null 2>&1 || true
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

docker compose -f compose.yaml up -d --quiet-pull

retry() {
  label="$1"
  shift
  attempt=1
  while [ "$attempt" -le 30 ]; do
    if "$@" >"$TMP_DIR/smoke.out" 2>"$TMP_DIR/smoke.err"; then
      return 0
    fi
    attempt=$((attempt + 1))
    sleep 1
  done
  echo "smoke: $label failed" >&2
  cat "$TMP_DIR/smoke.err" >&2 || true
  return 1
}

retry "hidden script brokered request" \
  docker compose -f compose.yaml exec -T runner sh /app/local/broker_smoke/script-with-hidden-url.sh

if ! grep -q '"authorization": "Bearer fake-secret"' "$TMP_DIR/smoke.out"; then
  echo "smoke: upstream did not receive injected fake secret" >&2
  cat "$TMP_DIR/smoke.out" >&2
  exit 1
fi

if grep -qi 'proxy-authorization' "$TMP_DIR/smoke.out"; then
  echo "smoke: proxy authorization leaked to upstream" >&2
  cat "$TMP_DIR/smoke.out" >&2
  exit 1
fi

if docker compose -f compose.yaml exec -T runner sh -lc 'env | grep -E "(TOKEN|VAULT|GITHUB|STRIPE)"'; then
  echo "smoke: runner env exposes broker or service token" >&2
  exit 1
fi

if docker compose -f compose.yaml exec -T runner sh -lc 'unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy NO_PROXY no_proxy; python /app/local/broker_smoke/request_headers.py http://upstream:8080/headers' >"$TMP_DIR/direct.out" 2>"$TMP_DIR/direct.err"; then
  echo "smoke: direct runner request unexpectedly reached upstream" >&2
  cat "$TMP_DIR/direct.out" >&2
  exit 1
fi

echo "smoke: hidden URL brokered, fake secret injected, no token env, direct bypass blocked"
