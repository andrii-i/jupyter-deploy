"""Template path provider for the AWS EKS OIDC template."""

from pathlib import Path

TEMPLATE_PATH = Path(__file__).resolve().parent / "template"
