import json
from sqlalchemy import select
from yijianjifa.app_services.desktop_api import DesktopApi
from yijianjifa.features.library.media_storage import MediaStorageService
from yijianjifa.features.notifications.service import NotificationRow
from yijianjifa.storage.db import session_scope


def call(api,op,p,rid):
    return api.dispatch(json.dumps({'v':1,'request_id':rid,'operation':op,'params':p}),trusted_origin=True)


def test_empty_copy_has_actionable_error(db_engine,app_paths,tmp_path):
    source=tmp_path/'video.mp4';source.write_bytes(b'fixture')
    MediaStorageService(db_engine,app_paths,kind='video').import_media(source,keep_copy=False,category_ids=[])
    api=DesktopApi(db_engine,app_paths)
    try:
        result=call(api,'mix.submit',{'quantity':1},'empty-copy')
        assert not result['ok']
        assert result['error']['code']=='empty_copy_selection'
        assert '不使用文案' in result['error']['message']
    finally:api.close(.1)


def test_mark_all_read_covers_more_than_one_page_and_replays(db_engine,app_paths):
    with session_scope(db_engine) as session:
        session.add_all([NotificationRow(status='completed',message=f'notice{i}',read=False) for i in range(25)])
    api=DesktopApi(db_engine,app_paths)
    try:
        result=call(api,'notification.read_all',{},'all-read')
        assert result['ok'],result
        assert result['data']['updated']==25
        assert call(api,'notification.read_all',{},'all-read')==result
        with session_scope(db_engine) as session:
            assert all(n.read for n in session.scalars(select(NotificationRow)))
    finally:api.close(.1)


def test_unread_notifications_fill_to_five_with_latest_read(db_engine,app_paths):
    from datetime import datetime,timezone,timedelta
    with session_scope(db_engine) as session:
        session.add_all([NotificationRow(status='completed',message=str(i),read=i>=2,created_at=datetime(2026,9,28,tzinfo=timezone.utc)+timedelta(minutes=i)) for i in range(8)])
    api=DesktopApi(db_engine,app_paths)
    try:
        data=call(api,'notification.list',{'offset':0,'limit':20,'view':'unread_recent'},'read')['data']
        assert data['total']==5 and data['unread']==2
        assert [n['message'] for n in data['items']]==['1','0','7','6','5']
        call(api,'notification.read_all',{},'all')
        data=call(api,'notification.list',{'offset':0,'limit':20,'view':'unread_recent'},'again')['data']
        assert [n['message'] for n in data['items']]==['7','6','5','4','3']
    finally:api.close(.1)
