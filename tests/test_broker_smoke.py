from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from local.broker_smoke.servers import start_adapter, start_fake_agent_vault, start_header_echo


def _fetch(url: str, *, proxy_url: str | None = None) -> dict[str, object]:
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url} if proxy_url else {}),
    )
    with opener.open(url, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def test_adapter_brokers_hidden_url_without_exposing_session_token() -> None:
    with (
        start_header_echo() as upstream,
        start_fake_agent_vault(
            required_proxy_token="adapter-session",
            injected_authorization="Bearer fake-secret",
        ) as fake_vault,
        start_adapter(
            upstream_proxy_url=fake_vault.proxy_url,
            session_token="adapter-session",
        ) as adapter,
    ):
        data = _fetch(upstream.url("/headers"), proxy_url=adapter.proxy_url)

    headers = data["headers"]
    assert isinstance(headers, dict)
    assert headers["authorization"] == "Bearer fake-secret"
    assert "proxy-authorization" not in headers


def test_fake_agent_vault_rejects_requests_without_proxy_authorization() -> None:
    with (
        start_header_echo() as upstream,
        start_fake_agent_vault(
            required_proxy_token="adapter-session",
            injected_authorization="Bearer fake-secret",
        ) as fake_vault,
    ):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _fetch(upstream.url("/headers"), proxy_url=fake_vault.proxy_url)

    assert exc_info.value.code == 407
