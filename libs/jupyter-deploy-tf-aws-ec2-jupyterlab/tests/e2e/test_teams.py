"""E2E tests for `jd teams` — the IAM **role** allowlist, positive and negative, with real creds.

`jd teams` gates assumed-role callers, which is what CI runs as and what the template expects of a
human (SSO, `aws configure sso`). So this is the file that can prove the allowlist end to end in
both directions with the credentials the test process already holds: a role that is on the list
reaches the app, and the same role taken off it does not.

Split out of ``test_auth.py`` on purpose. That file only ever sends requests, so it is fast and
cannot lock anyone out; everything here runs `jd teams`, where each edit is an SSM round-trip plus a
sidecar container recreate (~15-30s) and a mistake between a revoke and its restore would 403 every
later test. Keeping the two apart means a failure here is unambiguously about the allowlist, and a
failure there is unambiguously about a request.

Omitted: covered by the base template suite (`test_org_and_teams.py`)
  GitHub org/team membership semantics and the "cannot remove the last user while no org is
  allowlisted" interlock. Neither exists here: authorization is by IAM role name within one AWS
  account, with no org concept and no such interlock.
"""

import pytest
from pytest_jupyter_deploy.deployment import EndToEndDeployment
from pytest_jupyter_deploy.local_proxy import LocalProxyApplication

# Every test here edits the live allowlist, so the snapshot/restore net is mandatory: a failure
# between a revoke and its restore would 403 every later test in the file.
pytestmark = pytest.mark.usefixtures("restore_allowlist")

# A name no real role will have, for CRUD assertions that must not touch the caller's own access.
_PROBE_TEAM = "JupyterDeployE2ETeamsProbe"


def _require_role_caller(caller_principal: tuple[str, str]) -> str:
    """Return the caller's role name, failing if the caller is not an assumed role.

    A hard failure, not a skip. An IAM-user caller is not a valid configuration for this template —
    the expected identity is an assumed role (SSO, `aws configure sso`, CI) — so running as one is a
    credentials misconfiguration, and skipping would turn it into a green run with the positive and
    negative role-authorization assertions silently absent.
    """
    kind, name = caller_principal
    assert kind == "role", (
        f"These tests must run as an assumed role, but the caller is an IAM {kind} ({name!r}). "
        "`jd teams` gates roles only; re-run with role credentials."
    )
    return name


# --------------------------------------------------------------------------- the allowlist itself


def test_caller_is_allowlisted_after_deploy(
    e2e_deployment: EndToEndDeployment, caller_principal: tuple[str, str]
) -> None:
    """The deploying identity is on the allowlist with no manual step.

    The template's usability claim: AWS credentials are the ONLY prerequisite. If the deployer were
    not auto-allowlisted, a fresh `jd up` would produce a deployment its own creator is locked out
    of — and the only way back in would be an SSM command they do not know to run.
    """
    kind, name = caller_principal
    allowlisted = e2e_deployment.get_allowlisted_teams() if kind == "role" else e2e_deployment.get_allowlisted_users()

    assert name.lower() in [entry.lower() for entry in allowlisted], (
        f"The deploying {kind} {name!r} is not on the allowlist ({allowlisted}); "
        "a fresh deployment would lock out its own creator"
    )


def test_teams_list_matches_the_terraform_variable(e2e_deployment: EndToEndDeployment) -> None:
    """`jd teams list` and ``iam_role_names_allowlist`` agree — no split-brain.

    The allowlist has two representations: the live ``/etc/AUTH_ALLOWLIST`` on the instance (what
    `jd teams list` reads over SSM) and the terraform variable (what the next `jd up` renders). They
    must never drift, or an apply would silently revert access changes.
    """
    e2e_deployment.ensure_deployed()

    live = sorted(name.lower() for name in e2e_deployment.get_allowlisted_teams())
    in_state = sorted(name.lower() for name in e2e_deployment.get_list_str_variable_value("iam_role_names_allowlist"))

    assert live == in_state, f"`jd teams list` says {live} but terraform state says {in_state}"


def test_teams_add_remove_set_roundtrip(
    e2e_deployment: EndToEndDeployment,
    client_proxy_app: LocalProxyApplication,
    caller_principal: tuple[str, str],
) -> None:
    """`jd teams` add/remove/set round-trip, each writing back to the terraform variable.

    The write-back is the part worth pinning: each command echoes the new list, which the manifest
    runner stores into ``iam_role_names_allowlist``. Without it the live system and terraform state
    diverge, and the next `jd up` silently reverts the change.

    Ends by opening JupyterLab in a real browser: `set` is the destructive verb, so the check that
    matters after it is not "the list reads back correctly" but "the caller can still get in".
    """
    e2e_deployment.ensure_server_running()
    original = e2e_deployment.get_allowlisted_teams()

    try:
        e2e_deployment.cli.run_command(["jupyter-deploy", "teams", "add", _PROBE_TEAM])
        assert _PROBE_TEAM in e2e_deployment.get_allowlisted_teams()
        assert _PROBE_TEAM in e2e_deployment.get_list_str_variable_value("iam_role_names_allowlist"), (
            "teams add did not write back to iam_role_names_allowlist; state has diverged"
        )

        e2e_deployment.cli.run_command(["jupyter-deploy", "teams", "remove", _PROBE_TEAM])
        assert _PROBE_TEAM not in e2e_deployment.get_allowlisted_teams()
        assert _PROBE_TEAM not in e2e_deployment.get_list_str_variable_value("iam_role_names_allowlist")

        # `set` replaces the list wholesale. Keep the caller on it when the caller is a role,
        # otherwise this would revoke the running test's own access.
        kind, caller_name = caller_principal
        target = [_PROBE_TEAM, caller_name] if kind == "role" else [_PROBE_TEAM]
        e2e_deployment.cli.run_command(["jupyter-deploy", "teams", "set", *target])
        assert sorted(n.lower() for n in e2e_deployment.get_allowlisted_teams()) == sorted(n.lower() for n in target)

        client_proxy_app.verify_jupyterlab_accessible()
    finally:
        if original:
            e2e_deployment.cli.run_command(["jupyter-deploy", "teams", "set", *original])
        else:
            e2e_deployment.ensure_no_teams_allowlisted()


def test_revoke_then_restore_caller_access(
    e2e_deployment: EndToEndDeployment,
    client_proxy_app: LocalProxyApplication,
    caller_principal: tuple[str, str],
) -> None:
    """The headline auth test: reachable -> remove the caller -> 403 -> add it back -> reachable.

    Every edge is asserted **through the browser on the loopback proxy URL**, which is the whole
    point: it exercises the chain a user actually has — proxy, pinned TLS, Traefik ForwardAuth,
    JupyterLab rendering — where a crafted request with a hand-minted token would only prove the
    sidecar answered a status code.

    The requests therefore carry the **proxy's own** token, the one already in use, so the revoke
    also probes the sidecar's positive cache: it caches positive results keyed on the token but
    re-checks the allowlist on every cache hit rather than caching the allow/deny decision. A cached
    *decision* would leave a revoked identity with up to a minute of access, and that is the bug
    this pins.

    Asserted ONCE per edge, with no polling: `jd teams` recreates the sidecar behind
    ``docker compose up --wait``, so the command does not return until the new decision is live. A
    retry loop here could only hide a regression in that guarantee.

    Deliberately one test, not three: the sequence is atomic, so it cannot leave the deployment
    locked out even if an assertion fails mid-way (``restore_allowlist`` is the second net).

    Note the asymmetry between the two directions. ``verify_app_status(403)`` is the right assertion
    for a revoke because a browser cannot otherwise distinguish "forbidden" from "did not load",
    while ``verify_jupyterlab_accessible()`` is the right one for a restore because a 200 on
    ``/api/status`` is necessary but not sufficient evidence that the user got their app back.
    """
    name = _require_role_caller(caller_principal)
    e2e_deployment.ensure_server_running()

    client_proxy_app.verify_jupyterlab_accessible()

    e2e_deployment.cli.run_command(["jupyter-deploy", "teams", "remove", name])
    client_proxy_app.verify_app_status(403)

    e2e_deployment.cli.run_command(["jupyter-deploy", "teams", "add", name])
    client_proxy_app.verify_jupyterlab_accessible()


def test_allowlist_match_is_case_insensitive(
    e2e_deployment: EndToEndDeployment,
    client_proxy_app: LocalProxyApplication,
    caller_principal: tuple[str, str],
) -> None:
    """A case-flipped allowlist entry still authorizes the caller.

    IAM names are unique per account regardless of case, so the sidecar lower-cases both sides.
    Asserting it at the sidecar (not just at the shell script's dedup) matters because a
    case-sensitive match would deny a caller whose ARN casing differs from what a human typed.
    """
    name = _require_role_caller(caller_principal)
    e2e_deployment.ensure_server_running()
    original = e2e_deployment.get_allowlisted_teams()
    flipped = name.swapcase()
    assert flipped != name, f"{name!r} has no case to flip; this test cannot assert anything"

    e2e_deployment.cli.run_command(["jupyter-deploy", "teams", "set", flipped])
    try:
        client_proxy_app.verify_jupyterlab_accessible()
    finally:
        e2e_deployment.cli.run_command(["jupyter-deploy", "teams", "set", *original])


def test_allowlist_is_case_insensitive_but_preserves_casing(e2e_deployment: EndToEndDeployment) -> None:
    """`remove datascience` clears a stored `DataScience`, and stored casing is never rewritten.

    Both halves are load-bearing and pull in opposite directions. Case-insensitive compare is needed
    so a remove cannot silently no-op while access stays granted. Preserving the stored casing is
    needed because terraform re-renders the caller in its ARN casing on every `jd up`: folding the
    stored list to lower case would make the write-back differ from terraform's rendered list and
    restart the whole stack on every run.
    """
    original = e2e_deployment.get_allowlisted_teams()
    mixed_case = "DataScienceE2EProbe"

    try:
        e2e_deployment.cli.run_command(["jupyter-deploy", "teams", "add", mixed_case])
        assert mixed_case in e2e_deployment.get_allowlisted_teams(), "Stored casing was not preserved on add"

        # Re-adding the same name in a different case must not create a second entry, and must not
        # rewrite the casing already stored.
        e2e_deployment.cli.run_command(["jupyter-deploy", "teams", "add", mixed_case.lower()])
        stored = e2e_deployment.get_allowlisted_teams()
        assert stored.count(mixed_case) == 1, f"Re-adding in another case duplicated the entry: {stored}"
        assert mixed_case.lower() not in stored, f"Re-adding in another case rewrote the stored casing: {stored}"

        e2e_deployment.cli.run_command(["jupyter-deploy", "teams", "remove", mixed_case.lower()])
        assert mixed_case not in e2e_deployment.get_allowlisted_teams(), (
            "A lower-case remove silently no-oped against a mixed-case entry — access would stay granted"
        )
    finally:
        if mixed_case in e2e_deployment.get_allowlisted_teams():
            e2e_deployment.cli.run_command(["jupyter-deploy", "teams", "remove", mixed_case])
        if original and sorted(original) != sorted(e2e_deployment.get_allowlisted_teams()):
            e2e_deployment.cli.run_command(["jupyter-deploy", "teams", "set", *original])


def test_allowlist_change_does_not_restart_jupyter(e2e_deployment: EndToEndDeployment) -> None:
    """An allowlist change recreates only the sidecar; JupyterLab keeps running.

    Access management must not cost the user their session. ``update-allowlist.sh`` recreates exactly
    ``auth-sidecar``, so a marker written into the jupyter container's own filesystem (``/tmp`` is
    container-local — only ``/home/jovyan`` is a volume) survives the change. If the whole stack were
    recreated the marker would vanish, and so would every running kernel.
    """
    e2e_deployment.ensure_server_running()
    marker = "/tmp/jd-e2e-allowlist-marker"

    e2e_deployment.cli.run_command(["jupyter-deploy", "server", "exec", "--", "touch", marker])
    try:
        e2e_deployment.cli.run_command(["jupyter-deploy", "teams", "add", _PROBE_TEAM])
        e2e_deployment.cli.run_command(["jupyter-deploy", "teams", "remove", _PROBE_TEAM])

        result = e2e_deployment.cli.run_exec_with_retry(
            ["jupyter-deploy", "server", "exec", "--", "test", "-f", marker, "&&", "echo", "MARKER_PRESENT"]
        )
        assert "MARKER_PRESENT" in result.stdout, (
            "The jupyter container was recreated by an allowlist change; running kernels would be lost"
        )
    finally:
        e2e_deployment.cli.run_command(["jupyter-deploy", "server", "exec", "--", "rm", "-f", marker])


# Defined last on purpose: it runs `jd config` (plan only) and asserts the plan is empty, so every
# test above that edits the allowlist must have restored it first.
def test_allowlist_write_back_leaves_no_terraform_diff(e2e_deployment: EndToEndDeployment) -> None:
    """After CLI allowlist changes, `jd config` plans no changes — no split-brain.

    The allowlist is edited over SSM at runtime AND rendered by terraform from a variable, so the
    write-back has to reproduce terraform's exact rendering (sorted, original casing). If it did not,
    every `jd up` would see a diff and recreate the stack — turning a routine apply into an outage,
    and silently reverting the user's access changes.
    """
    e2e_deployment.ensure_deployed()

    e2e_deployment.cli.run_command(["jupyter-deploy", "config"])
    history = e2e_deployment.cli.run_command(["jupyter-deploy", "history", "show", "config", "-l", "200"])

    assert "No changes." in history.stdout, (
        "`jd config` planned changes after CLI-only allowlist edits: the write-back does not match "
        f"terraform's rendering, so the next `jd up` would revert them.\n{history.stdout[-2000:]}"
    )
