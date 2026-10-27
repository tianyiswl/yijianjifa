"""Frozen fields and evidence from the page's normal requests, no platform IO."""
from datetime import datetime, timedelta, timezone
import importlib.util
import pytest


def form_module():
    assert importlib.util.find_spec('yijianjifa.webui.kuaishou_publish_form'), '快手表单契约尚未实现'
    from yijianjifa.webui import kuaishou_publish_form
    return kuaishou_publish_form


def payload():
    return {'fields': {'title': '测试标题', 'body': '测试说明', 'tags': '#功能测试 #剪辑测试',
                      'cover': {'kind': 'first_frame', 'frame_ms': 0}},
            'execution_mode': 'platform_schedule',
            'scheduled_at': (datetime.now(timezone.utc)+timedelta(days=1)).replace(second=0, microsecond=0).isoformat()}


def test_frozen_caption_and_schedule_preserve_fields():
    m=form_module();p=payload();f=m.validate_form(p)
    assert f['text']=='测试标题\n测试说明'
    assert f['tags']==['功能测试','剪辑测试']
    assert f['caption']=='测试标题\n测试说明 #功能测试 #剪辑测试'
    assert f['time_text']==datetime.fromisoformat(p['scheduled_at']).astimezone(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M')
    assert f['publish_ms']==int(datetime.fromisoformat(p['scheduled_at']).timestamp()*1000)


@pytest.mark.parametrize('change', [
    {'tags':' '.join('#话题'+str(i) for i in range(5))},
    {'body':'长'*501}, {'location':{'name':'三千海'}}, {'declaration':{'label':'AI生成'}},
    {'cover':{'kind':'custom','path':'/tmp/cover.jpg'}}])
def test_never_drop_unsupported_or_overlong_fields(change):
    m=form_module();p=payload();p['fields'].update(change)
    with pytest.raises(ValueError):m.validate_form(p)


def test_expired_frozen_schedule_rejected_at_final_but_allowed_for_query():
    m=form_module();p=payload();p['scheduled_at']=(datetime.now(timezone.utc)+timedelta(hours=2)).replace(second=0,microsecond=0).isoformat()
    with pytest.raises(ValueError):m.validate_form(p)
    assert m.validate_form(p,query=True)['publish_ms']>0


def test_upload_resource_id_cannot_be_used_as_work_receipt():
    m=form_module()
    assert m.receipt_reference({'fileId':'123'})=={}
    assert m.receipt_reference({'publishId':'safe-123'})=={'publish_id':'safe-123'}
    assert m.receipt_reference({'workId':9007199254740993})=={}
    assert m.receipt_reference({'workId':'9007199254740993'})=={'work_id':'9007199254740993'}


def test_readback_requires_exact_receipt_and_retains_audit_state():
    m=form_module();p=payload();f=m.validate_form(p)
    row={'workId':'42','publishId':'24','title':f['caption'],'publishStatus':10,'publishType':2}
    assert m.verified_row([row],{'work_id':'41'},f) is None
    assert m.verified_row([row],{'work_id':'42'},f)['state']=='reviewing'
    assert m.verified_row([{**row,'publishStatus':4,'publishType':1}],{'work_id':'42'},f) is None
    assert m.verified_row([row,row],{'work_id':'42'},f) is None


def test_form_readback_rejects_plain_hashtag_and_changed_schedule():
    m=form_module();f=m.validate_form(payload())
    raw={'caption':f['caption'],'tags':f['tags'],'time_text':f['time_text'],
         'mode':'定时发布','upload_complete':True,'button_count':1,'button_enabled':True,
         'declaration_count':1,'declaration_variant':'legacy','declaration_label':'','declaration_item_count':0}
    assert m.verify_form(raw,f)
    assert not m.verify_form({**raw,'tags':[]},f)
    assert not m.verify_form({**raw,'time_text':'2020-01-01 12:00:00'},f)


def test_schedule_text_matches_current_native_minute_picker_without_changing_timestamp():
    m=form_module();p=payload();f=m.validate_form(p)
    local=datetime.fromisoformat(p['scheduled_at']).astimezone(timezone(timedelta(hours=8)))
    assert f['time_text']==local.strftime('%Y-%m-%d %H:%M')
    assert len(f['time_text'])==16
    assert f['publish_ms']==int(local.timestamp()*1000)
