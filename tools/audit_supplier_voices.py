"""Explicit, bounded supplier probes; no customer DB, credits or stored audio.

Private deployment use only. Each voice has one durable task identity. An
uncertain submission is queried, never submitted a second time. Reports contain
no credentials, supplier response bodies or signed download URLs.
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import threading
import time
import uuid


def validate_plan(rows, *, max_characters):
    if not isinstance(rows,list) or not 1<=len(rows)<=500:
        raise ValueError('Invalid voice audit size')
    ids=set(); characters=0
    for row in rows:
        voice, text=row.get('id'),row.get('test_text')
        if not isinstance(voice,str) or not voice.startswith('doubao:') or voice in ids:
            raise ValueError('Invalid or duplicate voice')
        if not isinstance(text,str) or not 1<=len(text)<=12:
            raise ValueError('Invalid short probe text')
        ids.add(voice);characters+=len(text)
    if characters>max_characters:
        raise ValueError('Probe character budget exceeded')
    return characters


def audit_voice(row, provider, persist, *, max_wait=40, clock=time.monotonic, sleep=time.sleep):
    if row['state'] in {'verified','failed'}:
        return row
    deadline=clock()+max_wait
    if row['state']=='pending':
        row['state']='submitting';row['submitted_at']=time.time();persist()
        try:
            provider.submit(row['test_text'],row['id'],1.0,row['task_id'])
        except Exception as error:
            # Only the class is safe to record; exception text can include URLs.
            row['last_error']=type(error).__name__
        row['state']='querying';persist()
    while True:
        try:
            result=provider.query(row['task_id'])
            if result.get('state')=='succeeded' and result.get('url'):
                row.update(state='verified',duration_ms=result.get('duration_ms'),verified_at=time.time())
                row.pop('last_error',None);persist();return row
            if result.get('state')=='failed':
                row.update(state='failed',last_error='supplier_task_failed');persist();return row
        except Exception as error:
            row['last_error']=type(error).__name__
        if clock()>=deadline:
            row['state']='unconfirmed';persist();return row
        sleep(min(2,max(0,deadline-clock())))


def private_json(path, value):
    path=Path(path)
    with tempfile.NamedTemporaryFile(mode='w',dir=path.parent,prefix='.audit-',delete=False,encoding='utf-8') as stream:
        tmp=Path(stream.name);os.chmod(tmp,0o600)
        try:
            json.dump(value,stream,ensure_ascii=False,indent=2)
            stream.flush();os.fsync(stream.fileno())
        except BaseException:
            tmp.unlink(missing_ok=True);raise
    tmp.replace(path)


def main():
    import fcntl
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan',type=Path,required=True)
    parser.add_argument('--report',type=Path,required=True)
    parser.add_argument('--execute',action='store_true')
    parser.add_argument('--workers',type=int,choices=(1,2),default=2)
    parser.add_argument('--max-characters',type=int,default=5000)
    parser.add_argument('--max-wait',type=int,default=40)
    args=parser.parse_args()
    rows=json.loads(args.plan.read_text(encoding='utf-8'))['items']
    characters=validate_plan(rows,max_characters=args.max_characters)
    if not args.execute:
        print(json.dumps({'voices':len(rows),'maximum_characters':characters,'execution':False}));return
    if not 0<args.max_characters<=5000 or not 0<=args.max_wait<=120:
        raise ValueError('Invalid execution budget')
    args.report.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    lock=(args.report.parent/'audit.lock').open('w')
    fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    digest=hashlib.sha256(args.plan.read_bytes()).hexdigest()
    if args.report.exists():
        report=json.loads(args.report.read_text(encoding='utf-8'))
        if report['plan_sha256']!=digest:raise ValueError('Audit plan changed; no submissions allowed')
    else:
        report={'plan_sha256':digest,'maximum_characters':characters,'started_at':time.time(),
                'items':[{**row,'task_id':uuid.uuid4().hex,'state':'pending'} for row in rows]}
        private_json(args.report,report)
    env={}
    for line in Path('/etc/yijianjifa-cloud-voice/service.env').read_text().splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            key,value=line.split('=',1);env[key]=shlex.split(value)[0] if value.strip() else ''
    sys.path.insert(0,str(Path('/opt/yijianjifa-cloud-voice/current').resolve()))
    from yijianjifa_cloud_voice.provider_async import AsyncVolcengineProvider
    def factory():
        key=env.get('YJJF_VOLC_API_KEY')
        if key:return AsyncVolcengineProvider(api_key=key,max_daily_words=args.max_characters,timeout=10)
        return AsyncVolcengineProvider(env.get('YJJF_VOLC_APP_ID'),env.get('YJJF_VOLC_ACCESS_KEY'),max_daily_words=args.max_characters,timeout=10)
    save_lock=threading.RLock()
    def persist():
        with save_lock:
            report['counts']=dict(Counter(row['state'] for row in report['items']))
            report['updated_at']=time.time();private_json(args.report,report)
    def probe(row):
        return audit_voice(row,factory(),persist,max_wait=args.max_wait)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        work=[pool.submit(probe,row) for row in report['items'] if row['state'] not in {'verified','failed'}]
        for index,future in enumerate(as_completed(work),1):
            future.result()
            if index%10==0 or index==len(work):
                print(json.dumps({'finished':index,'scheduled':len(work),'counts':report.get('counts',{})}),flush=True)
    report['finished_at']=time.time();persist()
    print('AUDIT_COMPLETE '+json.dumps(report['counts']),flush=True)


if __name__=='__main__':
    main()
