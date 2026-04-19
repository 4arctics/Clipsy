from __future__ import annotations

import os
import shlex
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import IO

from .config import AppConfig, expand_path


class GsrUnavailable(RuntimeError):
    pass


def require_gsr() -> str:
    binary = shutil.which("gpu-screen-recorder")
    if binary is None:
        raise GsrUnavailable(
            "gpu-screen-recorder was not found. Install it, then restart the daemon."
        )
    return binary


def ensure_voice_gate_hook(config_path: Path) -> Path:
    hook_dir = config_path.parent / "hooks"
    hook_dir.mkdir(parents=True, exist_ok=True)
    hook = hook_dir / "voice-gate-hook.sh"
    content = f"""#!/bin/sh
export CLIPSY_CONFIG={shlex.quote(str(config_path))}
exec {shlex.quote(sys.executable)} -m clipsy --config {shlex.quote(str(config_path))} postprocess "$@"
"""
    if not hook.exists() or hook.read_text(encoding="utf-8") != content:
        hook.write_text(content, encoding="utf-8")
        hook.chmod(0o755)
    return hook


def gsr_env() -> dict[str, str]:
    env = dict(os.environ)
    root = str(Path(__file__).resolve().parent.parent)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = root if not existing else f"{root}:{existing}"
    return env


def build_replay_command(config: AppConfig, config_path: Path) -> list[str]:
    clip = config.clip
    args = [
        require_gsr(),
        "-w",
        clip.capture,
        "-f",
        str(clip.fps),
        "-c",
        clip.container,
        "-r",
        str(clip.buffer_seconds),
        "-replay-storage",
        clip.replay_storage,
        "-restart-replay-on-save",
        _yes_no(clip.restart_on_save),
        "-k",
        clip.codec,
        "-q",
        _quality_arg(clip.quality, clip.bitrate_mode),
        "-bm",
        clip.bitrate_mode,
        "-fm",
        clip.framerate_mode,
        "-cursor",
        _yes_no(clip.cursor),
    ]
    _add_resolution(args, clip.resolution)
    if clip.restore_portal_session:
        args += ["-restore-portal-session", "yes"]
    args += _audio_args(config)
    if config.audio.mic_mode == "voice" and config.voice_gate.enabled:
        args += ["-sc", str(ensure_voice_gate_hook(config_path))]
    args += ["-ro", str(expand_path(config.record.save_path))]
    args += ["-o", str(expand_path(clip.save_path))]
    return args


def build_record_command(config: AppConfig, output_file: Path, config_path: Path) -> list[str]:
    record = config.record
    args = [
        require_gsr(),
        "-w",
        record.capture,
        "-f",
        str(record.fps),
        "-c",
        record.container,
        "-k",
        record.codec,
        "-q",
        _quality_arg(record.quality, record.bitrate_mode),
        "-bm",
        record.bitrate_mode,
        "-fm",
        record.framerate_mode,
        "-cursor",
        _yes_no(record.cursor),
    ]
    _add_resolution(args, record.resolution)
    if record.restore_portal_session:
        args += ["-restore-portal-session", "yes"]
    args += _audio_args(config)
    if config.audio.mic_mode == "voice" and config.voice_gate.enabled:
        args += ["-sc", str(ensure_voice_gate_hook(config_path))]
    args += ["-o", str(output_file)]
    return args


def make_recording_path(config: AppConfig) -> Path:
    directory = expand_path(config.record.save_path)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    suffix = config.record.container.strip(".") or "mkv"
    return directory / f"Recording_{stamp}.{suffix}"


def _audio_args(config: AppConfig) -> list[str]:
    audio = config.audio
    sources: list[str] = []
    if audio.desktop_enabled and audio.desktop_source:
        sources.append(audio.desktop_source)
    if audio.mic_mode in {"always", "voice"} and audio.mic_source:
        sources.append(audio.mic_source)
    if not sources:
        return []

    args: list[str] = []
    separate_tracks = audio.separate_tracks or audio.mic_mode == "voice"
    if separate_tracks:
        for source in sources:
            args += ["-a", source]
    else:
        args += ["-a", "|".join(sources)]
    if audio.audio_codec:
        args += ["-ac", audio.audio_codec]
    if audio.bitrate_kbps > 0:
        args += ["-ab", str(audio.bitrate_kbps)]
    return args


def _add_resolution(args: list[str], resolution: str) -> None:
    resolution = (resolution or "").strip()
    if resolution and resolution != "0x0":
        args += ["-s", resolution]


def _yes_no(value: bool) -> str:
    return "yes" if value else "no"


_QUALITY_TO_BITRATE = {"medium": 15000, "high": 25000, "very_high": 40000, "ultra": 60000}


def _quality_arg(quality: str, bitrate_mode: str) -> str:
    if bitrate_mode not in {"cbr", "vbr"}:
        return quality
    try:
        int(quality)
        return quality
    except ValueError:
        return str(_QUALITY_TO_BITRATE.get(quality, 40000))


@dataclass
class ManagedProcess:
    name: str
    command: list[str]
    log_file: IO[str]
    process: subprocess.Popen[str] | None = None

    def start(self) -> None:
        if self.is_running():
            return
        self.log_file.write(f"\n[{datetime.now().isoformat(timespec='seconds')}] starting {self.name}\n")
        self.log_file.write(" ".join(shlex.quote(part) for part in self.command) + "\n")
        self.log_file.flush()
        self.process = subprocess.Popen(
            self.command,
            stdout=self.log_file,
            stderr=self.log_file,
            stdin=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
            env=gsr_env(),
        )

    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def pid(self) -> int | None:
        if self.is_running() and self.process is not None:
            return self.process.pid
        return None

    def send_signal(self, sig: signal.Signals) -> None:
        if self.is_running() and self.process is not None:
            self.process.send_signal(sig)

    def stop(self, sig: signal.Signals = signal.SIGINT, timeout: float = 6.0) -> int | None:
        if not self.process:
            return None
        if self.process.poll() is None:
            self.process.send_signal(sig)
            try:
                return self.process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.process.kill()
                return self.process.wait(timeout=2)
        return self.process.returncode
