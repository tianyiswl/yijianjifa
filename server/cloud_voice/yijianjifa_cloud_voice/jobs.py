"""Single-node durable voice reservations and idempotent generation jobs.

Provider I/O never occurs inside a database transaction. Legacy jobs settle only
after a completed private audio object is fsynced. Direct jobs settle from a
trusted supplier query and retain only task metadata, never audio or signed URLs.
Unknown requests are queried, never replayed automatically, and stop holding
credit after 30 minutes.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
import uuid
from contextlib import closing
from pathlib import Path

from .errors import CloudError
from .provider import MAX_AUDIO_BYTES, ProviderAudioGone, ProviderFailure, ProviderUnknown, inspect_audio

COUNT_VERSION = 'unicode-codepoint-v1'


class JobService:
    def __init__(self, store, audio_dir, provider, voices, *, lease_seconds=900,
                 unknown_seconds=1800, max_batch_items=2000, max_batch_words=200000,
                 max_concurrent=2, clock=None):
        if not 1 <= lease_seconds <= 1800 or not 1 <= unknown_seconds <= 1800 or not 1 <= max_concurrent <= 32:
            raise ValueError('配音作业时限或并发上限不正确。')
        self.store, self.audio_dir, self.provider = store, Path(audio_dir), provider
        self.clock = clock or getattr(store, 'clock', time.time)
        self.lease_seconds, self.unknown_seconds = lease_seconds, unknown_seconds
        self.max_batch_items, self.max_batch_words, self.max_concurrent = max_batch_items, max_batch_words, max_concurrent
        self._voices = {}
        for voice in voices:
            row = {'id':voice,'name':voice} if isinstance(voice,str) else dict(voice)
            voice_id = row.get('id', row.get('voice'))
            if not isinstance(voice_id,str) or not voice_id.startswith('doubao:'):
                raise ValueError('经营方允许音色列表不正确。')
            row['id'] = voice_id
            row.setdefault('name',voice_id)
            self._voices[voice_id] = row
        self.audio_dir.mkdir(parents=True,exist_ok=True,mode=0o700)
        with self.store.transaction() as connection:
            connection.executescript('''
                CREATE TABLE IF NOT EXISTS voice_batches (
                    id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id),
                    idempotency_key TEXT NOT NULL, payload_hash TEXT NOT NULL,
                    state TEXT NOT NULL, lease_until REAL NOT NULL, created_at REAL NOT NULL,
                    UNIQUE(account_id,idempotency_key));
                CREATE TABLE IF NOT EXISTS voice_jobs (
                    id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id),
                    content_hash TEXT NOT NULL, text TEXT NOT NULL, voice TEXT NOT NULL,
                    speed REAL NOT NULL, purpose TEXT NOT NULL, words INTEGER NOT NULL,
                    state TEXT NOT NULL, reserved_words INTEGER NOT NULL DEFAULT 0,
                    attempt INTEGER NOT NULL DEFAULT 1, request_id TEXT NOT NULL,
                    started_at REAL, unknown_until REAL, duration_ms INTEGER,
                    error_code TEXT, created_at REAL NOT NULL,
                    UNIQUE(account_id,content_hash));
                CREATE TABLE IF NOT EXISTS voice_batch_items (
                    batch_id TEXT NOT NULL REFERENCES voice_batches(id), item_key TEXT NOT NULL,
                    job_id TEXT NOT NULL REFERENCES voice_jobs(id),
                    PRIMARY KEY(batch_id,item_key));
                CREATE INDEX IF NOT EXISTS voice_batch_jobs ON voice_batch_items(job_id);
                CREATE TABLE IF NOT EXISTS voice_supplier_budget (
                    day TEXT PRIMARY KEY, words INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS voice_job_attempts (
                    job_id TEXT NOT NULL, attempt INTEGER NOT NULL, snapshot TEXT NOT NULL,
                    archived_at REAL NOT NULL, PRIMARY KEY(job_id,attempt));
                CREATE TABLE IF NOT EXISTS voice_retry_actions (
                    account_id TEXT NOT NULL REFERENCES accounts(id),
                    idempotency_key TEXT NOT NULL, job_id TEXT NOT NULL REFERENCES voice_jobs(id),
                    attempt INTEGER NOT NULL, created_at REAL NOT NULL,
                    PRIMARY KEY(account_id,idempotency_key));
            ''')
            columns={row['name'] for row in connection.execute('PRAGMA table_info(voice_jobs)')}
            if 'delivery_mode' not in columns:
                connection.execute("ALTER TABLE voice_jobs ADD COLUMN delivery_mode TEXT NOT NULL DEFAULT 'legacy'")
            if 'provider_task_id' not in columns:
                connection.execute('ALTER TABLE voice_jobs ADD COLUMN provider_task_id TEXT')
        self._recover()

    def allowed_voices(self):
        return [dict(value) for value in self._voices.values()]

    def _normalize(self, payload):
        if not isinstance(payload,dict):
            raise CloudError('invalid_request','配音批次资料不正确。')
        key = payload.get('idempotency_key')
        if not isinstance(key,str) or not 1 <= len(key) <= 128 or any(ord(char)<32 for char in key):
            raise CloudError('invalid_request','配音批次编号不正确。')
        raw_items = payload.get('items')
        if not isinstance(raw_items,list) or not 1 <= len(raw_items) <= self.max_batch_items:
            raise CloudError('invalid_request',f'每批需有 1—{self.max_batch_items} 段配音。')
        items, keys, total = [], set(), 0
        for item in raw_items:
            if not isinstance(item,dict):
                raise CloudError('invalid_request','配音分段资料不正确。')
            segment_key, text, voice, speed = item.get('key'), item.get('text'), item.get('voice'), item.get('speed',1.0)
            if not isinstance(segment_key,str) or not re.fullmatch(r'[0-9a-f]{32}',segment_key) or segment_key in keys:
                raise CloudError('invalid_request','配音分段编号不正确或重复。')
            if not isinstance(text,str) or not text.strip() or len(text)>4000 or any(0xD800 <= ord(char) <= 0xDFFF for char in text):
                raise CloudError('invalid_request','每段配音需为 1—4000 字，且不能包含无效文字。')
            # The caller freezes actual spoken segments. Preserve its payload
            # exactly so the displayed estimate, provider input, and debit agree.
            if not isinstance(voice,str) or voice not in self._voices:
                raise CloudError('voice_unavailable','此声音暂不可用，请重新选择。')
            if not isinstance(speed,(int,float)) or isinstance(speed,bool) or not math.isfinite(speed) or not .5 <= speed <= 2:
                raise CloudError('invalid_request','配音语速支持 0.5—2 倍。')
            purpose=item.get('purpose','制作配音')
            if not isinstance(purpose,str) or not 1 <= len(purpose) <= 200 or any(ord(char)<32 for char in purpose):
                raise CloudError('invalid_request','配音用途需为 1—200 字的标题。')
            keys.add(segment_key)
            total+=len(text)
            if total>self.max_batch_words:
                raise CloudError('invalid_request',f'每批配音最多 {self.max_batch_words} 字。')
            items.append({'key':segment_key,'text':text,'voice':voice,'speed':float(speed),'purpose':purpose})
        digest=hashlib.sha256(json.dumps(items,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        return key,items,digest

    def _account(self,connection,account_id):
        row=connection.execute('SELECT * FROM accounts WHERE id=?',(account_id,)).fetchone()
        if row is None:
            raise CloudError('unauthorized','请重新登录配音账号。',401)
        return row

    def _owned_batch(self,connection,account_id,batch_id):
        row=connection.execute('SELECT * FROM voice_batches WHERE id=? AND account_id=?',(batch_id,account_id)).fetchone()
        if row is None:
            raise CloudError('not_found','找不到此配音批次。',404)
        return row

    def _owned_job(self,connection,account_id,job_id):
        row=connection.execute('SELECT * FROM voice_jobs WHERE id=? AND account_id=?',(job_id,account_id)).fetchone()
        if row is None:
            raise CloudError('not_found','找不到此配音作业。',404)
        return row

    def _release_job(self,connection,row):
        if row['reserved_words']:
            connection.execute('UPDATE accounts SET reserved=reserved-? WHERE id=?',(row['reserved_words'],row['account_id']))
            connection.execute('UPDATE voice_jobs SET reserved_words=0 WHERE id=?',(row['id'],))

    def _sweep(self,connection,now):
        connection.execute("UPDATE voice_batches SET state='expired' WHERE state='active' AND lease_until<=?",(now,))
        # A request can be late even while its Python call is still executing.
        connection.execute("UPDATE voice_jobs SET state='unknown',error_code='result_unknown' WHERE state='running' AND unknown_until<=?",(now,))
        rows=connection.execute("SELECT * FROM voice_jobs WHERE reserved_words>0 AND ((state='unknown' AND unknown_until<=?) OR state='pending')",(now,)).fetchall()
        for row in rows:
            if row['state']=='pending':
                live=connection.execute("SELECT 1 FROM voice_batch_items i JOIN voice_batches b ON b.id=i.batch_id WHERE i.job_id=? AND b.state='active' AND b.lease_until>? LIMIT 1",(row['id'],now)).fetchone()
                if live:
                    continue
            self._release_job(connection,row)
            if row['delivery_mode']=='direct' and row['state']=='unknown':
                connection.execute("UPDATE voice_jobs SET error_code='reservation_expired' WHERE id=?",(row['id'],))

    def _activate(self,connection,batch,now):
        rows=connection.execute("SELECT DISTINCT j.* FROM voice_jobs j JOIN voice_batch_items i ON i.job_id=j.id WHERE i.batch_id=?",(batch['id'],)).fetchall()
        amount=sum(row['words'] for row in rows if row['state']=='pending' and not row['reserved_words'])
        account=self._account(connection,batch['account_id'])
        available=account['credits']-account['used']-account['reserved']
        if amount>available:
            raise CloudError('insufficient_credit',f'本批需要 {amount} 字，剩余 {available} 字，请先充值。',409,{'required_words':amount,'available_words':available})
        if amount:
            connection.execute('UPDATE accounts SET reserved=reserved+? WHERE id=?',(amount,batch['account_id']))
            for row in rows:
                if row['state']=='pending' and not row['reserved_words']:
                    connection.execute('UPDATE voice_jobs SET reserved_words=words WHERE id=?',(row['id'],))
        connection.execute("UPDATE voice_batches SET state='active',lease_until=? WHERE id=?",(now+self.lease_seconds,batch['id']))
        return amount

    def _job_dict(self,row):
        errors={'result_unknown':'原配音结果待确认，请稍后查询；不会自动重复生成。','generation_failed':'配音服务未生成有效音频，未扣除字数。','invalid_audio':'配音服务未生成有效音频，未扣除字数。',
                'audio_link_expired':'原配音下载地址已过期，请重新核对原任务地址；不会重复生成。',
                'reservation_expired':'原配音超过核对时限，预留字数已释放；不会补扣或重复生成。'}
        code=row['error_code']
        return {'id':row['id'],'state':row['state'],'words':row['words'],'voice':row['voice'],'speed':row['speed'],'duration_ms':row['duration_ms'],'attempt':row['attempt'],'reserved_words':row['reserved_words'],'delivery_mode':row['delivery_mode'],'error':{'code':code,'message':errors.get(code,'配音暂未完成，请查询原结果。')} if code else None}

    def _batch_dict(self,connection,account_id,batch_id,new_reserved=0):
        batch=self._owned_batch(connection,account_id,batch_id)
        rows=connection.execute('SELECT i.item_key,j.* FROM voice_batch_items i JOIN voice_jobs j ON j.id=i.job_id WHERE i.batch_id=? ORDER BY i.rowid',(batch_id,)).fetchall()
        unique={row['id']:row for row in rows}
        return {'id':batch_id,'state':batch['state'],'lease_until':batch['lease_until'],'count_version':COUNT_VERSION,'total_words':sum(row['words'] for row in unique.values()),'required_words':sum(row['words'] for row in unique.values() if row['state']!='succeeded'),'new_reserved_words':new_reserved,'reserved_words':sum(row['reserved_words'] for row in unique.values()),'jobs':[dict(self._job_dict(row),key=row['item_key']) for row in rows]}

    def prepare(self,account_id,payload):
        key,items,digest=self._normalize(payload)
        now=self.clock()
        with self.store.transaction() as connection:
            self._account(connection,account_id)
            self._sweep(connection,now)
            batch=connection.execute('SELECT * FROM voice_batches WHERE account_id=? AND idempotency_key=?',(account_id,key)).fetchone()
            if batch:
                if batch['payload_hash']!=digest:
                    raise CloudError('idempotency_conflict','原配音批次内容已冻结，不能更改。',409)
                amount=self._activate(connection,batch,now)
                return self._batch_dict(connection,account_id,batch['id'],amount)
            batch_id=uuid.uuid4().hex
            connection.execute('INSERT INTO voice_batches VALUES (?,?,?,?,?,?,?)',(batch_id,account_id,key,digest,'active',now+self.lease_seconds,now))
            for item in items:
                content=hashlib.sha256(json.dumps([getattr(self.provider,'version','v1'),COUNT_VERSION,item['text'],item['voice'],item['speed']],ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
                job=connection.execute('SELECT id FROM voice_jobs WHERE account_id=? AND content_hash=?',(account_id,content)).fetchone()
                if job:
                    job_id=job['id']
                else:
                    job_id=uuid.uuid4().hex
                    mode='direct' if getattr(self.provider,'delivery_mode',None)=='direct' else 'legacy'
                    connection.execute('INSERT INTO voice_jobs (id,account_id,content_hash,text,voice,speed,purpose,words,state,reserved_words,attempt,request_id,created_at,delivery_mode) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(job_id,account_id,content,item['text'],item['voice'],item['speed'],item['purpose'],len(item['text']),'pending',0,1,uuid.uuid4().hex,now,mode))
                connection.execute('INSERT INTO voice_batch_items VALUES (?,?,?)',(batch_id,item['key'],job_id))
            batch=self._owned_batch(connection,account_id,batch_id)
            amount=self._activate(connection,batch,now)
            return self._batch_dict(connection,account_id,batch_id,amount)

    def batch(self,account_id,batch_id):
        with self.store.transaction() as connection:
            self._sweep(connection,self.clock())
            return self._batch_dict(connection,account_id,batch_id)

    def lease(self,account_id,batch_id):
        with self.store.transaction() as connection:
            now=self.clock()
            self._sweep(connection,now)
            batch=self._owned_batch(connection,account_id,batch_id)
            amount=self._activate(connection,batch,now)
            return self._batch_dict(connection,account_id,batch_id,amount)

    def release(self,account_id,batch_id):
        with self.store.transaction() as connection:
            self._owned_batch(connection,account_id,batch_id)
            connection.execute("UPDATE voice_batches SET state='released' WHERE id=?",(batch_id,))
            self._sweep(connection,self.clock())
            return self._batch_dict(connection,account_id,batch_id)

    def _paths(self,row):
        account=hashlib.sha256(row['account_id'].encode()).hexdigest()
        folder=self.audio_dir/account/row['id']
        stem=f"{row['attempt']}-{row['request_id']}"
        return folder/(stem+'.audio'),folder/(stem+'.json')

    def _completion(self,row):
        audio_path,marker_path=self._paths(row)
        try:
            marker=json.loads(marker_path.read_text(encoding='utf-8'))
            if marker['request_id']!=row['request_id'] or marker['attempt']!=row['attempt']:
                return None
            data=audio_path.read_bytes()
            duration,mime=inspect_audio(data)
            if marker['sha256']!=hashlib.sha256(data).hexdigest() or marker['duration_ms']!=duration or marker['mime']!=mime:
                return None
            return data,duration,mime
        except (OSError,ValueError,KeyError,TypeError,ProviderFailure):
            return None

    def _save_completion(self,row,data,duration,mime):
        audio_path,marker_path=self._paths(row)
        audio_path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        marker={'request_id':row['request_id'],'attempt':row['attempt'],'sha256':hashlib.sha256(data).hexdigest(),'duration_ms':duration,'mime':mime}
        for path,content in ((audio_path,data),(marker_path,json.dumps(marker,separators=(',',':')).encode())):
            temporary=path.with_name(path.name+'.partial-'+uuid.uuid4().hex)
            try:
                descriptor=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
                with os.fdopen(descriptor,'wb') as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary,path)
            finally:
                temporary.unlink(missing_ok=True)
        descriptor=os.open(audio_path.parent,os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _settle(self,job_id,request_id,duration):
        with self.store.transaction() as connection:
            now=self.clock()
            self._sweep(connection,now)
            row=connection.execute('SELECT * FROM voice_jobs WHERE id=?',(job_id,)).fetchone()
            if not row or row['request_id']!=request_id or row['state']=='succeeded':
                return
            if row['state'] not in ('running','unknown') or row['reserved_words']!=row['words']:
                return
            account=self._account(connection,row['account_id'])
            event_key=f"voice:{row['id']}:{row['attempt']}"
            existing=connection.execute('SELECT 1 FROM ledger WHERE event_key=?',(event_key,)).fetchone()
            if not existing:
                if account['reserved']<row['words']:
                    raise RuntimeError('voice_reservation_invariant')
                connection.execute('UPDATE accounts SET used=used+?,reserved=reserved-? WHERE id=?',(row['words'],row['words'],row['account_id']))
                available=account['credits']-account['used']-account['reserved']
                connection.execute('INSERT INTO ledger (id,account_id,event_key,kind,delta,available_after,purpose,created_at) VALUES (?,?,?,?,?,?,?,?)',(uuid.uuid4().hex,row['account_id'],event_key,'generation',-row['words'],available,row['purpose'],now))
            connection.execute("UPDATE voice_jobs SET state='succeeded',reserved_words=0,duration_ms=?,error_code=NULL WHERE id=?",(duration,job_id))

    def _recover(self):
        with closing(self.store.connect()) as connection:
            rows=connection.execute("SELECT * FROM voice_jobs WHERE delivery_mode='legacy' AND state IN ('running','unknown')").fetchall()
        for row in rows:
            completion=self._completion(row)
            if completion:
                self._settle(row['id'],row['request_id'],completion[1])
        with self.store.transaction() as connection:
            connection.execute("UPDATE voice_jobs SET state='unknown',error_code='result_unknown' WHERE state='running'")
            self._sweep(connection,self.clock())

    def _mark_failed(self,row,unknown=False,code=None):
        with self.store.transaction() as connection:
            self._sweep(connection,self.clock())
            current=connection.execute('SELECT * FROM voice_jobs WHERE id=?',(row['id'],)).fetchone()
            if not current or current['request_id']!=row['request_id'] or current['state']=='succeeded':
                return
            if unknown:
                if (current['delivery_mode']=='direct' and not current['reserved_words']
                        and current['unknown_until'] is not None and current['unknown_until']<=self.clock()):
                    code='reservation_expired'
                connection.execute("UPDATE voice_jobs SET state='unknown',error_code=? WHERE id=?",(code or 'result_unknown',row['id']))
            else:
                self._release_job(connection,current)
                connection.execute("UPDATE voice_jobs SET state='failed',error_code=? WHERE id=?",(code or 'generation_failed',row['id']))

    def start(self,account_id,batch_id,key):
        with self.store.transaction() as connection:
            now=self.clock()
            self._sweep(connection,now)
            batch=self._owned_batch(connection,account_id,batch_id)
            row=connection.execute('SELECT j.* FROM voice_jobs j JOIN voice_batch_items i ON i.job_id=j.id WHERE i.batch_id=? AND i.item_key=?',(batch_id,key)).fetchone()
            if row is None:
                raise CloudError('not_found','找不到此配音分段。',404)
            if row['state']!='pending':
                return self._job_dict(row)
            if batch['state']!='active' or batch['lease_until']<=now or row['reserved_words']!=row['words']:
                raise CloudError('batch_released','配音预留已释放，请继续原批次重新检查。',409)
            active=connection.execute("SELECT COUNT(*) FROM voice_jobs WHERE state='running' OR (delivery_mode='direct' AND state='unknown' AND reserved_words>0)").fetchone()[0]
            if active>=self.max_concurrent:
                raise CloudError('service_busy','云配音正在处理其他任务，请稍后继续原批次。',429)
            limit=getattr(self.provider,'max_daily_words',None)
            if not isinstance(limit,int) or isinstance(limit,bool) or limit<=0:
                raise CloudError('service_not_configured','云配音服务暂未开通。',503)
            day=time.strftime('%Y-%m-%d',time.gmtime(now))
            budget=connection.execute('SELECT words FROM voice_supplier_budget WHERE day=?',(day,)).fetchone()
            used=budget['words'] if budget else 0
            if used+row['words']>limit:
                raise CloudError('supplier_budget_exceeded','今日云配音服务额度已用完，请稍后继续；未扣除本段字数。',503)
            connection.execute('INSERT INTO voice_supplier_budget(day,words) VALUES (?,?) ON CONFLICT(day) DO UPDATE SET words=words+excluded.words',(day,row['words']))
            mode=row['delivery_mode']
            if mode=='legacy' and getattr(self.provider,'delivery_mode',None)=='direct':
                # Pending has not been submitted with this request identity. Old
                # running/unknown jobs returned above and can never be upgraded.
                mode='direct'
            if mode=='direct' and getattr(self.provider,'delivery_mode',None)!='direct':
                raise CloudError('service_not_configured','原配音需要供应商直下载服务，请联系支持；不会重新生成。',503)
            connection.execute("UPDATE voice_jobs SET state='running',started_at=?,unknown_until=?,error_code=NULL,delivery_mode=?,provider_task_id=? WHERE id=?",(now,now+self.unknown_seconds,mode,row['request_id'] if mode=='direct' else None,row['id']))
            row=connection.execute('SELECT * FROM voice_jobs WHERE id=?',(row['id'],)).fetchone()
        if row['delivery_mode']=='direct':
            return self._submit_direct(row)
        try:
            audio,_reported_duration=self.provider.synthesize(row['text'],row['voice'],row['speed'],row['request_id'])
            duration,mime=inspect_audio(audio)
        except ProviderFailure as error:
            self._mark_failed(row,code='invalid_audio' if str(error)=='invalid_audio' else 'generation_failed')
            return self.get(account_id,row['id'])
        except Exception:
            # Includes malformed/transport failures from replaceable adapters; no raw details escape.
            self._mark_failed(row,unknown=True)
            return self.get(account_id,row['id'])
        # A late result must not revive a released reservation, nor overwrite a retry.
        with self.store.transaction() as connection:
            self._sweep(connection,self.clock())
            current=self._owned_job(connection,account_id,row['id'])
            if current['request_id']!=row['request_id'] or current['reserved_words']!=row['words']:
                return self._job_dict(current)
        try:
            self._save_completion(row,audio,duration,mime)
        except OSError:
            self._mark_failed(row,unknown=True)
            return self.get(account_id,row['id'])
        self._settle(row['id'],row['request_id'],duration)
        return self.get(account_id,row['id'])

    def _recover_saved(self,account_id=None,job_id=None):
        with closing(self.store.connect()) as connection:
            query="SELECT * FROM voice_jobs WHERE delivery_mode='legacy' AND state IN ('running','unknown')"
            arguments=[]
            if account_id is not None:
                query+=' AND account_id=?'
                arguments.append(account_id)
            if job_id is not None:
                query+=' AND id=?'
                arguments.append(job_id)
            rows=connection.execute(query,arguments).fetchall()
        for row in rows:
            completion=self._completion(row)
            if completion:
                self._settle(row['id'],row['request_id'],completion[1])

    def maintenance(self):
        """Run before balance reads and from an optional single-node periodic tick."""
        self._recover_saved()
        with self.store.transaction() as connection:
            self._sweep(connection,self.clock())

    def _direct_result(self,row,result):
        """Treat adapters as a trust boundary; never charge malformed results."""
        if (not isinstance(result,dict) or result.get('task_id')!=row['provider_task_id']
                or result.get('state') not in ('running','succeeded','failed','expired')):
            raise ProviderUnknown('invalid_result')
        duration=result.get('duration_ms')
        if duration is not None and (not isinstance(duration,int) or isinstance(duration,bool) or duration<=0):
            raise ProviderUnknown('invalid_result')
        if result['state']=='succeeded':
            expires=result.get('expires_at')
            if (not isinstance(expires,(int,float)) or isinstance(expires,bool)
                    or not math.isfinite(expires) or expires<=self.clock()):
                raise ProviderUnknown('invalid_download')
            from .provider_async import validate_supplier_url
            validate_supplier_url(result.get('url'))
        return result

    def _submit_direct(self,row):
        try:
            result=self.provider.submit(row['text'],row['voice'],row['speed'],row['provider_task_id'])
            if (not isinstance(result,dict) or result.get('task_id')!=row['provider_task_id']
                    or result.get('state')!='running'):
                raise ProviderUnknown('invalid_submit_result')
        except ProviderFailure:
            self._mark_failed(row,code='generation_failed')
        except Exception:
            # The task identity was committed before submit. A timeout cannot
            # authorize another call to submit, only a query of that same ID.
            self._mark_failed(row,unknown=True)
        return self.get(row['account_id'],row['id'])

    def _refresh_direct(self,account_id,job_id):
        with self.store.transaction() as connection:
            self._sweep(connection,self.clock())
            row=self._owned_job(connection,account_id,job_id)
            if (row['delivery_mode']!='direct' or row['state'] not in ('running','unknown')
                    or not row['provider_task_id'] or row['reserved_words']!=row['words']
                    or getattr(self.provider,'delivery_mode',None)!='direct'):
                return
        try:
            result=self._direct_result(row,self.provider.query(row['provider_task_id']))
        except ProviderFailure:
            self._mark_failed(row,code='generation_failed')
            return
        except Exception:
            self._mark_failed(row,unknown=True)
            return
        if result['state']=='succeeded':
            # Only a validated supplier response can settle. No local-client
            # download receipt, body length or claimed word count is accepted.
            self._settle(row['id'],row['request_id'],result.get('duration_ms'))
        elif result['state']=='failed':
            self._mark_failed(row,code='generation_failed')
        elif result['state']=='expired':
            # This is an expired signed URL, not proof that the supplier lost
            # the generated audio. Keep querying the same task and reservation.
            self._mark_failed(row,unknown=True,code='audio_link_expired')
        else:
            with self.store.transaction() as connection:
                self._sweep(connection,self.clock())
                connection.execute("UPDATE voice_jobs SET state='running',error_code=NULL WHERE id=? AND request_id=? AND state IN ('running','unknown') AND reserved_words=words",
                                   (row['id'],row['request_id']))

    def download(self,account_id,job_id):
        with self.store.transaction() as connection:
            self._sweep(connection,self.clock())
            row=self._owned_job(connection,account_id,job_id)
            if row['delivery_mode']!='direct':
                raise CloudError('legacy_audio_required','此原配音请使用原音频下载方式。',409)
            if row['state']!='succeeded':
                raise CloudError('audio_not_ready','原配音尚未完成，请查询原结果。',409)
            if not row['provider_task_id'] or getattr(self.provider,'delivery_mode',None)!='direct':
                raise CloudError('audio_unavailable','原配音下载暂不可用，请联系支持；不会重新生成或再次扣除字数。',503)
        try:
            result=self._direct_result(row,self.provider.query(row['provider_task_id']))
        except ProviderAudioGone:
            raise CloudError('audio_gone','原音频已无法恢复，将自动重新生成并按成功字数扣费。',410) from None
        except Exception:
            raise CloudError('audio_unavailable','原配音下载暂不可用，请稍后重取原地址；不会重新生成或再次扣除字数。',503) from None
        if result['state']=='expired':
            raise CloudError('audio_link_expired','原配音下载地址已过期，请重新取得原地址；不会重新生成或再次扣除字数。',410)
        if result['state']!='succeeded':
            raise CloudError('audio_unavailable','原配音下载暂不可用，请稍后重取原地址；不会重新生成或再次扣除字数。',503)
        return {'kind':'supplier_url','job_id':row['id'],'attempt':row['attempt'],'url':result['url'],
                'expires_at':result['expires_at'],'max_bytes':MAX_AUDIO_BYTES}

    def get(self,account_id,job_id):
        # A lost response or a temporary settlement error is resolved by the
        # original query, without restarting the service or contacting upstream.
        self._recover_saved(account_id,job_id)
        self._refresh_direct(account_id,job_id)
        with self.store.transaction() as connection:
            self._sweep(connection,self.clock())
            return self._job_dict(self._owned_job(connection,account_id,job_id))

    def audio(self,account_id,job_id):
        with self.store.transaction() as connection:
            self._sweep(connection,self.clock())
            row=self._owned_job(connection,account_id,job_id)
            if row['delivery_mode']=='direct':
                raise CloudError('direct_download_required','此配音请从供应商原任务地址直接下载。',409)
            if row['state']!='succeeded':
                raise CloudError('audio_not_ready','原配音尚未完成，请查询原结果。',409)
        completion=self._completion(row)
        if not completion:
            # Never silently regenerate an already charged object.
            try:
                self._paths(row)[0].stat()
            except FileNotFoundError:
                raise CloudError('audio_gone','原音频已无法恢复，将自动重新生成并按成功字数扣费。',410) from None
            except OSError:
                pass
            raise CloudError('audio_unavailable','原音频暂时无法读取，请稍后继续，不会重复扣费。',503)
        return completion[0],completion[2]

    def restore(self,account_id,job_id,payload):
        expected=payload.get('expected_attempt')
        key=payload.get('idempotency_key')
        if type(expected) is not int or expected<1 or not isinstance(key,str) or not 1<=len(key)<=128:
            raise CloudError('invalid_request','音频恢复请求无效。')
        with self.store.transaction() as connection:
            row=self._owned_job(connection,account_id,job_id)
            if row['attempt']>expected:
                return self._job_dict(row)
            if row['attempt']!=expected or row['state']!='succeeded':
                raise CloudError('retry_not_ready','原任务尚未确认成功，不能自动重新生成。',409)
        # Only the server's own retrieval can authorize a replacement. Never
        # trust a desktop assertion that an upstream object disappeared.
        try:
            if row['delivery_mode']=='direct':
                self.download(account_id,job_id)
            else:
                self.audio(account_id,job_id)
        except CloudError as error:
            if error.code!='audio_gone':
                with self.store.transaction() as connection:
                    current=self._owned_job(connection,account_id,job_id)
                    if current['attempt']>expected:
                        return self._job_dict(current)
                raise
        else:
            raise CloudError('audio_still_available','原音频仍可下载，请重新获取，不重复扣费。',409)
        with self.store.transaction() as connection:
            current=self._owned_job(connection,account_id,job_id)
            if current['attempt']>expected:
                return self._job_dict(current)
            if current['attempt']!=expected or current['state']!='succeeded':
                raise CloudError('retry_not_ready','音频恢复状态已变化，请查询原任务。',409)
            account=self._account(connection,account_id)
            available=account['credits']-account['used']-account['reserved']
            if available<current['words']:
                raise CloudError('insufficient_credit','重新生成所需字数不足，请先充值后继续。',409)
            now=self.clock()
            connection.execute('INSERT INTO voice_job_attempts VALUES (?,?,?,?)',
                               (job_id,expected,json.dumps(dict(current),ensure_ascii=False),now))
            connection.execute("UPDATE voice_jobs SET state='pending',attempt=attempt+1,request_id=?,provider_task_id=NULL,started_at=NULL,unknown_until=NULL,duration_ms=NULL,error_code=NULL WHERE id=?",(uuid.uuid4().hex,job_id))
            # Reuse the original batch identity; reserve through normal activation.
            batches=connection.execute('SELECT b.* FROM voice_batches b JOIN voice_batch_items i ON i.batch_id=b.id WHERE i.job_id=? ORDER BY b.created_at DESC LIMIT 1',(job_id,)).fetchall()
            if not batches:
                raise CloudError('not_found','找不到原配音批次。',404)
            self._activate(connection,batches[0],now)
            return self._job_dict(self._owned_job(connection,account_id,job_id))

    def retry(self,account_id,job_id,payload=None):
        retry_key=None
        if payload is not None:
            retry_key=payload.get('idempotency_key') if isinstance(payload,dict) else None
            if not isinstance(retry_key,str) or not 1 <= len(retry_key) <= 128 or any(ord(char)<32 for char in retry_key):
                raise CloudError('invalid_request','继续配音的操作编号不正确。')
        with self.store.transaction() as connection:
            now=self.clock()
            self._sweep(connection,now)
            row=self._owned_job(connection,account_id,job_id)
            if retry_key:
                previous=connection.execute('SELECT job_id FROM voice_retry_actions WHERE account_id=? AND idempotency_key=?',(account_id,retry_key)).fetchone()
                if previous:
                    if previous['job_id']!=job_id:
                        raise CloudError('idempotency_conflict','此继续操作已用于另一段配音。',409)
                    return self._job_dict(row)
            if row['state']=='succeeded':
                return self._job_dict(row)
            if (row['delivery_mode']=='legacy' and row['state']=='unknown'
                    and getattr(self.provider,'delivery_mode',None)=='direct'):
                raise CloudError('direct_retry_forbidden','旧配音提交结果仍待核对，不能切换为新方式重新提交。',409)
            if row['delivery_mode']=='direct' and (row['state']=='unknown' or row['error_code']=='audio_expired'):
                raise CloudError('direct_retry_forbidden','原配音只能核对供应商原任务，不能重新提交或再次扣除字数。',409)
            if row['state'] not in ('failed','unknown') or row['reserved_words']:
                raise CloudError('retry_not_ready','原配音仍在等待结果，请稍后查询，不要重复生成。',409)
            connection.execute("UPDATE voice_jobs SET state='pending',attempt=attempt+1,request_id=?,provider_task_id=NULL,started_at=NULL,unknown_until=NULL,duration_ms=NULL,error_code=NULL WHERE id=?",(uuid.uuid4().hex,job_id))
            active=connection.execute("SELECT b.* FROM voice_batches b JOIN voice_batch_items i ON i.batch_id=b.id WHERE i.job_id=? AND b.state='active' AND b.lease_until>? LIMIT 1",(job_id,now)).fetchone()
            if active:
                self._activate(connection,active,now)
            if retry_key:
                connection.execute('INSERT INTO voice_retry_actions VALUES (?,?,?,?,?)',(account_id,retry_key,job_id,row['attempt']+1,now))
            return self._job_dict(self._owned_job(connection,account_id,job_id))
