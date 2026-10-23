from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import math
from yijianjifa.os_integration.background_process import run_hidden
import subprocess
import random
from .effect_options import TRANSITIONS

from yijianjifa.os_integration.resources import ffmpeg_path, ffprobe_path
from yijianjifa.os_integration.media_guard import guarded_media


@dataclass(frozen=True, slots=True)
class RenderSegment:
    path: Path
    source_start_ms: int
    duration_ms: int


@dataclass(frozen=True, slots=True)
class RenderRequest:
    segments: tuple[RenderSegment, ...]
    output_path: Path
    aspect_ratio: str = "9:16"
    resolution: str = "1080p"
    frame_fit: str = "preserve_blur"
    voice_path: Path | None = None
    music_path: Path | None = None
    subtitle_ass_path: Path | None = None
    keep_original_audio: bool = True
    original_gain: float = 1.0
    voice_gain: float = 1.0
    music_gain: float = 0.25
    transition_mode: str = "none"
    transition_name: str = "fade"
    transition_duration_ms: int = 500
    transition_seed: int = 0


@dataclass(frozen=True, slots=True)
class RenderResult:
    output_path: Path
    duration_ms: int
    width: int
    height: int


class FFmpegRenderer:
    def render(self, request: RenderRequest) -> RenderResult:
        if not request.segments:
            raise ValueError("at least one render segment is required")
        width, height = _profile_dimensions(request.aspect_ratio, request.resolution)
        total_ms = sum(segment.duration_ms for segment in request.segments)
        if total_ms <= 0:
            raise ValueError("render duration must be positive")

        output = Path(request.output_path)
        output.parent.mkdir(parents=True, exist_ok=True)

        command: list[str] = [str(ffmpeg_path()), "-y", "-hide_banner", "-loglevel", "error"]
        for segment in request.segments:
            command.extend(["-i", str(Path(segment.path))])

        voice_index: int | None = None
        if request.voice_path is not None:
            voice_index = len(request.segments)
            command.extend(["-i", str(Path(request.voice_path))])

        music_index: int | None = None
        if request.music_path is not None:
            music_index = len(request.segments) + (1 if voice_index is not None else 0)
            command.extend(["-stream_loop", "-1", "-i", str(Path(request.music_path))])

        filters: list[str] = []
        video_labels: list[str] = []
        counts = _frame_counts(request.segments)
        for index, segment in enumerate(request.segments):
            frame_count = counts[index]
            start = segment.source_start_ms / 1000
            duration = segment.duration_ms / 1000
            source = f"[{index}:v]trim=start={start:.6f}:duration={duration:.6f},setpts=PTS-STARTPTS"
            if request.frame_fit == "preserve_blur":
                filters.append(f"{source},split=2[v{index}bg0][v{index}fg0]")
                filters.append(
                    f"[v{index}bg0]scale={width}:{height}:force_original_aspect_ratio=increase,"
                    f"crop={width}:{height},boxblur=20:5[v{index}bg]"
                )
                filters.append(
                    f"[v{index}fg0]scale={width}:{height}:force_original_aspect_ratio=decrease[v{index}fg]"
                )
                filters.append(
                    f"[v{index}bg][v{index}fg]overlay=(W-w)/2:(H-h)/2,setsar=1,fps=25:eof_action=pass,settb=AVTB,format=yuv420p[vraw{index}]"
                )
            elif request.frame_fit == "crop_fill":
                filters.append(
                    f"{source},scale={width}:{height}:force_original_aspect_ratio=increase,"
                    f"crop={width}:{height},setsar=1,fps=25:eof_action=pass,settb=AVTB,format=yuv420p[vraw{index}]"
                )
            else:
                raise ValueError(f"unsupported frame fit: {request.frame_fit}")
            # Quantize cumulative boundaries, rather than losing a frame independently
            # per segment. Two cloned frames cover fps's end-of-input rounding.
            filters.append(f"[vraw{index}]tpad=stop_mode=clone:stop_duration=0.08,"
                           f"trim=end_frame={frame_count},setpts=N/(25*TB),fps=25:eof_action=pass,settb=AVTB[v{index}]")
            video_labels.append(f"[v{index}]")

        video_out = self._join_video(filters, video_labels, request)
        # xfade can end one frame early at EOF. Preserve the cumulative target
        # frame count after joining, including for silent videos without an audio clock.
        filters.append(f"{video_out}tpad=stop_mode=clone:stop_duration=0.08,"
                       f"trim=end_frame={sum(counts)},setpts=N/(25*TB),"
                       "fps=25:eof_action=pass,settb=AVTB[vtimed]")
        video_out = "[vtimed]"
        if request.subtitle_ass_path is not None:
            escaped = _escape_filter_path(Path(request.subtitle_ass_path))
            filters.append(f"{video_out}subtitles='{escaped}'[vsub]")
            video_out = "[vsub]"

        audio_labels: list[str] = []
        if request.keep_original_audio:
            original_parts: list[str] = []
            for index, segment in enumerate(request.segments):
                duration = segment.duration_ms / 1000
                start = segment.source_start_ms / 1000
                if _has_audio(Path(segment.path)):
                    filters.append(
                        f"[{index}:a]atrim=start={start:.6f}:duration={duration:.6f},"
                        f"asetpts=PTS-STARTPTS,volume={request.original_gain}[ao{index}]"
                    )
                else:
                    filters.append(
                        f"anullsrc=r=48000:cl=stereo,atrim=duration={duration:.6f},asetpts=PTS-STARTPTS[ao{index}]"
                    )
                original_parts.append(f"[ao{index}]")
            filters.append("".join(original_parts) + f"concat=n={len(original_parts)}:v=0:a=1[aorig]")
            audio_labels.append("[aorig]")

        total_s = total_ms / 1000
        if voice_index is not None:
            filters.append(
                f"[{voice_index}:a]atrim=duration={total_s:.6f},asetpts=PTS-STARTPTS,"
                f"volume={request.voice_gain}[avoice]"
            )
            audio_labels.append("[avoice]")

        if music_index is not None:
            fade_duration = min(1.0, total_s)
            fade_start = max(0.0, total_s - fade_duration)
            filters.append(
                f"[{music_index}:a]atrim=duration={total_s:.6f},asetpts=PTS-STARTPTS,"
                f"volume={request.music_gain},afade=t=out:st={fade_start:.6f}:d={fade_duration:.6f}[amusic]"
            )
            audio_labels.append("[amusic]")

        audio_out: str | None = None
        if len(audio_labels) == 1:
            filters.append(f"{audio_labels[0]}atrim=duration={total_s:.6f}[aout]")
            audio_out = "[aout]"
        elif len(audio_labels) > 1:
            filters.append(
                "".join(audio_labels)
                + f"amix=inputs={len(audio_labels)}:duration=longest:dropout_transition=0,"
                f"atrim=duration={total_s:.6f}[aout]"
            )
            audio_out = "[aout]"

        command.extend(["-filter_complex", ";".join(filters), "-map", video_out])
        if audio_out:
            command.extend(["-map", audio_out, "-c:a", "aac", "-b:a", "192k"])
        else:
            command.append("-an")
        command.extend(
            [
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-crf",
                "22",
                "-pix_fmt",
                "yuv420p",
                "-t",
                f"{total_s:.6f}",
                "-movflags",
                "+faststart",
                str(output),
            ]
        )
        # The separate helper owns the child and temporary file across SIGKILL.
        # Hold its pipe lease through probing and same-filesystem promotion.
        with guarded_media(command, output) as temporary:
            info = _probe(temporary)
            result = RenderResult(
                output_path=output,
                duration_ms=round(float(info["format"]["duration"]) * 1000),
                width=width,
                height=height,
            )
            temporary.replace(output)
            return result

    @staticmethod
    def _join_video(filters: list[str], labels: list[str], request: RenderRequest) -> str:
        if len(labels) == 1:
            filters.append(f"{labels[0]}null[vcat]")
            return "[vcat]"
        if request.transition_mode == "none":
            filters.append("".join(labels) + f"concat=n={len(labels)}:v=1:a=0[vcat]")
            return "[vcat]"
        if request.transition_mode not in {"fixed", "random"}:
            raise ValueError(f"unsupported transition mode: {request.transition_mode}")

        if request.transition_name not in TRANSITIONS:
            raise ValueError('unsupported simple transition')
        rng = random.Random(request.transition_seed)
        current = labels[0]
        counts = _frame_counts(request.segments)
        current_duration = counts[0] / 25
        for index in range(1, len(labels)):
            next_duration = counts[index] / 25
            transition_frames = min(round(request.transition_duration_ms * 25 / 1000), round(current_duration * 25) // 2, counts[index] // 2)
            transition = transition_frames / 25
            if transition <= 0:
                filters.append(f"{current}{labels[index]}concat=n=2:v=1:a=0[vx{index}]")
            else:
                name = rng.choice(TRANSITIONS) if request.transition_mode == "random" else request.transition_name
                padded = f"vpad{index}"
                filters.append(f"{current}tpad=stop_mode=clone:stop_duration={transition:.6f}[{padded}]")
                filters.append(
                    f"[{padded}]{labels[index]}xfade=transition={name}:"
                    f"duration={transition:.6f}:offset={current_duration:.6f}[vx{index}]"
                )
            current = f"[vx{index}]"
            current_duration += next_duration
        return current


def _frame_counts(segments):
    elapsed_ms, frame_end, counts = 0, 0, []
    for segment in segments:
        elapsed_ms += segment.duration_ms
        count = max(1, round(elapsed_ms * 25 / 1000) - frame_end)
        counts.append(count)
        frame_end += count
    return counts


def _profile_dimensions(aspect_ratio: str, resolution: str) -> tuple[int, int]:
    long_side = 1920 if resolution == "1080p" else 1280
    short_side = 1080 if resolution == "1080p" else 720
    if aspect_ratio == "9:16":
        return short_side, long_side
    if aspect_ratio == "16:9":
        return long_side, short_side
    if aspect_ratio == "1:1":
        side = short_side
        return side, side
    raise ValueError(f"unsupported aspect ratio: {aspect_ratio}")


class MediaProbeError(ValueError):
    """An actionable probe failure safe to include in a task's execution log."""

    def __init__(self, path, stage, reason, error_type):
        self.stage = stage
        self.material = Path(path).name
        self.reason = reason
        self.error_type = error_type
        self.diagnostic = dict(stage=stage, material=self.material,
                               reason=reason, error_type=error_type)
        super().__init__(f'{stage}失败；素材：{self.material}；原因：{reason}')


def _probe_json(path: Path, stage: str, arguments: list[str]) -> dict:
    tool = ffprobe_path()
    try:
        # FFprobe emits UTF-8 JSON. Windows text-mode pipe readers may decode
        # with GBK, fail in their reader thread and return stdout=None. Keep
        # bytes until json.loads so the machine's locale cannot corrupt them.
        result = run_hidden(
            [str(tool), '-v', 'error', *arguments, '-of', 'json', str(path)],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
        )
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode('utf-8', errors='replace') if isinstance(exc.stderr, bytes) else (exc.stderr or '')
        for local in (str(path), str(Path(path).resolve()), str(tool)):
            for spelling in (local, local.replace('\\', '/'), local.replace('/', '\\')):
                detail = detail.replace(spelling, Path(path).name if local != str(tool) else 'FFprobe')
        detail = ' '.join(detail.split())[-400:]
        reason = f'视频信息工具读取失败（退出码 {exc.returncode}）'
        if detail:
            reason += f'：{detail}'
        raise MediaProbeError(path, stage, reason, type(exc).__name__) from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaProbeError(path, stage, '读取超时，请检查素材文件是否可访问或已损坏。', type(exc).__name__) from exc
    except OSError as exc:
        raise MediaProbeError(path, stage, '视频信息工具无法启动，请检查工具文件和运行权限。', type(exc).__name__) from exc
    raw = result.stdout
    if not isinstance(raw, (str, bytes, bytearray)) or not raw.strip():
        raise MediaProbeError(path, stage, '视频信息工具没有返回数据，请检查素材文件是否可读取。', 'ProbeOutputError')
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise MediaProbeError(path, stage, '视频信息工具返回的数据无法解析，请检查素材格式或工具是否正常。', type(exc).__name__) from exc
    if not isinstance(value, dict):
        raise MediaProbeError(path, stage, '视频信息数据结构无效，无法读取素材。', 'ProbeSchemaError')
    return value


def _has_audio(path: Path) -> bool:
    stage = '检查素材音轨'
    value = _probe_json(path, stage, ['-select_streams', 'a', '-show_entries', 'stream=index'])
    streams = value.get('streams')
    if (not isinstance(streams, list) or any(
            not isinstance(stream, dict) or type(stream.get('index')) is not int or stream['index'] < 0
            for stream in streams)):
        raise MediaProbeError(path, stage, '素材音轨数据结构无效，无法确认原声音轨。', 'ProbeSchemaError')
    return bool(streams)


def _probe(path: Path) -> dict:
    stage = '读取素材视频信息'
    value = _probe_json(path, stage, ['-show_streams', '-show_format'])
    format_info = value.get('format')
    duration = format_info.get('duration') if isinstance(format_info, dict) else None
    try:
        valid_duration = (type(duration) in (str, int, float) and
                          math.isfinite(float(duration)) and float(duration) > 0)
    except (TypeError, ValueError, OverflowError):
        valid_duration = False
    if not valid_duration:
        raise MediaProbeError(path, stage, '未能读取有效视频时长，素材可能不完整或格式不受支持。', 'ProbeSchemaError')
    if 'streams' in value and (not isinstance(value['streams'], list) or
                               any(not isinstance(stream, dict) for stream in value['streams'])):
        raise MediaProbeError(path, stage, '素材视频流数据结构无效，无法读取素材。', 'ProbeSchemaError')
    return value


def _escape_filter_path(path: Path) -> str:
    value = str(path).replace("\\", "/")
    return value.replace("'", r"\'").replace(":", r"\:")
