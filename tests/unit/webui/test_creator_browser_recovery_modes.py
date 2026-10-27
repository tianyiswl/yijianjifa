"""All creator adapters honor one explicit same-profile recovery policy."""
from types import SimpleNamespace
import pytest

from yijianjifa.features.accounts.service import AccountService, AccountRow
from yijianjifa.storage.db import session_scope
from yijianjifa.webui.chromium_publish_session import LiveSession
from yijianjifa.webui import channels_multi_publish, chromium_multi_publish, bilibili_multi_publish, kuaishou_multi_publish


ADAPTERS = [
    ('channels', channels_multi_publish.ChannelsMultiPublishAdapter),
    ('douyin', chromium_multi_publish.ChromiumMultiPublishAdapter),
    ('bilibili', bilibili_multi_publish.BilibiliMultiPublishAdapter),
    ('kuaishou', kuaishou_multi_publish.KuaishouMultiPublishAdapter),
]


@pytest.fixture(params=ADAPTERS, ids=[value[0] for value in ADAPTERS])
def creator(request, db_engine, app_paths, monkeypatch):
    platform, factory = request.param
    account = AccountService(db_engine).create(platform, '123', '合成创作者')
    with session_scope(db_engine) as db:
        row = db.get(AccountRow, account.id)
        row.session_ref = 'synthetic-archive'; row.login_state = 'valid'
    launches, restores, registrations = [], [], []
    storage = {'login': 'fresh-persistent-profile'}
    faults = {'restore': False}
    class Page:
        def __init__(self): self.closed = False; self.url = 'about:blank'
        def goto(self, url, **kwargs): self.url = url
        def on(self, event, callback): registrations.append(event)
        def add_init_script(self, *args): pass
        def is_closed(self): return self.closed
        def bring_to_front(self): pass
        def screenshot(self, **kwargs): return b'synthetic-frame'
    class Context:
        def __init__(self): self.pages = [Page()]; self.guards = []
        def set_default_timeout(self, timeout): pass
        def route(self, pattern, callback): self.guards.append((pattern, callback))
        def close(self): self.pages[0].closed = True
    def launch(profile, **options):
        launches.append((profile, options)); return Context()
    def restore(*args):
        restores.append(args)
        if faults['restore']: raise ValueError('synthetic restore failure')
        storage['login'] = 'old-archive'
    if platform in {'channels', 'bilibili'}:
        monkeypatch.setattr(channels_multi_publish.creator_auth, 'restore_session', restore)
    elif platform == 'douyin':
        monkeypatch.setattr(chromium_multi_publish.legacy, 'restore_account_session', restore)
    else:
        monkeypatch.setattr(kuaishou_multi_publish.auth, 'restore_session', restore)
    adapter = factory(db_engine, app_paths)
    adapter._thread()
    adapter.driver = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))
    s = LiveSession({'account_id': account.id, 'platform': platform, 'platform_user_id': '123'},
                    {'delivery_id': 'synthetic-target', 'attempt': 1, 'digest': 'synthetic-digest'})
    try:
        yield SimpleNamespace(adapter=adapter, session=s, launches=launches, restores=restores,
                              storage=storage, faults=faults, registrations=registrations)
    finally: adapter._release_page(s)


def test_fresh_publication_still_uses_background_and_restores_its_frozen_archive(creator):
    c = creator; c.adapter._open(c.session)
    assert c.launches[0][1]['headless'] is True
    assert len(c.restores) == 1 and c.storage['login'] == 'old-archive'
    assert c.session.original_ref == 'synthetic-archive' and c.session.page is not None
    assert c.session.context.guards and not c.session.external_browser


def test_explicit_login_recovery_opens_a_real_window_with_guards_and_frozen_account(creator):
    c = creator; c.session.browser_login_visible = True
    c.adapter._open(c.session)
    assert c.launches[0][1]['headless'] is False
    assert c.session.browser_login_context_headed and c.session.external_browser
    assert c.session.context.guards and c.session.payload['platform_user_id'] == '123'
    assert not c.session.resume_authorized and not c.session.uploaded and not c.session.final


def test_reopening_a_profile_never_overwrites_new_login_with_an_old_archive(creator):
    c = creator
    c.session.browser_login_visible = c.session.browser_login_preserve_profile = True
    c.adapter._open(c.session)
    assert c.restores == [] and c.storage['login'] == 'fresh-persistent-profile'
    assert c.launches[0][1]['headless'] is False
    assert c.session.original_ref == 'synthetic-archive' and c.session.page is not None


def test_failed_fresh_archive_restore_cannot_publish_a_partially_initialized_page(creator):
    c = creator; c.faults['restore'] = True
    with pytest.raises(ValueError, match='synthetic restore failure'):
        c.adapter._open(c.session)
    assert c.session.page is None and c.session.original_ref is None
    assert c.session.context is not None and c.session.locked


@pytest.mark.parametrize('inspection,uploaded,final', [(True, False, False), (False, True, False), (False, True, True)])
def test_protected_sessions_cannot_be_replaced_by_login_recovery(creator, inspection, uploaded, final):
    c = creator; c.adapter._open(c.session)
    c.adapter.inspection_only = inspection
    c.session.uploaded, c.session.final = uploaded, final
    original = c.session.context
    assert c.adapter._show_intervention_browser(c.session) is False
    assert c.session.context is original and len(c.launches) == 1
