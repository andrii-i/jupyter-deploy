"""JupyterLab's own HTTP/WebSocket API, as reached through the local proxy.

Jupyter-specific knowledge that the transport helpers in :mod:`.requests` deliberately do not have:
which path is a cheap probe, how to get past the XSRF check, and how a kernel websocket is opened.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Iterator

import requests
import websocket

# A path that reaches JupyterLab through the reverse proxy (so any ForwardAuth middleware runs),
# needs no session state, and answers with a small JSON document.
AUTH_PROBE_PATH = "/api/status"

# The subprotocol JupyterLab negotiates for kernel channels. A proxy that drops the client's
# subprotocol list silently forces JSON framing, which breaks kernel traffic in ways only a running
# notebook reveals.
KERNEL_WS_SUBPROTOCOL = "v1.kernel.websocket.jupyter.org"


def api_session(base_url: str, timeout_seconds: int = 30) -> tuple[requests.Session, dict[str, str]]:
    """Return a session holding Jupyter's ``_xsrf`` cookie, plus the header map writes need.

    Templates that put the auth boundary in front of Jupyter run it with token auth disabled
    (``--IdentityProvider.token=``), so every caller is anonymous — and that is exactly when
    Jupyter's XSRF check *does* apply to non-GET requests. Fetching an **HTML page** is what sets
    the ``_xsrf`` cookie; a JSON API GET does not, which is why an otherwise-correct
    GET-``/api/kernels``-then-POST sequence gets a 403.
    """
    session = requests.Session()
    session.get(f"{base_url}/lab", timeout=timeout_seconds)
    xsrf = session.cookies.get("_xsrf")
    return session, ({"X-XSRFToken": xsrf} if xsrf else {})


def start_kernel(base_url: str, kernel_name: str = "python3", timeout_seconds: int = 60) -> str:
    """Start a kernel through ``base_url`` and return its id.

    Raises:
        requests.HTTPError: If Jupyter refuses the request.
    """
    session, xsrf_headers = api_session(base_url, timeout_seconds=timeout_seconds)
    with session:
        response = session.post(
            f"{base_url}/api/kernels", headers=xsrf_headers, json={"name": kernel_name}, timeout=timeout_seconds
        )
        response.raise_for_status()
        return str(response.json()["id"])


def list_kernel_ids(base_url: str, timeout_seconds: int = 60) -> list[str]:
    """Return the ids of the kernels Jupyter currently reports.

    Raises:
        requests.HTTPError: If Jupyter refuses the request.
    """
    response = requests.get(f"{base_url}/api/kernels", timeout=timeout_seconds)
    response.raise_for_status()
    return [str(kernel["id"]) for kernel in response.json()]


def delete_kernel(base_url: str, kernel_id: str, timeout_seconds: int = 30) -> None:
    """Delete a kernel, ignoring failures (teardown must never mask a test result)."""
    with contextlib.suppress(Exception):
        session, xsrf_headers = api_session(base_url, timeout_seconds=timeout_seconds)
        with session:
            session.delete(f"{base_url}/api/kernels/{kernel_id}", headers=xsrf_headers, timeout=timeout_seconds)


@contextlib.contextmanager
def kernel_websocket(base_url: str, timeout_seconds: int = 30) -> Iterator[websocket.WebSocket]:
    """Start a kernel through ``base_url`` and yield its connected channels websocket.

    ``base_url`` is an origin the app is served at (e.g. the proxy's ``http://127.0.0.1:<port>``).
    The kernel is deleted on the way out.
    """
    session, xsrf_headers = api_session(base_url, timeout_seconds=timeout_seconds)
    with session:
        response = session.post(
            f"{base_url}/api/kernels", headers=xsrf_headers, json={"name": "python3"}, timeout=timeout_seconds
        )
        response.raise_for_status()
        kernel_id = response.json()["id"]
        cookie_header = "; ".join(f"{k}={v}" for k, v in session.cookies.items()) or None

    ws_url = f"{base_url.replace('http://', 'ws://')}/api/kernels/{kernel_id}/channels?session_id={uuid.uuid4().hex}"
    connection = websocket.create_connection(
        ws_url,
        subprotocols=[KERNEL_WS_SUBPROTOCOL],
        timeout=timeout_seconds,
        cookie=cookie_header,
    )
    try:
        yield connection
    finally:
        with contextlib.suppress(Exception):
            connection.close()
        delete_kernel(base_url, kernel_id, timeout_seconds=timeout_seconds)
