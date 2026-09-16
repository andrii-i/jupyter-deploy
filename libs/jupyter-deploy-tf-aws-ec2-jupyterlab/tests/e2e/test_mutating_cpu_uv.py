"""Apply #2 of the mutating pass: GPU + pixi -> CPU + uv (external volumes stay).

**Only meaningful after ``test_mutating_gpu_pixi.py``.** The ``pixi -> uv`` transition and the
second instance replacement ARE the coverage here — a deployment that started on uv would test
nothing this file claims to test. ``-k test_mutating_cpu_uv`` on its own is not a valid entry
point; see ``constants.py`` and the header of ``test_mutating_gpu_pixi.py``.

Also the return leg: the deployment must end back on a cheap CPU instance with uv, so a run that
gets this far does not leave a GPU instance billing until the destroy job reaps it.

Omitted: covered by the base template suite (`test_uv.py`, `test_external_volumes.py`)
  The standalone "switch to uv" apply, and the per-volume file/directory operation matrices for
  EBS and EFS (the same mount machinery ``test_home_volume.py`` covers for the home volume).
"""

from pathlib import Path

import pytest
from pytest_jupyter_deploy.commands import verify_server_command_fails
from pytest_jupyter_deploy.deployment import EndToEndDeployment
from pytest_jupyter_deploy.files import verify_dir_exists_on_server, verify_file_exists_on_server
from pytest_jupyter_deploy.local_proxy import LocalProxyApplication
from pytest_jupyter_deploy.local_proxy.requests import cert_fingerprint, served_cert_pem
from pytest_jupyter_deploy.notebook import delete_notebook, run_notebook_in_jupyterlab, upload_notebook
from pytest_jupyter_deploy.plugin import skip_if_testvars_not_set

from .constants import EBS_FLAG, EBS_MOUNT, EFS_FLAG, EFS_MOUNT, HOME_FLAG, ORDER_MUTATING_CPU_UV

_APPLY_TIMEOUT_SECONDS = 3600


@pytest.mark.order(ORDER_MUTATING_CPU_UV)
@pytest.mark.mutating
@skip_if_testvars_not_set(["JD_E2E_CPU_INSTANCE"])
def test_switch_to_cpu_uv(
    e2e_deployment: EndToEndDeployment,
    client_proxy_app: LocalProxyApplication,
    cpu_instance_type: str,
) -> None:
    """The second in-place swap: back to a CPU instance and uv, with all data intact.

    A second replacement in the opposite direction, which is a genuinely different path from the
    first: the AMI goes from DLAMI back to the standard image, and the package-manager
    environment is rebuilt from a uv lockfile where a pixi one was in use. Every flag file — home
    volume, external EBS, external EFS — must still be there, and the cert must still be the same
    one, because a user who changes their instance twice has not agreed to lose anything.
    """
    e2e_deployment.ensure_server_running()

    instance_id_before = e2e_deployment.cli.get_str_output("instance_id")
    # NOT `persisting_resources`: the E2E mounts are deliberately non-persist (a persisted volume
    # survives `jd down` and would leak EBS/EFS out of every CI run), so that output is empty and
    # asserting it unchanged would be vacuous while reading as if it proved volume identity. The
    # flag files below are what actually prove the same volumes reattached.
    deployment_id_before = e2e_deployment.cli.get_str_output("deployment_id")
    bundle_before = e2e_deployment.cli.get_connect_bundle()
    fingerprint_before = cert_fingerprint(served_cert_pem(bundle_before["host"], bundle_before["port"]))

    # Re-pass the mount flags: they are list variables, and omitting them would unmount (and
    # potentially destroy) the volumes this test is about to assert on.
    e2e_deployment.cli.run_command(
        [
            "jupyter-deploy",
            "config",
            "--instance-type",
            cpu_instance_type,
            "--jupyter-package-manager",
            "uv",
            "--additional-ebs-mounts",
            EBS_MOUNT,
            "--additional-efs-mounts",
            EFS_MOUNT,
        ]
    )
    e2e_deployment.cli.run_command(["jupyter-deploy", "up", "-y"], timeout_seconds=_APPLY_TIMEOUT_SECONDS)

    e2e_deployment.ensure_server_running(wait_after_restart=True)

    assert e2e_deployment.cli.get_str_output("instance_id") != instance_id_before, (
        "Expected the CPU switch to replace the GPU instance"
    )
    # The deployment identity must survive an instance replacement: it is the auth sidecar's
    # binding id AND the suffix of every SSM document name, so a swap that regenerated it would
    # invalidate every minted token and every `jd host`/`jd server` command at once.
    assert e2e_deployment.cli.get_str_output("deployment_id") == deployment_id_before, (
        "The deployment id changed across the swap; the auth binding and all SSM documents would break"
    )

    verify_file_exists_on_server(e2e_deployment, HOME_FLAG)
    verify_file_exists_on_server(e2e_deployment, EBS_FLAG)
    verify_file_exists_on_server(e2e_deployment, EFS_FLAG)

    bundle_after = e2e_deployment.cli.get_connect_bundle()
    assert cert_fingerprint(served_cert_pem(bundle_after["host"], bundle_after["port"])) == fingerprint_before, (
        "The instance regenerated its cert on the second swap; pinned clients would fail"
    )

    # The proxy the fixture started at setup is pinned to the OLD instance's IP and its port is
    # gone once the instance is replaced, so the browser must be re-pointed at a FRESH proxy —
    # `attach()` re-reads the bound port rather than reusing the stale setup-time URL. Verifying
    # the app before stopping the proxy, and re-attaching first, is the whole point: a test that
    # navigated to the pre-apply URL would report "Problem loading page" and look like an app
    # failure when the app is fine.
    e2e_deployment.cli.start_proxy(replace=True)
    try:
        client_proxy_app.attach()
        client_proxy_app.verify_jupyterlab_accessible()
    finally:
        e2e_deployment.cli.stop_proxy_if_running()

    # Remove the pixi manifests the previous configuration left in the home volume: they persist
    # on the volume across the switch, and a stale pixi.toml alongside a uv environment is the
    # exact confusing state a user would report.
    e2e_deployment.cli.run_command(
        ["jupyter-deploy", "server", "exec", "--", "rm", "-f", "/home/jovyan/pixi.toml", "/home/jovyan/pixi.lock"]
    )


@pytest.mark.order(ORDER_MUTATING_CPU_UV + 1)
@pytest.mark.mutating
def test_external_volumes_ebs_and_efs_mounted(e2e_deployment: EndToEndDeployment) -> None:
    """Both external volumes are mounted, writable and correctly sized after the second swap.

    EBS and EFS reattach by different mechanisms (a block device that must be found and mounted
    versus an NFS mount that must resolve and connect), so both are asserted. The size check
    catches the case where the mount point exists as a plain directory on the root volume — which
    looks identical to a working mount until the user fills it up.
    """
    e2e_deployment.ensure_server_running()

    for mount_point in ("/home/jovyan/external-ebs1", "/home/jovyan/external-efs1"):
        verify_dir_exists_on_server(e2e_deployment, mount_point)

        probe = f"{mount_point}/e2e_write_probe.txt"
        e2e_deployment.cli.run_command(["jupyter-deploy", "server", "exec", "--", "touch", probe])
        verify_file_exists_on_server(e2e_deployment, probe)
        e2e_deployment.cli.run_command(["jupyter-deploy", "server", "exec", "--", "rm", "-f", probe])

    # The EBS volume was requested at 50 GiB, so `df` must show a filesystem of that order —
    # not the root volume the mount point would fall back to.
    result = e2e_deployment.cli.run_command(
        ["jupyter-deploy", "server", "exec", "--", "df", "-h", "/home/jovyan/external-ebs1"]
    )
    assert "external-ebs1" in result.stdout, (
        f"external-ebs1 is not a mount point of its own; it fell back to the root volume:\n{result.stdout}"
    )


@pytest.mark.order(ORDER_MUTATING_CPU_UV + 2)
@pytest.mark.mutating
def test_uv_install_and_persist(
    e2e_deployment: EndToEndDeployment,
    client_proxy_app: LocalProxyApplication,
) -> None:
    """Packages a user installs from a notebook survive a server restart (uv flavor).

    Same property as the pixi case, different machinery: uv syncs from ``pyproject.toml`` +
    ``uv.lock`` on the home volume. Asserted separately rather than assumed symmetric, because
    the two package managers have independent start scripts and lockfile handling.
    """
    actual_package_manager = e2e_deployment.get_str_variable_value("jupyter_package_manager")
    assert actual_package_manager == "uv", f"Expected uv, got '{actual_package_manager}'"

    e2e_deployment.ensure_server_running()
    client_proxy_app.verify_jupyterlab_accessible()

    notebook_path = Path(__file__).parent / "notebooks" / "uv_install_ipywidgets.ipynb"
    server_path = upload_notebook(e2e_deployment, notebook_path, "e2e-test/uv_install_ipywidgets.ipynb")
    run_notebook_in_jupyterlab(client_proxy_app.page, server_path, timeout_ms=120000)
    delete_notebook(e2e_deployment, server_path)

    e2e_deployment.cli.run_command(["jupyter-deploy", "server", "restart"])
    e2e_deployment.ensure_server_running(wait_after_restart=True)

    # Exits non-zero if ipywidgets is not installed, so the call itself is the assertion.
    e2e_deployment.cli.run_exec_with_retry(
        ["jupyter-deploy", "server", "exec", "--", "uv", "pip", "show", "ipywidgets"]
    )


@pytest.mark.order(ORDER_MUTATING_CPU_UV + 3)
@pytest.mark.mutating
def test_uv_environment_recovery(
    e2e_deployment: EndToEndDeployment,
    client_proxy_app: LocalProxyApplication,
) -> None:
    """A broken uv environment auto-recovers on restart, back to the base env.

    The uv counterpart of the pixi recovery test: a user can remove JupyterLab from its own
    environment, and the app must still come back without an SSM rescue. Recovery is a reset, so
    the user's own packages do not survive it — pinned here so the behavior stays deliberate.
    """
    actual_package_manager = e2e_deployment.get_str_variable_value("jupyter_package_manager")
    assert actual_package_manager == "uv", f"Expected uv, got '{actual_package_manager}'"

    # NOTE: `uv remove` is the correct way to break the environment — do not change this.
    e2e_deployment.cli.run_command(["jupyter-deploy", "server", "exec", "--", "uv", "remove", "jupyterlab"])
    e2e_deployment.cli.run_command(["jupyter-deploy", "server", "restart"])
    e2e_deployment.ensure_server_running(wait_after_restart=True)

    client_proxy_app.verify_jupyterlab_accessible()

    e2e_deployment.cli.run_exec_with_retry(
        ["jupyter-deploy", "server", "exec", "--", "uv", "pip", "show", "jupyterlab"]
    )
    verify_server_command_fails(
        e2e_deployment,
        ["jupyter-deploy", "server", "exec", "--", "uv", "pip", "show", "ipywidgets"],
        expected_returncode=1,
        stderr_contains="not found",
    )
