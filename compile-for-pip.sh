#!/bin/sh
set -e
uv pip compile pyproject.toml -o requirements-minimal.txt
uv pip compile pyproject.toml --group fast -o requirements.txt
