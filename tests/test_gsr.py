from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from clipsy.config import default_config
from clipsy.gsr import build_record_command, build_replay_command


class GsrCommandTests(unittest.TestCase):
    @patch("clipsy.gsr.require_gsr", return_value="/usr/bin/gpu-screen-recorder")
    def test_replay_command_contains_buffer_and_output_dir(self, _mock) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = default_config()
            config.clip.save_path = tmp
            config.record.save_path = tmp
            command = build_replay_command(config, Path(tmp) / "config.toml")
            self.assertIn("-r", command)
            self.assertIn("30", command)
            self.assertEqual(command[-2], "-o")
            self.assertEqual(command[-1], str(Path(tmp).resolve()))

    @patch("clipsy.gsr.require_gsr", return_value="/usr/bin/gpu-screen-recorder")
    def test_voice_mode_uses_separate_audio_args(self, _mock) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = default_config()
            config.audio.desktop_enabled = True
            config.audio.mic_mode = "voice"
            config.audio.separate_tracks = True
            output = Path(tmp) / "out.mkv"
            command = build_record_command(config, output, Path(tmp) / "config.toml")
            audio_flags = [item for item in command if item == "-a"]
            self.assertEqual(len(audio_flags), 2)
            self.assertIn("-sc", command)


if __name__ == "__main__":
    unittest.main()
