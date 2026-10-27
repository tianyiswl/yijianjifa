"""独立事务和执行者共享同一制作索引的持久领取边界。"""
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime,timezone
import importlib
import json
import threading

import pytest
from sqlalchemy import select

from yijianjifa.features.mixing.queue import MixQueue,MixTask
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting


def module():
    name='yijianjifa.features.mixing.serial_claim'
    assert importlib.util.find_spec(name) is not None,'逐条制作尚无持久索引领取保护'
    return importlib.import_module(name)


@contextmanager
def writing(engine):
    with engine.connect() as connection:
        connection.exec_driver_sql('BEGIN IMMEDIATE')
        try:
            with session_scope(connection) as session:yield session
            connection.commit()
        except BaseException:
            connection.rollback();raise


def task(engine):
    return MixQueue(engine,None).enqueue({'quantity':2,'copies':[{'id':'A'},{'id':'B'}],
        'daily_publish':{'execution_flow':'per_item_v1'}})


def test_committed_claim_blocks_second_executor_while_first_renders_outside_transaction(db_engine):
    api=module();batch=task(db_engine)
    a=api.SerialMixClaims(owner_id='executor-a');b=api.SerialMixClaims(owner_id='executor-b')
    rendered=[];outside=threading.Event();finish=threading.Event();errors=[]
    def first():
        try:
            with writing(db_engine) as session:claim=a.claim(session,batch.id,0,batch.snapshot)
            assert claim is not None
            rendered.append('render-and-tts-once');outside.set()
            assert finish.wait(5)
            with writing(db_engine) as session:
                assert a.verify(session,claim)
                assert a.finish(session,claim)
        except BaseException as failure:errors.append(failure);outside.set()
    thread=threading.Thread(target=first)
    thread.start()
    try:
        assert outside.wait(3)
        assert not errors
        with writing(db_engine) as session:
            assert b.claim(session,batch.id,0,batch.snapshot) is None
            assert a.claim(session,batch.id,0,batch.snapshot) is None
        assert rendered==['render-and-tts-once']
    finally:finish.set();thread.join(5)
    assert not thread.is_alive() and not errors
    with writing(db_engine) as session:assert b.claim(session,batch.id,0,batch.snapshot) is None


def test_simultaneous_independent_claims_have_exactly_one_committed_winner(db_engine):
    api=module();batch=task(db_engine);barrier=threading.Barrier(3);results=[];errors=[]
    def contend(owner):
        try:
            registry=api.SerialMixClaims(owner_id=owner)
            barrier.wait(timeout=3)
            with writing(db_engine) as session:claim=registry.claim(session,batch.id,0,batch.snapshot)
            results.append(claim)
        except BaseException as failure:errors.append(failure)
    threads=[threading.Thread(target=contend,args=(name,)) for name in ('a','b')]
    for thread in threads:thread.start()
    barrier.wait(timeout=3)
    for thread in threads:thread.join(5)
    assert not errors and all(not thread.is_alive() for thread in threads)
    assert len(results)==2 and sum(result is not None for result in results)==1


def test_owner_generation_token_index_and_snapshot_are_all_bound(db_engine):
    api=module();batch=task(db_engine);registry=api.SerialMixClaims(owner_id='bound-owner')
    with writing(db_engine) as session:claim=registry.claim(session,batch.id,0,batch.snapshot)
    with writing(db_engine) as session:
        assert registry.verify(session,claim)
        for forged in (replace(claim,owner_id='other'),replace(claim,index=1),replace(claim,generation=claim.generation+1),
                       replace(claim,generation=True),replace(claim,token='untrusted-token'),replace(claim,snapshot_digest='0'*64)):
            assert not registry.verify(session,forged)
            assert not registry.finish(session,forged)
        row=session.get(MixTask,batch.id);snapshot=json.loads(row.snapshot_json);snapshot['copies'][0]['id']='changed'
        row.snapshot_json=json.dumps(snapshot);session.flush()
        assert not registry.verify(session,claim)


@pytest.mark.parametrize('state',['waiting','running'])
def test_waiting_and_running_current_index_are_claimable_but_other_indices_are_not(db_engine,state):
    api=module();batch=task(db_engine);registry=api.SerialMixClaims()
    with writing(db_engine) as session:
        session.get(MixTask,batch.id).state=state
        assert registry.claim(session,batch.id,1,batch.snapshot) is None
        assert registry.claim(session,batch.id,0,batch.snapshot) is not None


@pytest.mark.parametrize('state',['paused','interrupted','cancelled','waiting_publish','completed'])
def test_non_execution_states_cannot_start_a_new_claim(db_engine,state):
    api=module();batch=task(db_engine);registry=api.SerialMixClaims()
    with writing(db_engine) as session:
        session.get(MixTask,batch.id).state=state
        assert registry.claim(session,batch.id,0,batch.snapshot) is None


def test_changed_snapshot_or_legacy_batch_cannot_be_claimed(db_engine):
    api=module();batch=task(db_engine);registry=api.SerialMixClaims()
    changed={**batch.snapshot,'copies':[{'id':'changed'},{'id':'B'}]}
    with writing(db_engine) as session:
        with pytest.raises(ValueError):registry.claim(session,batch.id,0,changed)
        row=session.get(MixTask,batch.id);snapshot=json.loads(row.snapshot_json);snapshot['daily_publish'].pop('execution_flow')
        row.snapshot_json=json.dumps(snapshot);session.flush()
        assert registry.claim(session,batch.id,0,snapshot) is None


def test_finishing_does_not_automatically_release_index_and_controlled_retry_increments_generation(db_engine):
    api=module();batch=task(db_engine);registry=api.SerialMixClaims(owner_id='first')
    with writing(db_engine) as session:
        old=registry.claim(session,batch.id,0,batch.snapshot)
        assert registry.finish(session,old)
        assert not registry.finish(session,old)
    reopened=api.SerialMixClaims(owner_id='new')
    with writing(db_engine) as session:
        assert reopened.claim(session,batch.id,0,batch.snapshot) is None
        assert not reopened.recover(session,batch.id,0,controlled_recovery=False)
        assert reopened.recover(session,batch.id,0,controlled_recovery=True)
        new=reopened.claim(session,batch.id,0,batch.snapshot)
        assert new is not None and new.generation==old.generation+1
        assert not reopened.verify(session,old) and not reopened.finish(session,old)


@pytest.mark.parametrize('evidence',[('same',True),(None,True),(None,None),('different',None)])
def test_active_owner_without_definite_stop_proof_never_expires(db_engine,evidence):
    api=module();batch=task(db_engine)
    first=api.SerialMixClaims(owner_id='active',process_state=lambda pid:('same',True))
    with writing(db_engine) as session:claim=first.claim(session,batch.id,0,batch.snapshot)
    other=api.SerialMixClaims(owner_id='other',process_state=lambda pid:evidence)
    with writing(db_engine) as session:
        saved=session.get(Setting,api.claim_key(batch.id,0));saved.updated_at=datetime(2000,1,1,tzinfo=timezone.utc)
        assert not other.recover(session,batch.id,0,controlled_recovery=True)
        assert not other.recover(session,batch.id,0,controlled_recovery=True,stopped_proof=lambda record:False)
        assert other.claim(session,batch.id,0,batch.snapshot) is None


@pytest.mark.parametrize('evidence',[(None,False),('new-birth',True)])
def test_proven_dead_or_reused_pid_allows_only_controlled_recovery(db_engine,evidence):
    api=module();batch=task(db_engine)
    first=api.SerialMixClaims(process_state=lambda pid:('old-birth',True))
    with writing(db_engine) as session:old=first.claim(session,batch.id,0,batch.snapshot)
    next_owner=api.SerialMixClaims(process_state=lambda pid:evidence)
    with writing(db_engine) as session:
        assert not next_owner.recover(session,batch.id,0,controlled_recovery=False)
        assert next_owner.recover(session,batch.id,0,controlled_recovery=True)
        new=next_owner.claim(session,batch.id,0,batch.snapshot)
        assert new.generation==old.generation+1


def test_joined_worker_proof_can_recover_same_process_without_claiming_pid_death(db_engine):
    api=module();batch=task(db_engine);registry=api.SerialMixClaims(process_state=lambda pid:('alive',True))
    with writing(db_engine) as session:old=registry.claim(session,batch.id,0,batch.snapshot)
    seen=[]
    def stopped(record):
        seen.append(record['owner_id'])
        with pytest.raises(TypeError):record['owner_id']='forged'
        return True
    with writing(db_engine) as session:
        assert registry.recover(session,batch.id,0,controlled_recovery=True,stopped_proof=stopped)
        assert registry.claim(session,batch.id,0,batch.snapshot).generation==old.generation+1
    assert seen==[old.owner_id]


def test_success_receipt_blocks_initial_claim_and_any_retry_release(db_engine):
    api=module();batch=task(db_engine);registry=api.SerialMixClaims()
    with writing(db_engine) as session:
        claim=registry.claim(session,batch.id,0,batch.snapshot)
        registry.finish(session,claim)
        session.add(Setting(key=f'mix-success:{batch.id}:0',value_json='{"finished_id":"owned"}'));session.flush()
        assert not registry.recover(session,batch.id,0,controlled_recovery=True,stopped_proof=lambda record:True)
        assert registry.claim(session,batch.id,0,batch.snapshot) is None
    second=task(db_engine)
    with writing(db_engine) as session:
        session.add(Setting(key=f'mix-success:{second.id}:0',value_json='{}'));session.flush()
        assert registry.claim(session,second.id,0,second.snapshot) is None


def test_rollback_never_leaves_a_claim_and_read_checks_make_no_writes(db_engine):
    api=module();batch=task(db_engine);registry=api.SerialMixClaims()
    with pytest.raises(RuntimeError):
        with writing(db_engine) as session:
            assert registry.claim(session,batch.id,0,batch.snapshot) is not None
            raise RuntimeError('外层事务受控回滚')
    with writing(db_engine) as session:claim=registry.claim(session,batch.id,0,batch.snapshot)
    with session_scope(db_engine) as session:
        before=[(row.key,row.value_json) for row in session.scalars(select(Setting))]
        assert registry.verify(session,claim)
        assert [(row.key,row.value_json) for row in session.scalars(select(Setting))]==before


def test_corrupt_durable_claim_fails_closed_without_replacing_it(db_engine):
    api=module();batch=task(db_engine);registry=api.SerialMixClaims()
    with writing(db_engine) as session:
        session.add(Setting(key=api.claim_key(batch.id,0),value_json='{"schema":1}'));session.flush()
        assert registry.claim(session,batch.id,0,batch.snapshot) is None
        assert not registry.recover(session,batch.id,0,controlled_recovery=True,stopped_proof=lambda record:True)
        assert session.get(Setting,api.claim_key(batch.id,0)).value_json=='{"schema":1}'


def test_default_process_probe_is_read_only_and_reports_own_birth_without_windows_kill(db_engine,monkeypatch):
    import os
    api=module()
    monkeypatch.setattr(os,'kill',lambda *args:pytest.fail('进程身份探针不得调用os.kill'))
    birth,alive=api.process_state(os.getpid())
    assert alive is True and type(birth) is str and birth


@pytest.mark.parametrize('mode,expected',[
    ('alive',('100:12',True)),('zombie',(None,False)),('denied',(None,None)),
    ('missing',(None,False)),('short',(None,None)),('wrong_pid',(None,None)),
])
def test_macos_process_probe_uses_read_only_kernel_identity(monkeypatch,mode,expected):
    import ctypes,errno,sys
    from types import SimpleNamespace
    api=module();monkeypatch.setattr(sys,'platform','darwin')
    class Read:
        def __call__(self,pid,flavor,arg,buffer,size):
            assert flavor==3 and arg==0
            if mode in {'missing','denied'}:
                ctypes.set_errno(errno.ESRCH if mode=='missing' else errno.EPERM);return 0
            if mode=='short':return 1
            value=buffer._obj;value.pbi_pid=pid+int(mode=='wrong_pid');value.pbi_status=5 if mode=='zombie' else 2
            value.pbi_start_tvsec=100;value.pbi_start_tvusec=12
            return size
    monkeypatch.setattr(ctypes,'CDLL',lambda path,**kw:SimpleNamespace(proc_pidinfo=Read()))
    assert api.process_state(1234)==expected


def test_pending_pause_intent_prevents_claim_even_before_state_settles(db_engine):
    api=module();batch=task(db_engine);registry=api.SerialMixClaims()
    with writing(db_engine) as session:
        row=session.get(MixTask,batch.id);row.state='running';row.pause_requested=True
        assert registry.claim(session,batch.id,0,batch.snapshot) is None


@pytest.mark.parametrize('changes',[{'state':[]},{'generation':True},{'token_digest':{}},{'pid':True},{'birth':False}])
def test_malformed_durable_field_types_never_unlock_or_raise(db_engine,changes):
    api=module();batch=task(db_engine);registry=api.SerialMixClaims()
    with writing(db_engine) as session:
        claim=registry.claim(session,batch.id,0,batch.snapshot)
        saved=session.get(Setting,api.claim_key(batch.id,0));record=json.loads(saved.value_json);record.update(changes)
        saved.value_json=json.dumps(record);session.flush();before=saved.value_json
        assert registry.claim(session,batch.id,0,batch.snapshot) is None
        assert not registry.verify(session,claim)
        assert not registry.recover(session,batch.id,0,controlled_recovery=True,stopped_proof=lambda record:True)
        assert saved.value_json==before


def test_boolean_or_non_boolean_stop_answers_are_not_owner_proof(db_engine):
    api=module();batch=task(db_engine);registry=api.SerialMixClaims(process_state=lambda pid:('alive',True))
    with writing(db_engine) as session:
        registry.claim(session,batch.id,0,batch.snapshot)
        assert not registry.recover(session,batch.id,0,controlled_recovery=True,stopped_proof=True)
        assert not registry.recover(session,batch.id,0,controlled_recovery=True,stopped_proof=lambda record:1)
        assert not registry.recover(session,batch.id,0,controlled_recovery=True,stopped_proof=lambda record:None)


@pytest.mark.parametrize('evidence,stopped',[(('old',True),False),((None,None),False),((None,True),False),
                                          ((None,False),True),(('new',True),True)])
def test_read_only_owner_stop_probe_uses_definite_process_evidence(db_engine,evidence,stopped):
    api=module();batch=task(db_engine)
    first=api.SerialMixClaims(process_state=lambda pid:('old',True))
    with writing(db_engine) as session:first.claim(session,batch.id,0,batch.snapshot)
    reader=api.SerialMixClaims(process_state=lambda pid:evidence)
    with session_scope(db_engine) as session:
        before=[(row.key,row.value_json,row.updated_at) for row in session.scalars(select(Setting))]
        assert reader.owner_stopped(session,batch.id,0) is stopped
        assert [(row.key,row.value_json,row.updated_at) for row in session.scalars(select(Setting))]==before


@pytest.mark.parametrize('released',[False,True])
def test_finished_or_released_owner_stop_fact_does_not_release_successful_item(db_engine,released):
    api=module();batch=task(db_engine);registry=api.SerialMixClaims()
    with writing(db_engine) as session:
        claim=registry.claim(session,batch.id,0,batch.snapshot);registry.finish(session,claim)
        if released:assert registry.recover(session,batch.id,0,controlled_recovery=True)
        session.add(Setting(key=f'mix-success:{batch.id}:0',value_json='{}'));session.flush()
        before=session.get(Setting,api.claim_key(batch.id,0)).value_json
        assert registry.owner_stopped(session,batch.id,0) is True
        assert session.get(Setting,api.claim_key(batch.id,0)).value_json==before
        assert not registry.recover(session,batch.id,0,controlled_recovery=True)
        assert registry.claim(session,batch.id,0,batch.snapshot) is None


@pytest.mark.parametrize('change',['missing','malformed','snapshot','index'])
def test_owner_stop_fact_requires_current_valid_claim_binding(db_engine,change):
    api=module();batch=task(db_engine);registry=api.SerialMixClaims()
    with writing(db_engine) as session:
        claim=registry.claim(session,batch.id,0,batch.snapshot);registry.finish(session,claim)
        saved=session.get(Setting,api.claim_key(batch.id,0))
        if change=='missing':session.delete(saved)
        if change=='malformed':saved.value_json='{"schema":true}'
        if change=='snapshot':
            row=session.get(MixTask,batch.id);snapshot=json.loads(row.snapshot_json);snapshot['copies'][0]['id']='changed'
            row.snapshot_json=json.dumps(snapshot)
        if change=='index':session.get(MixTask,batch.id).current_index=1
        session.flush()
        assert registry.owner_stopped(session,batch.id,0) is False
