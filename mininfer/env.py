"""`.env` loading — dependency-free, importable from anywhere.

Kept in its own module so the CLI (`mi.cli.main`) and the proxy (`mi.proxy`) can
both load it without importing each other. That separation matters: uvicorn's
`--reload` reloader imports `mi.proxy:app` directly in a child process and never
runs `cli.main`, so a `.env` loaded only from `main()` would be invisible to the
reloaded server — which is exactly the "keys set but every route says
`no_api_key`" failure.
"""
from __future__ import annotations

import os
import pathlib


def load_env(path: str | pathlib.Path = ".env") -> bool:
    """Merge `path` into `os.environ`, never clobbering an existing variable.

    Returns True when a file was found. python-dotenv is used when installed; a
    minimal parser covers the core install, where dotenv is only a `sync` extra.
    """
    p = pathlib.Path(path)
    if not p.exists():
        return False
    try:
        from dotenv import load_dotenv

        load_dotenv(p, override=False)
        return True
    except ImportError:
        pass
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val
    return True
