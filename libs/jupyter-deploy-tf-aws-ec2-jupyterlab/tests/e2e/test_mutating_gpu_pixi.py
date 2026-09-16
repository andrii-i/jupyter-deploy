"""Apply #1 of the mutating pass: CPU + uv -> GPU + pixi + external volumes.

**This file and ``test_mutating_cpu_uv.py`` mutate ONE deployment in place, in order.** That is
the design, not an accident of history. The base template fans the equivalent coverage across
five mutating jobs and seven `terraform apply` cycles (~85 minutes); this suite does it in two
applies on a single deployment, and gets *better* coverage for it — because the thing under test
is the transition, not the end state:

  - terraform replacing the instance under a persisted data volume,
  - the volume reattaching and remounting at boot,
  - the package-manager environment rebuilding from scratch,
  - the app returning on the SAME pinned cert (so every client's pin still validates).

A separate deployment per configuration exercises none of that: one deployed straight onto a GPU
with pixi never performs the swap. It is also not what a user does — a user changes their mind
about their instance and runs `jd config && jd up`.

Consequence: ``-k test_mutating_cpu_uv`` alone is NOT a valid entry point. The ``ORDER_*``
constants encode the dependency; do not "optimize" these into two independent runs.

Omitted: covered by the base template suite (`test_config_apply.py`, `test_external_volumes.py`,
`test_gpu.py`, `test_pixi.py`, `test_uv.py` — 5 files / 12 tests / 7 applies)
  The separate "provision external volumes", "upgrade the instance type", "switch to pixi" and
  "switch to uv" applies, plus the per-volume file/directory operation matrices (which
  ``test_home_volume.py`` already covers for the home volume — the code path is the same mount
  machinery). Folded here into two applies.
"""

from pathlib import Path

import pytest
from pytest_jupyter_deploy.deployment import EndToEndDeployment
from pytest_jupyter_deploy.files import verify_dir_exists_on_server, verify_file_exists_on_server
from pytest_jupyter_deploy.local_proxy import LocalProxyApplication
from pytest_jupyter_deploy.local_proxy.requests import cert_fingerprint, served_cert_pem
from pytest_jupyter_deploy.notebook import delete_notebook, run_notebook_in_jupyterlab, upload_notebook
from pytest_jupyter_deploy.plugin import skip_if_testvars_not_set

from .constants import EBS_FLAG, EBS_MOUNT, EFS_FLAG, EFS_MOUNT, HOME_FLAG, ORDER_MUTATING_GPU_PIXI

# A GPU instance swap provisions a DLAMI and can exceed the default deploy timeout.
_GPU_APPLY_TIMEOUT_SECONDS = 3600


@pytest.mark.order(ORDER_MUTATING_GPU_PIXI)
@pytest.mark.mutating
@skip_if_testvars_not_set(["JD_E2E_GPU_INSTANCE", "JD_E2E_LARGER_LOG_RETENTION_DAYS"])
def test_switch_to_gpu_pixi_with_external_volumes(
    e2e_deployment: EndToEndDeployment,
    client_proxy_app: LocalProxyApplication,
    gpu_instance_type: str,
    larger_log_retention_days: int,
) -> None:
    """One apply changes instance type, package manager, volumes and log retention together.

    Four changes in one `jd up` because that is the realistic shape of a user's second thought,
    and because bundling them costs one instance replacement instead of four. What the assertions
    pin is everything that must be TRUE ACROSS that replacement:

    - the instance id changes (the GPU DLAMI forces a replacement — this is the swap happening),
    - the home-volume flag file is still there (the data volume reattached and remounted),
    - the served cert is byte-identical (key and cert live on the data volume, so a replacement
      must not regenerate them — otherwise every client's pinned PEM goes stale and the app
      becomes unreachable through the proxy while looking perfectly healthy),
    - the app answers through the proxy again,
    - the external volumes are mounted and writable.
    """
    e2e_deployment.ensure_server_running()

    instance_id_before = e2e_deployment.cli.get_str_output("instance_id")
    bundle_before = e2e_deployment.cli.get_connect_bundle()
    fingerprint_before = cert_fingerprint(served_cert_pem(bundle_before["host"], bundle_before["port"]))
    e2e_deployment.cli.run_command(["jupyter-deploy", "server", "exec", "--", "touch", HOME_FLAG])

    # Set one variable through variables.yaml and the rest through injected CLI flags, so both
    # configuration surfaces are exercised by the same apply.
    e2e_deployment.update_override_value("log_files_retention_days", larger_log_retention_days)
    e2e_deployment.ensure_deployed_with(
        [
            "--instance-type",
            gpu_instance_type,
            "--jupyter-package-manager",
            "pixi",
            "--additional-ebs-mounts",
            EBS_MOUNT,
            "--additional-efs-mounts",
            EFS_MOUNT,
        ],
        timeout_seconds=_GPU_APPLY_TIMEOUT_SECONDS,
    )

    e2e_deployment.ensure_server_running(wait_after_restart=True)

    instance_id_after = e2e_deployment.cli.get_str_output("instance_id")
    assert instance_id_after != instance_id_before, (
        f"Expected the GPU switch to replace the instance, but it is still {instance_id_after}. "
        "If terraform did not replace it, nothing below is actually testing a swap."
    )

    verify_file_exists_on_server(e2e_deployment, HOME_FLAG)

    bundle_after = e2e_deployment.cli.get_connect_bundle()
    assert cert_fingerprint(served_cert_pem(bundle_after["host"], bundle_after["port"])) == fingerprint_before, (
        "The replacement instance serves a different cert; every pinned client would now fail "
        "verification even though the app itself is healthy"
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

    verify_dir_exists_on_server(e2e_deployment, "/home/jovyan/external-ebs1")
    verify_dir_exists_on_server(e2e_deployment, "/home/jovyan/external-efs1")

    # Seed the external volumes now (they did not exist before this apply); apply #2 checks them.
    e2e_deployment.cli.run_command(["jupyter-deploy", "server", "exec", "--", "touch", EBS_FLAG])
    e2e_deployment.cli.run_command(["jupyter-deploy", "server", "exec", "--", "touch", EFS_FLAG])


@pytest.mark.order(ORDER_MUTATING_GPU_PIXI + 1)
@pytest.mark.mutating
@skip_if_testvars_not_set(["JD_E2E_GPU_INSTANCE"])
def test_run_gpu_notebook(
    e2e_deployment: EndToEndDeployment,
    client_proxy_app: LocalProxyApplication,
    gpu_instance_type: str,
) -> None:
    """A notebook on the GPU instance sees the GPU through the driver stack.

    The end of a long chain that only a real GPU instance exercises: DLAMI selection, the NVIDIA
    driver on the host, the ``nvidia`` device reservation in compose, and CUDA visible to the
    kernel. Any link breaking leaves an instance that boots fine and bills at GPU rates while
    ``torch.cuda.is_available()`` is False.
    """
    actual_instance_type = e2e_deployment.get_str_variable_value("instance_type")
    assert actual_instance_type == gpu_instance_type, (
        f"Expected instance type {gpu_instance_type}, got {actual_instance_type}. Apply #1 did not take."
    )
    actual_package_manager = e2e_deployment.get_str_variable_value("jupyter_package_manager")
    assert actual_package_manager == "pixi", (
        f"Expected package manager 'pixi', got '{actual_package_manager}'. GPU tests need pixi for torch."
    )

    e2e_deployment.ensure_server_running()
    client_proxy_app.verify_jupyterlab_accessible()

    gpu_notebook = Path(__file__).parent / "notebooks" / "gpu_check.ipynb"
    server_path = upload_notebook(e2e_deployment, gpu_notebook, "e2e-test/gpu_check.ipynb")

    # torch via pixi takes ~90s; allow for network variability and poll less aggressively.
    run_notebook_in_jupyterlab(client_proxy_app.page, server_path, timeout_ms=300000, poll_interval_ms=5000)
    delete_notebook(e2e_deployment, server_path)

    result = e2e_deployment.cli.run_command(["jupyter-deploy", "server", "exec", "--", "pixi", "list"])
    assert "torch" in result.stdout, f"Expected torch to have been installed by the notebook: {result.stdout}"

    # torch is enormous. Remove it so it is not carried into every later image build; a GPU user
    # installing it is what we needed to prove, not that it stays installed.
    e2e_deployment.cli.run_command(["jupyter-deploy", "server", "exec", "--", "pixi", "remove", "--pypi", "torch"])
    result = e2e_deployment.cli.run_command(["jupyter-deploy", "server", "exec", "--", "pixi", "list"])
    assert "torch" not in result.stdout, f"Expected torch to have been removed: {result.stdout}"


@pytest.mark.order(ORDER_MUTATING_GPU_PIXI + 2)
@pytest.mark.mutating
def test_pixi_install_and_persist(
    e2e_deployment: EndToEndDeployment,
    client_proxy_app: LocalProxyApplication,
) -> None:
    """Packages a user installs from a notebook survive a server restart.

    The pixi manifest and lock live on the home volume, so a container restart re-syncs from
    them rather than starting clean. If it did not, a restart would silently discard everything
    the user had installed — and restarts happen for reasons the user did not choose.
    """
    actual_package_manager = e2e_deployment.get_str_variable_value("jupyter_package_manager")
    assert actual_package_manager == "pixi", f"Expected pixi, got '{actual_package_manager}'"

    e2e_deployment.ensure_server_running()
    client_proxy_app.verify_jupyterlab_accessible()

    notebook_path = Path(__file__).parent / "notebooks" / "pixi_install_libraries.ipynb"
    server_path = upload_notebook(e2e_deployment, notebook_path, "e2e-test/pixi_install_libraries.ipynb")
    run_notebook_in_jupyterlab(client_proxy_app.page, server_path, timeout_ms=120000)
    delete_notebook(e2e_deployment, server_path)

    e2e_deployment.cli.run_command(["jupyter-deploy", "server", "restart"])
    e2e_deployment.ensure_server_running(wait_after_restart=True)

    result = e2e_deployment.cli.run_exec_with_retry(["jupyter-deploy", "server", "exec", "--", "pixi", "list"])
    assert "pytest" in result.stdout, f"Expected pytest to survive the server restart: {result.stdout}"
    assert "conda-build" in result.stdout, f"Expected conda-build to survive the server restart: {result.stdout}"


@pytest.mark.order(ORDER_MUTATING_GPU_PIXI + 3)
@pytest.mark.mutating
def test_pixi_environment_recovery(
    e2e_deployment: EndToEndDeployment,
    client_proxy_app: LocalProxyApplication,
) -> None:
    """A broken environment auto-recovers on restart, back to the base env.

    A user can uninstall JupyterLab itself from inside JupyterLab. Without recovery the app would
    never come back and the only route in would be SSM — so the startup script detects the
    failure and resets to the base environment. The second half of the assertion is the cost of
    that: recovery is a reset, so user packages are NOT preserved, and the test pins that
    honestly rather than pretending otherwise.
    """
    actual_package_manager = e2e_deployment.get_str_variable_value("jupyter_package_manager")
    assert actual_package_manager == "pixi", f"Expected pixi, got '{actual_package_manager}'"

    # NOTE: `pixi remove` is the correct way to break the environment — do not change this to
    # deleting files, which exercises a different (and unrealistic) failure.
    e2e_deployment.cli.run_command(["jupyter-deploy", "server", "exec", "--", "pixi", "remove", "--pypi", "jupyterlab"])
    e2e_deployment.cli.run_command(["jupyter-deploy", "server", "restart"])
    e2e_deployment.ensure_server_running(wait_after_restart=True)

    client_proxy_app.verify_jupyterlab_accessible()

    result = e2e_deployment.cli.run_exec_with_retry(["jupyter-deploy", "server", "exec", "--", "pixi", "list"])
    assert "jupyterlab" in result.stdout, f"Expected jupyterlab to be reinstalled by recovery: {result.stdout}"
    assert "pytest" not in result.stdout, f"Expected pytest to be gone after recovery: {result.stdout}"
    assert "conda-build" not in result.stdout, f"Expected conda-build to be gone after recovery: {result.stdout}"
