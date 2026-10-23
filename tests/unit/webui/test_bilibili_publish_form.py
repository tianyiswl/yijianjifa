"""Semantic B站 field/readback contracts, without any real account or upload."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import pytest
from yijianjifa.webui import bilibili_publish_form as c


@pytest.fixture
def payload():
    when=(datetime.now(timezone.utc)+timedelta(days=1)).replace(hour=4,minute=0,second=0,microsecond=0)
    return {'execution_mode':'platform_schedule','scheduled_at':when.isoformat(),'fields':{
        'title':'合成测试','body':'明确简介','tags':'#功能测试 #剪辑测试','category_id':101,'category_mode':'human','category_name':'合成分区',
        'creation_statement_id':-1,'creation_statement_name':'不标记','content_authorization':False}}


def raw(payload):
    f=c.validate_form(payload)
    return dict(title=f['title'],title_count=1,body=f['body'],body_count=1,tags=f['tags'],
        category_id=f['category_id'],category_mode=f['category_mode'],creation_statement_id=f['creation_statement_id'],
        copyright=f['copyright'],content_authorization=f['content_authorization'],source=f['source'],
        cover_reference='https://i0.hdslb.com/bfs/archive/synthetic.jpg',upload_complete=True,button_count=1,button_enabled=True,
        dtime=f['dtime'],time_enabled=bool(f['dtime']),date=f['date'],clock=f['clock'],timezone='Asia/Shanghai',
        videos=[dict(filename='synthetic-upload',cid=24,status=3)])


def request(payload,r):
    f=c.validate_form(payload)
    return dict(title=f['title'],desc=f['body'],tag=','.join(f['tags']),human_type2=f['category_id'],
        copyright=f['copyright'],source=f['source'],creation_statement={'id':f['creation_statement_id']},dtime=f['dtime'],
        cover=r['cover_reference'],videos=[dict(filename='synthetic-upload',cid=24)])


def test_never_default_original_or_category_or_statement(payload):
    f=c.validate_form(payload)
    assert f['copyright']==3 and f['content_authorization'] is False
    for name in ('category_id','category_name','category_mode','creation_statement_id','creation_statement_name'):
        p=deepcopy(payload);p['fields'].pop(name)
        with pytest.raises(ValueError):c.validate_form(p)


def intent_payload(payload, statement='no_mark'):
    value=deepcopy(payload)
    for key in ('category_id','category_name','category_mode','creation_statement_id','creation_statement_name'):
        value['fields'].pop(key,None)
    value['fields'].update(category_choice='human:tech',creation_statement_choice=statement)
    return value


def test_offline_intent_validates_without_inventing_an_upload_id(payload):
    p=intent_payload(payload,'ai_generated');saved=deepcopy(p)
    f=c.validate_form(p,local_choices=True)
    assert f['category_id'] is None and f['creation_statement_id'] is None and f['copyright']==3
    assert p==saved
    with pytest.raises(ValueError):c.validate_form(p)
    assert not c.verify_form(raw(payload),f)


def test_current_options_resolve_ids_and_official_statement_names_without_changing_intent(payload):
    p=intent_payload(payload,'ai_generated');saved=deepcopy(p)
    options={'categories':[{'id':999,'name':'科技数码','mode':'human'}],
        'statements':[{'id':77,'name':'该内容使用人工智能合成技术'}]}
    f=c.validate_form(p,options=options)
    assert f['category_id']==999 and f['creation_statement_id']==77
    assert f['creation_statement_name']=='该内容使用人工智能合成技术' and p==saved
    assert c.verify_options(options,f)
    options['categories'].append({'id':998,'name':'科技数码','mode':'human'})
    with pytest.raises(ValueError):c.validate_form(p,options=options)


def test_offline_reprint_remains_explicit_and_requires_source_without_authorization(payload):
    p=intent_payload(payload,'reprint')
    with pytest.raises(ValueError):c.validate_form(p,local_choices=True)
    p['fields']['reprint_source']='许可作者来源'
    assert c.validate_form(p,local_choices=True)['copyright']==2
    p['fields']['content_authorization']=True
    with pytest.raises(ValueError):c.validate_form(p,local_choices=True)


def test_reprint_requires_source_and_no_self_made_authorization(payload):
    payload['fields'].update(creation_statement_id=-2,creation_statement_name='转载')
    with pytest.raises(ValueError):c.validate_form(payload)
    payload['fields']['reprint_source']='转自获许可的作者'
    assert c.validate_form(payload)['copyright']==2
    payload['fields']['content_authorization']=True
    with pytest.raises(ValueError):c.validate_form(payload)


def test_original_is_only_explicit_authorization(payload):
    payload['fields']['content_authorization']=True
    assert c.validate_form(payload)['copyright']==1
    payload['fields']['content_authorization']='true'
    with pytest.raises(ValueError):c.validate_form(payload)


def test_web_limits_count_utf16_units_including_emoji(payload):
    payload['fields'].update(title='😀'*40,body='😀'*1000,tags='#'+('😀'*10))
    assert c.validate_form(payload)['title']=='😀'*40
    for field,value in [('title','😀'*41),('body','😀'*1001),('tags','#'+('😀'*11))]:
        p=deepcopy(payload);p['fields'][field]=value
        with pytest.raises(ValueError):c.validate_form(p)


def test_long_statement_names_are_unavailable_not_truncated(payload):
    payload['fields']['creation_statement_name']='字'*256
    with pytest.raises(ValueError):c.validate_form(payload)
    result=c.safe_options({'neutral_mark':{'mark_list':[{'id':10,'content':'字'*256}]}})
    assert result['statements']==[]


@pytest.mark.parametrize('patch',[{'title':''},{'title':'字'*81},{'body':'字'*2001},{'tags':''},{'tags':'#重复 #重复'},
    {'tags':'#'+('字'*21)},{'tags':'#甲,乙'},{'category_id':True},{'creation_statement_id':False},{'reprint_source':'来源'},
    {'cover':{'kind':'uploaded'}}])
def test_invalid_fields_never_reach_browser(payload,patch):
    payload['fields'].update(patch)
    with pytest.raises(ValueError):c.validate_form(payload)


def test_five_minute_timezone_schedule_and_past_query(payload):
    when=datetime.fromisoformat(payload['scheduled_at']);payload['scheduled_at']=(when+timedelta(minutes=1)).isoformat()
    with pytest.raises(ValueError):c.validate_form(payload)
    payload['scheduled_at']=(when-timedelta(days=2)).isoformat()
    with pytest.raises(ValueError):c.validate_form(payload)
    assert c.validate_form(payload,query=True)['dtime']>0


def test_options_are_actual_directory_ids_and_names_not_guessed(payload):
    current=1791370000
    pre=dict(type_mode=2,neutral_mark={'mark_list':[{'id':-1,'content':'不标记'}]},myinfo={'deftime':current+14400,'deftime_end':current+1296000})
    options=c.safe_options(pre,[{'id':101,'name':'合成分区'},{'id':True,'name':'坏值'}],now=current)
    assert c.verify_options(options,c.validate_form(payload))
    assert options['schedule']['min_lead_seconds']==14400
    assert len(options['categories'])==1
    payload['fields']['category_name']='相同ID但名称已经改变'
    assert not c.verify_options(options,c.validate_form(payload))
    legacy=c.safe_options(dict(type_mode=0,typelist=[{'name':'父级','children':[{'id':9,'name':'叶级'}]}]))
    assert legacy['categories']==[dict(id=9,name='叶级',mode='legacy',parent_name='父级')]
    assert legacy['statements']==[] and legacy['schedule'] is None


@pytest.mark.parametrize('field,value',[('title','别的标题'),('tags',['纯文字']),('copyright',1),('creation_statement_id',-2),
    ('category_id',102),('upload_complete',False),('button_count',2),('button_enabled',False),('time_enabled',False),
    ('clock','12:05'),('timezone','America/New_York'),('cover_reference','')])
def test_readback_must_agree_before_final(payload,field,value):
    r=raw(payload);assert c.verify_form(r,c.validate_form(payload))
    r[field]=value;assert not c.verify_form(r,c.validate_form(payload))


@pytest.mark.parametrize('field,value',[('title','别的标题'),('desc','其他简介'),('human_type2',102),('copyright',1),
    ('creation_statement',{'id':-2}),('tag','错误标签'),('dtime',1),('cover','https://i0.hdslb.com/other'),
    ('videos',[{'filename':'another-file','cid':24}]),('videos',[{'filename':'synthetic-upload','cid':25}]),('aid',99),('mission_id',100)])
def test_own_post_is_bound_to_uploaded_file_and_every_explicit_field(payload,field,value):
    r=raw(payload);q=request(payload,r)
    assert c.matches_submit(q,c.validate_form(payload),r)
    q[field]=value;assert not c.matches_submit(q,c.validate_form(payload),r)


def test_normal_cover_conversion_binds_only_exact_selected_bitmap():
    bitmap='data:image/jpeg;base64,c3ludGhldGlj'
    body=('--test\r\nContent-Disposition: form-data; name="cover"\r\n\r\n'+bitmap+
          '\r\n--test\r\nContent-Disposition: form-data; name="csrf"\r\n\r\nPRIVATE\r\n--test--\r\n').encode()
    remote='https://i0.hdslb.com/bfs/archive/synthetic.jpg';content='multipart/form-data; boundary=test'
    assert c.uploaded_cover_reference(content,body,bitmap,{'url':remote})==remote
    assert c.uploaded_cover_reference(content,body,bitmap+'wrong',{'url':remote}) is None
    assert c.uploaded_cover_reference(content,body,bitmap,{'url':'https://private.invalid/x'}) is None


def test_receipts_and_result_query_require_ids_exact_title_and_schedule(payload):
    f=c.validate_form(payload);ref={'work_id':'42','publish_id':'BV1xx411c7mD'}
    assert c.receipt_reference(dict(aid=42,bvid='BV1xx411c7mD'))==ref
    assert c.receipt_reference(dict(aid=True,bvid='bad'))=={}
    row=dict(aid=42,bvid=ref['publish_id'],title=f['title'],state=-40,dtime=f['dtime'])
    assert c.verified_row([row],ref,f)['state']=='platform_scheduled'
    for patch in ({'aid':41},{'title':'相似标题'},{'dtime':f['dtime']+300}):
        assert c.verified_row([{**row,**patch}],ref,f) is None
    assert c.verified_row([row],{},f) is None
    assert c.verified_row([row,row],ref,f) is None
    assert c.verified_row([{**row,'state':-50}],ref,f)['state']=='processing'


def test_finite_diagnostic_identifies_blocker_without_disclosing_form(payload):
    f=c.validate_form(payload);r=raw(payload)
    assert all(c.form_diagnostic(r,f).values())
    r['cover_reference']=''
    checks=c.form_diagnostic(r,f)
    assert [key for key,value in checks.items() if not value]==['cover_present']
    assert all(type(value) is bool for value in checks.values())
    assert not c.verify_form(r,f)
    r=raw(payload);r['dtime']=True
    assert not c.form_diagnostic(r,f)['dtime_matches']
    assert not c.verify_form(r,f)
