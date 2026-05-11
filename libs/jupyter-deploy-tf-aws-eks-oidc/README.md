# Jupyter Deploy AWS EKS OIDC template

The **AWS EKS OIDC Template** deploys **JupyterLab** workspaces on an Amazon EKS cluster,
with HTTPS, GitHub OAuth via Dex, and the [jupyter-k8s](https://github.com/jupyter-infra/jupyter-k8s)
operator for workspace lifecycle management.

**Documentation:** [jupyter-deploy.readthedocs.io](https://jupyter-deploy.readthedocs.io)

## Prerequisites

- An AWS account with CLI credentials configured
- A domain registered in Amazon Route 53
- A GitHub OAuth app for authentication

## Usage

```bash
pip install jupyter-deploy jupyter-deploy-tf-aws-eks-oidc
jd init aws:eks:oidc my-cluster
cd my-cluster
jd config
jd up
```

## License

MIT License — see [LICENSE](LICENSE).
