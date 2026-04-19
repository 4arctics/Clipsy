from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

from .config import effective_socket_path, load_config


class IpcError(RuntimeError):
    pass


def send_command(
    action: str,
    payload: dict[str, Any] | None = None,
    socket_path: str | Path | None = None,
    timeout: float = 3.0,
) -> dict[str, Any]:
    if socket_path is None:
        socket_path = effective_socket_path(load_config())
    path = Path(socket_path)
    message = {"action": action}
    if payload:
        message.update(payload)

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(str(path))
            client.sendall((json.dumps(message) + "\n").encode("utf-8"))
            chunks: list[bytes] = []
            while True:
                data = client.recv(4096)
                if not data:
                    break
                chunks.append(data)
    except OSError as exc:
        raise IpcError(f"Could not talk to clipsy daemon at {path}: {exc}") from exc

    if not chunks:
        return {"ok": False, "error": "daemon returned no response"}
    try:
        return json.loads(b"".join(chunks).decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise IpcError(f"Daemon returned invalid JSON: {exc}") from exc
