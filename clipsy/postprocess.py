from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from .config import AppConfig


class PostprocessError(RuntimeError):
    pass


def postprocess_saved_file(path: str | Path, event_type: str, config: AppConfig) -> dict[str, object]:
    video = Path(path)
    if config.audio.mic_mode != "voice" or not config.voice_gate.enabled:
        return {"ok": True, "changed": False, "reason": "voice gate disabled"}
    if not video.exists():
        return {"ok": False, "changed": False, "error": f"{video} does not exist"}
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        return {"ok": False, "changed": False, "error": "ffmpeg/ffprobe not found"}

    audio_streams = _audio_stream_count(video)
    if audio_streams == 0:
        return {"ok": True, "changed": False, "reason": "no audio streams"}

    if audio_streams == 1 and config.audio.desktop_enabled:
        return {
            "ok": True,
            "changed": False,
            "reason": "single mixed audio stream; cannot gate only the mic",
        }

    temp = video.with_name(f"{video.stem}.voicegate.tmp{video.suffix}")
    raw = video.with_name(f"{video.stem}.raw{video.suffix}")
    command = _ffmpeg_command(video, temp, audio_streams, config)
    result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode != 0:
        return {
            "ok": False,
            "changed": False,
            "error": result.stderr[-3000:],
            "event_type": event_type,
        }

    if config.voice_gate.keep_raw_copy:
        if raw.exists():
            raw.unlink()
        video.replace(raw)
    else:
        video.unlink()
    temp.replace(video)
    return {"ok": True, "changed": True, "path": str(video), "event_type": event_type}


def _audio_stream_count(path: Path) -> int:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a",
            "-show_entries",
            "stream=index",
            "-of",
            "json",
            str(path),
        ],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        raise PostprocessError(result.stderr.strip())
    data = json.loads(result.stdout or "{}")
    return len(data.get("streams", []))


def _ffmpeg_command(input_file: Path, output_file: Path, audio_streams: int, config: AppConfig) -> list[str]:
    gate = config.voice_gate
    agate = (
        f"agate=threshold={gate.threshold}:ratio={gate.ratio}:"
        f"attack={gate.attack_ms}:release={gate.release_ms}"
    )
    audio_codec = gate.output_audio_codec or "aac"
    if audio_streams >= 2:
        filter_complex = f"[0:a:1]{agate}[mic];[0:a:0][mic]amix=inputs=2:duration=longest[aout]"
        return [
            "ffmpeg",
            "-hide_banner",
            "-y",
            "-i",
            str(input_file),
            "-filter_complex",
            filter_complex,
            "-map",
            "0:v:0",
            "-map",
            "[aout]",
            "-c:v",
            "copy",
            "-c:a",
            audio_codec,
            "-shortest",
            str(output_file),
        ]

    return [
        "ffmpeg",
        "-hide_banner",
        "-y",
        "-i",
        str(input_file),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0",
        "-c:v",
        "copy",
        "-af",
        agate,
        "-c:a",
        audio_codec,
        "-shortest",
        str(output_file),
    ]
