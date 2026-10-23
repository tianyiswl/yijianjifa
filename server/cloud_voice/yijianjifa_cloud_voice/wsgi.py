"""Deployment entry point. Environment configuration stays on the operator server."""

import json
import os
from pathlib import Path

from .api import create_app
from .process_lock import LockedApplication,ServiceProcessLock
from .store import Store


def create_from_environment():
    database=os.environ.get('YJJF_CLOUD_DATABASE','')
    if not database or not Path(database).is_absolute():
        raise RuntimeError('经营方需配置绝对路径的服务数据库。')
    database=Path(database).resolve()
    lock=ServiceProcessLock(database)
    try:
        return LockedApplication(_configured_app(database),lock)
    except BaseException:
        lock.close()
        raise


def _configured_app(database):
    store=Store(database)
    api_key=os.environ.get('YJJF_VOLC_API_KEY','')
    app_id=os.environ.get('YJJF_VOLC_APP_ID','')
    access_key=os.environ.get('YJJF_VOLC_ACCESS_KEY','')
    audio_dir=os.environ.get('YJJF_CLOUD_AUDIO_DIR','')
    voices_file=os.environ.get('YJJF_CLOUD_VOICES_FILE','')
    max_daily=os.environ.get('YJJF_CLOUD_MAX_DAILY_WORDS','')
    configured=[bool(value) for value in (api_key,app_id,access_key,voices_file,max_daily)]
    jobs=None
    if any(configured) or audio_dir:
        if (not voices_file or not max_daily or
                (api_key and (app_id or access_key)) or
                (not api_key and (not app_id or not access_key))):
            raise RuntimeError('经营方云配音设置不完整，服务未启动。')
        try:
            daily=int(max_daily)
            # New direct-delivery tasks never create audio files. Keep a path
            # for reading legacy server-cached completions after upgrading.
            audio_dir=Path(audio_dir) if audio_dir else database.parent/'audio'
            if daily<=0 or not audio_dir.is_absolute() or not Path(voices_file).is_absolute():
                raise ValueError()
            with open(voices_file,encoding='utf-8') as source:
                voices=json.load(source)
            if not isinstance(voices,list) or not voices:
                raise ValueError()
        except (OSError,ValueError):
            raise RuntimeError('经营方音色允许列表、音频目录或每日支出上限无效。') from None
        from .jobs import JobService
        from .provider_async import AsyncVolcengineProvider
        try:
            provider=(AsyncVolcengineProvider(api_key=api_key,max_daily_words=daily) if api_key else
                      AsyncVolcengineProvider(app_id,access_key,max_daily_words=daily))
        except ValueError:
            raise RuntimeError('经营方云配音鉴权设置无效，请仅配置新版API Key或旧版App ID和Access Token。') from None
        jobs=JobService(store,audio_dir,provider,voices)
    raw_trusted=os.environ.get('YJJF_CLOUD_TRUSTED_PROXIES','')
    trusted_proxies=[value.strip() for value in raw_trusted.split(',')] if raw_trusted else []
    try:
        return create_app(store,jobs=jobs,trusted_proxies=trusted_proxies)
    except ValueError:
        raise RuntimeError('经营方可信代理配置无效，需逐项填写明确的IP地址。') from None
