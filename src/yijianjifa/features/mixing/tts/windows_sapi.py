from __future__ import annotations

import json
from pathlib import Path
from yijianjifa.os_integration.background_process import run_hidden
import subprocess

from yijianjifa.features.mixing.tts.base import VoiceInfo, VoiceResult
from yijianjifa.os_integration.resources import ffprobe_path


def _ps_quote(value: str) -> str:
    return value.replace("'", "''")


class WindowsSapiTTS:
    def list_voices(self) -> list[VoiceInfo]:
        script = (
            "Add-Type -AssemblyName System.Speech; "
            "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            "$s.GetInstalledVoices() | ForEach-Object { $_.VoiceInfo.Name } | ConvertTo-Json"
        )
        result = run_hidden(
            ["powershell", "-NoProfile", "-Command", script],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        raw = json.loads(result.stdout or "[]")
        if isinstance(raw, str):
            raw = [raw]
        return [VoiceInfo(id=name, name=name) for name in raw]

    def synthesize(self, text: str, voice: str | None, speed: float, output: Path) -> VoiceResult:
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        rate = max(-10, min(10, round((float(speed) - 1.0) * 10)))
        choose = f"$s.SelectVoice('{_ps_quote(voice)}'); " if voice else ""
        script = (
            "Add-Type -AssemblyName System.Speech; "
            "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            f"{choose}$s.Rate={rate}; "
            f"$s.SetOutputToWaveFile('{_ps_quote(str(output))}'); "
            f"$s.Speak('{_ps_quote(str(text))}'); $s.Dispose();"
        )
        run_hidden(
            ["powershell", "-NoProfile", "-Command", script],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return VoiceResult(output_path=output, duration_ms=_probe_duration_ms(output))


def _probe_duration_ms(path: Path) -> int:
    result = run_hidden(
        [str(ffprobe_path()), "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return round(float(result.stdout.strip()) * 1000)
