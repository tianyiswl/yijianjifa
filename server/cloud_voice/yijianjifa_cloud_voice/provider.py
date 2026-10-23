"""Operator-only bounded speech provider; no credentials enter the desktop app."""
from __future__ import annotations

import base64
import binascii
import io
import json
import math
import socket
import time
import urllib.error
import urllib.request
import wave


class ProviderFailure(Exception):
    """Upstream explicitly rejected generation; safe to release user reservation."""


class ProviderAudioGone(Exception):
    """Trusted query proves a previously generated audio object was deleted."""


class ProviderUnknown(Exception):
    """An upstream request may have run; never implicitly send it again."""


MAX_AUDIO_BYTES = 32 * 1024 * 1024


def inspect_audio(data: bytes) -> tuple[int, str]:
    """Validate bounded PCM WAV or contiguous MPEG audio frames without a decoder."""
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_AUDIO_BYTES:
        raise ProviderFailure('invalid_audio')
    if data[:4] == b'RIFF' and data[8:12] == b'WAVE':
        try:
            with wave.open(io.BytesIO(data), 'rb') as stream:
                frames = stream.getnframes()
                rate = stream.getframerate()
                if stream.getcomptype() != 'NONE' or not 1 <= stream.getnchannels() <= 2 or not 1 <= stream.getsampwidth() <= 4 or not 8000 <= rate <= 192000:
                    raise ValueError
                expected = frames * stream.getnchannels() * stream.getsampwidth()
                if not frames or len(stream.readframes(frames)) != expected:
                    raise ValueError
                return max(1, round(frames * 1000 / rate)), 'audio/wav'
        except (wave.Error, EOFError, ValueError):
            raise ProviderFailure('invalid_audio') from None
    offset = 0
    if data[:3] == b'ID3':
        if len(data) < 10 or any(value & 0x80 for value in data[6:10]):
            raise ProviderFailure('invalid_audio')
        size = sum(value << shift for value, shift in zip(data[6:10], (21, 14, 7, 0)))
        offset = 10 + size + (10 if data[5] & 0x10 else 0)
    count, seconds = 0, 0.0
    while offset + 4 <= len(data):
        if len(data) - offset == 128 and data[offset:offset+3] == b'TAG':
            offset = len(data)
            break
        header = int.from_bytes(data[offset:offset+4], 'big')
        version, layer = (header >> 19) & 3, (header >> 17) & 3
        bitrate_index, sample_index = (header >> 12) & 15, (header >> 10) & 3
        if header >> 21 != 0x7ff or version == 1 or layer != 1 or bitrate_index in (0, 15) or sample_index == 3:
            raise ProviderFailure('invalid_audio')
        bitrates = (0,32,40,48,56,64,80,96,112,128,160,192,224,256,320) if version == 3 else (0,8,16,24,32,40,48,56,64,80,96,112,128,144,160)
        rate = (44100,48000,32000)[sample_index] // (1 if version == 3 else 2 if version == 2 else 4)
        samples = 1152 if version == 3 else 576
        length = (144 if version == 3 else 72) * bitrates[bitrate_index] * 1000 // rate + ((header >> 9) & 1)
        if length < 4 or offset + length > len(data):
            raise ProviderFailure('invalid_audio')
        offset += length
        count += 1
        seconds += samples / rate
    if count < 2 or offset != len(data):
        raise ProviderFailure('invalid_audio')
    return max(1, round(seconds * 1000)), 'audio/mpeg'


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


class VolcengineProvider:
    """Fixed official SSE endpoint; budget is also enforced durably by JobService."""
    version = 'seed-tts-2.0-unicode-codepoint-v1'
    endpoint = 'https://openspeech.bytedance.com/api/v3/tts/unidirectional/sse'

    def __init__(self, app_id: str, access_key: str, *, max_daily_words: int, opener=None, timeout=60):
        if not isinstance(app_id,str) or not isinstance(access_key,str) or not app_id or not access_key or len(app_id)>128 or len(access_key)>4096 or any(char.isspace() for char in app_id+access_key) or not isinstance(max_daily_words, int) or isinstance(max_daily_words, bool) or max_daily_words <= 0:
            raise ValueError('云配音经营方配置与每日支出上限不能为空。')
        if not isinstance(timeout, (int, float)) or not 1 <= timeout <= 120:
            raise ValueError('云配音连接时限不正确。')
        self._app_id, self._access_key = str(app_id), str(access_key)
        self.max_daily_words = max_daily_words
        self.opener, self.timeout = opener or urllib.request.build_opener(_NoRedirect()).open, timeout

    def __repr__(self):
        return '<VolcengineProvider configured>'

    def synthesize(self, text: str, voice: str, speed: float, request_id: str):
        if not isinstance(text,str) or not text.strip() or len(text)>4000 or not isinstance(voice,str) or not voice.startswith('doubao:') or not isinstance(speed,(int,float)) or isinstance(speed,bool) or not math.isfinite(speed) or not .5 <= speed <= 2:
            raise ProviderFailure('invalid_request')
        body = {'user':{'uid':'yijianjifa-hosted'},'req_params':{'text':text,'speaker':voice.split(':',1)[1], 'audio_params':{'format':'mp3','sample_rate':24000,'speech_rate':round((speed-1)*100)}}}
        request = urllib.request.Request(self.endpoint, data=json.dumps(body,ensure_ascii=False).encode(), headers={'Content-Type':'application/json','X-Api-App-Id':self._app_id,'X-Api-Access-Key':self._access_key,'X-Api-Resource-Id':'seed-tts-2.0','X-Api-Request-Id':request_id})
        chunks, size, complete, events = [], 0, False, 0
        stream_size, deadline = 0, time.monotonic()+self.timeout
        try:
            with self.opener(request,timeout=self.timeout) as response:
                # readline is bounded before allocation; no unbounded HTTP line iterator.
                while True:
                    if time.monotonic()>deadline:
                        raise ProviderUnknown('provider_result_unknown')
                    raw = response.readline(4 * 1024 * 1024 + 1)
                    stream_size += len(raw)
                    if stream_size>64*1024*1024:
                        raise ProviderUnknown('oversized_response')
                    if not raw:
                        break
                    if len(raw)>4*1024*1024:
                        raise ProviderUnknown('oversized_response')
                    if not raw.startswith(b'data:'):
                        continue
                    events += 1
                    if events > 100000:
                        raise ProviderUnknown('oversized_response')
                    event = json.loads(raw[5:])
                    if not isinstance(event,dict):
                        raise ProviderUnknown('invalid_response')
                    if event.get('code') not in (0,20000000):
                        raise ProviderFailure('provider_rejected')
                    if event.get('data'):
                        chunk = base64.b64decode(event['data'],validate=True)
                        size += len(chunk)
                        if size>MAX_AUDIO_BYTES:
                            raise ProviderUnknown('oversized_audio')
                        chunks.append(chunk)
                    if event.get('code')==20000000:
                        complete=True
                        break
        except ProviderFailure:
            raise
        except urllib.error.HTTPError as error:
            if error.code in (400,401,403,404,413,422,429):
                raise ProviderFailure('provider_rejected') from None
            raise ProviderUnknown('provider_unavailable') from None
        except (urllib.error.URLError,TimeoutError,socket.timeout,OSError,ValueError,TypeError,binascii.Error):
            raise ProviderUnknown('provider_result_unknown') from None
        if not complete or not chunks:
            raise ProviderUnknown('provider_result_unknown')
        audio=b''.join(chunks)
        duration,_=inspect_audio(audio)
        return audio,duration
