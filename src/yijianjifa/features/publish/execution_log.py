"""Bounded per-attempt execution events; no credentials or page contents."""
import json
import re
from datetime import datetime,timezone
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting

def log_key(delivery_id,attempt):
    return 'publish.execution:'+str(delivery_id)+':'+str(attempt)

def append_event(engine,delivery_id,attempt,step,checks=None):
    if not delivery_id:return
    # Only code-owned finite names, booleans and counters may be persisted.
    safe={k:v for k,v in (checks or {}).items() if isinstance(k,str) and re.fullmatch(r'[a-zA-Z_]{1,64}',k) and
          (type(v) in (bool,int) or (type(v) is str and re.fullmatch(r'[a-zA-Z_]{1,64}',v)))}
    if not re.fullmatch(r'[a-zA-Z_.]{1,80}',step):return
    try:
        with session_scope(engine) as db:
            key=log_key(delivery_id,attempt);row=db.get(Setting,key)
            events=json.loads(row.value_json) if row else []
            previous=next((e for e in reversed(events) if e['step']==step),None)
            if previous and previous['checks']==safe:return
            events.append({'time':datetime.now(timezone.utc).isoformat(),'step':step,'checks':safe})
            value=json.dumps(events[-500:],ensure_ascii=False)
            if row:row.value_json=value
            else:db.add(Setting(key=key,value_json=value))
    except Exception:
        # Diagnostics must never prevent the user's publication.
        return
