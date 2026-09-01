"""E2E tests for the GPU workspace journey on the EKS OIDC template.

Gated on JD_E2E_GPU_ENABLED (needs G/VT on-demand quota in the account), and
skipped at runtime unless the deployment currently has the GPU pool
(enable_default_gpu_pool). test_gpu_pool.py covers deployments with the flag
off by enabling and disabling the pool itself.
"""

from pathlib import Path

import pytest
from pytest_jupyter_deploy.deployment import EndToEndDeployment
from pytest_jupyter_deploy.notebook import delete_notebook, run_notebook_in_jupyterlab, upload_notebook
from pytest_jupyter_deploy.oauth2_proxy.dex import DexGitHubOAuth2ProxyApplication
from pytest_jupyter_deploy.plugin import skip_if_testvars_not_set
from pytest_jupyter_deploy.workspaces.kubectl import (
    kubectl_apply_workspace,
    kubectl_delete_workspace,
    kubectl_get_workspace_access_url,
)

from .conftest import WORKSPACE_NAMESPACE, WORKSPACES_DIR
from .test_utils import (
    GPU_WORKSPACE,
    gpu_node_count,
    poll,
    require_gpu_pool,
    verify_gpu_workspace_provisioning_and_scale_to_zero,
)

NOTEBOOKS_DIR = Path(__file__).parent / "notebooks"


@skip_if_testvars_not_set(["JD_E2E_GPU_ENABLED"])
@pytest.mark.mutating
@pytest.mark.usefixtures("kubernetes_cluster_login")
def test_gpu_workspace_provisioning_nvidia_smi_and_scale_to_zero(e2e_deployment: EndToEndDeployment) -> None:
    """Full GPU workspace lifecycle: fenced provisioning, a visible device, scale-to-zero.

    1. Create a workspace from the jupyterlab-gpu template
    2. Karpenter provisions a workspace-gpu node (role label + taint + gpu.present)
    3. The device plugin advertises nvidia.com/gpu and nvidia-smi sees the device
    4. Delete → the GPU pool scales back to zero
    """
    e2e_deployment.ensure_deployed()
    require_gpu_pool()

    verify_gpu_workspace_provisioning_and_scale_to_zero(e2e_deployment)


@skip_if_testvars_not_set(["JD_E2E_GPU_ENABLED", "JD_E2E_USER"])
@pytest.mark.mutating
@pytest.mark.usefixtures("kubernetes_cluster_login")
def test_gpu_workspace_kernel_sees_cuda(
    e2e_deployment: EndToEndDeployment,
    dex_oauth_app: DexGitHubOAuth2ProxyApplication,
) -> None:
    """The notebook kernel can install torch and use CUDA on a GPU workspace.

    Runs the gpu_check notebook through the JupyterLab UI, mirroring the
    ec2-base precedent: nvidia-smi, torch installed at test time via uv (the
    stock image ships no CUDA userland; the CUDA-enabled wheel comes from the
    torch package itself), then torch.cuda.is_available from the kernel.
    Targets the built-in g-family pool. No torch cleanup is needed: the
    workspace and its volume are deleted at the end of the test.
    """
    e2e_deployment.ensure_deployed()
    require_gpu_pool()

    kubectl_apply_workspace(GPU_WORKSPACE, WORKSPACES_DIR)
    try:
        # First start provisions a node and pulls the image: minutes, not seconds.
        e2e_deployment.cli.poll_scoped_server_status(GPU_WORKSPACE, "Running", timeout_s=600)
        e2e_deployment.cli.wait_for_workspace_pod_exec_ready(GPU_WORKSPACE)

        access_url = kubectl_get_workspace_access_url(GPU_WORKSPACE, WORKSPACE_NAMESPACE)
        dex_oauth_app.verify_workspace_accessible(access_url)

        notebook_path = NOTEBOOKS_DIR / "gpu_check.ipynb"
        server_path = upload_notebook(
            e2e_deployment,
            notebook_path,
            "e2e-test/gpu_check.ipynb",
            name=GPU_WORKSPACE,
            scope=WORKSPACE_NAMESPACE,
        )

        # torch pulls ~2.5 GiB of CUDA wheels; long timeout, slow poll.
        run_notebook_in_jupyterlab(dex_oauth_app.page, server_path, timeout_ms=300000, poll_interval_ms=5000)

        delete_notebook(e2e_deployment, server_path, name=GPU_WORKSPACE, scope=WORKSPACE_NAMESPACE)
    finally:
        kubectl_delete_workspace(GPU_WORKSPACE)

    poll(
        lambda: gpu_node_count() == 0,
        timeout_s=600,
        msg="gpu NodePool did not scale to zero after workspace deletion",
    )
