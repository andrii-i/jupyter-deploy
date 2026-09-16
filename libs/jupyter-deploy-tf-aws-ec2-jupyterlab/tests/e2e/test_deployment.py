"""E2E test for the full deployment lifecycle from scratch (jupyterlab template).

Mirrors the base template's test_deployment, but reaches JupyterLab through the local
client proxy (no public URL, no OAuth) rather than the GitHub OAuth2 proxy. These tests
only run when deploying from scratch (the ``full_deployment`` marker); pass
``--with-full-deployment`` to include them against an existing project.
"""

import pytest
from pytest_jupyter_deploy.deployment import EndToEndDeployment
from pytest_jupyter_deploy.local_proxy import LocalProxyApplication

from .constants import ORDER_DEPLOYMENT


@pytest.mark.order(ORDER_DEPLOYMENT)
@pytest.mark.full_deployment
def test_immediately_available_after_deployment(
    client_proxy_app: LocalProxyApplication,
) -> None:
    """After `jd up`, JupyterLab must be reachable through the client proxy right away.

    There is no browser sign-in — the client proxy injects the caller's STS-identity token.

    The ``client_proxy_app`` fixture deliberately calls ONLY ``ensure_deployed()`` and starts
    the proxy — it does not call ``ensure_server_running()``, which would run `jd server restart`
    whenever the server did not yet report available and so would silently heal the exact
    failure this test exists to detect. What is asserted is that ``/lab`` responds with no
    warm-up and no intervention, the instant `jd up` returns.
    """
    client_proxy_app.verify_jupyterlab_accessible(max_retries=20)


@pytest.mark.order(ORDER_DEPLOYMENT + 1)
@pytest.mark.full_deployment
def test_deployment_history_captured(e2e_deployment: EndToEndDeployment) -> None:
    """config and up logs are captured in jd history after a fresh deployment.

    Verifies at least one config log and one up log exist, and that they contain the
    expected terraform init / apply output. (This flow deploys in-container and then runs
    the tests against the existing project, so the test session's own ``jd config`` adds a
    second config log — hence ``>= 1`` rather than an exact count.)
    """
    e2e_deployment.ensure_deployed()

    config_list_result = e2e_deployment.cli.run_command(["jupyter-deploy", "history", "list", "config", "--text"])
    config_logs = [line for line in config_list_result.stdout.strip().split("\n") if line.strip()]
    assert len(config_logs) >= 1, f"Expected at least 1 config log, found {len(config_logs)}"

    up_list_result = e2e_deployment.cli.run_command(["jupyter-deploy", "history", "list", "up", "--text"])
    up_logs = [line for line in up_list_result.stdout.strip().split("\n") if line.strip()]
    assert len(up_logs) >= 1, f"Expected at least 1 up log, found {len(up_logs)}"

    config_show_result = e2e_deployment.cli.run_command(["jupyter-deploy", "history", "show", "config"])
    assert "Terraform has been successfully initialized!" in config_show_result.stdout, (
        "Expected 'Terraform has been successfully initialized!' in config log"
    )

    up_show_result = e2e_deployment.cli.run_command(["jupyter-deploy", "history", "show", "up"])
    up_content = up_show_result.stdout
    assert "Apply complete!" in up_content, "Expected 'Apply complete!' in up log"
    assert "Outputs:" in up_content, "Expected 'Outputs:' section in up log"
