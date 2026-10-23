"""Operator-only asynchronous TTS. Audio is downloaded by the desktop client.

The supplier task ID is fixed before submission. An uncertain response always
keeps that identity for query; this adapter never retries a submission itself.
"""
from __future__ import annotations

import ipaddress
import json
import math
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

from .provider import ProviderFailure, ProviderUnknown

MAX_JSON_BYTES = 1024 * 1024
_SUPPLIER_DOMAINS = ('bytespeech.com', 'bytetos.com')
_TASK_ID = re.compile(r'[A-Za-z0-9_-]{20,64}\Z')
_LABEL = re.compile(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z')


def validate_supplier_url(url: str) -> str:
    """Keep signed URLs intact while refusing ambiguous or non-supplier hosts.

    This policy is mirrored in the desktop download adapter. Never include the
    signed address in an exception: query parameters are temporary credentials.
    """
    try:
        if not isinstance(url, str) or not 1 <= len(url) <= 8192:
            raise ValueError
        if any(ord(char) <= 32 or ord(char) >= 127 for char in url) or '\\' in url or '#' in url:
            raise ValueError
        if re.search(r'%(?![0-9A-Fa-f]{2})|%(?:0[0-9a-f]|1[0-9a-f]|7f|5c)', url, re.IGNORECASE):
            raise ValueError
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname
        if (parsed.scheme != 'https' or not host or parsed.username is not None or
                parsed.password is not None or parsed.fragment or parsed.port not in (None, 443) or
                '%' in parsed.netloc or parsed.netloc.endswith(':') or len(host) > 253 or
                not all(_LABEL.fullmatch(label) for label in host.split('.'))):
            raise ValueError
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ValueError
        if not any(host == domain or host.endswith('.' + domain) for domain in _SUPPLIER_DOMAINS):
            raise ValueError
    except (ValueError, TypeError, UnicodeError):
        raise ProviderUnknown('invalid_supplier_url') from None
    return url


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


class AsyncVolcengineProvider:
    """Fixed official submit/query endpoints, without server-side audio I/O."""
    delivery_mode = 'direct'
    version = 'seed-tts-2.0-unicode-codepoint-v1'
    submit_endpoint = 'https://openspeech.bytedance.com/api/v3/tts/submit'
    query_endpoint = 'https://openspeech.bytedance.com/api/v3/tts/query'

    def __init__(self, app_id: str | None = None, access_key: str | None = None, *,
                 api_key: str | None = None, max_daily_words: int,
                 opener=None, timeout=60, clock=None):
        # New console API Key and legacy App ID/Access Token are alternatives.
        # Never guess the credential type or send both sets of headers.
        if api_key is not None:
            valid_auth = (app_id is None and access_key is None and isinstance(api_key, str)
                          and 0 < len(api_key) <= 4096
                          and not any(char.isspace() or ord(char) < 32 for char in api_key))
        else:
            valid_auth = (isinstance(app_id, str) and isinstance(access_key, str)
                          and 0 < len(app_id) <= 128 and 0 < len(access_key) <= 4096
                          and not any(char.isspace() or ord(char) < 32 for char in app_id + access_key))
        if (not valid_auth or not isinstance(max_daily_words, int)
                or isinstance(max_daily_words, bool) or max_daily_words <= 0):
            raise ValueError('云配音经营方配置与每日支出上限不能为空。')
        if (not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or
                not math.isfinite(timeout) or not 1 <= timeout <= 120):
            raise ValueError('云配音连接时限不正确。')
        self._app_id, self._access_key, self._api_key = app_id, access_key, api_key
        self.max_daily_words, self.timeout = max_daily_words, timeout
        self.opener = opener or urllib.request.build_opener(_NoRedirect()).open
        self.clock = clock or time.time

    def __repr__(self):
        return '<AsyncVolcengineProvider configured>'

    def _request(self, endpoint, payload, task_id, *, submitting=False):
        headers = {'Content-Type': 'application/json', 'X-Api-Resource-Id': 'seed-tts-2.0',
                   'X-Api-Request-Id': task_id}
        if self._api_key is not None:
            headers['X-Api-Key'] = self._api_key
        else:
            headers.update({'X-Api-App-Id': self._app_id, 'X-Api-Access-Key': self._access_key})
        request = urllib.request.Request(endpoint,
            data=json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8'),
            headers=headers)
        try:
            with self.opener(request, timeout=self.timeout) as response:
                # Even an injected transport may not turn an API request into a
                # redirect, or return an unbounded provider body.
                final_url = getattr(response, 'geturl', lambda: endpoint)()
                status = getattr(response, 'status', 200)
                if final_url != endpoint or (status is not None and status != 200):
                    raise ProviderUnknown('provider_result_unknown')
                raw = response.read(MAX_JSON_BYTES + 1)
                if not isinstance(raw, bytes) or len(raw) > MAX_JSON_BYTES:
                    raise ProviderUnknown('oversized_response')
                result = json.loads(raw)
        except urllib.error.HTTPError as error:
            # A duplicate unique_id can be reported as a bad request. A missing
            # task response is also not proof that the original submission did
            # not run. Only definite pre-execution rejection releases a submit.
            if submitting and error.code in (401, 403, 413):
                raise ProviderFailure('provider_rejected') from None
            raise ProviderUnknown('provider_result_unknown') from None
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError,
                ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
            raise ProviderUnknown('provider_result_unknown') from None
        if not isinstance(result, dict) or type(result.get('code')) is not int or result['code'] != 20000000:
            # No undocumented business error is interpreted as permission to
            # generate again. Query task_status=3 is the explicit failure path.
            raise ProviderUnknown('provider_result_unknown')
        data = result.get('data')
        if not isinstance(data, dict):
            raise ProviderUnknown('invalid_response')
        if data.get('task_id') != task_id:
            raise ProviderUnknown('provider_task_mismatch')
        if type(data.get('task_status')) is not int or data['task_status'] not in (1, 2, 3):
            raise ProviderUnknown('invalid_response')
        return data

    def submit(self, text: str, voice: str, speed: float, request_id: str):
        if (not isinstance(text, str) or not text.strip() or len(text) > 4000 or
                any(0xD800 <= ord(char) <= 0xDFFF for char in text) or
                not isinstance(voice, str) or not voice.startswith('doubao:') or
                len(voice) <= 7 or len(voice) > 256 or
                not isinstance(speed, (int, float)) or isinstance(speed, bool) or
                not math.isfinite(speed) or not .5 <= speed <= 2 or
                not isinstance(request_id, str) or not _TASK_ID.fullmatch(request_id)):
            raise ProviderFailure('invalid_request')
        body = {'user': {'uid': 'yijianjifa-hosted'}, 'unique_id': request_id,
                'req_params': {'text': text, 'speaker': voice.split(':', 1)[1],
                               'audio_params': {'format': 'mp3', 'sample_rate': 24000,
                                                'speech_rate': round((speed - 1) * 100),
                                                'enable_timestamp': True}}}
        data = self._request(self.submit_endpoint, body, request_id, submitting=True)
        if data['task_status'] == 3:
            raise ProviderFailure('provider_rejected')
        # A submit response alone never settles words. A separate query must
        # confirm completion and obtain the short-lived download descriptor.
        return {'task_id': request_id, 'state': 'running'}

    def query(self, task_id: str):
        if not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id):
            raise ProviderUnknown('invalid_request')
        data = self._request(self.query_endpoint, {'task_id': task_id}, task_id)
        if data['task_status'] == 1:
            return {'task_id': task_id, 'state': 'running'}
        if data['task_status'] == 3:
            return {'task_id': task_id, 'state': 'failed'}
        url = validate_supplier_url(data.get('audio_url'))
        expires = data.get('url_expire_time')
        if (not isinstance(expires, (int, float)) or isinstance(expires, bool) or
                not math.isfinite(expires) or expires <= 0):
            raise ProviderUnknown('invalid_response')
        duration = self._duration(data.get('sentences'))
        result = {'task_id': task_id, 'state': 'succeeded' if expires > self.clock() else 'expired',
                  'expires_at': float(expires), 'duration_ms': duration}
        if result['state'] == 'succeeded':
            result['url'] = url
        return result

    @staticmethod
    def _duration(sentences):
        if isinstance(sentences, dict):
            sentences = [sentences]
        if not isinstance(sentences, list):
            return None
        ends = [item.get('endTime') for item in sentences if isinstance(item, dict)]
        ends = [value for value in ends if isinstance(value, (int, float)) and
                not isinstance(value, bool) and math.isfinite(value) and value > 0 and
                math.isfinite(value * 1000)]
        return max(1, round(max(ends) * 1000)) if ends else None
