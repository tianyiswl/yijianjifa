import pytest
from PySide6.QtCore import QUrl
from yijianjifa.webui.qt_runtime import media_request_allowed


def test_only_the_locked_app_page_can_request_local_media():
    assert media_request_allowed(QUrl('p011media://asset/token'),QUrl('null'),b'GET',QUrl('about:blank'))


@pytest.mark.parametrize('url,origin,method,page',[
    ('p011media://asset/token','https://example.com',b'GET','about:blank'),
    ('p011media://asset/token','null',b'GET','https://example.com'),
    ('p011media://asset/token','',b'GET','about:blank'),
    ('p011media://asset/token','about:blank',b'GET','about:blank'),
    ('p011media://asset/token','null',b'POST','about:blank'),
    ('p011media://asset/token?x=1','null',b'GET','about:blank'),
    ('p011media://asset/token#x','null',b'GET','about:blank'),
    ('p011media://user@asset/token','null',b'GET','about:blank'),
    ('p011media://asset:90/token','null',b'GET','about:blank'),
    ('p011media://other/token','null',b'GET','about:blank'),
    ('file:///private/token','null',b'GET','about:blank'),
])
def test_other_origins_and_url_variants_stay_denied(url,origin,method,page):
    assert not media_request_allowed(QUrl(url),QUrl(origin),method,QUrl(page))
