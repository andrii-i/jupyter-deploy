"""Client-side helpers for the AWS-identity auth sidecar contract.

The sidecar (``template/services/auth-sidecar/main.go``) is an app-agnostic Traefik ForwardAuth
service: it validates a ``k8s-aws-v1.`` presigned ``sts:GetCallerIdentity`` token, replays it to a
pinned STS endpoint, and authorizes the ARN STS returns against per-deployment IAM allowlists. Its
whole contract is a status code — **200** allow / **401** authentication failure / **403**
authenticated but not permitted / **503** STS transiently unavailable.

This module is the *client* half of that contract, so tests can construct exactly the token a real
caller would send and, just as importantly, tokens a caller should NOT be able to use. The
structural checks in the sidecar's ``verify()`` all run **before** it contacts STS, so the crafted
cases here are rejected offline: no valid credentials needed, no outbound call, no STS flakiness.

Imports ``boto3`` at module scope. That is deliberate and safe: the pytest entry point
(``plugin.py``) never imports this module, so an install without the AWS extra never sees it — the
same isolation the ``kubernetes/`` submodules rely on.
"""

from __future__ import annotations

import base64
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import boto3
from jupyter_deploy.api.aws.sts import eks_token

# The sidecar's own constants (main.go). Duplicated rather than imported: a test that reads the
# value it is asserting cannot catch a change to it.
TOKEN_PREFIX = "k8s-aws-v1."
BINDING_HEADER = "x-k8s-aws-id"
MAX_TOKEN_EXPIRY_SECONDS = 900
SIDECAR_CACHE_TTL_SECONDS = 60


# --------------------------------------------------------------------------- caller identity


def caller_principal() -> tuple[str, str]:
    """Return the calling AWS identity as ``(kind, name)``, parsed like the sidecar does.

    Mirrors ``parsePrincipal`` in ``auth-sidecar/main.go``::

        arn:aws:sts::<acct>:assumed-role/<RoleName>/<session>  -> ("role", "<RoleName>")
        arn:aws:iam::<acct>:user/<path...>/<UserName>          -> ("user", "<UserName>")

    Which allowlist gates the caller depends on this: an assumed-role caller is matched against
    ``ROLE_NAME_ALLOWLIST`` (``jd teams``) and an IAM-user caller against ``USER_NAME_ALLOWLIST``
    (``jd users``). In CI the identity is an assumed role; locally it may be either — so a test
    that hard-codes one passes on one machine and fails on the other.

    Raises:
        AssertionError: If the ARN is a principal shape the sidecar does not authorize (root,
            federated-user, service principal — all of which it rejects with 403).
    """
    arn = str(boto3.client("sts").get_caller_identity()["Arn"])
    parts = arn.split(":")
    assert len(parts) == 6, f"Unexpected caller ARN shape: {arn}"
    service, resource = parts[2], parts[5]

    if service == "sts" and resource.startswith("assumed-role/"):
        role_name = resource[len("assumed-role/") :].split("/")[0]
        assert role_name, f"Could not read a role name from: {arn}"
        return "role", role_name
    if service == "iam" and resource.startswith("user/"):
        user_name = resource[len("user/") :].split("/")[-1]
        assert user_name, f"Could not read a user name from: {arn}"
        return "user", user_name

    raise AssertionError(
        f"Caller {arn} is neither an assumed role nor an IAM user; the auth sidecar cannot "
        "authorize this principal shape, so these tests cannot run as it."
    )


def allowlist_group(kind: str) -> str:
    """Return the `jd` command group that gates a principal of ``kind`` ("teams" / "users")."""
    return "teams" if kind == "role" else "users"


# --------------------------------------------------------------------------- token minting


def mint_token(binding_id: str, region: str, expires_in_seconds: int = 60) -> str:
    """Mint a real ``k8s-aws-v1.`` token for the calling identity, bound to ``binding_id``."""
    return eks_token.get_eks_bearer_token(binding_id=binding_id, region=region, expires_in_seconds=expires_in_seconds)


def auth_headers(token: str, binding_id: str) -> dict[str, str]:
    """Return the header pair the proxy injects: bearer token + binding header."""
    return {"Authorization": f"Bearer {token}", BINDING_HEADER: binding_id}


def long_lived_headers(binding_id: str, region: str) -> dict[str, str]:
    """Mint headers whose token lasts the sidecar's maximum, for replay across allowlist edits.

    NOT the ``jd proxy connect-info`` bundle's token, which lives 60s: each allowlist edit is an SSM
    round-trip plus a sidecar container recreate (~15-30s), so a revoke-then-restore sequence
    outlives it. The sidecar's positive cache is keyed on the token string, so once the entry lapsed
    the replay would take the slow path and the assertion would be measuring credential freshness
    instead of authorization. 900s is the sidecar's own ``maxTokenExpirySec``, so this is still a
    token it accepts.
    """
    return auth_headers(mint_token(binding_id, region, MAX_TOKEN_EXPIRY_SECONDS), binding_id)


# --------------------------------------------------------------------------- token crafting


def decode_token(token: str) -> str:
    """Return the presigned STS URL a ``k8s-aws-v1.`` token encodes.

    Raises:
        AssertionError: If the token does not carry the expected prefix.
    """
    assert token.startswith(TOKEN_PREFIX), f"Not a {TOKEN_PREFIX} token: {token[:32]}…"
    body = token[len(TOKEN_PREFIX) :]
    padding = "=" * (-len(body) % 4)
    return base64.urlsafe_b64decode(body + padding).decode("utf-8")


def encode_token(presigned_url: str) -> str:
    """Encode a URL as a ``k8s-aws-v1.`` token body (base64url, unpadded)."""
    body = base64.urlsafe_b64encode(presigned_url.encode("utf-8")).rstrip(b"=").decode("utf-8")
    return f"{TOKEN_PREFIX}{body}"


def token_query(token: str) -> dict[str, str]:
    """Return the query parameters of the presigned URL a token encodes (SigV4 material included)."""
    return dict(parse_qsl(urlparse(decode_token(token)).query, keep_blank_values=True))


def retarget_token(token: str, host: str | None = None, query: dict[str, str] | None = None) -> str:
    """Return ``token`` with its embedded URL's host and/or query parameters rewritten.

    The signature is NOT recomputed — deliberately. Every check this feeds happens in the sidecar
    before it ever contacts STS (host pin, action, expiry bounds, signing-time bounds), so an
    invalid signature is irrelevant to the assertion and the request costs no STS call. For the one
    case that must reach STS, tamper with ``X-Amz-Signature`` and let STS do the rejecting.
    """
    parsed = urlparse(decode_token(token))
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if query:
        params.update(query)
    return encode_token(urlunparse(parsed._replace(netloc=host or parsed.netloc, query=urlencode(params))))
