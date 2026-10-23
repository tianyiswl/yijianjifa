from __future__ import annotations

from pathlib import Path
import subprocess
import re

from yijianjifa.features.mixing.tts.base import VoiceInfo, VoiceResult
from yijianjifa.os_integration.resources import ffprobe_path


class MacOSSayTTS:
    def list_voices(self) -> list[VoiceInfo]:
        result = subprocess.run(
            ["say", "-v", "?"],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
        )
        voices, seen = [], set()
        for line in result.stdout.splitlines():
            match = re.match(r'^(.+?)\s+[a-z]{2,3}_[A-Z]{2}\s+#', line)
            if match:
                name = match.group(1).strip()
                if name not in seen:
                    voices.append(VoiceInfo(id=name, name=name))
                    seen.add(name)
        return voices

    def synthesize(self, text: str, voice: str | None, speed: float, output: Path) -> VoiceResult:
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        words_per_minute = max(80, min(500, round(175 * float(speed))))
        command = ["say"]
        if voice:
            command.extend(["-v", voice])
        command.extend(["-r", str(words_per_minute), "-o", str(output)])
        subprocess.run(command, input=str(text).encode("utf-8"), check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
        return VoiceResult(output_path=output, duration_ms=_probe_duration_ms(output))


def _probe_duration_ms(path: Path) -> int:
    result = subprocess.run(
        [str(ffprobe_path()), "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return round(float(result.stdout.strip()) * 1000)
