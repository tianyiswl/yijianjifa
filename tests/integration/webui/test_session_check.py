"""Live-provider checks keep DB/session evidence real and fake only Chromium IO."""
import json
from pathlib import Path
import subprocess
import threading
from types import SimpleNamespace

import pytest

from yijianjifa.features.accounts.service import AccountService
from yijianjifa.features.accounts.session_store import SessionStore


def surface_value(kind):
    from yijianjifa.webui.session_check import SURFACE
    script = r'''const fs=require('node:fs'),p=JSON.parse(fs.readFileSync(0,'utf8'));
    function node(text='',attrs={},children=[]) {return {textContent:text||children.map(c=>c.textContent).join(' '),children,isConnected:true,
      parentElement:null,...attrs,getClientRects(){return [{width:200,height:100}]},
      getAttribute(name){return this[name]??null},querySelectorAll(selector){
        if(selector==='input')return children.filter(x=>x.tag==='input');
        if(selector.includes('password'))return children.filter(x=>x.type==='password');
        if(selector.includes('canvas')||selector.includes('iframe'))return children.filter(x=>x.qr);
        return [];
      }};}
    const title=node('登录视频号助手'),oauth=node('',{id:'wx-oauth-container',qr:true});
    const pass=node('',{tag:'input',type:'password'}),label=node('手机号登录');
    const isChannel=p.kind.startsWith('channels'),isMask=p.kind==='creator-mask';
    const widget=node('',{className:isChannel?'qrcode-area':'login-modal'},isChannel?[title,oauth]:[label,pass]);
    if(p.kind==='channels-hidden')widget.hidden=true;
    for(const child of widget.children)child.parentElement=widget;
    const inputs=isMask?[pass]:[],panels=isMask?[widget]:[],channelPanels=isChannel?[widget]:[];
    const leaves=isChannel?[title,oauth]:isMask?[label,pass]:[node('登录视频号助手'),node('手机号登录')];
    global.getComputedStyle=e=>({visibility:'visible',display:e.hidden?'none':'block',opacity:'1'});
    global.location={origin:isChannel?'https://channels.weixin.qq.com':'https://creator.douyin.com',
      pathname:isChannel?'/platform':'/creator-micro/home'};
    global.document={querySelectorAll(selector){
      if(selector==='body *')return [...channelPanels,...panels,...leaves];
      if(selector==='input')return inputs;
      if(selector.includes('captcha'))return [];
      if(selector.includes('qrcode-area'))return channelPanels;
      if(selector.includes('form')||selector.includes('role="dialog"'))return panels;
      if(selector.includes('password'))return inputs;
      return [];
    }};
    console.log(JSON.stringify(eval('('+p.script+')')()));'''
    output = subprocess.run(['node', '-e', script], input=json.dumps({'script': SURFACE, 'kind': kind}),
                            text=True, capture_output=True, check=True)
    return json.loads(output.stdout)


@pytest.mark.parametrize('kind', ['channels-home', 'creator-mask'])
def test_real_surface_script_detects_explicit_login_widget_without_a_login_route(kind):
    assert surface_value(kind)['login_required'] is True


@pytest.mark.parametrize('kind', ['ordinary-navigation', 'channels-hidden'])
def test_real_surface_script_ignores_login_words_without_visible_login_structure(kind):
    assert surface_value(kind)['login_required'] is False


def configured(platform, engine, paths):
    if platform == 'douyin':
        from yijianjifa.webui import chromium_publish as auth
        return auth.make_chromium_login(engine, paths), auth, '123', 'https://creator.douyin.com/creator-micro/home'
    if platform == 'kuaishou':
        from yijianjifa.webui import kuaishou_auth as auth
        return auth.make_kuaishou_provider(engine, paths), auth, '123', auth.HOME
    if platform == 'bilibili':
        from yijianjifa.webui import bilibili_auth as auth
        return auth.make_bilibili_provider(engine, paths), auth, '123', auth.HOME
    from yijianjifa.webui import channels_auth as auth
    return auth.make_channels_provider(engine, paths), auth, 'finder-123', auth.HOME


def identity(platform, uid):
    if platform == 'douyin':
        return dict(uid=uid, uid_after=uid, nickname='Synthetic account', status=0, profile_status=0, after_status=0)
    origin = {'kuaishou': 'https://cp.kuaishou.com', 'bilibili': 'https://member.bilibili.com', 'channels': 'https://channels.weixin.qq.com'}[platform]
    result = dict(origin=origin, logged_in=True, uid=uid, profile_uid=uid, nickname='Synthetic account')
    if platform == 'channels': result['expired'] = False
    return result


@pytest.fixture(params=['douyin', 'kuaishou', 'bilibili', 'channels'])
def check_fixture(request, db_engine, app_paths, monkeypatch):
    platform = request.param
    provider, auth, uid, home = configured(platform, db_engine, app_paths)
    service = AccountService(db_engine)
    account = service.create(platform, uid, 'Synthetic saved account')
    store = SessionStore(app_paths.data_dir / 'profiles')
    cookie = dict(name='synthetic', value='synthetic-secret', domain=home.split('/')[2], path='/', secure=True)
    archive = dict(cookies=[cookie], origins=[])
    if platform == 'douyin':
        blob = b'synthetic native archive'
        monkeypatch.setattr(auth, 'native_to_chromium', lambda value, expected: [dict(cookie)])
        source = auth.chromium_profile(store, uid)
    elif platform == 'kuaishou':
        blob = auth.encode_session(archive, uid)
        source = auth.chromium_profile(store, uid)
    else:
        from yijianjifa.webui import creator_auth
        blob = creator_auth.encode_session(auth.SPEC, archive, uid)
        source = creator_auth.chromium_profile(auth.SPEC, store, uid)
    ref = service.attach_session(account.id, store, blob)
    (source / 'Default').mkdir()
    (source / 'Default' / 'Cookies').write_bytes(b'original-private-cookie-database')
    (source / '.source-ref').write_text(ref)
    observations = [identity(platform, uid), identity(platform, uid)]
    surface = dict(origin=home.split('/')[0] + '//' + home.split('/')[2], login_required=False, challenge=False)
    events = []
    class Page:
        url = 'about:blank'
        def __init__(self): self.reads = 0; self.closed = False; self.cancel_on_read = False
        def goto(self, url, **kwargs):
            assert url == home
            self.url = url; events.append(('visit', url))
        def evaluate(self, expression, args=None):
            if 'p011ReadOnlySurface' in expression:
                return dict(surface)
            self.reads += 1
            value = observations[min(self.reads - 1, len(observations) - 1)]
            if self.cancel_on_read: cancel.set()
            if isinstance(value, Exception): raise value
            return value
        def is_closed(self): return self.closed
        def wait_for_timeout(self, value): assert 0 < value <= 250
        def locator(self, *args, **kwargs): raise AssertionError('No upload or click is allowed')
        def get_by_role(self, *args, **kwargs): raise AssertionError('No login interaction is allowed')
    page = Page()
    class Context:
        pages = [page]
        def __init__(self): self.closed = False; self.route_handler = None; self.websocket_handler = None
        def set_default_timeout(self, value): assert 0 < value <= 5000
        def route(self, pattern, handler): assert pattern == '**/*'; self.route_handler = handler
        def route_web_socket(self, pattern, handler): assert pattern == '**/*'; self.websocket_handler = handler
        def cookies(self): return [dict(cookie)]
        def add_cookies(self, rows): assert all(row['domain'] == cookie['domain'] for row in rows)
        def add_init_script(self, value): assert 'localStorage' in value
        def close(self):
            self.closed = True; page.closed = True; events.append(('close', None))
    context = Context()
    profiles = []; launch_options = []
    def launch(profile, **options):
        profile = Path(profile)
        assert options['headless'] is True and options['accept_downloads'] is False
        assert profile != source and (profile / 'Default' / 'Cookies').read_bytes() == b'original-private-cookie-database'
        (profile / 'Default' / 'Cookies').write_bytes(b'temporary-rotated-state')
        profiles.append(profile)
        launch_options.append(options)
        return context
    class Driver:
        exclusive = True
        chromium = SimpleNamespace(launch_persistent_context=launch)
        def stop(self): events.append(('stop', None))
    from yijianjifa.webui import playwright_runtime
    monkeypatch.setattr(playwright_runtime, 'acquire_playwright', lambda: Driver())
    # Browser semantics use the exact child probe with fake Chromium IO. The
    # separate process tests exercise this probe via a real fresh interpreter.
    from yijianjifa.webui import session_check
    def inline(request, cancel, probe):
        return probe(SimpleNamespace(**request['account']), Path(request['directory']) / 'browser', cancel, request['deadline']), True
    monkeypatch.setattr(session_check, '_execute_check', inline)
    cancel = threading.Event()
    return SimpleNamespace(platform=platform, provider=provider, account=account, service=service, store=store, ref=ref,
                           source=source, observations=observations, surface=surface, page=page, context=context,
                           profiles=profiles, launch_options=launch_options, events=events, cancel=cancel, uid=uid)


def check(fixture):
    method = getattr(fixture.provider, 'check_session', None)
    assert callable(method), 'Real login providers must expose online read-only session checking'
    result = method(fixture.account.id, fixture.cancel)
    if 'diagnostic' in result:
        from yijianjifa.webui.session_check_diagnostics import sanitize_diagnostic
        assert sanitize_diagnostic(result['diagnostic']) is not None
    # Existing provider semantics stay exact; diagnostics have dedicated tests.
    return {k: v for k, v in result.items() if k != 'diagnostic'}


def test_provider_valid_requires_two_live_matching_observations_and_discards_private_clone(check_fixture):
    f = check_fixture
    f.service.set_login_state(f.account.id, 'expired')
    result = check(f)
    assert result == {'state': 'valid', 'platform_user_id': f.uid}
    assert f.page.reads == 2 and f.context.closed
    # Two independent live page loads are required, not two reads of one store.
    assert [event[0] for event in f.events] == ['visit', 'visit', 'close', 'stop']
    assert len(f.profiles) == 1 and not f.profiles[0].exists()
    assert (f.source / 'Default' / 'Cookies').read_bytes() == b'original-private-cookie-database'
    assert f.service.get(f.account.id).session_ref == f.ref
    assert f.service.get(f.account.id).login_state == 'expired'  # Runtime owns the conditional DB update.
    assert 'secret' not in json.dumps(result) and 'profile:' not in json.dumps(result)


def test_provider_second_live_identity_change_cannot_return_valid(check_fixture):
    f = check_fixture
    f.observations[1] = identity(f.platform, '456' if f.platform != 'channels' else 'finder-456')
    result = check(f)
    assert result['state'] == 'identity_mismatch' and result['platform_user_id'] != f.uid
    assert f.page.reads == 2 and f.context.closed and not f.profiles[0].exists()
    assert f.service.get(f.account.id).login_state == 'valid'


def test_provider_waits_for_live_identity_after_both_independent_navigations(check_fixture):
    f = check_fixture
    good = identity(f.platform, f.uid)
    # Creator SPA state often appears after domcontentloaded; the second load
    # must be allowed to hydrate without losing the first verified identity.
    f.observations[:] = [None, None, None, good, None, None, good]
    assert check(f) == {'state': 'valid', 'platform_user_id': f.uid}
    assert f.page.reads == 7
    assert [event[0] for event in f.events] == ['visit', 'visit', 'close', 'stop']


def test_platform_automatic_navigation_retries_identity_within_original_budget(check_fixture):
    f = check_fixture
    f.observations.insert(0, RuntimeError('Execution context was destroyed, most likely because of a navigation'))
    assert check(f) == {'state': 'valid', 'platform_user_id': f.uid}
    assert f.page.reads == 3 and len([v for v in f.events if v[0] == 'visit']) == 2


def test_live_identity_can_verify_even_when_peripheral_creator_scripts_delay_domcontentloaded(check_fixture, monkeypatch):
    f = check_fixture
    visit = f.page.goto
    def delayed_creator_page(url, **options):
        if options.get('wait_until') != 'commit':
            raise TimeoutError('Peripheral creator scripts never fire DOMContentLoaded')
        return visit(url, **options)
    monkeypatch.setattr(f.page, 'goto', delayed_creator_page)
    assert check(f) == {'state': 'valid', 'platform_user_id': f.uid}
    assert f.page.reads == 2 and len([v for v in f.events if v[0] == 'visit']) == 2


def test_multistep_identity_reads_have_a_bounded_budget_without_changing_other_platforms(check_fixture, monkeypatch):
    f = check_fixture
    evaluate = f.page.evaluate; budgets = []
    def platform_identity(expression, budget):
        if 'p011ReadOnlySurface' not in expression:
            budgets.append(budget)
            if f.platform == 'douyin' and budget <= 4000:
                raise TimeoutError('Three successful sequential identity reads exceed four seconds')
        return evaluate(expression, budget)
    monkeypatch.setattr(f.page, 'evaluate', platform_identity)
    assert check(f) == {'state': 'valid', 'platform_user_id': f.uid}
    assert len(budgets) == 2
    assert all(0 < value <= (8000 if f.platform == 'douyin' else 4000) for value in budgets)


def test_provider_explicit_login_surface_is_confirmed_twice_without_waiting_or_marking_expired(check_fixture):
    f = check_fixture
    f.observations[:] = [None, None]
    f.surface['login_required'] = True
    result = check(f)
    assert result['state'] == 'login_required' and f.page.reads == 2
    assert f.context.closed and not f.profiles[0].exists()
    assert f.service.get(f.account.id).login_state == 'valid'


@pytest.mark.parametrize('problem', ['network', 'challenge', 'unknown', 'cancel'])
def test_provider_unavailable_is_not_inferred_as_expired(check_fixture, problem):
    f = check_fixture
    if problem == 'network': f.observations[:] = [TimeoutError('private token/path')]
    elif problem == 'challenge': f.surface.update(challenge=True, login_required=True)
    elif problem == 'unknown': f.observations[:] = [None]
    else: f.page.cancel_on_read = True
    result = check(f)
    assert result['state'] == 'unavailable' and result['reason']
    assert 'private' not in json.dumps(result) and 'token' not in json.dumps(result)
    assert f.service.get(f.account.id).login_state == 'valid'
    assert f.context.closed and not f.profiles[0].exists()


def test_provider_account_lock_prevents_an_online_check_during_a_live_session(check_fixture):
    f = check_fixture
    from yijianjifa.webui.chromium_publish import account_lock
    lock = account_lock(f.account.id)
    assert lock.acquire(blocking=False)
    try:
        assert check(f) == {'state': 'unavailable', 'reason': 'account_busy'}
        assert f.profiles == [] and f.page.reads == 0
    finally:
        lock.release()


def test_provider_missing_archive_requests_login_without_browser_or_claiming_network_expiry(check_fixture):
    f = check_fixture
    f.store.delete(f.account.id)
    assert check(f) == {'state': 'login_required', 'reason': 'session_missing'}
    assert f.page.reads == 0 and f.profiles == []
    assert f.service.get(f.account.id).login_state == 'valid'


def test_cancel_during_profile_clone_removes_temp_and_releases_account_lock(check_fixture, monkeypatch):
    f = check_fixture
    from yijianjifa.webui import session_check
    from yijianjifa.webui.chromium_publish import account_lock
    clone = session_check._clone
    def cancelled_clone(source, target, cancel, deadline):
        assert target.exists()
        cancel.set()
        return clone(source, target, cancel, deadline)
    monkeypatch.setattr(session_check, '_clone', cancelled_clone)
    assert check(f) == {'state': 'unavailable', 'reason': 'cancelled'}
    assert not list(f.store.root.glob('.' + f.platform + '-check-*'))
    assert f.profiles == []
    lock = account_lock(f.account.id)
    assert lock.acquire(blocking=False)
    lock.release()


def test_close_failure_never_reports_valid_or_leaves_a_lock_after_exclusive_driver_stops(check_fixture, monkeypatch):
    f = check_fixture
    from yijianjifa.webui.chromium_publish import account_lock
    attempts = []
    def fail_close():
        attempts.append(True)
        raise RuntimeError('synthetic close failure')
    monkeypatch.setattr(f.context, 'close', fail_close)
    assert check(f) == {'state': 'unavailable', 'reason': 'cleanup_failed'}
    assert len(attempts) == 2 and not f.profiles[0].exists()
    lock = account_lock(f.account.id)
    assert lock.acquire(blocking=False)
    lock.release()


def test_identity_evaluate_actually_times_out_a_never_resolving_platform_script():
    from yijianjifa.webui.session_check import _evaluate
    import time
    class Page:
        def evaluate(self, script, budget):
            assert 0 < budget <= 40
            runner = r'''const fs=require('node:fs'),p=JSON.parse(fs.readFileSync(0,'utf8'));
            eval('('+p.script+')')(p.budget).then(()=>process.stdout.write('unexpected'))
              .catch(e=>process.stdout.write(e.message));'''
            done = subprocess.run(['node', '-e', runner], input=json.dumps({'script': script, 'budget': budget}),
                                  text=True, capture_output=True, check=True, timeout=2)
            assert done.stdout == 'session-check-timeout'
            raise TimeoutError(done.stdout)
    with pytest.raises(TimeoutError, match='session-check-timeout'):
        _evaluate(Page(), 'async () => await new Promise(() => {})', threading.Event(), time.monotonic() + .04)


def test_navigation_retry_preserves_the_original_deadline(monkeypatch):
    from yijianjifa.webui import session_check
    now = [100.0]; budgets = []
    monkeypatch.setattr(session_check.time, 'monotonic', lambda: now[0])
    class Page:
        def evaluate(self, script, budget):
            budgets.append(budget); now[0] += 1
            raise RuntimeError('Execution context was destroyed, most likely because of a navigation')
        def is_closed(self): return False
        def wait_for_timeout(self, value): raise AssertionError('Expired deadline cannot schedule another attempt')
    with pytest.raises(session_check._Unavailable) as error:
        session_check._evaluate(Page(), '() => null', threading.Event(), 100.5)
    assert error.value.reason == 'timeout' and budgets == [500]


def test_provider_readonly_route_blocks_upload_publish_and_web_writes(check_fixture):
    f = check_fixture
    assert check(f)['state'] == 'valid'
    assert f.context.route_handler is not None
    outcomes = []
    class Route:
        def __init__(self, method): self.request = SimpleNamespace(method=method, url='https://channels.weixin.qq.com/cgi-bin/mmfinderassistant-bin/post/post_create')
        def abort(self, *args): outcomes.append('blocked')
        def continue_(self): outcomes.append('allowed')
    for method in ('POST', 'PUT', 'PATCH', 'DELETE', 'GET', 'HEAD'):
        f.context.route_handler(Route(method))
    assert outcomes == ['blocked', 'blocked', 'blocked', 'blocked', 'allowed', 'allowed']


def test_readonly_check_blocks_service_workers_and_websocket_write_bypasses(check_fixture):
    f = check_fixture
    assert check(f)['state'] == 'valid'
    assert f.launch_options[0].get('service_workers') == 'block'
    assert callable(f.context.websocket_handler)
    class Socket:
        def close(self): raise AssertionError('Sync close inside the SDK dispatcher would block its event loop')
        def connect_to_server(self): raise AssertionError('Read-only check cannot open a websocket tunnel')
    assert f.context.websocket_handler(Socket()) is None


def test_only_channels_exact_readonly_identity_post_is_permitted(check_fixture):
    f = check_fixture
    assert check(f)['state'] == 'valid'
    outcomes = []
    class Route:
        def __init__(self, url): self.request = SimpleNamespace(method='POST', url=url)
        def abort(self): outcomes.append(False)
        def continue_(self): outcomes.append(True)
    # Official 404.b0af01fb.js getAuthData POSTs only auth/auth_data. Adjacent
    # actions, uploads, host suffixes, encoded paths and method overrides fail.
    urls = [
        'https://channels.weixin.qq.com/cgi-bin/mmfinderassistant-bin/auth/auth_data',
        'https://channels.weixin.qq.com/cgi-bin/mmfinderassistant-bin/auth/auth_data?_aid=synthetic',
        'http://channels.weixin.qq.com/cgi-bin/mmfinderassistant-bin/auth/auth_data',
        'https://channels.weixin.qq.com.evil.invalid/cgi-bin/mmfinderassistant-bin/auth/auth_data',
        'https://channels.weixin.qq.com/cgi-bin/mmfinderassistant-bin/auth/auth_data/extra',
        'https://channels.weixin.qq.com/cgi-bin/mmfinderassistant-bin/auth/auth_update_switch',
        'https://channels.weixin.qq.com/cgi-bin/mmfinderassistant-bin/notification/notification_read',
        'https://channels.weixin.qq.com/cgi-bin/mmfinderassistant-bin/post/post_create',
        'https://channels.weixin.qq.com/cgi-bin/mmfinderassistant-bin/auth/%61uth_data',
    ]
    for url in urls: f.context.route_handler(Route(url))
    assert outcomes == ([True, True] if f.platform == 'channels' else [False, False]) + [False] * 7


def test_current_platform_readonly_bootstrap_identity_routes_are_exact(check_fixture):
    f = check_fixture
    assert check(f)['state'] == 'valid'
    outcomes = []
    class Route:
        def __init__(self, url): self.request = SimpleNamespace(method='POST', url=url)
        def abort(self): outcomes.append(False)
        def continue_(self): outcomes.append(True)
    for url in [
        'https://channels.weixin.qq.com/cgi-bin/mmfinderassistant-bin/auth/get_auth_info',
        'https://channels.weixin.qq.com/cgi-bin/mmfinderassistant-bin/auth/auth_with_external',
        'https://cp.kuaishou.com/rest/v2/creator/pc/authority/account/current',
        'https://cp.kuaishou.com/rest/v2/creator/pc/authority/account/current/extra',
        'https://cp.kuaishou.com/rest/pc/home/role',
        'https://cp.kuaishou.com/rest/pc/home/role/clear',
    ]: f.context.route_handler(Route(url))
    assert outcomes == [f.platform == 'channels', False, f.platform == 'kuaishou', False, False, False]
