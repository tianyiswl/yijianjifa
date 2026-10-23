from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import platform
from typing import Protocol


@dataclass(frozen=True, slots=True)
class VoiceInfo:
    id: str
    name: str


@dataclass(frozen=True, slots=True)
class SentenceSpan:
    text: str
    start_ms: int
    end_ms: int


@dataclass(frozen=True, slots=True)
class VoiceResult:
    output_path: Path
    duration_ms: int
    sentence_spans: tuple[SentenceSpan, ...] = ()


class TTSAdapter(Protocol):
    def list_voices(self) -> list[VoiceInfo]: ...

    def synthesize(self, text: str, voice: str | None, speed: float, output: Path) -> VoiceResult: ...


def system_tts_adapter() -> TTSAdapter:
    system = platform.system()
    if system == "Windows":
        from yijianjifa.features.mixing.tts.windows_sapi import WindowsSapiTTS

        return WindowsSapiTTS()
    if system == "Darwin":
        from yijianjifa.features.mixing.tts.macos_say import MacOSSayTTS

        return MacOSSayTTS()
    raise RuntimeError("System TTS is supported on Windows and macOS in 一剪即发 V1")
