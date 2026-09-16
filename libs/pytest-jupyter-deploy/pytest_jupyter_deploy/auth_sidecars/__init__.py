"""Client-side helpers for the auth-sidecar contracts, one module per identity provider.

A sidecar is the app-agnostic Traefik ForwardAuth service a template puts in front of its app; its
contract is a status code, and the module here is the *client* half of it — enough to mint the token
a real caller would send, and the tokens a caller should not be able to use.

Deliberately re-exports nothing. Each module carries its own provider SDK at module scope
(``aws_auth_sidecar`` imports ``boto3``), so a re-export here would pull that SDK into every
``pytest_jupyter_deploy.auth_sidecars`` import and break the bare-install isolation those modules
rely on. Import the specific module instead::

    from pytest_jupyter_deploy.auth_sidecars.aws_auth_sidecar import long_lived_headers

Same rule as :mod:`pytest_jupyter_deploy.kubernetes` and :mod:`pytest_jupyter_deploy.oauth2_proxy`.
"""
