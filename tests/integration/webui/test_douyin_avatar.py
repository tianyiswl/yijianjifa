"""Douyin login avatars: observed page resources and rechecked identity only."""
import json,subprocess
from types import SimpleNamespace
import pytest
from yijianjifa.webui import chromium_publish as auth
URL='https://p3.douyinpic.com/avatar/test.jpeg?size=100'
AVATAR='data:image/jpeg;base64,/9j/'

def identity(uid='123',url=URL):
 return dict(uid=uid,uid_after=uid,status=0,profile_status=0,after_status=0,nickname='测试账号',avatar_url=url)

def test_login_script_selects_actual_loaded_profile_avatar():
 script="""const fs=require('node:fs'),v=JSON.parse(fs.readFileSync(0,'utf8'));
 global.location={origin:'https://creator.douyin.com'};
 const answers=[{uid:'123',status_code:0},{status_code:0,user_profile:{nick_name:'测试账号',avatar_url:{invalid:true},avatar_thumb:{url_list:[v.url]}}},{uid:'123',status_code:0}];
 global.fetch=async()=>({ok:true,json:async()=>answers.shift()});
 global.document={images:[{complete:true,naturalWidth:100,currentSrc:v.loaded}]};
 (async()=>{const read=new (Object.getPrototypeOf(async function(){}).constructor)(v.script+'return identity;');console.log(JSON.stringify(await read()))})().catch(e=>{console.error(e);process.exit(1)});
 """
 loaded=URL.replace('size=100','size=200')
 result=subprocess.run(['node','-e',script],input=json.dumps({'script':auth.READ_LOGIN_IDENTITY,'url':URL,'loaded':loaded}),text=True,encoding='utf-8',capture_output=True,check=True)
 assert json.loads(result.stdout)['avatar_url']==loaded


@pytest.mark.parametrize('candidate,loaded,expected', [
 ('http://p3.douyinpic.com/avatar/test.jpeg?api=1', 'https://p3.douyinpic.com/avatar/test.jpeg?loaded=1', 'https://p3.douyinpic.com/avatar/test.jpeg?loaded=1'),
 ('//p3.douyinpic.com/avatar/test.jpeg?api=1', 'https://p3.douyinpic.com/avatar/test.jpeg?loaded=1', 'https://p3.douyinpic.com/avatar/test.jpeg?loaded=1'),
 ('http://p3.douyinpic.com:443/avatar/test.jpeg', 'https://p3.douyinpic.com/avatar/test.jpeg', 'https://p3.douyinpic.com/avatar/test.jpeg'),
 ('http://p3.douyinpic.com/avatar/test.jpeg', 'https://other.example.test/avatar/test.jpeg', 'https://p3.douyinpic.com/avatar/test.jpeg'),
 ('http://p3.douyinpic.com/avatar/test.jpeg', 'https://p3.douyinpic.com/avatar/another.jpeg', 'https://p3.douyinpic.com/avatar/test.jpeg'),
 ('https://name:secret@p3.douyinpic.com/avatar/test.jpeg', 'https://p3.douyinpic.com/avatar/test.jpeg', ''),
 ('http://p3.douyinpic.com:80/avatar/test.jpeg', 'https://p3.douyinpic.com/avatar/test.jpeg', ''),
 ('https://p3.douyinpic.com:8443/avatar/test.jpeg', 'https://p3.douyinpic.com/avatar/test.jpeg', ''),
 ('data:image/png;base64,fixture', 'https://p3.douyinpic.com/avatar/test.jpeg', ''),
 ('ftp://p3.douyinpic.com/avatar/test.jpeg', 'https://p3.douyinpic.com/avatar/test.jpeg', ''),
])
def test_login_avatar_normalizes_safe_upgrade_without_selecting_other_images(candidate, loaded, expected):
 script="""const fs=require('node:fs'),v=JSON.parse(fs.readFileSync(0,'utf8'));
 global.location={origin:'https://creator.douyin.com'};
 const answers=[{uid:'123',status_code:0},{status_code:0,user_profile:{nick_name:'fixture',avatar_thumb:{url_list:[v.candidate]}}},{uid:'123',status_code:0}];
 global.fetch=async()=>({ok:true,json:async()=>answers.shift()});
 global.document={images:[{complete:true,naturalWidth:100,currentSrc:v.loaded}]};
 (async()=>{const read=new (Object.getPrototypeOf(async function(){}).constructor)(v.script+'return identity;');console.log(JSON.stringify(await read()))})().catch(()=>process.exit(1));
 """
 result=subprocess.run(['node','-e',script],input=json.dumps({'script':auth.READ_LOGIN_IDENTITY,'candidate':candidate,'loaded':loaded}),text=True,encoding='utf-8',capture_output=True,check=True)
 assert json.loads(result.stdout)['avatar_url']==expected

def test_observation_passes_captured_avatar_and_rechecks_account(monkeypatch):
 values=iter([identity(),identity()])
 page=SimpleNamespace(get_by_text=lambda *a,**k:SimpleNamespace(count=lambda:1))
 monkeypatch.setattr(auth,'evaluate_script',lambda *a:next(values))
 monkeypatch.setattr(auth,'session_blob',lambda context,uid:b'fixture-session')
 result=auth.observe_login_session(page,object(),avatar_reader=lambda url:AVATAR if url==URL else '')
 assert result['avatar_data']==AVATAR and result['platform_user_id']=='123'
 assert result['ordinary_available'] is True and 'avatar_url' not in result

@pytest.mark.parametrize('change',[{'uid':'456','uid_after':'456'},{'avatar_url':URL+'&changed=1'}])
def test_changed_account_or_avatar_cannot_save_stale_image(monkeypatch,change):
 values=iter([identity(),{**identity(),**change}])
 page=SimpleNamespace(get_by_text=lambda *a,**k:SimpleNamespace(count=lambda:1))
 monkeypatch.setattr(auth,'evaluate_script',lambda *a:next(values))
 monkeypatch.setattr(auth,'session_blob',lambda *a:b'fixture-session')
 if 'uid' in change:
  with pytest.raises(ValueError):auth.observe_login_session(page,object(),avatar_reader=lambda url:AVATAR)
 else:
  assert 'avatar_data' not in auth.observe_login_session(page,object(),avatar_reader=lambda url:AVATAR)

def test_avatar_absence_does_not_prevent_login(monkeypatch):
 values=iter([identity(url=''),identity(url='')])
 page=SimpleNamespace(get_by_text=lambda *a,**k:SimpleNamespace(count=lambda:0))
 monkeypatch.setattr(auth,'evaluate_script',lambda *a:next(values))
 monkeypatch.setattr(auth,'session_blob',lambda *a:b'fixture-session')
 assert auth.observe_login_session(page,object())['platform_user_id']=='123'

def test_missing_page_image_uses_bounded_public_avatar_reader(monkeypatch):
 from yijianjifa.features.accounts import avatar
 values=iter([identity(),identity()]);calls=[]
 page=SimpleNamespace(get_by_text=lambda *a,**k:SimpleNamespace(count=lambda:1))
 monkeypatch.setattr(auth,'evaluate_script',lambda *a:next(values))
 monkeypatch.setattr(auth,'session_blob',lambda *a:b'fixture-session')
 monkeypatch.setattr(avatar,'fetch_avatar',lambda url:calls.append(url) or AVATAR)
 result=auth.observe_login_session(page,object(),avatar_reader=lambda url:'')
 assert result['avatar_data']==AVATAR and calls==[URL]


@pytest.mark.parametrize('first_host,second_host', [
 ('p3.douyinpic.com','p11.douyinpic.com'),
 ('p11.douyinpic.com','p26.douyinpic.com'),
 ('p3-pc.douyinpic.com','p26-pc.douyinpic.com'),
 ('p3.douyinpic.com','p11-pc.douyinpic.com'),
])
def test_same_profile_avatar_survives_only_a_known_cdn_shard_change(monkeypatch,first_host,second_host):
 first='https://'+first_host+'/avatar/fixture.jpeg?size=100'
 second='https://'+second_host+'/avatar/fixture.jpeg?size=100'
 values=iter([identity(url=first),identity(url=second)]);reads=[]
 page=SimpleNamespace(get_by_text=lambda *a,**k:SimpleNamespace(count=lambda:1))
 monkeypatch.setattr(auth,'evaluate_script',lambda *a:next(values))
 monkeypatch.setattr(auth,'session_blob',lambda *a:b'fixture-session')
 result=auth.observe_login_session(page,object(),avatar_reader=lambda url:reads.append(url) or AVATAR)
 assert result['avatar_data']==AVATAR and result['platform_user_id']=='123'
 assert reads==[first]


@pytest.mark.parametrize('second', [
 'https://p11.douyinpic.com/avatar/another.jpeg?size=100',
 'https://p11.douyinpic.com/avatar/test.jpeg?size=200',
 'https://assets.douyinpic.com/avatar/test.jpeg?size=100',
 'https://p11.other.douyinpic.com/avatar/test.jpeg?size=100',
 'https://p11.douyinpic.com.other.test/avatar/test.jpeg?size=100',
 'https://p11.byteimg.com/avatar/test.jpeg?size=100',
 'https://p11.douyincdn.com/avatar/test.jpeg?size=100',
 'https://p11.douyinpic.com:8443/avatar/test.jpeg?size=100',
 'https://user:secret@p11.douyinpic.com/avatar/test.jpeg?size=100',
 'https://@p11.douyinpic.com/avatar/test.jpeg?size=100',
 'https://:@p11.douyinpic.com/avatar/test.jpeg?size=100',
])
def test_shard_recovery_never_equates_different_or_untrusted_avatar_urls(monkeypatch,second):
 values=iter([identity(),identity(url=second)])
 page=SimpleNamespace(get_by_text=lambda *a,**k:SimpleNamespace(count=lambda:1))
 monkeypatch.setattr(auth,'evaluate_script',lambda *a:next(values))
 monkeypatch.setattr(auth,'session_blob',lambda *a:b'fixture-session')
 assert 'avatar_data' not in auth.observe_login_session(page,object(),avatar_reader=lambda url:AVATAR)


def test_cdn_shard_alias_cannot_make_a_changed_uid_pass_verification(monkeypatch):
 values=iter([identity(),identity(uid='456',url=URL.replace('p3.','p11.'))])
 page=SimpleNamespace(get_by_text=lambda *a,**k:SimpleNamespace(count=lambda:1))
 monkeypatch.setattr(auth,'evaluate_script',lambda *a:next(values))
 monkeypatch.setattr(auth,'session_blob',lambda *a:b'fixture-session')
 with pytest.raises(ValueError):
  auth.observe_login_session(page,object(),avatar_reader=lambda url:AVATAR)
