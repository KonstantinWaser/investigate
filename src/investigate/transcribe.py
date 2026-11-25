"""Local speech to text: ffmpeg decodes the audio, faster-whisper transcribes it on the CPU.

Both are optional (`uv sync --extra transcribe` for faster-whisper, ffmpeg from the system). When
either is missing, `unavailable()` says what, and callers carry on without a transcript.

ffmpeg writes a temporary WAV file and faster-whisper reads that file, so a long video is never
held in memory as raw samples. ffmpeg has a timeout; callers also cap the video length.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Protocol

MODEL_SIZE = "small"  # multilingual; the model downloads once on first use (about 460 MB)
SAMPLE_RATE = 16000
FFMPEG_TIMEOUT_SECONDS = 600
HOW_TO_ENABLE = "Install ffmpeg and run `uv sync --extra transcribe`."


class Transcriber(Protocol):
    def unavailable(self) -> str | None:
        """Why transcription cannot run, or None when it can."""
        ...

    def transcribe(self, audio: Path) -> str: ...


class WhisperTranscriber:
    def __init__(self, model_size: str = MODEL_SIZE) -> None:
        self._model_size = model_size
        self._model = None

    def unavailable(self) -> str | None:
        missing = []
        if shutil.which("ffmpeg") is None:
            missing.append("ffmpeg is not installed")
        if importlib.util.find_spec("faster_whisper") is None:
            missing.append("faster-whisper is not installed")
        return " and ".join(missing) or None

    def transcribe(self, audio: Path) -> str:
        from faster_whisper import WhisperModel

        with TemporaryDirectory() as tmp:
            wav = Path(tmp) / "audio.wav"
            try:
                subprocess.run(  # noqa: S603
                    [  # noqa: S607
                        "ffmpeg",
                        "-v",
                        "error",
                        "-y",
                        "-i",
                        str(audio),
                        "-ac",
                        "1",
                        "-ar",
                        str(SAMPLE_RATE),
                        str(wav),
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=True,
                    timeout=FFMPEG_TIMEOUT_SECONDS,
                )
            except subprocess.CalledProcessError as exc:
                raise RuntimeError(
                    f"ffmpeg failed: {' '.join((exc.stderr or '').split())[:200]}"
                ) from exc
            if self._model is None:
                self._model = WhisperModel(self._model_size, device="cpu", compute_type="int8")
            segments, _ = self._model.transcribe(str(wav), vad_filter=True)
            return " ".join(s.text.strip() for s in segments).strip()
