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


def _render_env(key: str, value: str) -> str:
    """`KEY=value`, quoted when the value would otherwise be misread.

    A `#` starts a comment and whitespace ends the value, so both need quoting for
    the line to survive `python-dotenv` and `sh -c '. .env'` unchanged.
    """
    if value and not any(ch in value for ch in ' \t"\'#$`\\'):
        return f"{key}={value}"
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'{key}="{escaped}"'


def set_env_var(path: str | pathlib.Path, key: str, value: str, *,
                template: str | pathlib.Path | None = None) -> str:
    """Write `KEY=value` into a `.env`, returning what happened.

    The dashboard keeps keys in the browser and sends them per request; this is
    the opt-in second step that also puts one on the server, so ingest and the
    next boot can use it. The rule is deliberately conservative:

    * a variable that already holds a **non-empty** value is left exactly as it is
      and reported `"already_set"` — a secret the operator put there is never
      replaced by one typed into a web form;
    * an empty placeholder (`OPENROUTER_API_KEY=`) is filled, and an absent line is
      appended (`"written"`), because that is the `.env.example` -> `.env` flow;
    * a missing file is first seeded from `template` (`.env.example`) when given,
      and reported `"created"`.

    The write is atomic and the file is left mode `0600`, so a crash cannot leave a
    half-written secret and other local users cannot read it.
    """
    p = pathlib.Path(path)
    created = False
    if not p.exists() and template is not None:
        t = pathlib.Path(template)
        if t.exists():
            p.write_text(t.read_text(encoding="utf-8"), encoding="utf-8")
            created = True

    lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
    rendered = _render_env(key, value)
    matched = False
    for i, raw in enumerate(lines):
        if raw.lstrip().startswith("#") or "=" not in raw:
            continue
        name, _, existing = raw.partition("=")
        if name.strip() != key:
            continue
        matched = True
        if existing.strip().strip('"').strip("'"):
            return "already_set"
        lines[i] = rendered
        break
    if not matched:
        lines.append(rendered)

    tmp = p.with_name(p.name + ".tmp")
    body = "\n".join(lines) + "\n"
    tmp.write_text(body, encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:                       # Windows has no meaningful mode bits
        pass
    try:
        os.replace(tmp, p)
    except OSError:
        # A bind-mounted `.env` is a mount point, and Linux refuses to rename over
        # one (EBUSY) — which is exactly how the container sees the repo's file.
        # The mount leaves no second inode to swap in, so write in place: not
        # atomic, but the only option, and it keeps "To server" working in Docker.
        p.write_text(body, encoding="utf-8")
        try:
            tmp.unlink()
        except OSError:
            pass
    return "created" if created else "written"
