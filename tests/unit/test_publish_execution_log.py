import json
from yijianjifa.features.publish.execution_log import append_event,log_key
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting

def test_events_are_persistent_attempt_bound_deduplicated_and_private_data_free(db_engine):
    append_event(db_engine,'target',1,'form.check',{'tags_matches':False,'private':'token-value!','nested':{'secret':'x'}})
    append_event(db_engine,'target',1,'execution.start')
    append_event(db_engine,'target',1,'form.check',{'tags_matches':False})
    append_event(db_engine,'target',1,'form.check',{'tags_matches':True})
    append_event(db_engine,'target',2,'execution.start')
    with session_scope(db_engine) as db:
        events=json.loads(db.get(Setting,log_key('target',1)).value_json)
        assert len(events)==3 and events[0]['checks']=={'tags_matches':False}
        assert events[-1]['checks']=={'tags_matches':True} and all(e['time'] for e in events)
        assert len(json.loads(db.get(Setting,log_key('target',2)).value_json))==1
