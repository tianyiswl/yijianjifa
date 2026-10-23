"""Regression for the deployed four-voice allowlist rejecting Ahu batches."""
import json
from pathlib import Path
from yijianjifa_cloud_voice.store import Store
from yijianjifa_cloud_voice.jobs import JobService
from yijianjifa_cloud_voice.errors import CloudError
from unittest.mock import Mock
voice='doubao:zh_male_wennuanahu_uranus_bigtts'
base=[{'id':v} for v in ['doubao:zh_female_vv_uranus_bigtts','doubao:zh_female_xiaohe_uranus_bigtts','doubao:zh_male_m191_uranus_bigtts','doubao:zh_male_taocheng_uranus_bigtts']]
def test_ahu_allowlist_reload_accepts_previously_rejected_batch_without_double_reservation(tmp_path):
 p=tmp_path;store=Store(p/'test.db');provider=Mock();provider.version='test-v1';provider.max_daily_words=100000
 with store.transaction() as c:c.execute('INSERT INTO accounts (id,username,password_hash,recovery_hash,credits,used,reserved,created_at) VALUES (?,?,?,?,?,?,?,?)',('qa','qa','x','y',100,0,0,0))
 payload={'idempotency_key':'ahu-regression','items':[{'key':'a'*32,'text':'阿虎测试。','voice':voice,'speed':1.0,'purpose':'制作任务'}]}
 jobs=JobService(store,p/'audio',provider,base)
 try:jobs.prepare('qa',payload)
 except CloudError as e:assert e.code=='voice_unavailable'
 else:raise AssertionError('Expected rejection')
 with store.connect() as c:assert tuple(c.execute('select credits,used,reserved from accounts').fetchone())==(100,0,0)
 expanded=base+[{'id':voice,'name':'温暖阿虎 2.0'}]
 jobs=JobService(store,p/'audio',provider,expanded);first=jobs.prepare('qa',payload);second=jobs.prepare('qa',payload);assert first['id']==second['id']
 with store.connect() as c:assert tuple(c.execute('select credits,used,reserved from accounts').fetchone())==(100,0,5)
 assert not provider.method_calls
 assert len(jobs.allowed_voices())==5
