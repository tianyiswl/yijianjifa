"""Isolated creator-page login lifecycle, with platform-specific identity checks.

Login evidence does not imply posting capability. Session archives are scoped
by platform and stable identity, and are persisted only by LoginFlow.
"""
import json
import math
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable
from urllib.parse import urlsplit
from .loaded_avatar import _avatar_url, LoadedAvatarCache
from yijianjifa.features.accounts.service import AccountIdentityMismatch


@dataclass(frozen=True)
class CreatorAuthSpec:
    platform: str
    home: str
    domains: frozenset[str]
    origins: frozenset[str]
    identity_script: str
    verify_identity: Callable
    return_home_origins: frozenset[str] = frozenset()
    readonly_identity_posts: frozenset[tuple[str, str]] = frozenset()


def _identity(uid):
    if type(uid) is not str or not 1 <= len(uid) <= 255 or any(c.isspace() or ord(c)<32 or ord(c)==127 for c in uid):
        raise ValueError('invalid account identity')
    return uid


def _validate_state(spec, state):
    if type(state) is not dict or set(state) != {'cookies', 'origins'}:
        raise ValueError('invalid session state')
    cookies = state['cookies']; origins = state['origins']
    if type(cookies) is not list or not 1 <= len(cookies) <= 500 or type(origins) is not list or len(origins) > len(spec.origins):
        raise ValueError('invalid session state')
    seen = set()
    for cookie in cookies:
        keys = {'name', 'value', 'domain', 'path', 'expires', 'httpOnly', 'secure', 'sameSite', 'partitionKey'}
        if (type(cookie) is not dict or set(cookie) - keys
                or not all(type(cookie.get(k)) is str for k in ('name', 'value', 'domain', 'path'))
                or cookie['domain'].lstrip('.').lower() not in spec.domains
                or not cookie['name'] or not cookie['path'].startswith('/')
                or any(len(cookie[k]) > 65536 for k in ('name', 'value', 'domain', 'path'))):
            raise ValueError('invalid session cookie')
        key = tuple(cookie[k] for k in ('name', 'domain', 'path'))
        if key in seen: raise ValueError('duplicate session cookie')
        seen.add(key)
        for flag in ('httpOnly', 'secure'):
            if flag in cookie and type(cookie[flag]) is not bool: raise ValueError('invalid session cookie')
        if 'expires' in cookie and (type(cookie['expires']) not in (int, float) or not math.isfinite(cookie['expires'])):
            raise ValueError('invalid session cookie')
        if 'sameSite' in cookie and cookie['sameSite'] not in {'Strict', 'Lax', 'None'}:
            raise ValueError('invalid session cookie')
        if 'partitionKey' in cookie and cookie['partitionKey'] not in spec.origins:
            raise ValueError('invalid session partition')
    seen_origins = set()
    for item in origins:
        if (type(item) is not dict or set(item) != {'origin', 'localStorage'}
                or item['origin'] not in spec.origins or item['origin'] in seen_origins
                or type(item['localStorage']) is not list or len(item['localStorage']) > 500):
            raise ValueError('invalid session origin')
        seen_origins.add(item['origin']); names = set()
        for row in item['localStorage']:
            if (type(row) is not dict or set(row) != {'name', 'value'}
                    or any(type(row[k]) is not str or len(row[k]) > 262144 for k in ('name', 'value'))
                    or row['name'] in names):
                raise ValueError('invalid session storage')
            names.add(row['name'])
    return state


def encode_session(spec, state, uid):
    _identity(uid)
    scoped = dict(cookies=[dict(c) for c in state['cookies'] if c.get('domain', '').lstrip('.').lower() in spec.domains],
                  origins=[dict(o) for o in state['origins'] if o.get('origin') in spec.origins])
    payload = dict(version=1, platform=spec.platform, identity=uid, state=_validate_state(spec, scoped))
    blob = json.dumps(payload, separators=(',', ':'), allow_nan=False).encode('utf-8')
    if len(blob) > 10 * 1024 * 1024:raise ValueError('session too large')
    return blob


def decode_session(spec, blob, uid):
    _identity(uid)
    if type(blob) is not bytes or not 1 <= len(blob) <= 10 * 1024 * 1024:raise ValueError('invalid session archive')
    def unique_pairs(pairs):
        value={}
        for key,item in pairs:
            if key in value:raise ValueError('duplicate archive key')
            value[key]=item
        return value
    value=json.loads(blob,object_pairs_hook=unique_pairs)
    if (type(value) is not dict or set(value)!={'version','platform','identity','state'}
            or type(value['version']) is not int or value['version']!=1
            or value['platform']!=spec.platform or value['identity']!=uid):
        raise ValueError('session identity or platform mismatch')
    return _validate_state(spec,value['state'])


def chromium_profile(spec, store, uid):
    directory=store.ensure_profile_dir('chromium:'+spec.platform+':'+_identity(uid))/'browser'
    store._private_directory(directory)
    return directory


def restore_session(spec, context, store, account, profile):
    if not account.session_ref:return
    blob=store.get(account.id,ref=account.session_ref)
    if not blob:return
    state=decode_session(spec,blob,account.platform_user_id)
    marker=profile/'.source-ref'
    same_source=marker.is_file() and marker.read_text()==account.session_ref
    rows=state['cookies']
    if same_source:
        present={(c['name'],c['domain'],c['path']) for c in context.cookies()}
        rows=[c for c in rows if (c['name'],c['domain'],c['path']) not in present]
    if rows:context.add_cookies(rows)
    script='''(() => { const origins = %s, overwrite = %s, source = %s;
      const data = origins.find(item => item.origin === location.origin);
      const marker = '__p011_creator_source_ref';
      if (data && localStorage.getItem(marker) !== source) {
        for (const row of data.localStorage) {
          if (row.name !== marker && (overwrite || localStorage.getItem(row.name) === null)) localStorage.setItem(row.name, row.value);
        }
        localStorage.setItem(marker, source);
      }
    })();''' % (json.dumps(state['origins']), 'false' if same_source else 'true', json.dumps(account.session_ref))
    context.add_init_script(script)
    marker.write_text(account.session_ref);marker.chmod(0o600)


_LOCAL_STORAGE_SNAPSHOT = '''expectedOrigin => {
  if (location.origin !== expectedOrigin) return null;
  const rows = [];
  for (let i = 0; i < localStorage.length; ++i) {
    const name = localStorage.key(i);
    if (name === null) continue;
    const value = localStorage.getItem(name);
    if (value !== null) rows.push({name, value});
  }
  return {origin: location.origin, localStorage: rows};
}'''


def capture_session_state(spec, context, page=None):
    """Read session state without visiting origins or creating temporary pages.

    Playwright's storage_state() can navigate hidden helper pages to previously
    visited origins. Login monitoring must leave the user's current pages alone.
    Only cookies and localStorage of already-open, allowed documents are read;
    observe_session still binds this snapshot to identity checks on both sides.
    """
    cookies=context.cookies()
    if type(cookies) is not list:raise ValueError('invalid session cookies')
    scoped=[dict(c) for c in cookies if type(c) is dict
            and type(c.get('domain')) is str
            and c['domain'].lstrip('.').lower() in spec.domains]
    pages=list(context.pages)
    if page is not None:pages=[page]+[p for p in pages if p is not page]
    origins=[];seen=set()
    for current in pages:
        try:
            if current.is_closed():continue
            parsed=urlsplit(current.url)
            origin=parsed.scheme+'://'+parsed.netloc
            if parsed.scheme!='https' or origin not in spec.origins or origin in seen:continue
            item=current.evaluate(_LOCAL_STORAGE_SNAPSHOT,origin)
        except Exception:
            # Closing or navigating an existing page cannot trigger a visit to
            # recover its storage. The persistent profile retains that origin.
            continue
        if item is None:continue
        if type(item) is not dict or item.get('origin')!=origin:
            raise ValueError('session origin changed')
        origins.append(item);seen.add(origin)
    return _validate_state(spec,dict(cookies=scoped,origins=origins))


def observe_session(spec, page, context, expected_uid=None, *, avatar_reader=None):
    identity=page.evaluate(spec.identity_script)
    uid,name=spec.verify_identity(identity,expected_uid)
    avatar=avatar_reader(identity.get('avatar_url','')) if avatar_reader else ''
    blob=encode_session(spec,capture_session_state(spec,context,page),uid)
    current=page.evaluate(spec.identity_script)
    again,current_name=spec.verify_identity(current,uid)
    result=dict(platform_user_id=again,nickname=current_name,session_blob=blob)
    if avatar and _avatar_url(identity.get('avatar_url'))==_avatar_url(current.get('avatar_url')):
        result['avatar_data']=avatar
    return result


def make_creator_provider(engine, paths, spec):
    from yijianjifa.features.accounts.service import AccountService
    from yijianjifa.features.accounts.session_store import SessionStore
    from .chromium_publish import account_lock,browser_options
    service=AccountService(engine);store=SessionStore(paths.data_dir/'profiles')

    def run(account_id,cancel,*,keep_open=False):
        if keep_open and not account_id:raise ValueError('backend requires existing account')
        from playwright.sync_api import sync_playwright
        if cancel.is_set():return None
        options=browser_options('visible')
        account=service.get(account_id) if account_id else None
        if account and account.platform!=spec.platform:raise ValueError('account platform mismatch')
        expected=account.platform_user_id if account else None
        lock=account_lock(account_id or spec.platform+':new-account')
        if not lock.acquire(blocking=False):raise ValueError('account busy')
        temp=None;context=None;result=None
        try:
            if account:profile=chromium_profile(spec,store,expected)
            else:
                temp=Path(tempfile.mkdtemp(prefix='.'+spec.platform+'-login-',dir=store.root))
                temp.chmod(0o700);profile=temp
            with sync_playwright() as pw:
                context=pw.chromium.launch_persistent_context(str(profile),**options)
                context.set_default_timeout(5000)
                if account:restore_session(spec,context,store,account,profile)
                page=context.pages[0] if context.pages else context.new_page()
                avatars=LoadedAvatarCache(page.main_frame)
                page.on('requestfinished',avatars.capture)
                def read_avatar(url):
                    if not _avatar_url(url):return ''
                    stop=time.monotonic()+2
                    while not cancel.is_set() and not page.is_closed() and time.monotonic()<stop:
                        data=avatars.data_for(url)
                        if data is not None:return data
                        page.wait_for_timeout(100)
                    return ''
                page.goto(spec.home,wait_until='domcontentloaded',timeout=30000)
                deadline=time.monotonic()+600
                returned_url=None;returns=0
                while not cancel.is_set() and not page.is_closed() and (result is not None or time.monotonic()<deadline):
                    try:
                        result=observe_session(spec,page,context,expected,avatar_reader=read_avatar)
                        if cancel.is_set():
                            result=None;break
                        if not keep_open:break
                    except AccountIdentityMismatch:raise
                    except ValueError:pass
                    except Exception:
                        if page.is_closed():break
                    # B站's official login can land on its public home page.
                    # Return to the creator page for the actual identity check;
                    # the landing page itself never counts as logged in.
                    current_url=getattr(page,'url','')
                    parsed=urlsplit(current_url)
                    origin=parsed.scheme+'://'+parsed.netloc
                    if origin in spec.return_home_origins:
                        if current_url!=returned_url and returns<3:
                            returned_url=current_url;returns+=1
                            page.goto(spec.home,wait_until='domcontentloaded',timeout=30000)
                            continue
                    else:returned_url=None
                    try:page.wait_for_timeout(750)
                    except Exception:break
                try:context.close()
                except Exception:
                    # A user can close the persistent browser before the
                    # worker's cleanup call. Only its last verified observation
                    # may survive that already-closed error.
                    if not page.is_closed():raise
                context=None
            return None if cancel.is_set() else result
        finally:
            if context:
                try:context.close()
                except Exception:pass
            if temp:shutil.rmtree(temp,ignore_errors=True)
            lock.release()
    def login(account_id,cancel):return run(account_id,cancel)
    def open_backend(account_id,cancel):
        # Normal creator navigation, with no upload or posting capability probe.
        # LoginFlow still owns the final same-account/reference save guard.
        return run(account_id,cancel,keep_open=True)
    login.open_backend=open_backend
    from .session_check import make_session_checker
    login.check_session=make_session_checker(engine,paths,platform=spec.platform,home=spec.home,origins=spec.origins,
        profile_for=lambda store,uid:chromium_profile(spec,store,uid),
        restore=lambda context,store,account,profile:restore_session(spec,context,store,account,profile),
        expression=spec.identity_script,verify=lambda value:spec.verify_identity(value,None),
        readonly_posts=spec.readonly_identity_posts)
    return login
