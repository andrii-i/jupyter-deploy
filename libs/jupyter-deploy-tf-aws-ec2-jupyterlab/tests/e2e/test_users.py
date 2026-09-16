"""E2E tests for `jd users` — the IAM **user** allowlist.

Asymmetric with ``test_teams.py`` on purpose, and the asymmetry is the honest state of the coverage:

- The **CRUD surface is fully exercised** — `add`, `list`, `remove`, `set` — including the write-back
  into ``iam_user_names_allowlist`` that keeps the live system and terraform state in step.
- The **positive auth path is not**, and cannot be from here: proving that a token minted by an
  IAM-user principal is authorized needs a real IAM user's credentials, and tests must not create IAM
  principals. Doing it properly means a pre-provisioned user with no policies attached (a credential
  that can do nothing but ``GetCallerIdentity``) and its access key in Secrets Manager — a
  CI-template change, not a test-time one. Tabled; the expected caller for this template is an
  assumed role.
- One **negative auth test** stands in for it, and it is the one that matters most: a role name
  sitting on the *user* allowlist authorizes nothing.

Every mutating test additionally opens **JupyterLab in a real browser through the proxy** afterwards.
That is the real risk of editing this list: `jd users` and `jd teams` write to the same
``/etc/AUTH_ALLOWLIST`` and recreate the same container, so a bug in the users path could revoke a
role caller's access as a side effect — and the user editing it would have no reason to suspect the
connection. Asserting it through the browser rather than with a hand-minted token is deliberate: it
exercises the flow a user actually has (proxy -> pinned TLS -> ForwardAuth -> JupyterLab renders),
where a status code from a crafted request only proves the sidecar answered.
"""

import pytest
from pytest_jupyter_deploy.deployment import EndToEndDeployment
from pytest_jupyter_deploy.local_proxy import LocalProxyApplication

# Every test here edits the live allowlist, so the snapshot/restore net is mandatory.
pytestmark = pytest.mark.usefixtures("restore_allowlist")


# IAM user names no real user will have. Lower-case: unlike the role probes, these also stand in for
# the conventional shape of a username.
_PROBE_USER = "jupyter-deploy-e2e-probe"
_OTHER_PROBE_USER = "jupyter-deploy-e2e-probe-two"


# --------------------------------------------------------------------------- CRUD


def test_users_list_matches_the_terraform_variable(e2e_deployment: EndToEndDeployment) -> None:
    """`jd users list` and ``iam_user_names_allowlist`` agree — no split-brain.

    Read-only, so it is also the cheap canary: if this disagrees, one of the mutating tests below
    left the live file and terraform state out of step.
    """
    e2e_deployment.ensure_deployed()

    live = sorted(name.lower() for name in e2e_deployment.get_allowlisted_users())
    in_state = sorted(name.lower() for name in e2e_deployment.get_list_str_variable_value("iam_user_names_allowlist"))

    assert live == in_state, f"`jd users list` says {live} but terraform state says {in_state}"


def test_users_add(
    e2e_deployment: EndToEndDeployment,
    client_proxy_app: LocalProxyApplication,
) -> None:
    """`jd users add` puts a name on the list, writes it back, and leaves role access untouched."""
    e2e_deployment.ensure_server_running()
    original = e2e_deployment.get_allowlisted_users()

    try:
        e2e_deployment.cli.run_command(["jupyter-deploy", "users", "add", _PROBE_USER])

        assert _PROBE_USER in e2e_deployment.get_allowlisted_users()
        assert _PROBE_USER in e2e_deployment.get_list_str_variable_value("iam_user_names_allowlist"), (
            "users add did not write back to iam_user_names_allowlist; state has diverged"
        )
        client_proxy_app.verify_jupyterlab_accessible()
    finally:
        _restore_users(e2e_deployment, original)


def test_users_remove(
    e2e_deployment: EndToEndDeployment,
    client_proxy_app: LocalProxyApplication,
) -> None:
    """`jd users remove` takes a name off the list, writes it back, and leaves role access untouched."""
    e2e_deployment.ensure_server_running()
    original = e2e_deployment.get_allowlisted_users()

    try:
        e2e_deployment.cli.run_command(["jupyter-deploy", "users", "add", _PROBE_USER])
        assert _PROBE_USER in e2e_deployment.get_allowlisted_users(), "Setup failed: the probe user was not added"

        e2e_deployment.cli.run_command(["jupyter-deploy", "users", "remove", _PROBE_USER])

        assert _PROBE_USER not in e2e_deployment.get_allowlisted_users()
        assert _PROBE_USER not in e2e_deployment.get_list_str_variable_value("iam_user_names_allowlist"), (
            "users remove did not write back to iam_user_names_allowlist; the next `jd up` would re-add it"
        )
        client_proxy_app.verify_jupyterlab_accessible()
    finally:
        _restore_users(e2e_deployment, original)


def test_users_set(
    e2e_deployment: EndToEndDeployment,
    client_proxy_app: LocalProxyApplication,
) -> None:
    """`jd users set` replaces the list wholesale, writes it back, and leaves role access untouched.

    `set` is the destructive verb — it drops names the caller did not mention — so the "role access
    survives" check matters most here: a `set` that reached into the roles section would lock the
    caller out of a deployment they were only reorganising user access on.
    """
    e2e_deployment.ensure_server_running()
    original = e2e_deployment.get_allowlisted_users()
    target = [_PROBE_USER, _OTHER_PROBE_USER]

    try:
        e2e_deployment.cli.run_command(["jupyter-deploy", "users", "set", *target])

        assert sorted(n.lower() for n in e2e_deployment.get_allowlisted_users()) == sorted(n.lower() for n in target), (
            "users set did not replace the list with exactly the given names"
        )
        assert sorted(
            n.lower() for n in e2e_deployment.get_list_str_variable_value("iam_user_names_allowlist")
        ) == sorted(n.lower() for n in target), "users set did not write the replaced list back"
        client_proxy_app.verify_jupyterlab_accessible()
    finally:
        _restore_users(e2e_deployment, original)


def _restore_users(e2e_deployment: EndToEndDeployment, original: list[str]) -> None:
    """Put the user allowlist back to ``original`` (``set`` for a non-empty list, clear otherwise)."""
    if original:
        e2e_deployment.cli.run_command(["jupyter-deploy", "users", "set", *original])
    else:
        e2e_deployment.ensure_no_users_allowlisted()


# --------------------------------------------------------------------------- negative auth


def test_role_name_on_the_user_allowlist_is_not_authorized(
    e2e_deployment: EndToEndDeployment,
    caller_principal: tuple[str, str],
    client_proxy_app: LocalProxyApplication,
) -> None:
    """The role and user allowlists must not cross-match on name.

    ``allows()`` switches on the principal *kind* STS reports, so a role name sitting on the user
    allowlist authorizes nothing. Without the switch, `jd users add SomeRoleName` would silently
    grant a role access through the wrong list — the two allowlists would collapse into one
    namespace, and an operator managing user access could hand out role access by accident.

    This is the only auth assertion in this file, and it stands in for the positive IAM-user path
    that needs a pre-provisioned user (see the module docstring).
    """
    kind, name = caller_principal
    assert kind == "role", (
        f"This test must run as an assumed role, but the caller is an IAM {kind} ({name!r}). "
        "It is this file's ONLY auth assertion, so skipping it would leave the user allowlist with "
        "no authorization coverage at all; re-run with role credentials."
    )

    client_proxy_app.verify_jupyterlab_accessible()

    e2e_deployment.cli.run_command(["jupyter-deploy", "teams", "remove", name])
    e2e_deployment.cli.run_command(["jupyter-deploy", "users", "add", name])
    try:
        # 403, not "did not load": a role name sitting on the USER allowlist must authorize nothing.
        client_proxy_app.verify_app_status(403)
    finally:
        e2e_deployment.cli.run_command(["jupyter-deploy", "users", "remove", name])
        e2e_deployment.cli.run_command(["jupyter-deploy", "teams", "add", name])
    client_proxy_app.verify_jupyterlab_accessible()
