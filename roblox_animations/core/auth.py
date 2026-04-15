"""
OAuth 2.0 PKCE authentication for accessing Roblox assets.

Flow (mirrors the Roblox Blender Plugin):
  1. Generate PKCE verifier + challenge (SHA-256, base64url).
  2. Generate random CSRF state.
  3. Open browser to the Roblox authorization URL.
  4. Start a local HTTP server on localhost:8181 in a background thread.
    5. User approves in browser → Roblox redirects to either localhost or a hosted callback page.
    6. The local callback handler exchanges the auth-code + verifier for tokens using the same redirect URI that started the flow.
  7. Timer callback on the main thread stores the tokens in memory for this session.
  8. get_auth_headers() returns a Bearer token for asset requests.
      If the access token is expired it is silently refreshed via the in-memory refresh token.
  9. On logout the refresh token is revoked and all local state is cleared.

Usage:
    from roblox_animations.core.auth import get_auth_headers, is_logged_in

    headers = get_auth_headers()          # {} when not authenticated
    if headers:
        fetch_private_asset(asset_id, extra_headers=headers)
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from base64 import urlsafe_b64encode
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, HTTPServer
from secrets import token_urlsafe
from typing import Optional
from urllib.parse import parse_qs, urlencode, urlparse

# ---------------------------------------------------------------------------
# OAuth endpoints (Roblox production)
# ---------------------------------------------------------------------------

_AUTH_URL = "https://apis.roblox.com/oauth/v1/authorize"
_TOKEN_URL = "https://apis.roblox.com/oauth/v1/token"
_REVOKE_URL = "https://apis.roblox.com/oauth/v1/token/revoke"
_REDIRECT_PATH = "/oauth2/callback"
_PORT = 8181
_HOSTED_REDIRECT_ENV = "RBX_OAUTH_REDIRECT_URI"
# Asset delivery is documented under the legacy asset management scope even
# though other Assets API endpoints use asset:read / asset:write.
_SCOPES = "openid asset:read legacy-asset:manage"
_CODE_LENGTH = 128
_CLIENT_ID = "4633080443763556453"
_STATE_LENGTH = 128

# ---------------------------------------------------------------------------
# In-memory token store
# ---------------------------------------------------------------------------


class _TokenStore:
    def __init__(self) -> None:
        self.access_token: str = ""
        self.expires_at: float = 0.0
        self.refresh_token: str = ""

    @property
    def is_valid(self) -> bool:
        # 30-second safety margin so we refresh before expiry
        return bool(self.access_token) and time.time() < (self.expires_at - 30)

    def clear(self) -> None:
        self.access_token = ""
        self.expires_at = 0.0
        self.refresh_token = ""


_store = _TokenStore()

# ---------------------------------------------------------------------------
# Login background-thread state
# ---------------------------------------------------------------------------

_login_done: threading.Event = threading.Event()
_login_cancel: threading.Event = threading.Event()
_login_result: dict = {}
_login_thread: Optional[threading.Thread] = None

# ---------------------------------------------------------------------------
# PKCE / crypto helpers
# ---------------------------------------------------------------------------


def generate_pkce_pair() -> tuple:
    """Returns (code_verifier, code_challenge) per the PKCE spec (S256)."""
    verifier = token_urlsafe(96)[:_CODE_LENGTH]
    challenge = (
        urlsafe_b64encode(sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    return verifier, challenge


def generate_state() -> str:
    """Returns a cryptographically random state string for CSRF protection."""
    return token_urlsafe(96)[:_STATE_LENGTH]


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _token_post(url: str, data: dict) -> dict:
    """POST application/x-www-form-urlencoded to a token endpoint, return JSON."""
    encoded = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=encoded,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            err = json.loads(body.decode("utf-8"))
            desc = err.get("error_description") or err.get("error") or str(exc)
        except Exception:
            desc = str(exc)
        raise RuntimeError(f"Token request failed ({exc.code}): {desc}") from exc


# ---------------------------------------------------------------------------
# Runtime auth accessors  (always call from the Blender main thread)
# ---------------------------------------------------------------------------


def _get_addon_package() -> str:
    return __package__.rsplit(".", 1)[0] if "." in __package__ else __package__


def _get_window_manager():
    try:
        import bpy  # noqa: PLC0415

        return bpy.context.window_manager
    except Exception:
        return None


def _get_client_id() -> str:
    return _CLIENT_ID


def _get_local_redirect_uri() -> str:
    return f"http://localhost:{_PORT}{_REDIRECT_PATH}"


def get_oauth_redirect_uri() -> str:
    configured = os.environ.get(_HOSTED_REDIRECT_ENV, "").strip()
    return configured or _get_local_redirect_uri()


def get_api_key() -> str:
    """Returns the session API key, or empty string if not set."""
    window_manager = _get_window_manager()
    if window_manager and hasattr(window_manager, "rbx_api_key_session"):
        return window_manager.rbx_api_key_session.strip()
    return ""


# ---------------------------------------------------------------------------
# Token lifecycle (main-thread only)
# ---------------------------------------------------------------------------


def _apply_token_response(token_data: dict) -> None:
    """Store tokens from a successful token response in memory.  Main thread only."""
    _store.access_token = token_data.get("access_token", "")
    _store.expires_at = time.time() + float(token_data.get("expires_in", 3600))
    new_rt = token_data.get("refresh_token")
    if new_rt:
        _store.refresh_token = new_rt


def _refresh_tokens(refresh_token: str) -> None:
    """Synchronous token refresh.  Main thread only (makes a network call)."""
    client_id = _get_client_id()
    if not client_id:
        raise ValueError("No Roblox OAuth client ID configured.")
    token_data = _token_post(
        _TOKEN_URL,
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        },
    )
    _apply_token_response(token_data)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def is_logged_in() -> bool:
    """True if we have a valid access token or a session refresh token."""
    if _store.is_valid:
        return True
    return bool(_store.refresh_token)


def is_login_in_progress() -> bool:
    """True while the browser-based login flow is running."""
    return (
        _login_thread is not None
        and _login_thread.is_alive()
        and not _login_done.is_set()
    )


def get_auth_headers() -> dict:
    """
    Returns request headers for authenticated asset access, else ``{}``.

    Priority:
      1. OAuth Bearer token (auto-refreshed if expired)
      2. API key via ``x-api-key`` header

    Call only from the Blender main thread.
    """
    # --- OAuth Bearer ---
    if _store.is_valid:
        return {"Authorization": f"Bearer {_store.access_token}"}

    refresh_token = _store.refresh_token
    if refresh_token:
        try:
            _refresh_tokens(refresh_token)
            if _store.is_valid:
                return {"Authorization": f"Bearer {_store.access_token}"}
        except Exception as exc:
            print(f"[RbxAuth] Token refresh failed: {exc}")

    # --- API key fallback ---
    api_key = get_api_key()
    if api_key:
        return {"x-api-key": api_key}

    return {}


def logout() -> None:
    """Revoke tokens server-side and clear all local state.  Main thread only."""
    refresh_token = _store.refresh_token
    _store.clear()

    if refresh_token:
        try:
            client_id = _get_client_id()
            if client_id:
                _token_post(
                    _REVOKE_URL,
                    {"client_id": client_id, "token": refresh_token},
                )
        except Exception:
            pass  # best-effort revoke; tokens are already cleared locally


def cancel_login() -> None:
    """Signal the background login thread to stop waiting."""
    _login_cancel.set()


# ---------------------------------------------------------------------------
# OAuth callback HTTP server
# ---------------------------------------------------------------------------


class _CallbackHandler(BaseHTTPRequestHandler):
    """Minimal HTTP handler that receives the OAuth2 redirect."""

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != _REDIRECT_PATH:
            self._send(404, "Not found")
            return

        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}

        # CSRF check
        if params.get("state") != self.server._expected_state:
            _login_result["error"] = "State mismatch — possible CSRF attack."
            self._send(400, "State mismatch. Please try logging in again.")
            self.server._done.set()
            return

        # Authorization server error
        error = params.get("error")
        if error:
            desc = params.get("error_description", error)
            _login_result["error"] = desc
            self._send(400, f"Authorization error: {desc}")
            self.server._done.set()
            return

        code = params.get("code")
        if not code:
            _login_result["error"] = "No authorization code in callback."
            self._send(400, "No authorization code received.")
            self.server._done.set()
            return

        # Exchange code for tokens
        try:
            redirect_uri = self.server._redirect_uri
            token_data = _token_post(
                _TOKEN_URL,
                {
                    "grant_type": "authorization_code",
                    "code": code,
                    "client_id": self.server._client_id,
                    "code_verifier": self.server._verifier,
                    "redirect_uri": redirect_uri,
                },
            )
            _login_result["token_data"] = token_data
            self._send(200, "Authorization complete. You can close this window.")
        except Exception as exc:
            _login_result["error"] = str(exc)
            self._send(500, f"Token exchange failed: {exc}")
        finally:
            self.server._done.set()

    def _send(self, code: int, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args) -> None:  # silence server output
        pass


def _login_worker(
    client_id: str,
    verifier: str,
    state: str,
    redirect_uri: str,
    server_ready: threading.Event,
) -> None:
    """Background thread: start the callback server and wait for the redirect."""
    done_event = threading.Event()

    try:
        server = HTTPServer(("localhost", _PORT), _CallbackHandler)
    except OSError as exc:
        _login_result["error"] = (
            f"Could not bind to localhost:{_PORT} — {exc}. "
            "Is another app using that port?"
        )
        server_ready.set()  # unblock caller so it can report the error
        _login_done.set()
        return

    server._expected_state = state
    server._client_id = client_id
    server._verifier = verifier
    server._redirect_uri = redirect_uri
    server._done = done_event
    server.timeout = 1.0  # poll interval so we can detect cancellation

    server_ready.set()  # server is bound — safe to open the browser now

    try:
        while not done_event.is_set() and not _login_cancel.is_set():
            server.handle_request()
    finally:
        server.server_close()
        _login_done.set()


def _login_timer_callback() -> Optional[float]:
    """
    Called by ``bpy.app.timers`` on the Blender main thread every 0.5 s.
    Returns ``None`` to unregister once the login flow completes.
    """
    if not _login_done.is_set():
        return 0.5  # keep polling

    # Apply result on the main thread
    if "token_data" in _login_result:
        try:
            _apply_token_response(_login_result["token_data"])
            print("[RbxAuth] Login successful.")
        except Exception as exc:
            print(f"[RbxAuth] Failed to store tokens: {exc}")
    elif "error" in _login_result:
        print(f"[RbxAuth] Login failed: {_login_result['error']}")

    # Redraw any VIEW_3D areas so the panel updates
    try:
        import bpy  # noqa: PLC0415

        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == "VIEW_3D":
                    area.tag_redraw()
    except Exception:
        pass

    return None  # unregister


def start_login_async() -> None:
    """
    Start the OAuth PKCE browser login flow.

    Opens the user's browser to the Roblox authorization page and starts a
    background thread to receive the callback.  A ``bpy.app.timers`` callback
    finalises the login on the main thread once the browser redirects back.

    Raises ``ValueError`` if no client ID is configured in addon preferences.
    Raises ``OSError`` (re-raised from binding) only if we cannot start the server
    at all (the error is also stored in ``_login_result["error"]`` for the timer).
    """
    global _login_thread

    client_id = _get_client_id()
    if not client_id:
        raise ValueError("Roblox OAuth client ID is missing (this should not happen).")

    # Cancel any stale login thread so port 8181 is freed before we rebind
    if _login_thread is not None and _login_thread.is_alive():
        _login_cancel.set()
        _login_thread.join(timeout=3.0)

    verifier, challenge = generate_pkce_pair()
    state = generate_state()
    redirect_uri = get_oauth_redirect_uri()

    _login_done.clear()
    _login_cancel.clear()
    _login_result.clear()

    server_ready = threading.Event()

    _login_thread = threading.Thread(
        target=_login_worker,
        args=(client_id, verifier, state, redirect_uri, server_ready),
        daemon=True,
    )
    _login_thread.start()

    # Wait for the server to bind before opening the browser.
    # This prevents the race where roblox redirects back before the server is ready.
    server_ready.wait(timeout=5.0)

    if "error" in _login_result:
        # Server failed to bind — timer will report it; don't open browser
        import bpy  # noqa: PLC0415
        if not bpy.app.timers.is_registered(_login_timer_callback):
            bpy.app.timers.register(_login_timer_callback, first_interval=0.1)
        return

    # Build the browser authorization URL
    auth_url = _AUTH_URL + "?" + urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": _SCOPES,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
    )
    webbrowser.open(auth_url)

    # Register a timer to finalise the login on the main thread
    import bpy  # noqa: PLC0415

    if not bpy.app.timers.is_registered(_login_timer_callback):
        bpy.app.timers.register(_login_timer_callback, first_interval=0.5)
