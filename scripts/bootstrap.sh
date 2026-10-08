#!/bin/sh
# Keep the physical environment outside a dot-directory; preserve normal uv commands.
set -eu
cd "$(dirname "$0")/.."

if [ -L .venv ]; then
    if [ "$(readlink .venv)" != "work/python-env" ]; then
        printf '%s\n' 'Unexpected .venv symlink; inspect it before changing the environment.' >&2
        exit 1
    fi
elif [ -e .venv ]; then
    printf '%s\n' 'An existing .venv directory needs inspection; no files were replaced.' >&2
    exit 1
fi

if [ ! -x work/python-env/bin/python ]; then
    uv venv work/python-env --python 3.12
fi
if [ ! -L .venv ]; then
    ln -s work/python-env .venv
fi
uv sync --extra local --locked
uv run --locked llm-lab --help
