"""Measure each spoken sentence; subtitle timing follows synthesized audio."""
from pathlib import Path
from yijianjifa.os_integration.background_process import run_hidden
import subprocess
import unicodedata
from .tts.base import VoiceResult, SentenceSpan, system_tts_adapter
from .sentences import split_spoken_sentences, split_subtitle_chunks
from .subtitles import build_subtitle_spec
from .subtitle_styles import validate_style, ass_style, ass_text, ass_position
from .ffmpeg_renderer import _profile_dimensions
from yijianjifa.os_integration.resources import ffmpeg_path


def synthesize_sentences(text, voice, speed, folder, adapter=None):
    adapter = adapter or system_tts_adapter()
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    spans, files, offset = [], [], 0
    for i, sentence in enumerate(split_spoken_sentences(text)):
        result = adapter.synthesize(sentence, voice, speed, folder / f'sentence-{i}.aiff')
        if result.duration_ms <= 0:
            raise ValueError('系统配音未产生有效时长')
        files.append(result.output_path)
        spans.append(SentenceSpan(sentence, offset, offset + result.duration_ms))
        offset += result.duration_ms
    if not files:
        raise ValueError('没有可配音的内容')
    if len(files) == 1:
        return VoiceResult(files[0], offset, tuple(spans))
    output = folder / 'voice.wav'
    command = [str(ffmpeg_path()), '-y', '-v', 'error']
    for path in files:
        command += ['-i', str(path)]
    filters = ';'.join(f'[{i}:a]aresample=48000,aformat=sample_fmts=s16:channel_layouts=mono[a{i}]' for i in range(len(files)))
    filters += ';' + ''.join(f'[a{i}]' for i in range(len(files))) + f'concat=n={len(files)}:v=0:a=1[out]'
    command += ['-filter_complex', filters, '-map', '[out]', str(output)]
    run_hidden(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return VoiceResult(output, offset, tuple(spans))


def write_subtitles(path, copy, options, duration, spans, *, aspect_ratio="9:16", resolution="1080p"):
    spoken_style = options.get("spoken_style", {})
    full_style = options.get("full_style", {})
    validate_style(spoken_style)
    validate_style(full_style)
    # ASS uses a resolution-independent design canvas; libass scales it to the output.
    width, height = _profile_dimensions(aspect_ratio, "1080p")
    spec = build_subtitle_spec(spoken_enabled=options.get('spoken_enabled', True),
                              full_enabled=options.get('full_enabled', False),
                              batch_full_text=options.get('batch_full_text', ''),
                              copy_full_text=copy['full'], title=copy['title'])
    events = []
    def stamp(ms):
        cs = round(ms / 10)
        return f'{cs // 360000}:{cs // 6000 % 60:02}:{cs // 100 % 60:02}.{cs % 100:02}'
    if spec.spoken_enabled:
        # Keep the requested font size; paginate long sentences instead of
        # wrapping or shrinking. Timing stays within the measured sentence.
        limit = max(1, min(spoken_style.get('chars', 12), int((width - 120) / (spoken_style.get('size', 75) * 1.05))))
        for span in spans:
            text = ' '.join(span.text.splitlines())
            if not text:
                continue
            offset = 0
            for chunk in split_subtitle_chunks(text, limit):
                start = span.start_ms + round((span.end_ms - span.start_ms) * offset / len(text))
                end = span.start_ms + round((span.end_ms - span.start_ms) * (offset + len(chunk)) / len(text))
                visible = ''.join(c for c in chunk if not unicodedata.category(c).startswith('P')).strip()
                if not visible:
                    offset += len(chunk)
                    continue
                literal = ass_text(visible, {k:v for k,v in spoken_style.items() if k != 'chars'})
                events.append(f'Dialogue: 0,{stamp(start)},{stamp(end)},Spoken,,0,0,0,,'+ass_position(spoken_style,width,height)+r'{\q2}'+literal)
                offset += len(chunk)
    if spec.full_time_text:
        events.append(f'Dialogue: 1,0:00:00.00,{stamp(duration)},Full,,0,0,0,,{ass_position(full_style,width,height)}{ass_text(spec.full_time_text, full_style)}')
    if not events:
        return None
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'''[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
{ass_style('Spoken', spoken_style)}
{ass_style('Full', full_style)}
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
''' + '\n'.join(events), encoding='utf-8')
    return path
