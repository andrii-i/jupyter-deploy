"""Ballast Deployment helpers for autoscaling E2E tests.

A "ballast" is a throwaway Deployment that generates schedulable load for the duration of a
test block and deletes itself on exit. Two kinds: CPU-request ballast (sleep pods whose
requests overflow the current nodes, forcing a node-count autoscaler to grow the pool) and
connection ballast (pods holding open TLS connections, driving a connection-count metric for
pod autoscaling).

Template-agnostic: the caller supplies the node pool's nodeSelector (and optional tolerations
for tainted pools), so this works for any template's node grouping.
"""

import string
import subprocess
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

_BALLAST_MANIFEST_PATH = Path(__file__).parent / "ballast-pod.yaml"
_CONNECTION_BALLAST_MANIFEST_PATH = Path(__file__).parent / "connection-ballast.yaml"


def _indent_yaml(pairs: dict[str, str], indent: int) -> str:
    pad = " " * indent
    if not pairs:
        return f"{pad}{{}}"
    return "\n".join(f"{pad}{key}: {value}" for key, value in pairs.items())


def _tolerations_yaml(tolerations: list[dict[str, str]], indent: int) -> str:
    pad = " " * indent
    if not tolerations:
        return f"{pad}[]"
    blocks = []
    for tol in tolerations:
        first, *rest = tol.items()
        lines = [f"{pad}- {first[0]}: {first[1]}"]
        lines += [f"{pad}  {k}: {v}" for k, v in rest]
        blocks.append("\n".join(lines))
    return "\n".join(blocks)


@contextmanager
def ballast_deployment(
    *,
    name: str,
    namespace: str,
    image: str,
    replicas: int,
    cpu_request: str,
    node_selector: dict[str, str],
    tolerations: list[dict[str, str]] | None = None,
) -> Generator[str, None, None]:
    """Apply a ballast Deployment for the duration of the block, deleting it on exit.

    Yields the ballast app name (== label selector value `app=<name>`). Deletion is
    best-effort and runs even if the body raises, so a scale-up test self-reverts.

    node_selector pins the ballast to a node pool; tolerations (optional) let it land on a
    tainted pool. cpu_request should be a large fraction of a node's allocatable CPU (see
    nodes.get_node_allocatable_cpu_millicores) so pods can't co-locate.
    """
    template = string.Template(_BALLAST_MANIFEST_PATH.read_text())
    manifest = template.substitute(
        name=name,
        namespace=namespace,
        image=image,
        replicas=str(replicas),
        cpu_request=cpu_request,
        node_selector_yaml=_indent_yaml(node_selector, indent=8),
        tolerations_yaml=_tolerations_yaml(tolerations or [], indent=8),
    )
    subprocess.run(["kubectl", "apply", "-f", "-"], input=manifest, text=True, check=True, capture_output=True)
    try:
        yield name
    finally:
        subprocess.run(
            ["kubectl", "delete", "deployment", name, "-n", namespace, "--ignore-not-found", "--wait=false"],
            capture_output=True,
            text=True,
        )


@contextmanager
def connection_ballast_deployment(
    *,
    name: str,
    namespace: str,
    image: str,
    replicas: int,
    target_host: str,
    connections_per_pod: int,
    port: int = 443,
    node_selector: dict[str, str] | None = None,
) -> Generator[str, None, None]:
    """Apply a connection-holding ballast for the duration of the block, deleting it on exit.

    Each pod holds connections_per_pod TLS connections open to target_host:port with
    keep-alives, so a connection-count autoscaling metric holds at replicas x
    connections_per_pod. Yields the ballast app name. The exit delete BLOCKS until the
    pods are gone, so the caller can poll scale-down immediately after the block.
    """
    template = string.Template(_CONNECTION_BALLAST_MANIFEST_PATH.read_text())
    manifest = template.substitute(
        name=name,
        namespace=namespace,
        image=image,
        replicas=str(replicas),
        target_host=target_host,
        port=str(port),
        connections=str(connections_per_pod),
        node_selector_yaml=_indent_yaml(node_selector or {}, indent=8),
    )
    subprocess.run(["kubectl", "apply", "-f", "-"], input=manifest, text=True, check=True, capture_output=True)
    try:
        yield name
    finally:
        subprocess.run(
            ["kubectl", "delete", "deployment", name, "-n", namespace, "--ignore-not-found"],
            capture_output=True,
            text=True,
        )
