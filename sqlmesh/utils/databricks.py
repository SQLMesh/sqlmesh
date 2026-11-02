from __future__ import annotations

import importlib.util
import re
import typing as t
from functools import lru_cache
from threading import Lock

HeaderFactory = t.Callable[[], t.Dict[str, str]]


class SharedU2MCredentialsProvider:
    """A Databricks SQL connector `credentials_provider` that shares one U2M OAuth login across connections.

    Without this, every `databricks.sql.connect()` call builds its own OAuth provider. For U2M OAuth that means
    each thread runs its own browser flow, and these race on the CSRF `state` parameter
    (https://github.com/tobymao/sqlmesh/issues/5646). Even if the browser flow is only run once, each connection
    would still hold its own copy of the refresh token. Databricks can issue single-use refresh tokens, so the
    first connection to refresh would invalidate the token held by every other connection.

    Instead, a single connector OAuth provider is created lazily (behind a lock, so the browser flow only runs once)
    and every connection reads its headers from it. Reads are also serialized so only one thread can refresh the
    token at a time. This lets each thread have its own connection while sharing a single login.

    If reading the token fails (for example because the refresh token expired or was revoked) the login is
    discarded, so the next connection or request logs in again rather than failing for the rest of the process.
    """

    def __init__(self, server_hostname: str, auth_type: str):
        self._server_hostname = server_hostname
        self._auth_type = auth_type
        self._auth_provider: t.Optional[t.Any] = None
        self._lock = Lock()
        # Incremented when a login attempt finishes, so threads that waited on a failed attempt can tell it happened
        self._login_attempts_finished = 0
        self._login_error: t.Optional[Exception] = None

    def auth_type(self) -> str:
        return self._auth_type

    def __call__(self, *args: t.Any, **kwargs: t.Any) -> HeaderFactory:
        login_attempts_seen = self._login_attempts_finished
        with self._lock:
            self._ensure_logged_in(login_attempts_seen)
        return self._headers

    def _headers(self) -> t.Dict[str, str]:
        headers: t.Dict[str, str] = {}
        login_attempts_seen = self._login_attempts_finished
        with self._lock:
            auth_provider = self._ensure_logged_in(login_attempts_seen)
            try:
                auth_provider.add_headers(headers)
            except Exception:
                self._auth_provider = None
                raise
        return headers

    def _ensure_logged_in(self, login_attempts_seen: int) -> t.Any:
        """Returns the shared connector auth provider, logging in if needed. Must be called with the lock held."""
        if self._auth_provider is not None:
            return self._auth_provider

        # A login attempt failed while this thread was waiting on the lock. Re-raise its error rather than opening
        # another browser window for every waiting thread; callers that arrive afterwards will try again.
        if self._login_attempts_finished != login_attempts_seen and self._login_error is not None:
            raise self._login_error

        try:
            self._auth_provider = self._create_auth_provider()
            self._login_error = None
        except Exception as e:
            self._login_error = e
            raise
        finally:
            self._login_attempts_finished += 1
        return self._auth_provider

    def _create_auth_provider(self) -> t.Any:
        # These are internal to the connector, but using them means the OAuth flow is identical to the one the
        # connector runs when `auth_type` is passed to `connect()` (same client ID, redirect ports, and scopes)
        from databricks.sql import __version__
        from databricks.sql.auth.auth import get_python_sql_connector_auth_provider
        from databricks.sql.common.unified_http_client import UnifiedHttpClient
        from databricks.sql.utils import build_client_context

        # The provider outlives any single connection, so it gets its own HTTP client rather than borrowing one
        # that would be closed along with its connection
        http_client = UnifiedHttpClient(
            build_client_context(self._server_hostname, __version__, user_agent_entry="sqlmesh")
        )
        return get_python_sql_connector_auth_provider(
            self._server_hostname, http_client=http_client, auth_type=self._auth_type
        )


@lru_cache(maxsize=1)
def supports_shared_u2m_login() -> bool:
    """Whether the installed connector has the internals `SharedU2MCredentialsProvider` builds on (4.1.0+)."""
    try:
        return importlib.util.find_spec("databricks.sql.common.unified_http_client") is not None
    except ModuleNotFoundError:
        return False


_shared_u2m_credentials_providers: t.Dict[t.Tuple[str, str], SharedU2MCredentialsProvider] = {}
_shared_u2m_credentials_providers_lock = Lock()


def get_shared_u2m_credentials_provider(
    server_hostname: str, auth_type: str
) -> SharedU2MCredentialsProvider:
    """Returns the process-wide U2M credentials provider for the given workspace and auth type.

    Engine adapters and config copies are created several times per process, so the provider is kept at module
    level to avoid prompting the user to log in more than once.
    """
    # The same workspace can be written with or without a scheme or trailing slash
    normalized_hostname = re.sub(r"^https?://", "", server_hostname.strip(), flags=re.I)
    normalized_hostname = normalized_hostname.rstrip("/").lower()
    key = (normalized_hostname, auth_type)
    with _shared_u2m_credentials_providers_lock:
        if key not in _shared_u2m_credentials_providers:
            _shared_u2m_credentials_providers[key] = SharedU2MCredentialsProvider(
                normalized_hostname, auth_type
            )
        return _shared_u2m_credentials_providers[key]
