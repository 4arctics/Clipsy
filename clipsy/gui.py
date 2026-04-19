from __future__ import annotations

import json
import os
import shlex
import shutil
import socket
import subprocess
import threading
from dataclasses import asdict, replace
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .config import AppConfig, default_config_path, effective_socket_path, load_config, save_config
from .ipc import IpcError, send_command


def run_gui(config_path: Path | None = None) -> None:
    path = (config_path or default_config_path()).expanduser().resolve()
    server = _ClipsyUiServer(("127.0.0.1", _free_port()), _Handler, path)
    url = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        _run_app_window(url, server)
    except RuntimeError:
        print(f"Clipsy UI open in your browser: {url}")
        print("Press Ctrl+C or click 'Close UI' in the browser to quit.")
        subprocess.Popen(["xdg-open", url])
        try:
            thread.join()
        except KeyboardInterrupt:
            pass
    except KeyboardInterrupt:
        pass
    finally:
        _shutdown_server(server)


class _ClipsyUiServer(ThreadingHTTPServer):
    def __init__(self, server_address, handler_class, config_path: Path):
        super().__init__(server_address, handler_class)
        self.config_path = config_path


class _Handler(BaseHTTPRequestHandler):
    server: _ClipsyUiServer

    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        try:
            self._do_GET()
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _do_GET(self) -> None:
        if self.path in {"/", "/index.html"}:
            self._send_html(APP_HTML)
            return
        if self.path == "/assets/clipsy.svg":
            self._send_bytes(_read_asset("clipsy.svg"), "image/svg+xml")
            return
        if self.path == "/api/config":
            self._send_json({"ok": True, "config": _config_to_dict(load_config(self.server.config_path))})
            return
        if self.path == "/api/status":
            self._send_json(_daemon_command("status", self.server.config_path))
            return
        if self.path == "/api/devices":
            self._send_json({"ok": True, "devices": _list_devices()})
            return
        if self.path == "/api/hyprland":
            config = load_config(self.server.config_path)
            self._send_json({"ok": True, "hyprland": _hyprland_status(config)})
            return
        self._send_json({"ok": False, "error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        try:
            self._do_POST()
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _do_POST(self) -> None:
        if self.path == "/api/config":
            body = self._read_json()
            config = _config_from_dict(body.get("config", {}), load_config(self.server.config_path))
            saved_path = save_config(config, self.server.config_path)
            self._send_json({"ok": True, "config": _config_to_dict(config), "message": f"Settings saved to {saved_path}"})
            return
        if self.path == "/api/action":
            body = self._read_json()
            action = str(body.get("action", ""))
            if action == "close_ui":
                self._send_json({"ok": True, "message": "UI closing"})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            if action == "install_hyprland":
                if "config" in body:
                    config = _config_from_dict(body.get("config", {}), load_config(self.server.config_path))
                    save_config(config, self.server.config_path)
                else:
                    config = load_config(self.server.config_path)
                self._send_json(_install_hyprland_binds(config))
                return
            self._send_json(_daemon_command(action, self.server.config_path))
            return
        self._send_json({"ok": False, "error": "not found"}, HTTPStatus.NOT_FOUND)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    def _send_html(self, content: str) -> None:
        data = content.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_bytes(self, data: bytes, content_type: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _run_app_window(url: str, server: _ClipsyUiServer) -> None:
    try:
        from PySide6.QtCore import QEvent, QObject, QUrl, Qt
        from PySide6.QtGui import QIcon, QKeyEvent
        from PySide6.QtWebEngineWidgets import QWebEngineView
        from PySide6.QtWidgets import QApplication, QMainWindow
    except ImportError as exc:
        raise RuntimeError(
            "The native Clipsy window needs PySide6 with Qt WebEngine installed."
        ) from exc

    _MOD_NAMES: dict[int, str] = {}
    for _attr, _mod_name in [
        ("Key_Alt", "ALT"), ("Key_AltGr", "ALT"),
        ("Key_Control", "CTRL"), ("Key_Meta", "SUPER"),
        ("Key_Super_L", "SUPER"), ("Key_Super_R", "SUPER"),
        ("Key_Shift", "SHIFT"),
    ]:
        _kv = getattr(Qt.Key, _attr, None)
        if _kv is not None:
            _MOD_NAMES[int(_kv)] = _mod_name

    _MOD_ORDER = ["SUPER", "CTRL", "ALT", "SHIFT"]
    _SPECIAL: dict[int, str] = {
        int(Qt.Key.Key_Space): "SPACE",
        int(Qt.Key.Key_Return): "RETURN",
        int(Qt.Key.Key_Enter): "RETURN",
        int(Qt.Key.Key_Tab): "TAB",
        int(Qt.Key.Key_Backspace): "BACKSPACE",
        int(Qt.Key.Key_Delete): "DELETE",
        int(Qt.Key.Key_Up): "UP",
        int(Qt.Key.Key_Down): "DOWN",
        int(Qt.Key.Key_Left): "LEFT",
        int(Qt.Key.Key_Right): "RIGHT",
        int(Qt.Key.Key_Home): "HOME",
        int(Qt.Key.Key_End): "END",
        int(Qt.Key.Key_PageUp): "PAGEUP",
        int(Qt.Key.Key_PageDown): "PAGEDOWN",
        int(Qt.Key.Key_Insert): "INSERT",
    }
    _KEY_F1 = int(Qt.Key.Key_F1)
    _KEY_F_MAX = int(getattr(Qt.Key, "Key_F35", Qt.Key.Key_F12))

    class _HotkeyFilter(QObject):
        def __init__(self) -> None:
            super().__init__()
            self._held: set[str] = set()

        def eventFilter(self, obj: object, event: object) -> bool:  # type: ignore[override]
            try:
                t = event.type()  # type: ignore[attr-defined]
                if t == QEvent.Type.FocusOut:
                    self._held.clear()
                    return False
                if not isinstance(event, QKeyEvent):
                    return False
                if t == QEvent.Type.KeyPress:
                    k = event.key()
                    mod = _MOD_NAMES.get(k)
                    if mod:
                        self._held.add(mod)
                        return False
                    if k == int(Qt.Key.Key_Escape):
                        view.page().runJavaScript("if(window._qtHotkey)window._qtHotkey(null)")
                        return False
                    if self._held:
                        name = self._key_name(k, event)
                        if name:
                            mods = " ".join(m for m in _MOD_ORDER if m in self._held)
                            combo = json.dumps(f"{mods},{name}")
                            view.page().runJavaScript(f"if(window._qtHotkey)window._qtHotkey({combo})")
                elif t == QEvent.Type.KeyRelease:
                    mod = _MOD_NAMES.get(event.key())
                    if mod:
                        self._held.discard(mod)
            except Exception:
                pass
            return False

        def _key_name(self, k: int, event: QKeyEvent) -> str:
            if _KEY_F1 <= k <= _KEY_F_MAX:
                return f"F{k - _KEY_F1 + 1}"
            if k in _SPECIAL:
                return _SPECIAL[k]
            text: str = event.text()
            if text and text.isprintable() and len(text) == 1:
                return text.upper()
            return ""

    class ClipsyWindow(QMainWindow):
        def closeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
            _shutdown_server(server)
            super().closeEvent(event)

    app = QApplication.instance() or QApplication([])
    app.setApplicationName("Clipsy")
    app.setDesktopFileName("clipsy")
    app.setWindowIcon(QIcon(str(_asset_path("clipsy.svg"))))

    view = QWebEngineView()
    view.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
    view.setUrl(QUrl(url))

    hotkey_filter = _HotkeyFilter()
    view.installEventFilter(hotkey_filter)

    window = ClipsyWindow()
    window.setWindowTitle("Clipsy Control")
    window.setWindowIcon(QIcon(str(_asset_path("clipsy.svg"))))
    window.resize(1280, 820)
    window.setMinimumSize(980, 660)
    window.menuBar().hide()
    window.setCentralWidget(view)
    view.page().windowCloseRequested.connect(window.close)
    window.show()
    app.exec()


def _shutdown_server(server: _ClipsyUiServer) -> None:
    try:
        server.shutdown()
    except Exception:
        pass
    try:
        server.server_close()
    except Exception:
        pass


def _asset_path(name: str) -> Path:
    return Path(__file__).resolve().parent.parent / "assets" / name


def _read_asset(name: str) -> bytes:
    return _asset_path(name).read_bytes()


HYPRLAND_BEGIN = "# >>> clipsy hotkeys >>>"
HYPRLAND_END = "# <<< clipsy hotkeys <<<"


def _hyprland_config_path() -> Path:
    configured = os.environ.get("HYPRLAND_CONFIG")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path("~/.config/hypr/hyprland.conf").expanduser().resolve()


def _hyprland_status(config: AppConfig) -> dict[str, Any]:
    path = _hyprland_config_path()
    return {
        "path": str(path),
        "exists": path.exists(),
        "installed": _hyprland_block_installed(path),
        "preview": _hyprland_block(config).splitlines(),
        "hotkeys": _parse_hyprland_hotkeys(path),
    }


def _parse_hyprland_hotkeys(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    if HYPRLAND_BEGIN not in text or HYPRLAND_END not in text:
        return {}
    _, rest = text.split(HYPRLAND_BEGIN, 1)
    block, _ = rest.split(HYPRLAND_END, 1)
    result: dict[str, str] = {}
    for line in block.splitlines():
        stripped = line.strip()
        if not stripped.startswith("bind"):
            continue
        eq = stripped.find("=")
        if eq == -1:
            continue
        parts = [p.strip() for p in stripped[eq + 1 :].split(",", 3)]
        if len(parts) < 4 or parts[2] != "exec":
            continue
        mods, key, command = parts[0], parts[1], parts[3].strip()
        hotkey = f"{mods},{key}" if mods else key
        if _cmd_ends_with(command, "clipsy clip"):
            result["clip"] = hotkey
        elif _cmd_ends_with(command, "clipsy record-toggle"):
            result["record_toggle"] = hotkey
        elif _cmd_ends_with(command, "clipsy reload"):
            result["reload_config"] = hotkey
    return result


def _cmd_ends_with(command: str, suffix: str) -> bool:
    return command == suffix or command.endswith(f" {suffix}")


def _install_hyprland_binds(config: AppConfig) -> dict[str, Any]:
    path = _hyprland_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    backup_path: str | None = None
    if path.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = path.with_name(f"{path.name}.clipsy-backup-{stamp}")
        backup.write_text(original, encoding="utf-8")
        backup_path = str(backup)

    block = _hyprland_block(config)
    updated = _replace_managed_block(original, block)
    path.write_text(updated, encoding="utf-8")

    reload_result = _reload_hyprland()
    message = "Hotkeys installed in hyprland.conf"
    if reload_result:
        message += "; Hyprland reloaded"
    else:
        message += "; run hyprctl reload if they do not work yet"
    return {
        "ok": True,
        "message": message,
        "path": str(path),
        "backup": backup_path,
        "reloaded": reload_result,
    }


def _hyprland_block_installed(path: Path) -> bool:
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    return HYPRLAND_BEGIN in text and HYPRLAND_END in text


def _replace_managed_block(original: str, block: str) -> str:
    if HYPRLAND_BEGIN in original and HYPRLAND_END in original:
        before, rest = original.split(HYPRLAND_BEGIN, 1)
        _old, after = rest.split(HYPRLAND_END, 1)
        return before.rstrip() + "\n\n" + block + after
    return original.rstrip() + "\n\n" + block + "\n"


def _hyprland_block(config: AppConfig) -> str:
    lines = [
        HYPRLAND_BEGIN,
        "# Managed by Clipsy. Change these in the Clipsy app, then click Install hotkeys.",
        _hyprland_bind(config.hotkeys.clip, _clipsy_command("clip")),
        _hyprland_bind(config.hotkeys.record_toggle, _clipsy_command("record-toggle")),
        _hyprland_bind(config.hotkeys.reload_config, _clipsy_command("reload")),
        HYPRLAND_END,
    ]
    return "\n".join(lines)


def _hyprland_bind(hotkey: str, command: str) -> str:
    mods, key = _split_hotkey(hotkey)
    return f"bind = {mods}, {key}, exec, {command}"


def _split_hotkey(hotkey: str) -> tuple[str, str]:
    raw = hotkey.replace("+", ",").strip()
    if "," in raw:
        mods, key = raw.rsplit(",", 1)
    else:
        parts = raw.split()
        mods, key = (" ".join(parts[:-1]), parts[-1]) if len(parts) > 1 else ("SUPER", raw)
    mods = " ".join(part.strip().upper() for part in mods.replace(",", " ").split() if part.strip())
    key = key.strip().upper()
    return mods or "SUPER", key or "F9"


def _clipsy_command(subcommand: str) -> str:
    if shutil.which("clipsy"):
        return f"clipsy {subcommand}"
    root = Path(__file__).resolve().parent.parent
    return f"cd {shlex.quote(str(root))} && /usr/bin/python -m clipsy {subcommand}"


def _reload_hyprland() -> bool:
    if not shutil.which("hyprctl"):
        return False
    result = subprocess.run(
        ["hyprctl", "reload"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=5,
    )
    return result.returncode == 0


def _daemon_command(action: str, config_path: Path) -> dict[str, Any]:
    action_map = {
        "clip": "clip",
        "record_toggle": "record_toggle",
        "record_start": "record_start",
        "record_stop": "record_stop",
        "reload_config": "reload_config",
        "status": "status",
        "quit": "quit",
    }
    if action not in action_map:
        return {"ok": False, "error": f"unknown action: {action}"}
    try:
        socket_path = effective_socket_path(load_config(config_path))
        return send_command(action_map[action], socket_path=socket_path)
    except IpcError as exc:
        msg = str(exc)
        if "No such file or directory" in msg or "Connection refused" in msg:
            return {"ok": False, "error": "Daemon is not running. Start it with: clipsy daemon"}
        return {"ok": False, "error": msg}


def _list_devices() -> dict[str, list[dict[str, str]]]:
    return {
        "capture": _run_device_command(["gpu-screen-recorder", "--list-capture-options"]),
        "monitors": _run_device_command(["gpu-screen-recorder", "--list-monitors"]),
        "audio": _run_device_command(["gpu-screen-recorder", "--list-audio-devices"]),
    }


def _run_device_command(command: list[str]) -> list[dict[str, str]]:
    try:
        result = subprocess.run(command, check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as exc:
        return [{"id": "", "label": str(exc)}]
    if result.returncode != 0:
        return [{"id": "", "label": result.stderr.strip() or "command failed"}]
    devices = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        item_id, _, label = line.partition("|")
        devices.append({"id": item_id.strip(), "label": (label or item_id).strip()})
    return devices


def _config_to_dict(config: AppConfig) -> dict[str, Any]:
    return asdict(config)


def _config_from_dict(raw: dict[str, Any], current: AppConfig) -> AppConfig:
    clip = raw.get("clip", {})
    record = raw.get("record", {})
    audio = raw.get("audio", {})
    voice_gate = raw.get("voice_gate", {})
    hotkeys = raw.get("hotkeys", {})
    general = raw.get("general", {})

    return AppConfig(
        general=replace(
            current.general,
            socket_path=_str(general, "socket_path", current.general.socket_path),
            notify=_bool(general, "notify", current.general.notify),
            log_file=_str(general, "log_file", current.general.log_file),
        ),
        clip=replace(
            current.clip,
            enabled=_bool(clip, "enabled", current.clip.enabled),
            capture=_str(clip, "capture", current.clip.capture),
            buffer_seconds=_int(clip, "buffer_seconds", current.clip.buffer_seconds),
            resolution=_str(clip, "resolution", current.clip.resolution),
            fps=_int(clip, "fps", current.clip.fps),
            save_path=_str(clip, "save_path", current.clip.save_path),
            container=_str(clip, "container", current.clip.container),
            codec=_str(clip, "codec", current.clip.codec),
            quality=_str(clip, "quality", current.clip.quality),
            bitrate_mode=_str(clip, "bitrate_mode", current.clip.bitrate_mode),
            framerate_mode=_str(clip, "framerate_mode", current.clip.framerate_mode),
            replay_storage=_str(clip, "replay_storage", current.clip.replay_storage),
            restart_on_save=_bool(clip, "restart_on_save", current.clip.restart_on_save),
            restore_portal_session=_bool(clip, "restore_portal_session", current.clip.restore_portal_session),
            cursor=_bool(clip, "cursor", current.clip.cursor),
        ),
        record=replace(
            current.record,
            capture=_str(record, "capture", current.record.capture),
            resolution=_str(record, "resolution", current.record.resolution),
            fps=_int(record, "fps", current.record.fps),
            save_path=_str(record, "save_path", current.record.save_path),
            container=_str(record, "container", current.record.container),
            codec=_str(record, "codec", current.record.codec),
            quality=_str(record, "quality", current.record.quality),
            bitrate_mode=_str(record, "bitrate_mode", current.record.bitrate_mode),
            framerate_mode=_str(record, "framerate_mode", current.record.framerate_mode),
            restore_portal_session=_bool(record, "restore_portal_session", current.record.restore_portal_session),
            cursor=_bool(record, "cursor", current.record.cursor),
            pause_replay_buffer=_bool(record, "pause_replay_buffer", current.record.pause_replay_buffer),
        ),
        audio=replace(
            current.audio,
            desktop_enabled=_bool(audio, "desktop_enabled", current.audio.desktop_enabled),
            desktop_source=_str(audio, "desktop_source", current.audio.desktop_source),
            mic_mode=_str(audio, "mic_mode", current.audio.mic_mode),
            mic_source=_str(audio, "mic_source", current.audio.mic_source),
            separate_tracks=_bool(audio, "separate_tracks", current.audio.separate_tracks),
            audio_codec=_str(audio, "audio_codec", current.audio.audio_codec),
            bitrate_kbps=_int(audio, "bitrate_kbps", current.audio.bitrate_kbps),
        ),
        voice_gate=replace(
            current.voice_gate,
            enabled=_bool(voice_gate, "enabled", current.voice_gate.enabled),
            threshold=_str(voice_gate, "threshold", current.voice_gate.threshold),
            ratio=_float(voice_gate, "ratio", current.voice_gate.ratio),
            attack_ms=_int(voice_gate, "attack_ms", current.voice_gate.attack_ms),
            release_ms=_int(voice_gate, "release_ms", current.voice_gate.release_ms),
            output_audio_codec=_str(voice_gate, "output_audio_codec", current.voice_gate.output_audio_codec),
            keep_raw_copy=_bool(voice_gate, "keep_raw_copy", current.voice_gate.keep_raw_copy),
        ),
        hotkeys=replace(
            current.hotkeys,
            clip=_str(hotkeys, "clip", current.hotkeys.clip),
            record_toggle=_str(hotkeys, "record_toggle", current.hotkeys.record_toggle),
            reload_config=_str(hotkeys, "reload_config", current.hotkeys.reload_config),
        ),
    )


def _str(data: dict[str, Any], key: str, fallback: str) -> str:
    value = data.get(key, fallback)
    return str(value)


def _int(data: dict[str, Any], key: str, fallback: int) -> int:
    try:
        return int(data.get(key, fallback))
    except (TypeError, ValueError):
        return fallback


def _float(data: dict[str, Any], key: str, fallback: float) -> float:
    try:
        return float(data.get(key, fallback))
    except (TypeError, ValueError):
        return fallback


def _bool(data: dict[str, Any], key: str, fallback: bool) -> bool:
    value = data.get(key, fallback)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in {"true", "1", "yes", "on"}
    return bool(value)


APP_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="dark">
  <title>Clipsy Control</title>
  <link rel="icon" href="/assets/clipsy.svg" type="image/svg+xml">
  <style>
    :root {
      --bg: #080b09;
      --panel: #101410;
      --panel-2: #151a15;
      --panel-3: #1c231c;
      --text: #edf6e9;
      --muted: #9aa895;
      --faint: #65705f;
      --line: rgba(205, 244, 190, 0.13);
      --line-strong: rgba(205, 244, 190, 0.24);
      --green: #76f043;
      --green-2: #a9ff70;
      --red: #ff5555;
      --amber: #ffd166;
      --shadow: rgba(1, 6, 2, 0.52);
      --ease: cubic-bezier(0.2, 0.8, 0.2, 1);
      color-scheme: dark;
      font-family: "Geist", "Satoshi", "SF Pro Display", system-ui, sans-serif;
      font-size: 16px;
      letter-spacing: 0;
    }

    * { box-sizing: border-box; }

    html, body {
      min-height: 100%;
      margin: 0;
      background:
        linear-gradient(115deg, rgba(118, 240, 67, 0.08), transparent 24%),
        linear-gradient(180deg, #0d120d 0%, var(--bg) 44%, #050806 100%);
      color: var(--text);
    }

    body::before {
      content: "";
      position: fixed;
      inset: 0;
      pointer-events: none;
      z-index: 1;
      opacity: 0.18;
      background-image:
        repeating-linear-gradient(0deg, rgba(255,255,255,0.045) 0 1px, transparent 1px 4px),
        repeating-linear-gradient(90deg, rgba(118,240,67,0.035) 0 1px, transparent 1px 56px);
      mix-blend-mode: soft-light;
    }

    button, input, select {
      font: inherit;
      letter-spacing: 0;
    }

    button {
      border: 0;
      cursor: pointer;
    }

    .app {
      position: relative;
      z-index: 2;
      width: min(1420px, calc(100% - 48px));
      min-height: 100dvh;
      margin: 0 auto;
      padding: 28px 0 40px;
      display: grid;
      grid-template-rows: auto 1fr;
      gap: 20px;
    }

    .shell {
      display: grid;
      grid-template-columns: 310px minmax(0, 1fr);
      min-height: calc(100dvh - 128px);
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
      background: rgba(11, 15, 11, 0.92);
      box-shadow: 0 28px 80px var(--shadow);
    }

    .topbar {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      align-items: center;
      gap: 16px;
      min-height: 78px;
      padding: 0 4px;
    }

    .brand {
      display: flex;
      align-items: center;
      gap: 16px;
      min-width: 0;
    }

    .mark {
      width: 48px;
      height: 48px;
      border-radius: 8px;
      display: grid;
      place-items: center;
      background: #050805;
      border: 1px solid rgba(118, 240, 67, 0.38);
      box-shadow: 0 12px 34px rgba(118, 240, 67, 0.24), inset 0 1px 0 rgba(255,255,255,0.08);
      overflow: hidden;
    }

    .mark img {
      width: 100%;
      height: 100%;
      display: block;
    }

    .brand h1 {
      margin: 0;
      font-size: 1.45rem;
      line-height: 1.1;
      font-weight: 780;
    }

    .brand p {
      margin: 5px 0 0;
      color: var(--muted);
      font-size: 0.92rem;
      line-height: 1.4;
    }

    .top-actions {
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }

    .status-pill {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      min-height: 38px;
      padding: 0 14px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: rgba(15, 21, 15, 0.72);
      color: var(--muted);
      font-size: 0.9rem;
    }

    .dot {
      width: 8px;
      height: 8px;
      border-radius: 99px;
      background: var(--faint);
      box-shadow: 0 0 0 4px rgba(101, 112, 95, 0.14);
    }

    .dot.live {
      background: var(--green);
      box-shadow: 0 0 0 4px rgba(118, 240, 67, 0.14), 0 0 22px rgba(118, 240, 67, 0.62);
    }

    .rail {
      padding: 22px;
      background:
        linear-gradient(180deg, rgba(20, 28, 19, 0.96), rgba(9, 12, 9, 0.98)),
        repeating-linear-gradient(135deg, rgba(255,255,255,0.035) 0 1px, transparent 1px 9px);
      border-right: 1px solid var(--line);
      display: flex;
      flex-direction: column;
      gap: 18px;
    }

    .preview {
      min-height: 184px;
      border: 1px solid var(--line-strong);
      border-radius: 8px;
      padding: 12px;
      background: #060906;
      position: relative;
      overflow: hidden;
    }

    .preview::before {
      content: "";
      position: absolute;
      inset: 0;
      background:
        linear-gradient(90deg, transparent 0 47%, rgba(118, 240, 67, 0.18) 48% 52%, transparent 53%),
        linear-gradient(180deg, rgba(118, 240, 67, 0.12), transparent 60%);
      transform: translateX(-35%);
      animation: sweep 5.2s var(--ease) infinite;
    }

    .preview-grid {
      position: relative;
      height: 100%;
      min-height: 158px;
      display: grid;
      grid-template-columns: repeat(8, 1fr);
      grid-template-rows: repeat(5, 1fr);
      gap: 4px;
    }

    .tile {
      border-radius: 4px;
      background: rgba(118, 240, 67, 0.08);
      border: 1px solid rgba(118, 240, 67, 0.11);
    }

    .tile.big {
      grid-column: 1 / 6;
      grid-row: 1 / 5;
      background:
        linear-gradient(135deg, rgba(118, 240, 67, 0.13), rgba(255,255,255,0.035)),
        #111811;
    }

    .tile.side { grid-column: 6 / 9; }
    .tile.timeline {
      grid-column: 1 / 9;
      grid-row: 5;
      display: flex;
      align-items: end;
      gap: 3px;
      padding: 8px;
    }

    .bar {
      width: 100%;
      border-radius: 3px;
      background: var(--green);
      opacity: 0.8;
      transform-origin: bottom;
      animation: pulse 1.9s var(--ease) infinite;
    }

    .bar:nth-child(2n) { animation-delay: 0.18s; opacity: 0.52; }
    .bar:nth-child(3n) { animation-delay: 0.31s; opacity: 0.7; }

    @keyframes pulse {
      0%, 100% { transform: scaleY(0.55); }
      50% { transform: scaleY(1); }
    }

    @keyframes sweep {
      0%, 100% { transform: translateX(-52%); opacity: 0; }
      36%, 64% { opacity: 1; }
      100% { transform: translateX(55%); }
    }

    .nav {
      display: grid;
      gap: 8px;
    }

    .tab {
      height: 48px;
      border-radius: 8px;
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 0 13px;
      color: var(--muted);
      background: transparent;
      border: 1px solid transparent;
      transition: transform 220ms var(--ease), background 220ms var(--ease), color 220ms var(--ease), border-color 220ms var(--ease);
    }

    .tab svg {
      width: 19px;
      height: 19px;
      flex: 0 0 auto;
    }

    .tab:hover,
    .tab.active {
      color: var(--text);
      background: rgba(118, 240, 67, 0.09);
      border-color: rgba(118, 240, 67, 0.18);
    }

    .tab:active { transform: scale(0.985); }

    .rail-footer {
      margin-top: auto;
      padding: 14px;
      border-radius: 8px;
      border: 1px solid var(--line);
      color: var(--muted);
      background: rgba(255, 255, 255, 0.025);
      font-size: 0.85rem;
      line-height: 1.45;
    }

    main {
      min-width: 0;
      display: grid;
      grid-template-rows: auto 1fr auto;
      background:
        linear-gradient(180deg, rgba(16, 21, 16, 0.86), rgba(8, 11, 8, 0.96));
    }

    .hero {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 20px;
      align-items: center;
      padding: 26px 28px 22px;
      border-bottom: 1px solid var(--line);
    }

    .hero h2 {
      margin: 0;
      font-size: 2rem;
      line-height: 1.05;
      font-weight: 820;
    }

    .hero p {
      margin: 8px 0 0;
      max-width: 68ch;
      color: var(--muted);
      line-height: 1.5;
    }

    .hero-meta {
      display: grid;
      grid-template-columns: repeat(3, minmax(94px, 1fr));
      gap: 8px;
      min-width: 320px;
    }

    .metric {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 11px 12px;
      background: rgba(255,255,255,0.027);
    }

    .metric strong {
      display: block;
      font-size: 1.05rem;
      font-variant-numeric: tabular-nums;
      color: var(--text);
    }

    .metric span {
      display: block;
      margin-top: 3px;
      color: var(--faint);
      font-size: 0.78rem;
    }

    .content {
      padding: 28px;
      display: grid;
      gap: 18px;
      align-content: start;
    }

    .panel {
      display: none;
      animation: enter 380ms var(--ease) both;
    }

    .panel.active {
      display: grid;
      gap: 18px;
    }

    @keyframes enter {
      from { opacity: 0; transform: translateY(12px); }
      to { opacity: 1; transform: translateY(0); }
    }

    .settings-grid {
      display: grid;
      grid-template-columns: repeat(12, 1fr);
      gap: 14px;
    }

    .group {
      grid-column: span 6;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: rgba(14, 19, 14, 0.82);
      overflow: hidden;
    }

    .group.wide { grid-column: span 12; }
    .group.third { grid-column: span 4; }

    .group-head {
      min-height: 62px;
      padding: 16px 18px;
      border-bottom: 1px solid var(--line);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      background: rgba(255,255,255,0.018);
    }

    .group-head h3 {
      margin: 0;
      font-size: 1rem;
      font-weight: 720;
    }

    .group-head span {
      color: var(--faint);
      font-size: 0.82rem;
    }

    .group-body {
      padding: 16px;
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 13px;
    }

    .group.wide .group-body {
      grid-template-columns: repeat(4, minmax(0, 1fr));
    }

    .group-body.simple {
      grid-template-columns: repeat(3, minmax(0, 1fr));
    }

    .field {
      display: grid;
      gap: 7px;
      min-width: 0;
    }

    .field.full { grid-column: 1 / -1; }

    .field-help {
      color: var(--faint);
      font-size: 0.78rem;
      line-height: 1.35;
    }

    .plain-help {
      color: var(--muted);
      line-height: 1.5;
      margin: 0;
    }

    .hidden-bind { display: none; }

    .advanced {
      grid-column: 1 / -1;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: rgba(8, 12, 8, 0.58);
      overflow: hidden;
    }

    .advanced summary {
      min-height: 42px;
      display: flex;
      align-items: center;
      padding: 0 12px;
      color: var(--muted);
      cursor: pointer;
      user-select: none;
    }

    .advanced-grid {
      border-top: 1px solid var(--line);
      padding: 13px;
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 13px;
    }

    .hypr-preview {
      display: grid;
      gap: 8px;
      margin-top: 10px;
      color: var(--muted);
      font-family: "Geist Mono", "Cascadia Mono", ui-monospace, monospace;
      font-size: 0.8rem;
      line-height: 1.45;
    }

    .hypr-preview div {
      padding: 9px 10px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: rgba(8,12,8,0.62);
      overflow-wrap: anywhere;
    }

    .hotkey-list {
      display: grid;
      gap: 12px;
    }

    .hotkey-card {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 12px;
      align-items: center;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 13px;
      background: rgba(8,12,8,0.62);
    }

    .hotkey-card strong {
      display: block;
      margin-bottom: 3px;
      font-weight: 720;
    }

    .hotkey-card span {
      color: var(--faint);
      font-size: 0.82rem;
    }

    .hotkey-capture {
      min-width: 148px;
      justify-content: center;
      font-family: "Geist Mono", "Cascadia Mono", ui-monospace, monospace;
      font-size: 0.88rem;
    }

    .hotkey-capture.listening {
      color: #071006;
      background: var(--green);
      border-color: transparent;
    }

    label {
      color: var(--muted);
      font-size: 0.8rem;
      line-height: 1.2;
    }

    input,
    select {
      width: 100%;
      min-height: 42px;
      border-radius: 8px;
      border: 1px solid var(--line);
      color: var(--text);
      background: #080c08;
      padding: 0 12px;
      outline: none;
      transition: border-color 200ms var(--ease), background 200ms var(--ease), box-shadow 200ms var(--ease);
    }

    input:focus,
    select:focus {
      border-color: rgba(118, 240, 67, 0.54);
      box-shadow: 0 0 0 3px rgba(118, 240, 67, 0.12);
    }

    select {
      appearance: none;
      background-image:
        linear-gradient(45deg, transparent 50%, var(--muted) 50%),
        linear-gradient(135deg, var(--muted) 50%, transparent 50%);
      background-position:
        calc(100% - 18px) 18px,
        calc(100% - 13px) 18px;
      background-size: 5px 5px, 5px 5px;
      background-repeat: no-repeat;
    }

    .switch-field {
      min-height: 42px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 10px 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: rgba(8, 12, 8, 0.74);
    }

    .switch-field label {
      color: var(--text);
      font-size: 0.88rem;
    }

    .switch {
      position: relative;
      width: 48px;
      height: 26px;
      flex: 0 0 auto;
    }

    .switch input {
      position: absolute;
      opacity: 0;
      inset: 0;
    }

    .slider {
      position: absolute;
      inset: 0;
      border-radius: 99px;
      background: #2a3129;
      border: 1px solid var(--line);
      transition: background 220ms var(--ease), border-color 220ms var(--ease);
    }

    .slider::after {
      content: "";
      position: absolute;
      width: 20px;
      height: 20px;
      left: 2px;
      top: 2px;
      border-radius: 99px;
      background: #c6d0bf;
      transition: transform 220ms var(--ease), background 220ms var(--ease);
    }

    .switch input:checked + .slider {
      background: rgba(118, 240, 67, 0.26);
      border-color: rgba(118, 240, 67, 0.46);
    }

    .switch input:checked + .slider::after {
      transform: translateX(22px);
      background: var(--green);
    }

    .actions {
      padding: 18px 28px;
      border-top: 1px solid var(--line);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 14px;
      flex-wrap: wrap;
      background: rgba(4, 7, 4, 0.82);
    }

    .btn-row {
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
    }

    .btn {
      min-height: 42px;
      border-radius: 8px;
      padding: 0 14px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 9px;
      color: var(--text);
      background: rgba(255,255,255,0.06);
      border: 1px solid var(--line);
      transition: transform 200ms var(--ease), background 200ms var(--ease), border-color 200ms var(--ease), color 200ms var(--ease);
    }

    .btn:hover {
      background: rgba(255,255,255,0.09);
      border-color: var(--line-strong);
    }

    .btn:active { transform: scale(0.98); }

    .btn.primary {
      color: #071006;
      background: var(--green);
      border-color: transparent;
      box-shadow: 0 12px 28px rgba(118, 240, 67, 0.22);
      font-weight: 760;
    }

    .btn.primary:hover { background: var(--green-2); }
    .btn.danger { color: #ffd8d8; border-color: rgba(255, 85, 85, 0.24); background: rgba(255, 85, 85, 0.08); }
    .btn svg { width: 18px; height: 18px; }

    .toast {
      min-height: 42px;
      display: flex;
      align-items: center;
      color: var(--muted);
      font-size: 0.9rem;
    }

    .toast.good { color: var(--green-2); }
    .toast.bad { color: #ffb0b0; }

    .device-list {
      display: grid;
      gap: 8px;
      max-height: 300px;
      overflow: auto;
      padding-right: 4px;
    }

    .device {
      display: grid;
      grid-template-columns: minmax(0, 180px) minmax(0, 1fr);
      gap: 10px;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px 12px;
      background: rgba(8,12,8,0.62);
      color: var(--muted);
      font-size: 0.85rem;
    }

    .device code {
      color: var(--green-2);
      overflow-wrap: anywhere;
      font-family: "Geist Mono", "Cascadia Mono", ui-monospace, monospace;
      font-size: 0.8rem;
    }

    .shortcut {
      display: grid;
      gap: 8px;
      color: var(--muted);
      font-family: "Geist Mono", "Cascadia Mono", ui-monospace, monospace;
      font-size: 0.86rem;
      line-height: 1.45;
    }

    .shortcut div {
      padding: 10px 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: rgba(8,12,8,0.65);
      overflow-wrap: anywhere;
    }

    @media (max-width: 1020px) {
      .app { width: min(100% - 28px, 760px); }
      .shell { grid-template-columns: 1fr; }
      .rail { border-right: 0; border-bottom: 1px solid var(--line); }
      .nav { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .preview { display: none; }
      .hero { grid-template-columns: 1fr; }
      .hero-meta { min-width: 0; }
      .group,
      .group.third { grid-column: span 12; }
      .group.wide .group-body,
      .group-body.simple,
      .advanced-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }

    @media (max-width: 640px) {
      .app { width: 100%; padding: 0; }
      .topbar, .shell { border-radius: 0; }
      .topbar { padding: 14px; grid-template-columns: 1fr; }
      .shell { min-height: 100dvh; border-left: 0; border-right: 0; }
      .rail, .hero, .content, .actions { padding: 18px; }
      .nav { grid-template-columns: 1fr; }
      .hero h2 { font-size: 1.55rem; }
      .hero-meta { grid-template-columns: 1fr; }
      .group-body,
      .group.wide .group-body,
      .group-body.simple,
      .advanced-grid { grid-template-columns: 1fr; }
      .actions { align-items: stretch; }
      .btn-row, .btn { width: 100%; }
      .device { grid-template-columns: 1fr; }
      .hotkey-card { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div class="app">
    <header class="topbar">
      <div class="brand">
        <div class="mark" aria-hidden="true">
          <img src="/assets/clipsy.svg" alt="">
        </div>
        <div>
          <h1>Clipsy Control</h1>
          <p>Replay buffer, full recording, audio capture, and Hyprland triggers.</p>
        </div>
      </div>
      <div class="top-actions">
        <div class="status-pill"><span id="daemonDot" class="dot"></span><span id="daemonText">Checking daemon</span></div>
        <button class="btn" data-action="reload_config" title="Reload daemon config">
          <svg viewBox="0 0 24 24" fill="none"><path d="M20 12a8 8 0 1 1-2.35-5.66M20 4v5h-5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>
          Reload
        </button>
        <button class="btn danger" data-action="close_ui" title="Close this UI">
          <svg viewBox="0 0 24 24" fill="none"><path d="M7 7l10 10M17 7 7 17" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"/></svg>
          Close UI
        </button>
      </div>
    </header>

    <div class="shell">
      <aside class="rail">
        <div class="preview" aria-hidden="true">
          <div class="preview-grid">
            <div class="tile big"></div>
            <div class="tile side"></div>
            <div class="tile side"></div>
            <div class="tile side"></div>
            <div class="tile timeline">
              <span class="bar" style="height: 38%"></span><span class="bar" style="height: 64%"></span><span class="bar" style="height: 42%"></span><span class="bar" style="height: 84%"></span><span class="bar" style="height: 52%"></span><span class="bar" style="height: 71%"></span><span class="bar" style="height: 34%"></span><span class="bar" style="height: 60%"></span>
            </div>
          </div>
        </div>

        <nav class="nav" aria-label="Settings sections">
          <button class="tab active" data-tab="clips">
            <svg viewBox="0 0 24 24" fill="none"><path d="M5 6h14v12H5zM9 9.5v5l5-2.5-5-2.5Z" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/></svg>
            Clips
          </button>
          <button class="tab" data-tab="record">
            <svg viewBox="0 0 24 24" fill="none"><path d="M7 7h10v10H7z" stroke="currentColor" stroke-width="1.7"/><path d="M12 15.2a3.2 3.2 0 1 0 0-6.4 3.2 3.2 0 0 0 0 6.4Z" fill="currentColor"/></svg>
            Recording
          </button>
          <button class="tab" data-tab="audio">
            <svg viewBox="0 0 24 24" fill="none"><path d="M8 10v4M12 7v10M16 9v6M4 12v1M20 11v2" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>
            Audio
          </button>
          <button class="tab" data-tab="devices">
            <svg viewBox="0 0 24 24" fill="none"><path d="M4 6h16v10H4zM9 20h6M12 16v4" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>
            Devices
          </button>
          <button class="tab" data-tab="hotkeys">
            <svg viewBox="0 0 24 24" fill="none"><path d="M5 8h14M7 12h3M14 12h3M7 16h10" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/><path d="M4 5h16v14H4z" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/></svg>
            Hotkeys
          </button>
        </nav>

        <div class="rail-footer">
          <strong>Capture backend</strong><br>
          Set the simple stuff first. Advanced encoder settings are still there if you want them.
        </div>
      </aside>

      <main>
        <section class="hero">
          <div>
            <h2 id="sectionTitle">Clip settings</h2>
            <p id="sectionSubtitle">Choose how long clips are, where they save, and how sharp they look.</p>
          </div>
          <div class="hero-meta">
            <div class="metric"><strong id="metricClip">30s</strong><span>clip buffer</span></div>
            <div class="metric"><strong id="metricFps">60</strong><span>frames/sec</span></div>
            <div class="metric"><strong id="metricCodec">h264</strong><span>codec</span></div>
          </div>
        </section>

        <section class="content">
          <form id="settingsForm">
            <div class="panel active" data-panel="clips">
              <div class="settings-grid">
                <div class="group wide">
                  <div class="group-head"><div><h3>Clips</h3><span>The settings you will change most often</span></div></div>
                  <div class="group-body simple">
                    <div class="switch-field"><label for="clip_enabled">Keep clip buffer running</label><span class="switch"><input id="clip_enabled" type="checkbox" data-bind="clip.enabled"><span class="slider"></span></span></div>
                    <div class="field">
                      <label for="clip_buffer">Clip length</label>
                      <select id="clip_buffer" data-bind="clip.buffer_seconds">
                        <option value="15">15 seconds</option>
                        <option value="30">30 seconds</option>
                        <option value="60">1 minute</option>
                        <option value="120">2 minutes</option>
                        <option value="300">5 minutes</option>
                      </select>
                      <span class="field-help">How far back the Save Clip button reaches.</span>
                    </div>
                    <div class="field">
                      <label for="clip_resolution">Clip resolution</label>
                      <select id="clip_resolution" data-bind="clip.resolution">
                        <option value="0x0">Same as screen</option>
                        <option value="2560x1440">1440p</option>
                        <option value="1920x1080">1080p</option>
                        <option value="1280x720">720p</option>
                        <option value="3840x2160">4K</option>
                      </select>
                    </div>
                    <div class="field">
                      <label for="clip_fps">Clip FPS</label>
                      <select id="clip_fps" data-bind="clip.fps">
                        <option value="30">30 FPS</option>
                        <option value="60">60 FPS</option>
                        <option value="120">120 FPS</option>
                        <option value="240">240 FPS</option>
                      </select>
                    </div>
                    <div class="field">
                      <label for="clip_save">Save clips to</label>
                      <input id="clip_save" data-bind="clip.save_path">
                    </div>
                    <div class="field">
                      <label for="clip_capture">Screen to record</label>
                      <input id="clip_capture" data-bind="clip.capture" list="captureSources">
                      <span class="field-help">Use a monitor name, or use portal if screen capture acts weird.</span>
                    </div>
                    <details class="advanced">
                      <summary>Advanced clip settings</summary>
                      <div class="advanced-grid">
                        <div class="field"><label for="clip_container">File type</label><select id="clip_container" data-bind="clip.container"><option>mkv</option><option>mp4</option><option>webm</option></select></div>
                        <div class="field"><label for="clip_codec">Video codec</label><select id="clip_codec" data-bind="clip.codec"><option>h264</option><option>hevc</option><option>av1</option><option>auto</option></select></div>
                        <div class="field"><label for="clip_quality">Quality (QP)</label><select id="clip_quality" data-bind="clip.quality"><option value="18">18 — near lossless</option><option value="20">20 — very high</option><option value="23">23 — high</option><option value="28">28 — medium</option><option>very_high</option><option>ultra</option></select></div>
                        <div class="field"><label for="clip_storage">Buffer storage</label><select id="clip_storage" data-bind="clip.replay_storage"><option>ram</option><option>disk</option></select></div>
                        <div class="field"><label for="clip_bitrate">Bitrate mode</label><select id="clip_bitrate" data-bind="clip.bitrate_mode"><option>cbr</option><option>qp</option><option>vbr</option><option>auto</option></select></div>
                        <div class="field"><label for="clip_framerate">Frame pacing</label><select id="clip_framerate" data-bind="clip.framerate_mode"><option>vfr</option><option>cfr</option><option>content</option></select></div>
                        <div class="switch-field"><label for="clip_cursor">Capture cursor</label><span class="switch"><input id="clip_cursor" type="checkbox" data-bind="clip.cursor"><span class="slider"></span></span></div>
                        <div class="switch-field"><label for="clip_restart">Clear buffer after saving</label><span class="switch"><input id="clip_restart" type="checkbox" data-bind="clip.restart_on_save"><span class="slider"></span></span></div>
                        <div class="switch-field"><label for="clip_portal">Remember portal choice</label><span class="switch"><input id="clip_portal" type="checkbox" data-bind="clip.restore_portal_session"><span class="slider"></span></span></div>
                      </div>
                    </details>
                  </div>
                </div>
              </div>
            </div>

            <div class="panel" data-panel="record">
              <div class="settings-grid">
                <div class="group wide">
                  <div class="group-head"><div><h3>Recording</h3><span>For longer videos you start and stop manually</span></div></div>
                  <div class="group-body simple">
                    <div class="field">
                      <label for="record_resolution">Recording resolution</label>
                      <select id="record_resolution" data-bind="record.resolution">
                        <option value="0x0">Same as screen</option>
                        <option value="2560x1440">1440p</option>
                        <option value="1920x1080">1080p</option>
                        <option value="1280x720">720p</option>
                        <option value="3840x2160">4K</option>
                      </select>
                    </div>
                    <div class="field">
                      <label for="record_fps">Recording FPS</label>
                      <select id="record_fps" data-bind="record.fps">
                        <option value="30">30 FPS</option>
                        <option value="60">60 FPS</option>
                        <option value="120">120 FPS</option>
                      </select>
                    </div>
                    <div class="field"><label for="record_save">Save recordings to</label><input id="record_save" data-bind="record.save_path"></div>
                    <div class="field">
                      <label for="record_container">File type</label>
                      <select id="record_container" data-bind="record.container"><option>mkv</option><option>mp4</option><option>webm</option></select>
                      <span class="field-help">MKV is safest if your PC crashes while recording.</span>
                    </div>
                    <div class="field"><label for="record_capture">Screen to record</label><input id="record_capture" data-bind="record.capture" list="captureSources"></div>
                    <div class="switch-field"><label for="record_pause">Pause clips while recording</label><span class="switch"><input id="record_pause" type="checkbox" data-bind="record.pause_replay_buffer"><span class="slider"></span></span></div>
                    <details class="advanced">
                      <summary>Advanced recording settings</summary>
                      <div class="advanced-grid">
                        <div class="field"><label for="record_codec">Video codec</label><select id="record_codec" data-bind="record.codec"><option>h264</option><option>hevc</option><option>av1</option><option>auto</option></select></div>
                        <div class="field"><label for="record_quality">Quality (QP)</label><select id="record_quality" data-bind="record.quality"><option value="18">18 — near lossless</option><option value="20">20 — very high</option><option value="23">23 — high</option><option value="28">28 — medium</option><option>very_high</option><option>ultra</option></select></div>
                        <div class="field"><label for="record_bitrate">Bitrate mode</label><select id="record_bitrate" data-bind="record.bitrate_mode"><option>qp</option><option>vbr</option><option>cbr</option><option>auto</option></select></div>
                        <div class="field"><label for="record_framerate">Frame pacing</label><select id="record_framerate" data-bind="record.framerate_mode"><option>vfr</option><option>cfr</option><option>content</option></select></div>
                        <div class="switch-field"><label for="record_cursor">Capture cursor</label><span class="switch"><input id="record_cursor" type="checkbox" data-bind="record.cursor"><span class="slider"></span></span></div>
                        <div class="switch-field"><label for="record_portal">Remember portal choice</label><span class="switch"><input id="record_portal" type="checkbox" data-bind="record.restore_portal_session"><span class="slider"></span></span></div>
                      </div>
                    </details>
                  </div>
                </div>
              </div>
            </div>

            <div class="panel" data-panel="audio">
              <div class="settings-grid">
                <div class="group wide">
                  <div class="group-head"><div><h3>Audio</h3><span>Choose what sound goes into clips and recordings</span></div></div>
                  <div class="group-body simple">
                    <div class="switch-field"><label for="desktop_audio">Record game/desktop sound</label><span class="switch"><input id="desktop_audio" type="checkbox" data-bind="audio.desktop_enabled"><span class="slider"></span></span></div>
                    <div class="field">
                      <label for="mic_mode">Microphone</label>
                      <select id="mic_mode" data-bind="audio.mic_mode">
                        <option value="off">Off</option>
                        <option value="always">Always record mic</option>
                        <option value="voice">Only when I talk</option>
                      </select>
                    </div>
                    <div class="field"><label for="mic_source">Mic device</label><input id="mic_source" data-bind="audio.mic_source" list="audioSources"></div>
                    <div class="field"><label for="desktop_source">Desktop audio device</label><input id="desktop_source" data-bind="audio.desktop_source" list="audioSources"></div>
                    <div class="switch-field"><label for="separate_tracks">Keep mic on separate track</label><span class="switch"><input id="separate_tracks" type="checkbox" data-bind="audio.separate_tracks"><span class="slider"></span></span></div>
                    <p class="plain-help field full">Use "Only when I talk" if you want the mic quiet when you are not speaking. It is a noise gate applied after the clip saves.</p>
                    <details class="advanced">
                      <summary>Advanced audio settings</summary>
                      <div class="advanced-grid">
                        <div class="field"><label for="audio_codec">Audio codec</label><select id="audio_codec" data-bind="audio.audio_codec"><option>opus</option><option>aac</option><option>flac</option></select></div>
                        <div class="field"><label for="audio_bitrate">Audio bitrate kbps</label><input id="audio_bitrate" type="number" min="0" data-bind="audio.bitrate_kbps"></div>
                        <div class="switch-field"><label for="gate_enabled">Voice gate enabled</label><span class="switch"><input id="gate_enabled" type="checkbox" data-bind="voice_gate.enabled"><span class="slider"></span></span></div>
                        <div class="field"><label for="gate_threshold">Talk threshold</label><input id="gate_threshold" data-bind="voice_gate.threshold"></div>
                        <div class="field"><label for="gate_ratio">Gate strength</label><input id="gate_ratio" type="number" step="0.1" data-bind="voice_gate.ratio"></div>
                        <div class="field"><label for="gate_attack">Open speed ms</label><input id="gate_attack" type="number" data-bind="voice_gate.attack_ms"></div>
                        <div class="field"><label for="gate_release">Close speed ms</label><input id="gate_release" type="number" data-bind="voice_gate.release_ms"></div>
                        <div class="field"><label for="gate_codec">Gate output codec</label><select id="gate_codec" data-bind="voice_gate.output_audio_codec"><option>aac</option><option>opus</option></select></div>
                        <div class="switch-field"><label for="gate_raw">Keep raw ungated copy</label><span class="switch"><input id="gate_raw" type="checkbox" data-bind="voice_gate.keep_raw_copy"><span class="slider"></span></span></div>
                      </div>
                    </details>
                  </div>
                </div>
              </div>
            </div>

            <div class="panel" data-panel="devices">
              <div class="settings-grid">
                <div class="group third"><div class="group-head"><div><h3>Capture</h3><span>Portal and sources</span></div></div><div class="group-body"><div id="captureList" class="device-list field full"></div></div></div>
                <div class="group third"><div class="group-head"><div><h3>Monitors</h3><span>Detected outputs</span></div></div><div class="group-body"><div id="monitorList" class="device-list field full"></div></div></div>
                <div class="group third"><div class="group-head"><div><h3>Audio</h3><span>PipeWire sources</span></div></div><div class="group-body"><div id="audioList" class="device-list field full"></div></div></div>
              </div>
            </div>

            <div class="panel" data-panel="hotkeys">
              <div class="settings-grid">
                <div class="group wide">
                  <div class="group-head"><div><h3>Hotkeys</h3><span>Click a button, then press the keys you want</span></div></div>
                  <div class="group-body">
                    <input class="hidden-bind" id="hotkey_clip" data-bind="hotkeys.clip">
                    <input class="hidden-bind" id="hotkey_record" data-bind="hotkeys.record_toggle">
                    <input class="hidden-bind" id="hotkey_reload" data-bind="hotkeys.reload_config">
                    <div class="hotkey-list field full">
                      <div class="hotkey-card">
                        <div><strong>Save clip</strong><span>Saves the last few seconds based on Clip length.</span></div>
                        <button class="btn hotkey-capture" type="button" data-hotkey-target="hotkeys.clip"><span data-hotkey-display="hotkeys.clip">SUPER + F9</span></button>
                      </div>
                      <div class="hotkey-card">
                        <div><strong>Start / stop recording</strong><span>One hotkey toggles a full recording on and off.</span></div>
                        <button class="btn hotkey-capture" type="button" data-hotkey-target="hotkeys.record_toggle"><span data-hotkey-display="hotkeys.record_toggle">SUPER + F10</span></button>
                      </div>
                      <div class="hotkey-card">
                        <div><strong>Reload Clipsy settings</strong><span>Use this after changing capture settings while the daemon is running.</span></div>
                        <button class="btn hotkey-capture" type="button" data-hotkey-target="hotkeys.reload_config"><span data-hotkey-display="hotkeys.reload_config">SUPER + F11</span></button>
                      </div>
                    </div>
                  </div>
                </div>
                <div class="group wide">
                  <div class="group-head"><div><h3>Install into Hyprland</h3><span>Clipsy writes a small managed block into hyprland.conf</span></div></div>
                  <div class="group-body">
                    <div class="field full">
                      <p class="plain-help">Click this after saving hotkeys. Clipsy backs up your config first, writes the hotkey block, and runs <code>hyprctl reload</code>.</p>
                      <button class="btn primary" type="button" data-action="install_hyprland">Install hotkeys to Hyprland</button>
                      <div id="hyprPreview" class="hypr-preview" aria-live="polite"></div>
                    </div>
                  </div>
                </div>
              </div>
            </div>

            <datalist id="captureSources"></datalist>
            <datalist id="audioSources"></datalist>
          </form>
        </section>

        <footer class="actions">
          <div class="btn-row">
            <button class="btn primary" id="saveBtn" type="button">
              <svg viewBox="0 0 24 24" fill="none"><path d="M5 5h11l3 3v11H5zM8 5v6h8M8 19v-5h8v5" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/></svg>
              Save settings
            </button>
            <button class="btn" type="button" data-action="clip">
              <svg viewBox="0 0 24 24" fill="none"><path d="M5 6h14v12H5zM10 10v4l4-2-4-2Z" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/></svg>
              Save clip
            </button>
            <button class="btn" type="button" data-action="record_toggle">
              <svg viewBox="0 0 24 24" fill="none"><path d="M7 7h10v10H7z" stroke="currentColor" stroke-width="1.8"/><path d="M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6Z" fill="currentColor"/></svg>
              Toggle recording
            </button>
          </div>
          <div id="toast" class="toast">Ready</div>
        </footer>
      </main>
    </div>
  </div>

  <script>
    const state = { config: null, activeTab: "clips", capturingHotkey: null, _pendingHyprHotkeys: null };

    // Track modifier keys for browser fallback (Qt native window uses _qtHotkey instead)
    var heldMods = new Set();
    window.addEventListener("keydown", (e) => {
      if (["Alt", "Control", "Meta", "Shift"].includes(e.key)) heldMods.add(e.key);
    }, true);
    window.addEventListener("keyup", (e) => heldMods.delete(e.key), true);
    window.addEventListener("blur", () => heldMods.clear());

    // Called by the Python Qt layer with the detected combo (or null to cancel)
    window._qtHotkey = function(combo) {
      if (!state.capturingHotkey) return;
      if (combo === null) {
        state.capturingHotkey = null;
        $$(".hotkey-capture").forEach((b) => b.classList.remove("listening"));
        toast("Hotkey capture canceled.");
      } else {
        finishHotkeyCapture(combo);
      }
    };

    const subtitles = {
      clips: ["Clip settings", "Choose how long clips are, where they save, and how sharp they look."],
      record: ["Recording settings", "Choose quality and save location for full recordings."],
      audio: ["Audio settings", "Pick desktop sound, microphone mode, and the mic device."],
      devices: ["Detected devices", "Copy screen or audio device names if the defaults do not work."],
      hotkeys: ["Hyprland hotkeys", "Press the hotkey you want, save, then install it into Hyprland."]
    };

    const $ = (selector) => document.querySelector(selector);
    const $$ = (selector) => [...document.querySelectorAll(selector)];

    function getPath(obj, path) {
      return path.split(".").reduce((acc, key) => acc?.[key], obj);
    }

    function setPath(obj, path, value) {
      const keys = path.split(".");
      let target = obj;
      for (let i = 0; i < keys.length - 1; i++) target = target[keys[i]];
      target[keys[keys.length - 1]] = value;
    }

    function bindConfig(config) {
      state.config = structuredClone(config);
      $$("[data-bind]").forEach((input) => {
        const value = getPath(state.config, input.dataset.bind);
        if (input.type === "checkbox") input.checked = Boolean(value);
        else {
          ensureOption(input, value);
          input.value = value ?? "";
        }
      });
      if (state._pendingHyprHotkeys) {
        applyHyprlandHotkeys(state._pendingHyprHotkeys);
        state._pendingHyprHotkeys = null;
      }
      syncMetrics();
      syncHotkeyDisplays();
    }

    function applyHyprlandHotkeys(hk) {
      if (!state.config || !hk) return;
      const map = { clip: "hotkeys.clip", record_toggle: "hotkeys.record_toggle", reload_config: "hotkeys.reload_config" };
      let changed = false;
      for (const [field, path] of Object.entries(map)) {
        if (!hk[field]) continue;
        setPath(state.config, path, hk[field]);
        const input = $(`[data-bind="${path}"]`);
        if (input) input.value = hk[field];
        changed = true;
      }
      if (changed) syncHotkeyDisplays();
    }

    function collectConfig() {
      $$("[data-bind]").forEach((input) => {
        let value = input.type === "checkbox" ? input.checked : input.value;
        if (input.type === "number") value = input.step === "0.1" ? Number.parseFloat(value) : Number.parseInt(value, 10);
        setPath(state.config, input.dataset.bind, value);
      });
      return state.config;
    }

    function ensureOption(input, value) {
      if (!(input instanceof HTMLSelectElement) || value === undefined || value === null) return;
      const stringValue = String(value);
      if ([...input.options].some((option) => option.value === stringValue)) return;
      const option = document.createElement("option");
      option.value = stringValue;
      option.textContent = stringValue;
      input.appendChild(option);
    }

    function syncMetrics() {
      if (!state.config) return;
      $("#metricClip").textContent = `${state.config.clip.buffer_seconds}s`;
      $("#metricFps").textContent = String(state.config.clip.fps);
      $("#metricCodec").textContent = state.config.clip.codec;
    }

    function syncHotkeyDisplays() {
      $$("[data-hotkey-display]").forEach((el) => {
        const value = getPath(state.config, el.dataset.hotkeyDisplay);
        el.textContent = displayHotkey(value);
      });
    }

    function displayHotkey(value) {
      if (!value) return "Click to set";
      const normalized = String(value).includes(",") ? String(value) : String(value).replace(/\+/g, ",");
      const [mods, key] = normalized.split(",");
      return [...mods.trim().split(/\s+/).filter(Boolean), (key || "").trim()].filter(Boolean).join(" + ");
    }

    function toast(message, tone = "") {
      const el = $("#toast");
      el.textContent = message;
      el.className = `toast ${tone}`;
    }

    async function requestJson(url, options = {}) {
      const res = await fetch(url, {
        headers: { "Content-Type": "application/json" },
        ...options
      });
      return await res.json();
    }

    async function loadConfig() {
      const data = await requestJson("/api/config");
      if (data.ok) bindConfig(data.config);
      else toast(data.error || "Could not load config", "bad");
    }

    async function loadHyprland() {
      const data = await requestJson("/api/hyprland");
      if (!data.ok) return;
      renderHyprland(data.hyprland);
      if (data.hyprland.hotkeys && Object.keys(data.hyprland.hotkeys).length > 0) {
        if (state.config) {
          applyHyprlandHotkeys(data.hyprland.hotkeys);
        } else {
          state._pendingHyprHotkeys = data.hyprland.hotkeys;
        }
      }
    }

    async function loadStatus() {
      const data = await requestJson("/api/status");
      const dot = $("#daemonDot");
      const text = $("#daemonText");
      if (data.ok) {
        dot.classList.add("live");
        const recording = data.recording?.running ? "recording" : "ready";
        const buffer = data.clip?.running ? "buffer live" : "buffer idle";
        text.textContent = `${buffer} / ${recording}`;
      } else {
        dot.classList.remove("live");
        text.textContent = "daemon offline";
      }
    }

    async function loadDevices() {
      const data = await requestJson("/api/devices");
      if (!data.ok) return;
      renderDevices("#captureList", data.devices.capture);
      renderDevices("#monitorList", data.devices.monitors);
      renderDevices("#audioList", data.devices.audio);
      fillDatalist("#captureSources", [...data.devices.capture, ...data.devices.monitors]);
      fillDatalist("#audioSources", data.devices.audio);
    }

    function renderDevices(selector, items) {
      const el = $(selector);
      el.innerHTML = "";
      if (!items.length) {
        el.innerHTML = `<div class="device"><code>none</code><span>No devices found</span></div>`;
        return;
      }
      for (const item of items) {
        const row = document.createElement("div");
        row.className = "device";
        row.innerHTML = `<code></code><span></span>`;
        row.querySelector("code").textContent = item.id || "info";
        row.querySelector("span").textContent = item.label || item.id;
        el.appendChild(row);
      }
    }

    function fillDatalist(selector, items) {
      const el = $(selector);
      el.innerHTML = "";
      for (const item of items) {
        if (!item.id) continue;
        const option = document.createElement("option");
        option.value = item.id;
        option.label = item.label;
        el.appendChild(option);
      }
    }

    function renderHyprland(info) {
      const el = $("#hyprPreview");
      if (!el || !info) return;
      const status = info.installed ? "Installed block found" : "No Clipsy block installed yet";
      const lines = [`${status}: ${info.path}`, ...(info.preview || [])];
      el.innerHTML = "";
      for (const line of lines) {
        const row = document.createElement("div");
        row.textContent = line;
        el.appendChild(row);
      }
    }

    async function saveConfig(options = {}) {
      const config = collectConfig();
      const data = await requestJson("/api/config", {
        method: "POST",
        body: JSON.stringify({ config })
      });
      if (data.ok) {
        bindConfig(data.config);
        if (!options.quiet) toast("Settings saved. Reload daemon to apply capture changes.", "good");
        loadHyprland();
        return data.config;
      } else {
        if (!options.quiet) toast(data.error || "Could not save settings", "bad");
        return null;
      }
    }

    async function action(actionName) {
      let body = { action: actionName };
      if (actionName === "install_hyprland") {
        body.config = collectConfig();
      }
      const data = await requestJson("/api/action", {
        method: "POST",
        body: JSON.stringify(body)
      });
      if (data.ok) {
        toast(data.message || "Command sent", "good");
        if (actionName === "close_ui") window.close();
        if (actionName === "install_hyprland") loadHyprland();
      } else {
        toast(data.error || "Command failed", "bad");
      }
      setTimeout(loadStatus, 400);
    }

    function beginHotkeyCapture(target) {
      state.capturingHotkey = target;
      $$(".hotkey-capture").forEach((button) => {
        button.classList.toggle("listening", button.dataset.hotkeyTarget === target);
      });
      toast("Press the hotkey now. Example: SUPER + F9");
    }

    function finishHotkeyCapture(value) {
      if (!state.capturingHotkey) return;
      setPath(state.config, state.capturingHotkey, value);
      const input = $(`[data-bind="${state.capturingHotkey}"]`);
      if (input) input.value = value;
      state.capturingHotkey = null;
      $$(".hotkey-capture").forEach((button) => button.classList.remove("listening"));
      syncHotkeyDisplays();
      toast(`Hotkey set to ${displayHotkey(value)}. Click Install hotkeys to Hyprland when ready.`, "good");
      loadHyprland();
    }

    function keyName(event) {
      const ignored = new Set(["Meta", "Control", "Alt", "Shift", "Super", "OS"]);
      if (ignored.has(event.key)) return "";
      const map = {
        " ": "SPACE",
        ArrowUp: "UP",
        ArrowDown: "DOWN",
        ArrowLeft: "LEFT",
        ArrowRight: "RIGHT",
        Escape: "ESCAPE",
        Enter: "RETURN",
        Backspace: "BACKSPACE",
        Delete: "DELETE",
        PageUp: "PAGEUP",
        PageDown: "PAGEDOWN",
        Home: "HOME",
        End: "END",
        Insert: "INSERT",
      };
      if (map[event.key]) return map[event.key];
      if (/^F\d{1,2}$/.test(event.key)) return event.key.toUpperCase();
      if (event.key.length === 1) return event.key.toUpperCase();
      return event.key.toUpperCase();
    }

    function hotkeyFromEvent(event) {
      if (event.key === "Escape") return null;
      const mods = [];
      if (event.metaKey  || heldMods.has("Meta"))    mods.push("SUPER");
      if (event.ctrlKey  || heldMods.has("Control")) mods.push("CTRL");
      if (event.altKey   || heldMods.has("Alt"))     mods.push("ALT");
      if (event.shiftKey || heldMods.has("Shift"))   mods.push("SHIFT");
      const key = keyName(event);
      if (!key || mods.length === 0) return "";
      return `${mods.join(" ")},${key}`;
    }

    function setTab(name) {
      state.activeTab = name;
      $$(".tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.tab === name));
      $$(".panel").forEach((panel) => panel.classList.toggle("active", panel.dataset.panel === name));
      $("#sectionTitle").textContent = subtitles[name][0];
      $("#sectionSubtitle").textContent = subtitles[name][1];
    }

    $$(".tab").forEach((tab) => tab.addEventListener("click", () => setTab(tab.dataset.tab)));
    $$("[data-action]").forEach((button) => button.addEventListener("click", () => action(button.dataset.action)));
    $$(".hotkey-capture").forEach((button) => button.addEventListener("click", () => beginHotkeyCapture(button.dataset.hotkeyTarget)));
    window.addEventListener("keydown", (event) => {
      if (!state.capturingHotkey) return;
      event.preventDefault();
      event.stopPropagation();
      const value = hotkeyFromEvent(event);
      if (value === null) {
        state.capturingHotkey = null;
        $$(".hotkey-capture").forEach((button) => button.classList.remove("listening"));
        toast("Hotkey capture canceled.");
        return;
      }
      if (!value) {
        const modifierKeys = new Set(["Meta", "Control", "Alt", "Shift", "Super", "OS"]);
        if (!modifierKeys.has(event.key)) {
          toast("Press one modifier plus one key, like SUPER + F9 or ALT + S.", "bad");
        }
        return;
      }
      finishHotkeyCapture(value);
    }, true);
    $("#saveBtn").addEventListener("click", saveConfig);
    $$("[data-bind]").forEach((input) => input.addEventListener("input", () => {
      collectConfig();
      syncMetrics();
      syncHotkeyDisplays();
    }));

    loadConfig();
    loadDevices();
    loadHyprland();
    loadStatus();
    setInterval(loadStatus, 2500);
  </script>
</body>
</html>
"""
