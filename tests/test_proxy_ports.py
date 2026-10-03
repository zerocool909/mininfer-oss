"""Tests for `mi proxy` port handling: the busy-port guard and auto port-pick.

A busy port used to surface as a raw uvicorn traceback and a non-zero exit; the
guard turns it into an actionable message before uvicorn is ever started.
"""
from __future__ import annotations

import socket
from types import SimpleNamespace

from mininfer.cli import _free_port, _listening_pid, _port_busy, cmd_proxy


def _listener() -> tuple[socket.socket, int]:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    return s, int(s.getsockname()[1])


def test_free_port_is_usable():
    port = _free_port("127.0.0.1")
    assert 1024 <= port <= 65535
    with socket.socket() as s:  # the port we were handed must be bindable
        s.bind(("127.0.0.1", port))


def test_port_busy_detects_a_listener():
    s, port = _listener()
    try:
        assert _port_busy("127.0.0.1", port) is True
    finally:
        s.close()


def test_port_busy_is_false_when_free():
    assert _port_busy("127.0.0.1", _free_port("127.0.0.1")) is False


def test_listening_pid_returns_the_holder():
    s, port = _listener()
    try:
        # lsof may be absent on some hosts; only assert when it can see us.
        pid = _listening_pid(port)
        if pid is not None:
            assert pid > 0
    finally:
        s.close()


def test_cmd_proxy_refuses_a_busy_port(capsys):
    s, port = _listener()
    try:
        rc = cmd_proxy(SimpleNamespace(host="127.0.0.1", port=port,
                                       log_level="info", reload=False))
    finally:
        s.close()
    assert rc == 2
    err = capsys.readouterr().err
    assert "already in use" in err
    assert "--port 0" in err          # suggests the auto-pick escape hatch
    assert f":{port}" in err          # suggests the kill one-liner
