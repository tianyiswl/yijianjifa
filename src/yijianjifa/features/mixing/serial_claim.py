"""逐条制作的持久索引领取；调用者持有 BEGIN IMMEDIATE，提交后才可渲染。"""
from collections.abc import Mapping
from dataclasses import dataclass,field
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sys
from types import MappingProxyType
import uuid

from yijianjifa.storage.models import Setting


def snapshot_digest(snapshot):
    """使用冻结数据本身计算摘要，不受 JSON 空白和键顺序影响。"""
    if not isinstance(snapshot,Mapping):raise ValueError('制作快照无效')
    try:value=json.dumps(dict(snapshot),ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
    except (TypeError,ValueError):raise ValueError('制作快照无效') from None
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def claim_key(task_id,index):
    if (type(task_id) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',task_id)
            or type(index) is not int or index<0):raise ValueError('制作索引无效')
    return f'mix.serial.claim:{task_id}:{index}'


def process_state(pid):
    """只读进程出生身份和存活证据；权限不足或平台未知不构成停止证明。"""
    if type(pid) is not int or pid<=0:return None,None
    if sys.platform=='win32':
        import ctypes
        from ctypes import wintypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
        kernel.OpenProcess.restype=wintypes.HANDLE
        kernel.CloseHandle.argtypes=[wintypes.HANDLE];kernel.CloseHandle.restype=wintypes.BOOL
        kernel.GetExitCodeProcess.argtypes=[wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD)]
        kernel.GetExitCodeProcess.restype=wintypes.BOOL
        kernel.GetProcessTimes.argtypes=[wintypes.HANDLE,*[ctypes.POINTER(wintypes.FILETIME)]*4]
        kernel.GetProcessTimes.restype=wintypes.BOOL
        handle=kernel.OpenProcess(0x1000,False,pid)
        if not handle:return (None,False) if ctypes.get_last_error()==87 else (None,None)
        try:
            status=wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle,ctypes.byref(status)):return None,None
            if status.value!=259:return None,False
            times=[wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle,*[ctypes.byref(value) for value in times]):return None,None
            birth=times[0]
            return str((birth.dwHighDateTime<<32)|birth.dwLowDateTime),True
        finally:kernel.CloseHandle(handle)
    if sys.platform.startswith('linux'):
        try:
            values=Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()
            return (None,False) if values[0]=='Z' else (values[19],True)
        except FileNotFoundError:return None,False
        except (OSError,IndexError):return None,None
    if sys.platform=='darwin':
        # Read-only proc_pidinfo(PROC_PIDTBSDINFO), from Apple's sys/proc_info.h.
        # Birth uses microseconds to distinguish PID reuse; no signal is sent.
        import ctypes,errno
        class BSDInfo(ctypes.Structure):
            _fields_=[(name,ctypes.c_uint32) for name in (
                'pbi_flags','pbi_status','pbi_xstatus','pbi_pid','pbi_ppid','pbi_uid','pbi_gid',
                'pbi_ruid','pbi_rgid','pbi_svuid','pbi_svgid','rfu_1')]+[
                ('pbi_comm',ctypes.c_char*16),('pbi_name',ctypes.c_char*32)]+[
                (name,ctypes.c_uint32) for name in ('pbi_nfiles','pbi_pgid','pbi_pjobc','e_tdev','e_tpgid')]+[
                ('pbi_nice',ctypes.c_int32),('pbi_start_tvsec',ctypes.c_uint64),('pbi_start_tvusec',ctypes.c_uint64)]
        try:
            library=ctypes.CDLL('/usr/lib/libproc.dylib',use_errno=True)
            read=library.proc_pidinfo
            read.argtypes=[ctypes.c_int,ctypes.c_int,ctypes.c_uint64,ctypes.c_void_p,ctypes.c_int]
            read.restype=ctypes.c_int
            info=BSDInfo();size=ctypes.sizeof(info);ctypes.set_errno(0)
            actual=read(pid,3,0,ctypes.byref(info),size)
            if actual!=size:return (None,False) if actual==0 and ctypes.get_errno()==errno.ESRCH else (None,None)
            if info.pbi_pid!=pid:return None,None
            if info.pbi_status==5:return None,False  # SZOMB in Apple's sys/proc.h.
            if not info.pbi_start_tvsec or info.pbi_start_tvusec>=1000000:return None,None
            return f'{info.pbi_start_tvsec}:{info.pbi_start_tvusec}',True
        except (OSError,AttributeError):return None,None
    return None,None


_default_process_state=process_state
_HASH=re.compile(r'[0-9a-f]{64}')
_FIELDS={'schema','task_id','index','snapshot_digest','owner_id','generation','token_digest','pid','birth','state'}


@dataclass(frozen=True,slots=True)
class MixClaim:
    task_id:str
    index:int
    snapshot_digest:str
    owner_id:str
    generation:int
    token:str=field(repr=False)


class SerialMixClaims:
    """私有执行者证明不暴露给界面；本模块不自行开启、提交或恢复事务。"""
    def __init__(self,*,owner_id=None,process_state=None):
        self.owner_id=uuid.uuid4().hex if owner_id is None else owner_id
        if type(self.owner_id) is not str or not 1<=len(self.owner_id)<=128:raise ValueError('执行者身份无效')
        self.pid=os.getpid();self._process_state=process_state or _default_process_state
        try:birth,_=self._process_state(self.pid)
        except Exception:birth=None
        self.birth=birth if type(birth) is str and 1<=len(birth)<=128 else None

    @staticmethod
    def _task(session,task_id):
        from .queue import MixTask
        row=session.get(MixTask,task_id)
        if row is None:return None,None
        try:snapshot=json.loads(row.snapshot_json)
        except (TypeError,ValueError):return row,None
        return row,snapshot if type(snapshot) is dict else None

    @staticmethod
    def _record(saved,task_id,index):
        if saved is None:return None
        try:value=json.loads(saved.value_json)
        except (TypeError,ValueError):return None
        if (type(value) is not dict or set(value)!=_FIELDS or type(value['schema']) is not int or value['schema']!=1
                or value['task_id']!=task_id or type(value['index']) is not int or value['index']!=index
                or type(value['generation']) is not int or value['generation']<1
                or type(value['owner_id']) is not str or not 1<=len(value['owner_id'])<=128
                or type(value['pid']) is not int or value['pid']<=0
                or value['birth'] is not None and (type(value['birth']) is not str or not 1<=len(value['birth'])<=128)
                or type(value['state']) is not str or value['state'] not in {'active','finished','released'}
                or any(type(value[key]) is not str or not _HASH.fullmatch(value[key]) for key in ('snapshot_digest','token_digest'))):return None
        return value

    @staticmethod
    def _save(session,saved,key,record):
        value=json.dumps(record,sort_keys=True,separators=(',',':'),ensure_ascii=False)
        if saved is None:session.add(Setting(key=key,value_json=value))
        else:saved.value_json=value
        session.flush()

    def claim(self,session,task_id,index,snapshot):
        """只有本次事务领取成功并提交后，调用者才可离开事务执行制作。"""
        key=claim_key(task_id,index);row,current=self._task(session,task_id)
        expected=snapshot_digest(snapshot)
        if current is None or snapshot_digest(current)!=expected:raise ValueError('制作快照已改变')
        daily=current.get('daily_publish')
        if (type(daily) is not dict or daily.get('execution_flow')!='per_item_v1'
                or row.state not in {'waiting','running'} or row.pause_requested or index!=row.current_index or index>=row.quantity
                or session.get(Setting,f'mix-success:{task_id}:{index}') is not None):return None
        saved=session.get(Setting,key);previous=self._record(saved,task_id,index)
        if saved is not None and (previous is None or previous['state']!='released'):return None
        generation=previous['generation']+1 if previous else 1
        token=secrets.token_hex(32)
        record={'schema':1,'task_id':task_id,'index':index,'snapshot_digest':expected,'owner_id':self.owner_id,
                'generation':generation,'token_digest':hashlib.sha256(token.encode()).hexdigest(),
                'pid':self.pid,'birth':self.birth,'state':'active'}
        self._save(session,saved,key,record)
        return MixClaim(task_id,index,expected,self.owner_id,generation,token)

    def verify(self,session,claim):
        """只读核对当前领取；暂停和取消不消除正在退出的执行者身份。"""
        if (type(claim) is not MixClaim or claim.owner_id!=self.owner_id
                or type(claim.generation) is not int or claim.generation<1):return False
        try:key=claim_key(claim.task_id,claim.index)
        except ValueError:return False
        record=self._record(session.get(Setting,key),claim.task_id,claim.index)
        row,current=self._task(session,claim.task_id)
        if record is None or current is None or record['state']!='active' or row.current_index!=claim.index:return False
        return (record['owner_id']==claim.owner_id and record['generation']==claim.generation
                and record['snapshot_digest']==claim.snapshot_digest==snapshot_digest(current)
                and type(claim.token) is str
                and secrets.compare_digest(record['token_digest'],hashlib.sha256(claim.token.encode()).hexdigest()))

    def finish(self,session,claim):
        """制作调用已退出后，在结果事务内标记停止；不会自动释放或重新领取。"""
        if not self.verify(session,claim):return False
        key=claim_key(claim.task_id,claim.index);saved=session.get(Setting,key)
        record=self._record(saved,claim.task_id,claim.index);record['state']='finished'
        self._save(session,saved,key,record)
        return True

    def _stopped(self,record):
        try:birth,alive=self._process_state(record['pid'])
        except Exception:return False
        return alive is False or (alive is True and record['birth'] is not None
            and type(birth) is str and bool(birth) and birth!=record['birth'])

    def owner_stopped(self,session,task_id,index):
        """只读当前索引的停止事实，不等同于允许重试，也不释放成功凭据。"""
        try:key=claim_key(task_id,index)
        except ValueError:return False
        record=self._record(session.get(Setting,key),task_id,index)
        row,current=self._task(session,task_id)
        if record is None or current is None or row.current_index!=index:return False
        try:matches=snapshot_digest(current)==record['snapshot_digest']
        except ValueError:return False
        return matches and (record['state'] in {'finished','released'} or self._stopped(record))

    def recover(self,session,task_id,index,*,controlled_recovery,stopped_proof=None):
        """仅受控恢复可释放；活线程、权限不足、超时和单纯重开都不是停止证明。"""
        if controlled_recovery is not True or stopped_proof is not None and not callable(stopped_proof):return False
        key=claim_key(task_id,index);saved=session.get(Setting,key);record=self._record(saved,task_id,index)
        row,current=self._task(session,task_id)
        if (record is None or record['state']=='released' or current is None or row.current_index!=index
                or snapshot_digest(current)!=record['snapshot_digest']
                or session.get(Setting,f'mix-success:{task_id}:{index}') is not None):return False
        stopped=record['state']=='finished'
        if not stopped and stopped_proof is not None:
            try:stopped=stopped_proof(MappingProxyType(dict(record))) is True
            except Exception:stopped=False
        elif not stopped:stopped=self._stopped(record)
        if not stopped:return False
        record['state']='released';self._save(session,saved,key,record)
        return True
