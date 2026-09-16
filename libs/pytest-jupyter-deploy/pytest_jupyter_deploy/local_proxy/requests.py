"""Talking to a proxied upstream directly, over the same pinned TLS the proxy uses.

Two things live here, both about the transport rather than the app:

1. **A pinned-TLS client that is not the proxy.** When the assertion is about the upstream's own
   answer — an auth sidecar's status code, the cert it serves — interposing the real proxy adds a
   moving part that can mask exactly what is under test. These helpers make the test the client:
   a stdlib HTTPS connection trusting exactly the pinned PEM, mirroring the proxy's own posture
   (``server/tls.py``: pinned CA, ``check_hostname=False``, because the pin is on the cert and the
   upstream is dialed by a raw IP the cert was never issued for).
2. **A proxy driven from a fixed bundle**, for the cases that must go *through* the proxy with
   headers a real ``connect-info`` would never mint.

Deliberately stdlib ``http.client`` rather than ``requests`` for the pinned calls: ``requests`` /
``urllib3`` layer their own CA bundle and their own hostname match on top of a supplied
``ssl_context``, which would quietly *widen* the very pin under assertion.
"""

from __future__ import annotations

import contextlib
import hashlib
import http.client
import json
import re
import ssl
import subprocess
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

# The console script the CLI's [proxy] extra installs.
PROXY_CONSOLE_SCRIPT = "jupyter-deploy-client-proxy"


# --------------------------------------------------------------------------- pinned TLS client


def pinned_context(cert_pem: str) -> ssl.SSLContext:
    """Build an SSL context trusting exactly ``cert_pem`` and nothing else.

    ``check_hostname`` is off for the same reason the proxy turns it off: the pin is on the cert,
    not the address. A self-signed instance cert is issued for a fixed name (e.g.
    ``DNS:jupyter-deploy, IP:127.0.0.1``), never for the public IP it is reached at, so verifying
    the hostname would always fail.

    Raises:
        ssl.SSLError: If the PEM cannot be loaded.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_verify_locations(cadata=cert_pem)
    return context


def pinned_request(
    host: str,
    port: int,
    cert_pem: str,
    path: str,
    headers: dict[str, str] | None = None,
    timeout_seconds: int = 15,
) -> tuple[int, str]:
    """Issue one request to ``host:port`` over TLS pinned to ``cert_pem``; return ``(status, body)``."""
    conn = http.client.HTTPSConnection(host, port, timeout=timeout_seconds, context=pinned_context(cert_pem))
    try:
        conn.request("GET", path, headers=headers or {})
        response = conn.getresponse()
        return response.status, response.read().decode("utf-8", errors="replace")
    finally:
        conn.close()


def unverified_request(host: str, port: int, path: str, timeout_seconds: int = 15) -> tuple[int, str]:
    """Issue one request with TLS verification disabled; return ``(status, body)``.

    For asserting that reaching the door is not getting in: an open port plus a completed handshake
    must still be refused by the application-layer auth.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE

    conn = http.client.HTTPSConnection(host, port, timeout=timeout_seconds, context=context)
    try:
        conn.request("GET", path, headers={})
        response = conn.getresponse()
        return response.status, response.read().decode("utf-8", errors="replace")
    finally:
        conn.close()


def system_trust_request(host: str, port: int, path: str, timeout_seconds: int = 15) -> tuple[int, str]:
    """Issue one request verified against the system trust store; return ``(status, body)``.

    Exists to be *rejected*: a self-signed upstream must not validate against public CAs, which is
    why the pin is transported out-of-band rather than discovered on the wire.

    Raises:
        ssl.SSLCertVerificationError: If the cert does not chain to system trust (the normal case).
    """
    conn = http.client.HTTPSConnection(host, port, timeout=timeout_seconds, context=ssl.create_default_context())
    try:
        conn.request("GET", path, headers={})
        response = conn.getresponse()
        return response.status, response.read().decode("utf-8", errors="replace")
    finally:
        conn.close()


# --------------------------------------------------------------------------- cert inspection


def served_cert_pem(host: str, port: int, timeout_seconds: int = 15) -> str:
    """Return the PEM of the cert ``host:port`` actually serves.

    Fetched WITHOUT verification on purpose — the point is to compare what is served against the
    PEM published out-of-band, which is what the pin trusts.
    """
    return ssl.get_server_certificate((host, port), timeout=timeout_seconds)


def cert_fingerprint(cert_pem: str) -> str:
    """Return the SHA-256 fingerprint of a PEM certificate (hex, lowercase)."""
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert_pem)).hexdigest()


# --------------------------------------------------------------------------- proxy from a bundle


@contextlib.contextmanager
def static_bundle_proxy(bundle: dict, startup_timeout_seconds: int = 20) -> Iterator[str]:
    """Run a client proxy against a fixed ``bundle`` and yield its loopback origin.

    For asserting how the proxy *relays* an upstream answer, rather than what the upstream decides.
    ``--token-command`` is re-exec'd on refresh, so a ``cat`` of a static file is a bundle that
    never rotates — which is what lets a test pin headers a real mint would never produce.

    Raises:
        RuntimeError: If the proxy exits early or never reports a listening port.
    """
    with tempfile.TemporaryDirectory(prefix="jd-e2e-static-bundle-") as tmp:
        bundle_path = Path(tmp) / "bundle.json"
        bundle_path.write_text(json.dumps(bundle))
        log_dir = Path(tmp) / "logs"
        log_dir.mkdir()

        process = subprocess.Popen(
            [
                PROXY_CONSOLE_SCRIPT,
                "--token-command",
                f"cat {bundle_path}",
                "--listen-port",
                "0",
                "--log-dir",
                str(log_dir),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            yield f"http://127.0.0.1:{_await_listening_port(process, startup_timeout_seconds)}"
        finally:
            process.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=10)


def _await_listening_port(process: subprocess.Popen, timeout_seconds: int) -> int:
    """Read the proxy's "listening on http://127.0.0.1:<port>" line; return the port.

    Raises:
        RuntimeError: If the proxy exits first or the line does not arrive in time.
    """
    deadline = time.monotonic() + timeout_seconds
    assert process.stdout is not None
    while time.monotonic() < deadline:
        line = process.stdout.readline()
        if not line:
            raise RuntimeError(f"Client proxy exited before listening (code {process.poll()}).")
        match = re.search(r"listening on http://127\.0\.0\.1:(\d+)", line)
        if match:
            return int(match.group(1))
    raise RuntimeError(f"Client proxy did not report a listening port within {timeout_seconds}s.")
