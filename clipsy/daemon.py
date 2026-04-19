from __future__ import annotations

import asyncio
import json
import logging
import signal
from contextlib import suppress
from pathlib import Path
from typing import Any

from .config import AppConfig, effective_socket_path, expand_path, load_config
from .gsr import ManagedProcess, build_record_command, build_replay_command, make_recording_path
from .notify import notify, notify_clip


class ClipBuffer:
    def __init__(self, config: AppConfig, config_path: Path, log_file):
        self.config = config
        self.config_path = config_path
        self.log_file = log_file
        self.process: ManagedProcess | None = None

    def start(self) -> dict[str, Any]:
        if not self.config.clip.enabled:
            return {"ok": True, "running": False, "message": "clip buffer disabled"}
        if self.process and self.process.is_running():
            return {"ok": True, "running": True, "pid": self.process.pid()}
        command = build_replay_command(self.config, self.config_path)
        self.process = ManagedProcess("replay-buffer", command, self.log_file)
        self.process.start()
        return {"ok": True, "running": True, "pid": self.process.pid()}

    def save(self) -> dict[str, Any]:
        if not self.process or not self.process.is_running():
            return {"ok": False, "error": "replay buffer is not running"}
        self.process.send_signal(signal.SIGUSR1)
        notify_clip(self.config.clip.buffer_seconds, self.config.general.notify)
        return {"ok": True, "message": "clip save triggered", "pid": self.process.pid()}

    def stop(self) -> dict[str, Any]:
        if not self.process:
            return {"ok": True, "running": False}
        code = self.process.stop(signal.SIGINT)
        return {"ok": True, "running": False, "returncode": code}

    def status(self) -> dict[str, Any]:
        return {"running": bool(self.process and self.process.is_running()), "pid": self.process.pid() if self.process else None}


class Recorder:
    def __init__(self, config: AppConfig, config_path: Path, log_file):
        self.config = config
        self.config_path = config_path
        self.log_file = log_file
        self.process: ManagedProcess | None = None
        self.output_file: Path | None = None

    def start(self) -> dict[str, Any]:
        if self.process and self.process.is_running():
            return {"ok": True, "running": True, "path": str(self.output_file), "pid": self.process.pid()}
        self.output_file = make_recording_path(self.config)
        command = build_record_command(self.config, self.output_file, self.config_path)
        self.process = ManagedProcess("recording", command, self.log_file)
        self.process.start()
        notify("Clipper", "Recording started", self.config.general.notify)
        return {"ok": True, "running": True, "path": str(self.output_file), "pid": self.process.pid()}

    def stop(self) -> dict[str, Any]:
        if not self.process or not self.process.is_running():
            return {"ok": True, "running": False, "message": "recording was not running"}
        code = self.process.stop(signal.SIGINT, timeout=12)
        notify("Clipper", f"Recording saved: {self.output_file}", self.config.general.notify)
        return {"ok": True, "running": False, "path": str(self.output_file), "returncode": code}

    def toggle(self) -> dict[str, Any]:
        if self.process and self.process.is_running():
            return self.stop()
        return self.start()

    def status(self) -> dict[str, Any]:
        return {
            "running": bool(self.process and self.process.is_running()),
            "pid": self.process.pid() if self.process else None,
            "path": str(self.output_file) if self.output_file else None,
        }


class ClipsyDaemon:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.config = load_config(config_path)
        self.config.ensure_dirs()
        self.socket_path = effective_socket_path(self.config)
        self.log_handle = expand_path(self.config.general.log_file).open("a", encoding="utf-8")
        self.clip = ClipBuffer(self.config, self.config_path, self.log_handle)
        self.recorder = Recorder(self.config, self.config_path, self.log_handle)
        self.server: asyncio.AbstractServer | None = None
        self.clip_paused_for_recording = False
        self.shutting_down = False

    async def run(self) -> None:
        logging.info("starting clipsy daemon")
        with suppress(FileNotFoundError):
            self.socket_path.unlink()
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        self.clip.start()
        self.server = await asyncio.start_unix_server(self._handle_client, path=str(self.socket_path))
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with suppress(NotImplementedError):
                loop.add_signal_handler(sig, lambda sig=sig: asyncio.create_task(self.shutdown(sig)))
        async with self.server:
            try:
                await self.server.serve_forever()
            except asyncio.CancelledError:
                pass

    async def shutdown(self, sig: signal.Signals | None = None) -> None:
        if self.shutting_down:
            return
        self.shutting_down = True
        logging.info("shutting down clipsy daemon")
        self.recorder.stop()
        self.clip.stop()
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        with suppress(FileNotFoundError):
            self.socket_path.unlink()
        if not self.log_handle.closed:
            self.log_handle.close()

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await reader.readline()
            request = json.loads(raw.decode("utf-8"))
            response = await self.handle(request)
        except Exception as exc:  # Keep the daemon alive after malformed IPC.
            logging.exception("IPC handler failed")
            response = {"ok": False, "error": str(exc)}
        writer.write(json.dumps(response).encode("utf-8"))
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        action = request.get("action")
        if action == "clip":
            return self.clip.save()
        if action == "record_toggle":
            return self._record_toggle()
        if action == "record_start":
            return self._record_start()
        if action == "record_stop":
            return self._record_stop()
        if action == "reload_config":
            return self.reload_config()
        if action == "status":
            return {"ok": True, "clip": self.clip.status(), "recording": self.recorder.status()}
        if action == "quit":
            asyncio.create_task(self.shutdown())
            return {"ok": True, "message": "daemon stopping"}
        return {"ok": False, "error": f"unknown action: {action}"}

    def reload_config(self) -> dict[str, Any]:
        was_recording = self.recorder.status()["running"]
        if was_recording:
            return {"ok": False, "error": "stop recording before reloading config"}
        self.clip.stop()
        self.config = load_config(self.config_path)
        self.config.ensure_dirs()
        new_socket_path = effective_socket_path(self.config)
        self.clip = ClipBuffer(self.config, self.config_path, self.log_handle)
        self.recorder = Recorder(self.config, self.config_path, self.log_handle)
        result = self.clip.start()
        message = "config reloaded"
        if new_socket_path != self.socket_path:
            message = "config reloaded; restart daemon to apply socket_path"
        return {"ok": True, "message": message, "clip": result}

    def _record_toggle(self) -> dict[str, Any]:
        if self.recorder.status()["running"]:
            return self._record_stop()
        return self._record_start()

    def _record_start(self) -> dict[str, Any]:
        if self.config.record.pause_replay_buffer and self.clip.status()["running"]:
            self.clip.stop()
            self.clip_paused_for_recording = True
        return self.recorder.start()

    def _record_stop(self) -> dict[str, Any]:
        result = self.recorder.stop()
        if self.clip_paused_for_recording:
            self.clip_paused_for_recording = False
            result["clip_restart"] = self.clip.start()
        return result


def run_daemon(config_path: Path) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    daemon = ClipsyDaemon(config_path)
    try:
        asyncio.run(daemon.run())
    except SystemExit:
        return
    except KeyboardInterrupt:
        return
