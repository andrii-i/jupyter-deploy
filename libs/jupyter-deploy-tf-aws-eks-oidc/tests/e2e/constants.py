"""Shared constants for the EKS OIDC e2e suite."""

# Ordered mutating GPU cases (test_workspace_gpu) run as one
# enable_default_gpu_pool on->off window; the ordinal keeps them contiguous.
ORDER_GPU = 10

GPU_POOL_FLAG = "enable_default_gpu_pool"
# The Karpenter NodePool the flag synthesizes.
GPU_NODEPOOL = "workspace-gpu"
GPU_WORKSPACE = "e2e-gpu-workspace"
GPU_TEMPLATE_NAME = "jupyterlab-gpu"
# Display name of the default GPU WorkspaceTemplate the flag synthesizes
# (engine/platform_karpenter.tf), rendered as a card on the create page.
GPU_TEMPLATE_DISPLAY_NAME = "JupyterLab GPU"
GPU_ROLE = "workspaces-gpu"
GPU_ROLE_SELECTOR = f"jupyter-deploy/role={GPU_ROLE}"
# The karpenter-nodepools chart names each pool's EC2NodeClass after the pool.
GPU_EC2NODECLASS = GPU_NODEPOOL
