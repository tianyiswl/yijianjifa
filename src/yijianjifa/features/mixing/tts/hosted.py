"""Hosted TTS consumes durable original jobs, never retries unknown upstream work."""
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
from urllib.parse import quote
import uuid

from .base import VoiceInfo, VoiceResult
from .macos_say import _probe_duration_ms
from ..sentences import split_spoken_sentences


class VoicePaused(ValueError):
    """Recoverable voice action; the queue must keep the current item intact."""
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _key(text, voice, speed):
    return _digest([text, voice, float(speed), 'seed-tts-2.0'])[:32]


class HostedTTS:
    def __init__(self, cloud, cache_dir, *, probe=_probe_duration_ms, downloader=None,
                 poll_timeout=120, poll_interval=1, clock=time.monotonic, sleep=time.sleep):
        if any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0
               for value in (poll_timeout, poll_interval)) or poll_timeout > 300:
            raise ValueError('云配音查询等待时间无效。')
        self.cloud = cloud
        self.cache = Path(cloud.paths.data_dir) / 'cloud-voice-audio'
        self.legacy_cache = Path(cache_dir) / 'hosted-tts'
        self.records = Path(cloud.paths.data_dir) / 'cloud-voice-jobs'
        self.probe = probe
        self.downloader = downloader
        self.poll_timeout, self.poll_interval = poll_timeout, poll_interval
        self.clock, self.sleep = clock, sleep
        self._lock = threading.RLock()

    @staticmethod
    def _job_record(item):
        # Signed supplier URLs are native-only short-lived capabilities. They
        # never belong in durable task identity or the browser-facing record.
        allowed = {'id', 'key', 'state', 'words', 'voice', 'speed', 'duration_ms',
                   'attempt', 'reserved_words', 'error', 'delivery_mode'}
        return {key:value for key,value in item.items() if key in allowed}

    def list_voices(self):
        return [VoiceInfo(item['id'], item['name']) for item in self.cloud.available_voices()]

    def freeze(self, snapshot):
        if not self.cloud.endpoint:
            raise VoicePaused('service_not_enabled', '云配音暂未开放，请先使用免费的系统配音。')
        identity = self.cloud.identity().get('account_id')
        if not identity:
            raise VoicePaused('authentication_required', '请先登录配音账号，再开始本批制作。')
        try:
            items = self.plan_items(snapshot)
        except ValueError as error:
            raise VoicePaused('voice_plan_invalid', str(error)) from None
        if len(items) > 2000 or sum(len(item['text']) for item in items) > 200000:
            raise VoicePaused('voice_batch_limit', '每批云配音最多 2000 段、20 万字，请减少本批生成数量。')
        return {'source': 'hosted', 'account_id': identity}

    @staticmethod
    def plan_items(snapshot):
        options = snapshot.get('options', {})
        voice, speed = options.get('voice'), options.get('speed', 1.0)
        if not isinstance(voice, str) or not voice.startswith('doubao:'):
            return []
        if type(speed) not in (int, float) or not math.isfinite(speed) or not .5 <= speed <= 2:
            raise ValueError('云配音语速支持 0.5—2 倍。')
        items = {}
        for copy in snapshot.get('copies', []):
            for text in split_spoken_sentences(copy.get('voice', '')):
                if not 1 <= len(text) <= 4000:
                    raise ValueError('每段配音需为 1—4000 字，请按句拆分较长文案。')
                if any(0xD800 <= ord(char) <= 0xDFFF for char in text):
                    raise ValueError('配音文字中存在无效符号，请删除后重试。')
                key = _key(text, voice, speed)
                items.setdefault(key, dict(key=key, text=text, voice=voice, speed=float(speed), purpose='制作任务'))
        return list(items.values())

    def _record_path(self, task_id):
        return self.records / (_digest(str(task_id)) + '.json')

    def _load(self, task_id):
        path = self._record_path(task_id)
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            # A corrupt durable identity is not a reason to create another job.
            raise VoicePaused('local_record_invalid', '原配音记录无法读取，请保留数据并联系支持，不能重新扣费生成。') from None
        if not isinstance(value, dict):
            raise VoicePaused('local_record_invalid', '原配音记录无法读取，请保留数据并联系支持。')
        return value

    def _save(self, task_id, value):
        from yijianjifa.app_services.cloud_voice_access import _private_json
        try:
            _private_json(self._record_path(task_id), value)
        except OSError:
            raise VoicePaused('local_storage_unavailable', '原配音记录未能保存，请检查磁盘和目录权限后继续。') from None

    def _assert_account(self, identity):
        original = identity.get('account_id') if isinstance(identity, dict) else None
        current = self.cloud.identity().get('account_id')
        if not original or current != original:
            raise VoicePaused('voice_account_changed', '请重新登录原配音账号后继续本批，已生成配音不会重做。')
        return original

    def prepare_batch(self, task_id, snapshot, *, items=None):
        with self._lock, self.cloud._lock:
            identity = snapshot.get('voice_identity') or self.freeze(snapshot)
            account = self._assert_account(identity)
            items = self.plan_items(snapshot) if items is None else items
            if not items:
                return {}
            fingerprint = _digest(items)
            record = self._load(task_id)
            if record and (record.get('account_id') != account or record.get('fingerprint') != fingerprint):
                raise VoicePaused('voice_plan_changed', '原批次的配音账号或内容不一致，请恢复原设置后继续。')
            if not record:
                record = dict(account_id=account, fingerprint=fingerprint,
                              request_key=_digest(['mix-voice', task_id])[:32], jobs={})
                self._save(task_id, record)
            if record.get('batch_id'):
                self.cloud._request('POST', '/v1/batches/' + quote(record['batch_id'], safe='') + '/lease', {})
                response = self.cloud._request('GET', '/v1/batches/' + quote(record['batch_id'], safe=''))
            else:
                response = self.cloud._request('POST', '/v1/batches', dict(idempotency_key=record['request_key'], items=items))
            if not isinstance(response.get('id'), str) or not isinstance(response.get('jobs'), list):
                raise VoicePaused('invalid_response', '云配音批次记录不完整，请稍后继续原批次。')
            jobs = {job['key']: self._job_record(job) for job in response['jobs'] if isinstance(job, dict) and isinstance(job.get('id'), str) and isinstance(job.get('key'), str)}
            if set(jobs) != {item['key'] for item in items}:
                raise VoicePaused('invalid_response', '云配音批次内容不完整，请稍后继续原批次。')
            record.update(batch_id=response['id'], jobs=jobs,
                          required_words=response.get('required_words', sum(len(item['text']) for item in items)))
            authorization = snapshot.get('voice_retry_authorization')
            if isinstance(authorization, str) and len(authorization) == 32:
                record['retry_authorization'] = authorization
            self._save(task_id, record)
            return record

    def estimate_batch(self, snapshot):
        items = self.plan_items(snapshot)
        return {'source': 'hosted', 'words': sum(len(item['text']) for item in items),
                'exact': False, 'account_id': (snapshot.get('voice_identity') or {}).get('account_id'),
                'available_words': self.cloud._balance.get('available_words')}

    def release_batch(self, task_id, snapshot=None):
        with self._lock, self.cloud._lock:
            record = self._load(task_id)
            if not record or not record.get('batch_id'):
                return
            self._assert_account(record)
            self.cloud._request('POST', '/v1/batches/' + quote(record['batch_id'], safe='') + '/release', {})

    @contextmanager
    def batch_context(self, task_id, snapshot):
        """Renew while video rendering keeps already reserved future work pending."""
        record = self.prepare_batch(task_id, snapshot)
        stop, lease_error = threading.Event(), []
        def renew():
            while not stop.wait(60):
                try:
                    self._assert_account(record)
                    self.cloud._request('POST', '/v1/batches/' + quote(record['batch_id'], safe='') + '/lease', {})
                except VoicePaused as error:
                    lease_error[:] = [error]
                    return
        worker = threading.Thread(target=renew, name='cloud-voice-lease', daemon=True)
        if record:
            worker.start()
        try:
            yield _BoundHostedTTS(self, task_id, record, lease_error)
        finally:
            stop.set()
            if record:
                worker.join(timeout=.1)

    def synthesize(self, text, voice, speed, output):
        # Custom audition is also a durable one-item batch. A timeout reopens
        # this exact identity instead of manufacturing a fresh paid attempt.
        if not isinstance(text, str) or not text.strip() or not 1 <= len(text) <= 4000:
            raise ValueError('请输入 1—4000 字的配音文字。')
        if any(0xD800 <= ord(char) <= 0xDFFF for char in text):
            raise ValueError('配音文字中存在无效符号，请删除后重试。')
        if not isinstance(voice, str) or not voice.startswith('doubao:'):
            raise ValueError('请选择可用的云配音声音。')
        if type(speed) not in (int, float) or not math.isfinite(speed) or not .5 <= speed <= 2:
            raise ValueError('云配音语速支持 0.5—2 倍。')
        account = self.cloud.identity().get('account_id')
        task_id = 'audition:' + _digest([account, text, voice, float(speed)])
        snapshot = dict(copies=[{'voice': text}], options={'voice': voice, 'speed': speed}, voice_identity=self.freeze({}),
                        voice_retry_authorization=uuid.uuid4().hex)
        # Audition must preserve the exact text rather than use sentence-strip
        # rules intended for making: the displayed audition count is len(text).
        with self._lock:
            item_key = _key(text, voice, speed)
            record = self.prepare_batch(task_id, snapshot, items=[dict(key=item_key, text=text, voice=voice, speed=float(speed), purpose='文字试听')])
        return self._synthesize(task_id, record, text, voice, speed, output)

    def _synthesize(self, task_id, record, text, voice, speed, output, *, restoring=False):
        with self._lock, self.cloud._lock:
            self._assert_account(record)
            item = record.get('jobs', {}).get(_key(text, voice, speed))
            if not item:
                raise VoicePaused('voice_plan_changed', '当前配音不属于原批次，请恢复原内容后继续。')
            job_id = item['id']
            job_path = '/v1/jobs/' + quote(job_id, safe='')
            attempt = item.get('attempt', 1)
            cached = self.cache / _digest(record['account_id']) / (_digest(job_id) + f'.{attempt}.mp3')
            legacy = self.legacy_cache / _digest(record['account_id']) / (_digest(job_id) + '.mp3')
            if attempt == 1 and not cached.exists() and legacy.is_file():
                try:
                    valid_legacy = self.probe(legacy) > 0
                except OSError:
                    raise VoicePaused('local_storage_unavailable', '原音频暂时无法读取，请检查磁盘后继续；不会重新生成扣费。') from None
                except (ValueError, subprocess.SubprocessError):
                    valid_legacy = False
                if valid_legacy:
                    temporary_migration = cached.with_name(cached.name + '.' + uuid.uuid4().hex + '.partial')
                    try:
                        cached.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(legacy, temporary_migration)
                        temporary_migration.chmod(0o600)
                        temporary_migration.replace(cached)
                    except OSError:
                        raise VoicePaused('local_storage_unavailable', '原音频未能迁移保存，请检查磁盘后继续；不会重新生成扣费。') from None
                    finally:
                        temporary_migration.unlink(missing_ok=True)
            if cached.is_file():
                try:
                    duration = self.probe(cached)
                    if duration <= 0:
                        raise ValueError()
                except OSError:
                    raise VoicePaused('local_storage_unavailable', '本地音频暂时无法读取，请检查磁盘后继续；不会重新生成扣费。') from None
                except (ValueError, subprocess.SubprocessError):
                    cached.unlink(missing_ok=True)
                else:
                    target = Path(output).with_suffix('.mp3')
                    try:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        if target.resolve() != cached.resolve():
                            shutil.copyfile(cached, target)
                    except OSError:
                        raise VoicePaused('local_storage_unavailable', '音频输出未能保存，请检查磁盘后继续；原音频保留，不会重复扣费。') from None
                    return VoiceResult(target, duration)
            state = item.get('state')
            # An unknown async submission can already exist at the supplier.
            # Even an explicit continuation must query that original identity.
            retryable = state == 'failed' or state == 'unknown' and item.get('delivery_mode') != 'direct'
            if retryable and record.get('retry_authorization'):
                nonce = _digest([record['retry_authorization'], job_id])[:32]
                used = record.setdefault('retry_requests', {})
                if used.get(job_id) != nonce:
                    # Consume the human authorization before IO. A lost retry
                    # response can only query this original id, never POST again.
                    used[job_id] = nonce
                    self._save(task_id, record)
                    try:
                        item = self.cloud._request('POST', job_path + '/retry', {'idempotency_key': nonce})
                    except VoicePaused as error:
                        if error.code != 'service_unavailable':
                            used.pop(job_id, None)
                            self._save(task_id, record)
                            raise
                        item = self.cloud._request('GET', job_path)
                    state = item.get('state')
            if state in {'pending', None}:
                try:
                    item = self.cloud._request('POST', '/v1/batches/' + quote(record['batch_id'], safe='') + '/jobs/' + quote(_key(text, voice, speed), safe='') + '/start', {})
                except VoicePaused as error:
                    if error.code != 'service_unavailable':
                        raise
                    item = self.cloud._request('GET', job_path)
            else:
                item = self.cloud._request('GET', job_path)
            item_key = _key(text, voice, speed)
            self._remember_job(task_id, record, item_key, job_id, item)
            if item.get('delivery_mode') == 'direct':
                item = self._wait_direct(task_id, record, item_key, job_id, item)
            state = item.get('state')
            if state != 'succeeded':
                message = ('此段配音生成未成功，未扣字数；请在用量记录核对后明确重试。' if state == 'failed'
                           else '原配音结果待确认，请稍后继续查询，不会自动重新生成或重复扣费。')
                raise VoicePaused('voice_generation_failed' if state == 'failed' else 'job_unknown', message)
            attempt = item.get('attempt', 1)
            cached = self.cache / _digest(record['account_id']) / (_digest(job_id) + f'.{attempt}.mp3')
            try:
                cached.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                raise VoicePaused('local_storage_unavailable', '本地音频目录无法写入，请检查磁盘后继续。') from None
            temporary = cached.with_name(cached.stem + '.' + uuid.uuid4().hex + '.partial.mp3')
            try:
                # Check writable storage before a missing upstream object can
                # lead to a paid replacement. Downloader still requires no file.
                fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(fd)
                temporary.unlink()
                if item.get('delivery_mode') == 'direct':
                    self._download_direct(job_id, job_path, temporary, attempt)
                else:
                    # Compatibility only: pre-migration and local simulation
                    # jobs still expose the existing private audio object.
                    raw = self.cloud._request('GET', job_path + '/audio', binary=True)
                    if not raw:
                        raise VoicePaused('invalid_audio', '原配音音频暂不可用，请稍后重新获取，不会重复扣费。')
                    temporary.write_bytes(raw)
                    temporary.chmod(0o600)
                duration = self.probe(temporary)
                if duration <= 0:
                    raise ValueError()
                temporary.replace(cached)
            except VoicePaused as error:
                if error.code == 'audio_gone' and not restoring:
                    nonce = _digest(['restore-audio-v1', record['account_id'], job_id, attempt])
                    restored = self.cloud._request('POST', job_path + '/restore',
                        {'idempotency_key': nonce, 'expected_attempt': attempt})
                    self._remember_job(task_id, record, _key(text, voice, speed), job_id, restored)
                    return self._synthesize(task_id, record, text, voice, speed, output, restoring=True)
                raise
            except (OSError, ValueError, subprocess.SubprocessError):
                raise VoicePaused('invalid_audio', '原配音音频未能完整保存，请稍后重新获取，不会重复扣费。') from None
            finally:
                temporary.unlink(missing_ok=True)
            target = Path(output).with_suffix('.mp3')
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(cached, target)
            return VoiceResult(target, duration)

    def _remember_job(self, task_id, record, item_key, job_id, item):
        if not isinstance(item, dict) or item.get('id', job_id) != job_id:
            raise VoicePaused('invalid_response', '原配音任务身份无法核对，请稍后继续原任务。')
        record['jobs'][item_key].update(self._job_record(item))
        self._save(task_id, record)

    def _wait_direct(self, task_id, record, item_key, job_id, item):
        deadline = self.clock() + self.poll_timeout
        path = '/v1/jobs/' + quote(job_id, safe='')
        while item.get('state') in {'running', 'unknown'}:
            if isinstance(item.get('error'), dict) and item['error'].get('code') == 'reservation_expired':
                raise VoicePaused('reservation_expired', '原配音预留已到期，结果需要核对；不会补扣字数或自动重新生成，请联系支持。')
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise VoicePaused('job_running', '原配音仍在生成或核对中，请稍后继续；不会重新生成或重复扣费。')
            self.sleep(min(self.poll_interval, remaining))
            if self.clock() >= deadline:
                raise VoicePaused('job_running', '原配音仍在生成或核对中，请稍后继续；不会重新生成或重复扣费。')
            item = self.cloud._request('GET', path)
            self._remember_job(task_id, record, item_key, job_id, item)
        return item

    def _download_direct(self, job_id, job_path, temporary, expected_attempt):
        if self.downloader is None:
            from .supplier_download import SupplierAudioDownloader
            self.downloader = SupplierAudioDownloader()
        # One refresh for an expired signed URL. Neither path authorizes a
        # generation, retry, or another ledger event.
        for attempt in range(2):
            try:
                descriptor = self.cloud._request('GET', job_path + '/download')
                if not isinstance(descriptor, dict) or descriptor.get('job_id') != job_id:
                    raise VoicePaused('invalid_response', '原音频下载信息无法核对，请稍后继续原任务。')
                if descriptor.get('attempt', expected_attempt) != expected_attempt:
                    raise VoicePaused('audio_attempt_changed', '原音频已有更新，请继续原任务获取最新音频，不会重复生成。')
                self.downloader.download(descriptor, temporary)
                return
            except VoicePaused as error:
                if error.code != 'audio_link_expired' or attempt:
                    raise


class _BoundHostedTTS:
    def __init__(self, parent, task_id, record, lease_error):
        self.parent, self.task_id, self.record, self.lease_error = parent, task_id, record, lease_error

    def synthesize(self, text, voice, speed, output):
        if self.lease_error:
            raise self.lease_error[0]
        return self.parent._synthesize(self.task_id, self.record, text, voice, speed, output)
