import time
import typing as t
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from unittest.mock import patch

import pytest

from sqlmesh.utils import databricks
from sqlmesh.utils.databricks import (
    SharedU2MCredentialsProvider,
    get_shared_u2m_credentials_provider,
)

pytest.importorskip("databricks.sql")


@pytest.fixture(autouse=True)
def clear_shared_providers():
    databricks._shared_u2m_credentials_providers.clear()
    yield
    databricks._shared_u2m_credentials_providers.clear()


class FakeAuthProvider:
    """Records how many threads are reading headers at the same time."""

    def __init__(self) -> None:
        self.active_readers = 0
        self.max_active_readers = 0
        self._counter_lock = Lock()

    def add_headers(self, request_headers: t.Dict[str, str]) -> None:
        with self._counter_lock:
            self.active_readers += 1
            self.max_active_readers = max(self.max_active_readers, self.active_readers)
        # Give other threads a chance to enter if reads are not serialized
        time.sleep(0.01)
        request_headers["Authorization"] = "Bearer token"
        with self._counter_lock:
            self.active_readers -= 1


def test_browser_flow_runs_once_across_threads():
    provider = SharedU2MCredentialsProvider("dbc-test.cloud.databricks.com", "databricks-oauth")
    fake_auth_provider = FakeAuthProvider()
    flow_count = 0

    def slow_browser_flow() -> FakeAuthProvider:
        nonlocal flow_count
        flow_count += 1
        time.sleep(0.1)
        return fake_auth_provider

    with patch.object(provider, "_create_auth_provider", side_effect=slow_browser_flow):
        with ThreadPoolExecutor(max_workers=8) as executor:
            header_factories = list(executor.map(lambda _: provider(), range(8)))

    assert flow_count == 1
    assert all(factory() == {"Authorization": "Bearer token"} for factory in header_factories)


def test_header_reads_are_serialized():
    provider = SharedU2MCredentialsProvider("dbc-test.cloud.databricks.com", "databricks-oauth")
    fake_auth_provider = FakeAuthProvider()

    with patch.object(provider, "_create_auth_provider", return_value=fake_auth_provider):
        header_factory = provider()

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda _: header_factory(), range(32)))

    # Only one thread may read (and so possibly refresh) the token at a time, since refresh tokens can be single-use
    assert fake_auth_provider.max_active_readers == 1


def test_failed_browser_flow_is_retried():
    provider = SharedU2MCredentialsProvider("dbc-test.cloud.databricks.com", "databricks-oauth")
    fake_auth_provider = FakeAuthProvider()

    with patch.object(
        provider,
        "_create_auth_provider",
        side_effect=[RuntimeError("login cancelled"), fake_auth_provider],
    ):
        with pytest.raises(RuntimeError, match="login cancelled"):
            provider()
        assert provider()() == {"Authorization": "Bearer token"}


def test_get_shared_u2m_credentials_provider():
    provider = get_shared_u2m_credentials_provider(
        "dbc-test.cloud.databricks.com", "databricks-oauth"
    )
    assert provider.auth_type() == "databricks-oauth"
    assert (
        get_shared_u2m_credentials_provider("dbc-test.cloud.databricks.com", "databricks-oauth")
        is provider
    )
    assert (
        get_shared_u2m_credentials_provider("dbc-other.cloud.databricks.com", "databricks-oauth")
        is not provider
    )
    assert (
        get_shared_u2m_credentials_provider("dbc-test.cloud.databricks.com", "azure-oauth")
        is not provider
    )


def test_uses_connector_oauth_provider():
    """Runs the real connector auth provider with only the browser flow mocked out.

    The provider relies on connector internals, so this fails if a connector upgrade changes them.
    """
    import jwt

    server_hostname = "dbc-test.cloud.databricks.com"
    access_token = jwt.encode(
        {"exp": int(time.time()) + 3600, "iss": f"https://{server_hostname}/oidc"},
        "a-signing-key-that-is-at-least-32-bytes",
        algorithm="HS256",
    )
    provider = SharedU2MCredentialsProvider(server_hostname, "databricks-oauth")

    with patch(
        "databricks.sql.auth.oauth.OAuthManager.get_tokens",
        return_value=(access_token, "refresh-token"),
    ) as mock_get_tokens:
        header_factory = provider()
        provider()

    mock_get_tokens.assert_called_once()
    assert mock_get_tokens.call_args.kwargs["hostname"] == f"https://{server_hostname}/"
    assert header_factory() == {"Authorization": f"Bearer {access_token}"}
