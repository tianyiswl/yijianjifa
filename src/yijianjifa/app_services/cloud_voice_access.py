"""Native cloud-voice session; secrets never cross the WebUI bridge.

The release endpoint is supplied by the operator, not entered by customers. An
empty endpoint keeps the feature honestly unavailable and performs no requests.
"""
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen
import uuid

from yijianjifa.features.mixing.tts.hosted import VoicePaused


CLOUD_VOICE_ENDPOINT = ''


def _validated_cloud_voice_endpoint(value, *, allow_loopback=False):
    """Validate before urlsplit can discard controls or startup hashes the URL."""
    message = '云配音服务地址必须使用不含凭据的 HTTPS。'
    try:
        if not isinstance(value, str) or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError(message)
        value.encode('utf-8')
        endpoint = value.strip().rstrip('/')
        if not endpoint:
            return ''
        parsed = urlsplit(endpoint)
        loopback = allow_loopback and parsed.scheme == 'http' and parsed.hostname in {'127.0.0.1', 'localhost', '::1'}
        if ((parsed.scheme != 'https' and not loopback) or not parsed.hostname
                or any(char.isspace() for char in parsed.netloc)
                or parsed.username is not None or parsed.password is not None
                or '?' in endpoint or '#' in endpoint):
            raise ValueError(message)
        parsed.port
    except (ValueError, UnicodeError):
        raise ValueError(message) from None
    return endpoint


def _configured_cloud_voice_endpoint():
    """Opt-in service configuration; an invalid optional service stays disabled.

    Public source defaults to local/system voice. Official frozen builds can
    include an explicitly generated, non-secret service endpoint. Neither path
    changes existing per-endpoint credential or balance namespaces.
    """
    configured = os.environ.get('P011_CLOUD_VOICE_ENDPOINT')
    if configured is None and getattr(sys, 'frozen', False):
        from yijianjifa.os_integration.resources import resource_path
        try:
            settings = json.loads(resource_path('cloud-voice-service.json').read_text(encoding='utf-8'))
            configured = settings.get('endpoint') if isinstance(settings, dict) else None
        except (OSError, ValueError):
            configured = None
    if not isinstance(configured, str):
        return CLOUD_VOICE_ENDPOINT
    try:
        return _validated_cloud_voice_endpoint(configured)
    except ValueError:
        return ''



PACK = {'price_fen': 1990, 'words': 10000}
_MESSAGES = {
    'invalid_credentials': '配音账号或密码不正确。',
    'authentication_required': '配音账号登录已过期，请重新登录后继续原批次。',
    'invalid_session': '配音账号登录已过期，请重新登录后继续原批次。',
    'session_expired': '配音账号登录已过期，请重新登录后继续原批次。',
    'invalid_code': '兑换码无效，请核对后重试。',
    'code_used': '兑换码已经使用，请勿重复兑换。',
    'code_revoked': '兑换码已作废，请联系购买渠道。',
    'rate_limited': '操作太频繁，请稍后重试。',
    'invalid_recovery': '恢复凭证无效，请核对文件。',
    'account_exists': '此配音账号暂不可用，请换一个账号名。',
    'account_unavailable': '此配音账号暂不可用，请换一个账号名。',
    'invalid_username': '账号需为 4—32 位，可使用字母、数字或下划线。',
    'invalid_password': '密码需为6—128位。',
    'service_not_configured': '云配音服务暂未开通，请稍后继续原批次。',
    'service_unavailable': '云配音服务暂不可用，请稍后继续原批次，原作业和字数记录保留。',
    'service_busy': '云配音正在处理其他任务，请稍后继续原批次。',
    'supplier_budget_exceeded': '今日云配音服务额度已用完，请稍后继续；本段未扣字数。',
    'voice_unavailable': '此声音暂不可用，请重新选择。',
    'batch_released': '配音预留已释放，请点击继续本批重新检查。',
    'audio_gone': '原音频已无法恢复，将自动重新生成并按成功字数扣费。',
    'audio_still_available': '原音频仍可恢复，请继续下载，不重复扣费。',
    'audio_unavailable': '原配音文件暂不可用，请联系支持；不会再次扣字数。',
    'audio_not_ready': '原配音尚未完成，请稍后查询原结果，不要连续点击生成。',
    'audio_link_expired': '原音频下载链接已过期，请继续获取原任务的新链接，不会再次生成或扣字数。',
    'audio_expired': '供应商暂时无法提供原音频，请优先使用电脑中已保存的文件；不会自动重新生成或扣字数。',
    'audio_download_failed': '原音频下载未完成，请检查网络后继续原任务；不会重新生成或扣字数。',
    'direct_download_required': '此配音需要由客户端从供应商下载，请更新客户端后继续原任务。',
    'direct_retry_forbidden': '此配音已有供应商任务，请继续查询原任务，不会再次生成或扣字数。',
    'reservation_expired': '原配音预留已到期，结果需要核对；不会补扣字数或自动重新生成，请联系支持。',
    'job_running': '原配音仍在生成或核对中，请稍后继续原任务，不会重新生成或重复扣费。',
    'retry_not_ready': '原配音仍在核对中，请稍后查询原结果，不要连续点击生成；不会重复扣费。',
    'request_too_large': '本批配音内容过多，请减少本批生成数量后重新准备。',
    'idempotency_conflict': '原请求内容不一致，请保留原任务并联系支持，不会再次生成或充值。',
    'job_unknown': '原配音结果待确认，请稍后查询原结果，不会自动重新生成。',
    'unknown_result': '原配音结果待确认，请稍后查询原结果，不会自动重新生成。',
}


VOICE_POLICY_VERSION = 'paid-auto-restoration-v1'
VOICE_POLICY_NOTICE = '配音音频会长期保存在本机。能复用或重新下载原音频时不扣字数；本地音频丢失且原音频已无法恢复时，软件会自动重新生成，并按实际生成成功的字数扣费。字数不足时暂停，充值后可继续。'


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, target):
        # The fixed endpoint is authoritative. Redirecting a bearer-bearing
        # request can otherwise disclose an account session to another origin.
        return None


def _private_json(path, value):
    """Atomic native-only metadata; never use this for session credentials."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class CloudVoiceAccess:
    def __init__(self, paths, *, endpoint=None, opener=urlopen, vault=None, clock=time.time, local_test=False):
        self.paths = paths
        self.local_test = local_test is True
        if self.local_test:
            from .cloud_voice_test import validate_test_endpoint
            validate_test_endpoint(endpoint)
        self.endpoint = _validated_cloud_voice_endpoint(
            _configured_cloud_voice_endpoint() if endpoint is None else endpoint,
            allow_loopback=True)
        self.opener = build_opener(_NoRedirect()).open if opener is urlopen else opener
        self.vault, self.clock = vault, clock
        self.service = 'yijianjifa.cloud-voice.' + hashlib.sha256((str(Path(paths.data_dir).resolve()) + '|' + self.endpoint).encode()).hexdigest()[:24]
        self.mode_path = Path(paths.data_dir) / 'cloud-voice-mode.json'
        self.journal_path = Path(paths.data_dir) / 'cloud-voice-redemptions.json'
        self._lock = threading.RLock()
        self._balance = {}

    def _keyring(self):
        if self.vault is not None:
            return self.vault
        import keyring
        return keyring

    def _session(self):
        if not self.endpoint:
            return {}
        try:
            raw = self._keyring().get_password(self.service, 'session')
            value = json.loads(raw) if raw else {}
            return value if isinstance(value, dict) else {}
        except Exception:
            raise VoicePaused('credential_store_unavailable', '无法读取系统安全凭据，请检查钥匙串后继续。') from None

    def _save_session(self, value):
        fields = ('account_id', 'username', 'access_token', 'refresh_token')
        if not isinstance(value, dict) or any(not isinstance(value.get(key), str) or not value[key] for key in fields):
            raise VoicePaused('invalid_response', '云配音返回的账号资料不完整，请稍后重试。')
        try:
            self._keyring().set_password(self.service, 'session', json.dumps({key: value[key] for key in fields}))
        except Exception:
            raise VoicePaused('credential_store_unavailable', '无法保存系统安全凭据，请检查钥匙串后重新登录。') from None

    def _clear_session(self):
        try:
            if self._keyring().get_password(self.service, 'session'):
                self._keyring().delete_password(self.service, 'session')
        except Exception:
            raise VoicePaused('credential_store_unavailable', '无法清除系统安全凭据，请检查钥匙串。') from None
        self._balance = {}

    @property
    def mode(self):
        try:
            value = json.loads(self.mode_path.read_text(encoding='utf-8')).get('mode')
        except FileNotFoundError:
            return 'hosted'
        except (OSError, ValueError, AttributeError):
            return 'own'
        # An existing but damaged/unknown selection is never an opt-in to billing.
        return 'hosted' if value == 'hosted' else 'own'

    def set_mode(self, mode):
        if mode != 'hosted':
            raise ValueError('请选择系统配音或云配音，不再提供个人配音服务配置。')
        with self._lock:
            _private_json(self.mode_path, {'mode': mode})
        return self._public()

    def identity(self):
        """Native-only non-secret identity, without network IO."""
        session = self._session()
        return {'account_id': session.get('account_id'), 'username': session.get('username')}

    def _error(self, code, details=None):
        if code in {'insufficient_credit', 'insufficient_words'}:
            details = details if isinstance(details, dict) else {}
            required = details.get('required_words', details.get('required', 0))
            available = details.get('available_words', details.get('available', 0))
            if type(required) is int and type(available) is int and required >= 0 and available >= 0:
                return VoicePaused('insufficient_credit', f'本批需要 {required:,} 字，剩余 {available:,} 字，请充值后继续原批次。')
            return VoicePaused('insufficient_credit', '配音字数不足，请充值后继续原批次。')
        return VoicePaused(code, _MESSAGES.get(code, '云配音操作未完成，请检查账号和服务状态后继续原批次。'))

    def _request(self, method, path, payload=None, *, authenticated=True, binary=False, retry_auth=True):
        # A refresh rotates its token. Serialize all native requests so page
        # status and voice-catalog loads cannot replay the same refresh token
        # concurrently and revoke the newly created session family.
        with self._lock:
            return self._request_locked(method, path, payload, authenticated=authenticated, binary=binary, retry_auth=retry_auth)

    def _request_locked(self, method, path, payload=None, *, authenticated=True, binary=False, retry_auth=True):
        if not self.endpoint:
            raise VoicePaused('service_not_enabled', '云配音服务暂未开通，仍可使用系统配音。')
        headers = {'Accept': 'audio/*' if binary else 'application/json'}
        if payload is not None:
            headers['Content-Type'] = 'application/json'
        if authenticated:
            token = self._session().get('access_token')
            if not token:
                raise self._error('authentication_required')
            headers['Authorization'] = 'Bearer ' + token
        request = Request(self.endpoint + path, data=json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None, headers=headers, method=method)
        try:
            with self.opener(request, timeout=90 if path.endswith('/start') else 20) as response:
                raw = response.read(32 * 1024 * 1024 + 1 if binary else 2 * 1024 * 1024 + 1)
                if len(raw) > (32 * 1024 * 1024 if binary else 2 * 1024 * 1024):
                    raise VoicePaused('invalid_response', '云配音返回的数据过大，已停止处理。')
                if binary:
                    return raw
                value = json.loads(raw) if raw else {}
                if not isinstance(value, dict):
                    raise ValueError()
                return value
        except HTTPError as error:
            try:
                parsed = json.loads(error.read(65536))
                value = parsed.get('error', {}) if isinstance(parsed, dict) else {}
                code = value.get('code') if isinstance(value, dict) else None
                details = value.get('details') if isinstance(value, dict) else None
            except (ValueError, OSError):
                code, details = None, None
            expired = code in {None, 'session_expired', 'authentication_required', 'invalid_session', 'unauthorized'}
            if error.code == 401 and authenticated and retry_auth and expired:
                refresh = self._session().get('refresh_token')
                if refresh:
                    try:
                        fresh = self._request('POST', '/v1/sessions/refresh', {'refresh_token': refresh}, authenticated=False, retry_auth=False)
                        self._save_session(fresh)
                    except VoicePaused:
                        # Preserve account identity for frozen batches, but revoke
                        # unusable credentials in native secure storage.
                        raise self._error('authentication_required') from None
                    return self._request(method, path, payload, authenticated=True, binary=binary, retry_auth=False)
            if error.code == 401 and expired:
                code = 'authentication_required' if authenticated else 'invalid_credentials'
            raise self._error(code or 'service_unavailable', details) from None
        except (URLError, TimeoutError, OSError):
            raise VoicePaused('service_unavailable', '云配音连接未完成，请稍后继续原批次；原请求不会自动重新生成。') from None
        except VoicePaused:
            raise
        except (ValueError, UnicodeError):
            raise VoicePaused('invalid_response', '云配音返回的数据无法读取，请稍后继续原批次。') from None

    def _public(self, *, message=None):
        session = self._session() if self.endpoint else {}
        result = dict(enabled=bool(self.endpoint), authenticated=bool(session.get('access_token')), local_test=self.local_test,
                      policy_notice=VOICE_POLICY_NOTICE, policy_notice_required=self._policy_required(),
                      mode=self.mode, account_id=session.get('account_id'), account_label=session.get('username', ''),
                      available_words=self._balance.get('available_words', 0), reserved_words=self._balance.get('reserved_words', 0),
                      used_words=self._balance.get('used_words', 0), pack=dict(PACK))
        if message:
            result['message'] = message
        return result

    def _policy_seen_path(self):
        account = self._session().get('account_id')
        if not account:
            return None
        return Path(self.paths.data_dir) / 'cloud-voice-policy' / (hashlib.sha256((self.endpoint + '|' + account).encode()).hexdigest() + '.json')

    def _policy_required(self):
        path = self._policy_seen_path()
        try:
            return path is not None and json.loads(path.read_text()).get('version') != VOICE_POLICY_VERSION
        except (OSError, ValueError, AttributeError):
            return path is not None

    def policy_seen(self):
        with self._lock:
            path = self._policy_seen_path()
            if path is None:
                raise ValueError('请先登录配音账号。')
            _private_json(path, {'version': VOICE_POLICY_VERSION})
            return {'recorded': True}

    def read(self):
        with self._lock:
            if not self.endpoint:
                return self._public(message='云配音服务暂未开通。')
            if self.mode == 'own':
                result = self._public(message='原配音方式已停用，请重新选择系统配音或云配音。')
                result['balance_current'] = False
                return result
            if self._session().get('access_token'):
                try:
                    value = self._request('GET', '/v1/me')
                    self._balance = {key: value[key] for key in ('available_words', 'reserved_words', 'used_words') if type(value.get(key)) is int and value[key] >= 0}
                except VoicePaused as error:
                    result = self._public(message=str(error))
                    result['status_code'] = error.code
                    result['balance_current'] = False
                    if error.code in {'authentication_required', 'invalid_session', 'session_expired'}:
                        result['authenticated'] = False
                    return result
            result = self._public()
            result['balance_current'] = bool(result['authenticated'])
            return result

    def register(self, username, password):
        with self._lock:
            self._save_session(self._request('POST', '/v1/accounts', {'username': username, 'password': password}, authenticated=False))
            self._balance = {}
            return self.read()

    def login(self, username, password):
        with self._lock:
            self._save_session(self._request('POST', '/v1/sessions', {'username': username, 'password': password}, authenticated=False))
            self._balance = {}
            return self.read()

    def logout(self):
        with self._lock:
            if self._session().get('access_token'):
                self._request('DELETE', '/v1/sessions/current')
            self._clear_session()
            return self._public()

    def redeem(self, code):
        if not isinstance(code, str) or not 1 <= len(code.strip()) <= 256:
            raise ValueError('请填写兑换码。')
        code = code.strip().upper()
        with self._lock:
            account = self.identity().get('account_id')
            if not account:
                raise self._error('authentication_required')
            identity = hashlib.sha256((account + '|' + code).encode()).hexdigest()
            try:
                journal = json.loads(self.journal_path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                journal = {}
            key = journal.setdefault(identity, uuid.uuid4().hex)
            _private_json(self.journal_path, journal)
            result = self._request('POST', '/v1/redemptions', {'code': code, 'idempotency_key': key})
            if type(result.get('available_words')) is int:
                self._balance['available_words'] = result['available_words']
            return {key: value for key, value in result.items() if key in {'id', 'added_words', 'available_words', 'replayed'}}

    def usage(self, page=0):
        if type(page) is not int or not 0 <= page <= 100000:
            raise ValueError('用量页码无效。')
        return self._request('GET', '/v1/usage?page=' + str(page))

    def available_voices(self):
        value = self._request('GET', '/v1/voices')
        items = value.get('items', value.get('voices', []))
        return [dict(id=item['id'], name=item.get('name', item['id'])) for item in items if isinstance(item, dict) and isinstance(item.get('id'), str) and item['id'].startswith('doubao:')]

    def change_password(self, old, new):
        self._request('POST', '/v1/accounts/password', {'old_password': old, 'new_password': new})
        self._clear_session()
        return self._public()

    def logout_all(self, password):
        self._request('DELETE', '/v1/sessions', {'password': password})
        self._clear_session()
        return self._public()

    def export_recovery(self, password, path):
        path = Path(path)
        if path.exists() or path.is_symlink() or not path.parent.is_dir():
            raise ValueError('请选择新的恢复凭证文件，不能覆盖已有文件。')
        result = self._request('POST', '/v1/accounts/recovery', {'password': password})
        if not isinstance(result.get('account_id'), str) or not isinstance(result.get('recovery_secret'), str):
            raise VoicePaused('invalid_response', '恢复凭证未能生成，请稍后重试。')
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                json.dump(dict(version=1, endpoint=self.endpoint, account_id=result['account_id'], recovery_secret=result['recovery_secret']), stream)
                stream.flush()
                os.fsync(stream.fileno())
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return {'exported': True}

    def recover(self, path, password):
        path = Path(path)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 16384:
            raise ValueError('恢复凭证文件无效。')
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
            if value.get('version') != 1 or value.get('endpoint') != self.endpoint or any(not isinstance(value.get(key), str) for key in ('account_id', 'recovery_secret')):
                raise ValueError()
        except (ValueError, UnicodeError, AttributeError):
            raise ValueError('恢复凭证文件无效，请选择本软件导出的文件。') from None
        self._save_session(self._request('POST', '/v1/accounts/recover', {'account_id': value['account_id'], 'recovery_secret': value['recovery_secret'], 'new_password': password}, authenticated=False))
        self._balance = {}
        return self.read()
