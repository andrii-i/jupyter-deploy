"""E2E tests for the `jd proxy` command group and the local proxy's serving behavior.

This template has no public URL: every byte reaches JupyterLab through the local client
proxy, so the proxy IS the access path and gets a file of its own. Nothing here exists in the
base template's suite.

Omitted: covered by the base template suite (`libs/jupyter-deploy-tf-aws-ec2-base/tests/e2e`)
  Nothing — there is no base-template equivalent of any of this.

Deliberately elsewhere:
  - "connect-info / jd open fail cleanly while the host is stopped" lives in ``test_host.py``,
    which owns the suite's single EC2 stop/start cycle. Asserting it here would buy a second
    ~4-minute cycle for one status check.
  - The auth-sidecar's own decisions (401/403) are asserted in ``test_auth.py`` against a
    pinned client rather than through the proxy, which would add a part that can mask the
    status code. The one proxy-side case — that it relays an upstream 401 verbatim instead of
    turning it into a 502 or crashing — is here, because that is proxy behavior.
"""

import json
import time

import pytest
import requests
from pytest_jupyter_deploy.auth_sidecars.aws_auth_sidecar import BINDING_HEADER, TOKEN_PREFIX
from pytest_jupyter_deploy.cli import NOOP_BROWSER, JDCliError
from pytest_jupyter_deploy.deployment import EndToEndDeployment
from pytest_jupyter_deploy.local_proxy import LocalProxyApplication
from pytest_jupyter_deploy.local_proxy.jupyterlab import KERNEL_WS_SUBPROTOCOL, kernel_websocket
from pytest_jupyter_deploy.local_proxy.requests import static_bundle_proxy

# The proxy mints a 60s token and refreshes 15s before expiry, so a wait comfortably past 60s
# guarantees at least one refresh cycle has completed.
_CREDENTIAL_REFRESH_WAIT_SECONDS = 75

# A status.json naming a live-but-foreign PID: `state` and `pid` look running, but
# `process_created_at` cannot match PID 1's real start time, so the record must be treated as
# unconfirmed (possible PID reuse) rather than as this project's proxy.
_FOREIGN_PID_STATUS = {
    "schema_version": 1,
    "state": "running",
    "pid": 1,
    "port": 65000,
    "process_created_at": 0.0,
}
# Sorts after any real launch timestamp (YYYYMMDD-HHMMSS.SSS), so the scan reaches it first.
_FOREIGN_PID_DIR_NAME = "29991231-235959.999"


# --------------------------------------------------------------------------- connect-info


def test_proxy_connect_info_bundle_shape(connect_bundle: dict, deployment_id: str) -> None:
    """`jd proxy connect-info` emits one clean JSON bundle with everything the proxy needs."""
    assert set(connect_bundle) >= {"host", "port", "ca_cert", "headers", "expires_at"}, (
        f"Bundle is missing keys the proxy consumes: {sorted(connect_bundle)}"
    )
    assert connect_bundle["host"], "Bundle has no host; the endpoint did not resolve"
    assert connect_bundle["port"] == 443, f"Expected port 443, got {connect_bundle['port']}"
    assert "BEGIN CERTIFICATE" in connect_bundle["ca_cert"], "ca_cert is not a PEM certificate"

    headers = connect_bundle["headers"]
    assert headers.get("Authorization", "").startswith(f"Bearer {TOKEN_PREFIX}"), (
        f"Authorization is not a {TOKEN_PREFIX} bearer token: {headers.get('Authorization', '')[:32]}…"
    )
    assert headers.get(BINDING_HEADER) == deployment_id, (
        f"Binding header should be the deployment id {deployment_id}, got {headers.get(BINDING_HEADER)}"
    )
    assert connect_bundle["expires_at"], "Bundle has no expires_at; the proxy could not schedule a refresh"


def test_proxy_connect_info_mints_a_fresh_token_each_call(
    e2e_deployment: EndToEndDeployment, connect_bundle: dict
) -> None:
    """Two calls mint two distinct tokens — the rotation the proxy's refresh loop relies on."""
    second = e2e_deployment.cli.get_connect_bundle()

    first_token = connect_bundle["headers"]["Authorization"]
    second_token = second["headers"]["Authorization"]
    assert first_token != second_token, "connect-info returned the same token twice; it is not re-minting"
    assert second_token.startswith(f"Bearer {TOKEN_PREFIX}")
    # Host and pinned cert are properties of the instance, not of the token, so they must not move.
    assert second["host"] == connect_bundle["host"]
    assert second["ca_cert"] == connect_bundle["ca_cert"]


def test_proxy_connect_info_binding_id_matches_deployment_id(connect_bundle: dict, deployment_id: str) -> None:
    """The binding header equals the deployment id — the cross-deployment replay defense is wired.

    The sidecar rejects any token whose ``x-k8s-aws-id`` is not its own ``DEPLOYMENT_ID``, and
    the value is folded into the SigV4 signature at minting time. If the CLI signed a
    different id than the sidecar expects, every request would 401.
    """
    assert connect_bundle["headers"][BINDING_HEADER] == deployment_id


# --------------------------------------------------------------------------- lifecycle


def test_proxy_start_status_show_stop_lifecycle(e2e_deployment: EndToEndDeployment) -> None:
    """start -> status -> show -> stop: the full `jd proxy` lifecycle on one deployment."""
    e2e_deployment.ensure_server_running()
    e2e_deployment.cli.stop_proxy_if_running()

    url = e2e_deployment.cli.start_proxy()
    assert url.startswith("http://127.0.0.1:"), f"Proxy should bind loopback, got: {url}"

    assert e2e_deployment.cli.get_proxy_status() == "running"

    details = e2e_deployment.cli.get_proxy_details()
    assert details["pid"] > 0, f"show reported no pid: {details}"
    assert details["alive"] is True, f"show reported a dead proxy: {details}"
    assert details["running"] is True, f"show reported a non-running proxy: {details}"
    assert isinstance(details["port"], int) and details["port"] > 0
    assert details["log_dir"], "show reported no log dir"

    e2e_deployment.cli.stop_proxy()

    # After a stop there is no proxy to report on at all, so both verbs must fail.
    with pytest.raises(JDCliError):
        e2e_deployment.cli.get_proxy_status()


def test_proxy_start_refuses_to_replace_running_proxy(e2e_deployment: EndToEndDeployment) -> None:
    """A second `jd proxy start` errors instead of clobbering the first.

    Another terminal or browser tab may be using the running proxy, so `start` refuses; only
    `jd open` owns the lifecycle and replaces. The first proxy must survive on the same port.
    """
    e2e_deployment.ensure_server_running()
    e2e_deployment.cli.stop_proxy_if_running()
    e2e_deployment.cli.start_proxy()
    try:
        original_port = e2e_deployment.cli.get_proxy_port()

        with pytest.raises(JDCliError) as exc_info:
            e2e_deployment.cli.run_command(["jupyter-deploy", "proxy", "start"])
        assert "Traceback" not in str(exc_info.value), "Expected a handled error, not a traceback"

        assert e2e_deployment.cli.get_proxy_status() == "running", "The first proxy should have survived"
        assert e2e_deployment.cli.get_proxy_port() == original_port, "The first proxy should still hold its port"
    finally:
        e2e_deployment.cli.stop_proxy_if_running()


def test_proxy_stop_without_running_proxy_fails_gracefully(e2e_deployment: EndToEndDeployment) -> None:
    """`jd proxy stop` with nothing running exits non-zero with a handled error, not a traceback."""
    e2e_deployment.ensure_deployed()
    e2e_deployment.cli.stop_proxy_if_running()

    with pytest.raises(JDCliError) as exc_info:
        e2e_deployment.cli.stop_proxy()
    assert "Traceback" not in str(exc_info.value)


def test_proxy_open_requires_a_running_proxy(e2e_deployment: EndToEndDeployment) -> None:
    """`jd proxy open` with no proxy running fails gracefully — it never starts one.

    `jd proxy open` is a pure open, so the split of responsibilities stays legible: `jd open`
    and `jd proxy start` create proxies, `jd proxy open` only points a browser at one.
    """
    e2e_deployment.ensure_deployed()
    e2e_deployment.cli.stop_proxy_if_running()

    with pytest.raises(JDCliError) as exc_info:
        e2e_deployment.cli.run_command(
            ["jupyter-deploy", "proxy", "open"], env={"BROWSER": NOOP_BROWSER}, timeout_seconds=60
        )
    assert "Traceback" not in str(exc_info.value), "Expected a handled error, not a traceback"


def test_proxy_show_json_is_a_single_clean_document(running_proxy: str, e2e_deployment: EndToEndDeployment) -> None:
    """`jd proxy show --json` is machine-parseable: no rich wrapping, no ANSI, one document.

    A regression guard on the plumbing: the human form goes through ``print_json`` (which
    pretty-prints and colorizes) while ``--json`` must bypass it entirely, because the proxy
    port is read back out of this by tooling.
    """
    result = e2e_deployment.cli.run_command(["jupyter-deploy", "proxy", "show", "--json"])
    raw = result.stdout.strip()

    assert "\x1b[" not in raw, f"--json output carries ANSI escapes: {raw!r}"
    payload = json.loads(raw)  # raises if it is not exactly one JSON document
    assert isinstance(payload, dict)
    assert f"http://127.0.0.1:{payload['port']}" == running_proxy, (
        f"--json reported port {payload['port']}, but the proxy is serving at {running_proxy}"
    )


def test_proxy_status_is_pid_confirmed(e2e_deployment: EndToEndDeployment) -> None:
    """A status file naming a live-but-foreign PID is not reported as a running proxy.

    The PID-reuse guard: the proxy records its process creation time, and a record is only
    "running" when the live PID's creation time matches. Without this, a stale status file
    whose PID has been recycled would both mask a real start and get an unrelated process
    signalled by `jd proxy stop`.
    """
    e2e_deployment.ensure_deployed()
    e2e_deployment.cli.stop_proxy_if_running()

    foreign_dir = e2e_deployment.suite_config.project_dir / ".jd-proxy" / "server" / "default" / _FOREIGN_PID_DIR_NAME
    foreign_dir.mkdir(parents=True, exist_ok=True)
    (foreign_dir / "status.json").write_text(json.dumps(_FOREIGN_PID_STATUS))
    try:
        with pytest.raises(JDCliError) as exc_info:
            e2e_deployment.cli.get_proxy_status()
        assert "Traceback" not in str(exc_info.value)
    finally:
        (foreign_dir / "status.json").unlink(missing_ok=True)
        foreign_dir.rmdir()


# --------------------------------------------------------------------------- serving


def test_proxy_open_opens_jupyterlab_in_a_browser(
    e2e_deployment: EndToEndDeployment, client_proxy_app: LocalProxyApplication
) -> None:
    """`jd proxy open` points a browser at the running proxy and JupyterLab loads.

    The counterpart to `jd open`: same destination, but a pure open — it starts nothing, so it is the
    verb a user reaches for when a proxy is already up (a second tab, or after `jd proxy start`).
    Unlike `jd open` it prints no URL, so the only way to check it opened something usable is to read
    the proxy's port back and load the app there.
    """
    e2e_deployment.ensure_server_running()

    # `client_proxy_app` already started the proxy this opens a tab against.
    client_proxy_app.open_tab_via_cli()
    client_proxy_app.verify_jupyterlab_accessible()


def test_proxy_serves_the_jupyter_api(running_proxy: str) -> None:
    """`GET /api/status` through the proxy answers 200 with JSON — no browser involved.

    Proves the whole chain end to end with nothing to interpret: loopback bind -> pinned TLS
    to the instance -> Traefik -> ForwardAuth allow -> JupyterLab. The proxy injects the
    identity token itself, which is why there is no sign-in step to script.
    """
    response = requests.get(f"{running_proxy}/api/status", timeout=30)

    assert response.status_code == 200, f"Expected 200 through the proxy, got {response.status_code}: {response.text}"
    assert "started" in response.json(), f"Unexpected /api/status payload: {response.text}"


def test_proxy_relays_kernel_websocket_with_v1_subprotocol(running_proxy: str) -> None:
    """A kernel websocket through the proxy negotiates ``v1.kernel.websocket.jupyter.org``.

    Guard for the binary-framing path: the proxy drops the raw ``sec-websocket-protocol``
    header and re-offers the client's list on the upstream leg. If it dropped the list
    instead, JupyterLab would silently fall back to JSON framing and kernel traffic would
    break in ways only a running notebook shows.
    """
    with kernel_websocket(running_proxy) as connection:
        assert connection.getsubprotocol() == KERNEL_WS_SUBPROTOCOL, (
            f"Expected the {KERNEL_WS_SUBPROTOCOL} subprotocol, got {connection.getsubprotocol()!r}"
        )


def test_proxy_survives_credential_refresh(e2e_deployment: EndToEndDeployment) -> None:
    """The proxy keeps serving past the token's lifetime, on the same port, after re-minting.

    The token lives 60s and the proxy refreshes 15s ahead of expiry, so waiting past 60s
    forces at least one refresh. Both halves matter: `expires_at` must advance (it really
    re-ran ``connect-info``) and the port must not (a refresh is not a restart — a rebound
    port would drop every open browser tab and kernel websocket).
    """
    e2e_deployment.ensure_server_running()
    e2e_deployment.cli.stop_proxy_if_running()
    url = e2e_deployment.cli.start_proxy()
    try:
        before = e2e_deployment.cli.get_proxy_details()
        time.sleep(_CREDENTIAL_REFRESH_WAIT_SECONDS)
        after = e2e_deployment.cli.get_proxy_details()

        assert after["expires_at"] != before["expires_at"], (
            f"Credential never refreshed in {_CREDENTIAL_REFRESH_WAIT_SECONDS}s "
            f"(expires_at still {before['expires_at']})"
        )
        assert after["port"] == before["port"], "The proxy rebound its port; a refresh must not restart it"

        response = requests.get(f"{url}/api/status", timeout=30)
        assert response.status_code == 200, f"Proxy stopped serving after refresh: {response.status_code}"
    finally:
        e2e_deployment.cli.stop_proxy_if_running()


def test_proxy_logs_written_and_redact_the_token(running_proxy: str, e2e_deployment: EndToEndDeployment) -> None:
    """The proxy logs to its runtime dir and never writes an identity token there.

    The logs are the only debugging surface for a detached proxy, so they have to exist — and
    they are plain files in the project directory, so a bearer token in them would be a
    long-lived credential leak (and, before #348 ignored ``.jd-proxy/``, one that `jd up`
    uploaded to S3).
    """
    log_dir = e2e_deployment.cli.get_proxy_log_dir()
    log_files = sorted(log_dir.glob("*.log"))
    assert log_files, f"Proxy wrote no log file under {log_dir}"

    for log_file in log_files:
        content = log_file.read_text(errors="replace")
        assert TOKEN_PREFIX not in content, f"{log_file} contains a {TOKEN_PREFIX} token"
    assert any(f.stat().st_size > 0 for f in log_files), f"Proxy log files are all empty under {log_dir}"


def test_proxy_relays_an_upstream_401_verbatim(connect_bundle: dict) -> None:
    """A proxy holding a bogus token surfaces the upstream 401, not a 502 or a crash.

    The one case driven through the real proxy binary rather than a pinned client: the
    sidecar's 401 has to reach the browser as a 401. Masking it as a proxy error (or dying on
    it) would turn "your credentials are not accepted" into "the tunnel is broken", which
    points a user at entirely the wrong problem.
    """
    bogus = dict(connect_bundle)
    bogus["headers"] = {"Authorization": "Bearer not-a-token", BINDING_HEADER: "not-this-deployment"}

    with static_bundle_proxy(bogus) as origin:
        response = requests.get(f"{origin}/api/status", timeout=30)

    assert response.status_code == 401, (
        f"Expected the upstream 401 to be relayed verbatim, got {response.status_code}: {response.text[:200]}"
    )
