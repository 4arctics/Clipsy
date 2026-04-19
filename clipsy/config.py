from __future__ import annotations

import json
import os
import tomllib
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, TypeVar, get_args, get_origin


APP_NAME = "clipsy"
DEFAULT_CONFIG_PATH = Path("~/.config/clipsy/config.toml")
DEFAULT_STATE_DIR = Path("~/.local/state/clipsy")

T = TypeVar("T")


@dataclass
class GeneralConfig:
    socket_path: str = ""
    notify: bool = True
    log_file: str = "~/.local/state/clipsy/clipsy.log"


@dataclass
class ClipConfig:
    enabled: bool = True
    capture: str = "screen"
    buffer_seconds: int = 30
    resolution: str = "0x0"
    fps: int = 60
    save_path: str = "~/Videos/Clips"
    container: str = "mp4"
    codec: str = "hevc"
    quality: str = "18"
    bitrate_mode: str = "qp"
    framerate_mode: str = "vfr"
    replay_storage: str = "ram"
    restart_on_save: bool = False
    restore_portal_session: bool = True
    cursor: bool = True


@dataclass
class RecordConfig:
    capture: str = "screen"
    resolution: str = "0x0"
    fps: int = 60
    save_path: str = "~/Videos/Recordings"
    container: str = "mkv"
    codec: str = "h264"
    quality: str = "18"
    bitrate_mode: str = "qp"
    framerate_mode: str = "vfr"
    restore_portal_session: bool = True
    cursor: bool = True
    pause_replay_buffer: bool = True


@dataclass
class AudioConfig:
    desktop_enabled: bool = True
    desktop_source: str = "default_output"
    mic_mode: str = "off"
    mic_source: str = "default_input"
    separate_tracks: bool = False
    audio_codec: str = "opus"
    bitrate_kbps: int = 160


@dataclass
class VoiceGateConfig:
    enabled: bool = True
    threshold: str = "0.020"
    ratio: float = 12.0
    attack_ms: int = 5
    release_ms: int = 250
    output_audio_codec: str = "aac"
    keep_raw_copy: bool = False


@dataclass
class HotkeyConfig:
    clip: str = "SUPER,F9"
    record_toggle: str = "SUPER,F10"
    reload_config: str = "SUPER,F11"


@dataclass
class AppConfig:
    general: GeneralConfig
    clip: ClipConfig
    record: RecordConfig
    audio: AudioConfig
    voice_gate: VoiceGateConfig
    hotkeys: HotkeyConfig

    def ensure_dirs(self) -> None:
        expand_path(self.clip.save_path).mkdir(parents=True, exist_ok=True)
        expand_path(self.record.save_path).mkdir(parents=True, exist_ok=True)
        expand_path(self.general.log_file).parent.mkdir(parents=True, exist_ok=True)
        socket = effective_socket_path(self)
        socket.parent.mkdir(parents=True, exist_ok=True)


def default_config() -> AppConfig:
    return AppConfig(
        general=GeneralConfig(),
        clip=ClipConfig(),
        record=RecordConfig(),
        audio=AudioConfig(),
        voice_gate=VoiceGateConfig(),
        hotkeys=HotkeyConfig(),
    )


def default_config_path() -> Path:
    return expand_path(str(DEFAULT_CONFIG_PATH))


def expand_path(value: str | Path) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(value)))).resolve()


def runtime_dir() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return Path(runtime)
    return Path("/tmp") / f"clipsy-{os.getuid()}"


def effective_socket_path(config: AppConfig) -> Path:
    if config.general.socket_path:
        return expand_path(config.general.socket_path)
    return runtime_dir() / "clipsy.sock"


def load_config(path: str | Path | None = None) -> AppConfig:
    config_path = expand_path(path or default_config_path())
    base = default_config()
    if not config_path.exists():
        return base

    raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    config = AppConfig(
        general=_section(GeneralConfig, raw.get("general", {})),
        clip=_section(ClipConfig, raw.get("clip", {})),
        record=_section(RecordConfig, raw.get("record", {})),
        audio=_section(AudioConfig, raw.get("audio", {})),
        voice_gate=_section(VoiceGateConfig, raw.get("voice_gate", {})),
        hotkeys=_section(HotkeyConfig, raw.get("hotkeys", {})),
    )
    return validate_config(config)


def validate_config(config: AppConfig) -> AppConfig:
    if config.audio.mic_mode not in {"off", "always", "voice"}:
        config.audio.mic_mode = "off"
    if config.clip.buffer_seconds < 2:
        config.clip.buffer_seconds = 2
    if config.clip.fps < 1:
        config.clip.fps = 60
    if config.record.fps < 1:
        config.record.fps = 60
    if config.audio.bitrate_kbps < 0:
        config.audio.bitrate_kbps = 160
    if config.audio.mic_mode == "voice":
        config.audio.separate_tracks = True
    return config


def save_config(config: AppConfig, path: str | Path | None = None) -> Path:
    config = validate_config(config)
    config_path = expand_path(path or default_config_path())
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(to_toml(config), encoding="utf-8")
    return config_path


def to_toml(config: AppConfig) -> str:
    sections = asdict(validate_config(config))
    lines: list[str] = [
        "# clipsy config",
        "# capture can be: screen, portal, focused, region, a monitor name like HDMI-A-1, or a V4L2 path.",
        "# mic_mode can be: off, always, voice. voice mode uses a post-save FFmpeg noise gate.",
        "",
    ]
    for section, values in sections.items():
        lines.append(f"[{section}]")
        for key, value in values.items():
            lines.append(f"{key} = {_toml_value(value)}")
        lines.append("")
    return "\n".join(lines)


def _section(cls: type[T], values: dict[str, Any]) -> T:
    defaults = cls()
    kwargs: dict[str, Any] = {}
    for item in fields(defaults):
        if item.name not in values:
            kwargs[item.name] = getattr(defaults, item.name)
            continue
        kwargs[item.name] = _coerce(values[item.name], getattr(defaults, item.name), item.type)
    return cls(**kwargs)


def _coerce(value: Any, default: Any, annotation: Any) -> Any:
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin is not None and type(None) in args and value is None:
        return None
    if isinstance(default, bool):
        return _bool(value)
    if isinstance(default, int) and not isinstance(default, bool):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    if isinstance(default, float):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default
    if isinstance(default, str):
        return str(value)
    return value


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "yes", "true", "on"}
    return bool(value)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return json.dumps(str(value))
