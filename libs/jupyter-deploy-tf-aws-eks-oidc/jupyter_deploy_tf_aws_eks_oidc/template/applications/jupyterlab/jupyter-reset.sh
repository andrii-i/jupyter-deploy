#!/bin/bash
set -e

echo "Resetting Jupyter environment (uv)..."

if [ -d "/home/jovyan/.venv" ]; then
    rm -rf /home/jovyan/.venv
fi

if [ -f "/home/jovyan/pyproject.toml" ]; then
    rm -f /home/jovyan/pyproject.toml
fi

if [ -f "/home/jovyan/uv.lock" ]; then
    rm -f /home/jovyan/uv.lock
fi

if [ -d "/home/jovyan/.jupyter" ]; then
    rm -rf /home/jovyan/.jupyter
fi

cp /opt/uv/jupyter/pyproject.toml /home/jovyan/
cp /opt/uv/jupyter/uv.lock /home/jovyan/

echo "Recreating uv environment..."
uv sync --locked

uv run jupyter lab \
    --no-browser \
    --ip=0.0.0.0 \
    --IdentityProvider.token=
