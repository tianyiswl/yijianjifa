"""User-controlled creator login, separate from OAuth and publishing capability.

The official creator page's Vue store fields were checked against its public
2026-10-07 assets. Read existing page state only; never request private endpoints,
upload media, solve challenges, or treat a nickname/cookie as account identity.
"""
import json
import math
import re
import shutil
import tempfile
import time
from pathlib import Path
from yijianjifa.features.accounts.service import AccountIdentityMismatch

PLATFORM = 'kuaishou'
HOME = 'https://cp.kuaishou.com/profile'
DOMAINS = frozenset({'kuaishou.com', 'cp.kuaishou.com', 'passport.kuaishou.com'})
ORIGINS = frozenset({'https://cp.kuaishou.com', 'https://passport.kuaishou.com'})

# cp.63021c43.js mounts #app; 6551.e689a430.js binds userId/userName/logined
# to state.user. The header's profile card separately loads personalInfo.userId.
# Keep the extraction narrow; do not export the store or any credential fields.
IDENTITY_SCRIPT = r'''() => {
 if (location.origin !== 'https://cp.kuaishou.com') return null;
 const root = document.querySelector('#app')?.__vue__;
 const user = root?.$store?.state?.user;
 if (!user || user.logined !== true) return null;
 const id = value => typeof value === 'string' ? value :
   (Number.isSafeInteger(value) && value > 0 ? String(value) : null);
 const uid = id(user.userId), nickname = user.userName;
 if (!uid || typeof nickname !== 'string' || !nickname.trim()) return null;
 const queue = root ? [root] : [], seen = new Set();
 let profile = null, header = null;
 while (queue.length && seen.size < 500) {
   const vm = queue.shift(); if (!vm || seen.has(vm)) continue; seen.add(vm);
   if (vm.$options?.name === 'header-info-card') profile = vm;
   if (vm.userLogined === true && vm.userName === nickname && vm.$el?.isConnected) header = vm;
   if (Array.isArray(vm.$children)) queue.push(...vm.$children);
 }
 const info = profile?.personalInfo;
 if (info && id(info.userId) && id(info.userId) !== uid) return null;
 if (info && id(info.userId) && info.userName !== nickname) return null;
 if (!header && !(info && id(info.userId) === uid)) return null;
 return {origin:location.origin, logged_in:true, uid, nickname,
         profile_uid:info && id(info.userId) ? id(info.userId) : uid,
         avatar_url:typeof user.userAvatar === 'string' && user.userAvatar.length <= 4096 ? user.userAvatar : ''};
}'''


from .loaded_avatar import _avatar_url, LoadedAvatarCache


def verified_identity(value, expected_uid=None):
    if not isinstance(value, dict) or value.get('origin') != 'https://cp.kuaishou.com' or value.get('logged_in') is not True:
        raise ValueError('identity unavailable')
    uid = value.get('uid'); name = value.get('nickname')
    if (type(uid) is not str or not re.fullmatch(r'[1-9][0-9]{0,31}', uid)
            or value.get('profile_uid') != uid
            or type(name) is not str or not 1 <= len(name.strip()) <= 255):
        raise ValueError('identity unavailable or changed')
    if expected_uid is not None and expected_uid != uid:
        raise AccountIdentityMismatch('account identity mismatch')
    return uid, name.strip()


def _validate_state(state):
    if type(state) is not dict or set(state) != {'cookies', 'origins'}:
        raise ValueError('invalid session state')
    cookies = state['cookies']; origins = state['origins']
    if type(cookies) is not list or not 1 <= len(cookies) <= 500 or type(origins) is not list or len(origins) > len(ORIGINS):
        raise ValueError('invalid session state')
    seen = set()
    for cookie in cookies:
        keys = {'name', 'value', 'domain', 'path', 'expires', 'httpOnly', 'secure', 'sameSite', 'partitionKey'}
        if (type(cookie) is not dict or set(cookie) - keys
                or not all(type(cookie.get(k)) is str for k in ('name', 'value', 'domain', 'path'))
                or cookie['domain'].lstrip('.').lower() not in DOMAINS
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
        if 'partitionKey' in cookie and cookie['partitionKey'] not in ORIGINS:
            raise ValueError('invalid session partition')
    seen_origins = set()
    for item in origins:
        if (type(item) is not dict or set(item) != {'origin', 'localStorage'}
                or item['origin'] not in ORIGINS or item['origin'] in seen_origins
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


def encode_session(state, uid):
    verified_identity(dict(origin='https://cp.kuaishou.com', logged_in=True, uid=uid, profile_uid=uid, nickname='identity'))
    scoped = dict(cookies=[dict(c) for c in state['cookies'] if c.get('domain', '').lstrip('.').lower() in DOMAINS],
                  origins=[dict(o) for o in state['origins'] if o.get('origin') in ORIGINS])
    payload = dict(version=1, platform=PLATFORM, identity=uid, state=_validate_state(scoped))
    blob = json.dumps(payload, separators=(',', ':'), allow_nan=False).encode('utf-8')
    if len(blob) > 10 * 1024 * 1024: raise ValueError('session too large')
    return blob


def decode_session(blob, uid):
    if type(blob) is not bytes or not 1 <= len(blob) <= 10 * 1024 * 1024:
        raise ValueError('invalid session archive')
    def unique_pairs(pairs):
        value = {}
        for key, item in pairs:
            if key in value: raise ValueError('duplicate archive key')
            value[key] = item
        return value
    value = json.loads(blob, object_pairs_hook=unique_pairs)
    if (type(value) is not dict or set(value) != {'version', 'platform', 'identity', 'state'}
            or type(value['version']) is not int or value['version'] != 1
            or value['platform'] != PLATFORM or value['identity'] != uid):
        raise ValueError('session identity or platform mismatch')
    return _validate_state(value['state'])


def chromium_profile(store, uid):
    verified_identity(dict(origin='https://cp.kuaishou.com', logged_in=True, uid=uid, profile_uid=uid, nickname='identity'))
    directory = store.ensure_profile_dir('chromium:kuaishou:' + uid) / 'browser'
    store._private_directory(directory)
    return directory


def restore_session(context, store, account, profile):
    if not account.session_ref: return
    blob = store.get(account.id, ref=account.session_ref)
    if not blob: return
    state = decode_session(blob, account.platform_user_id)
    marker = profile / '.source-ref'
    same_source = marker.is_file() and marker.read_text() == account.session_ref
    rows = state['cookies']
    if same_source:
        present = {(c['name'], c['domain'], c['path']) for c in context.cookies()}
        rows = [c for c in rows if (c['name'], c['domain'], c['path']) not in present]
    if rows: context.add_cookies(rows)
    # Preserve rotated values from the live profile when replaying the same
    # archive. Restrict localStorage restoration to two explicit HTTPS origins.
    script = '''(() => { const origins = %s, overwrite = %s, source = %s;
      const data = origins.find(item => item.origin === location.origin);
      const marker = '__p011_kuaishou_source_ref';
      if (data && localStorage.getItem(marker) !== source) {
        for (const row of data.localStorage) {
          if (row.name !== marker && (overwrite || localStorage.getItem(row.name) === null)) localStorage.setItem(row.name, row.value);
        }
        localStorage.setItem(marker, source);
      }
    })();''' % (json.dumps(state['origins']), 'false' if same_source else 'true', json.dumps(account.session_ref))
    context.add_init_script(script)
    marker.write_text(account.session_ref); marker.chmod(0o600)


def observe_session(page, context, expected_uid=None, *, avatar_reader=None):
    identity=page.evaluate(IDENTITY_SCRIPT)
    uid, name = verified_identity(identity, expected_uid)
    avatar=avatar_reader(identity.get('avatar_url','')) if avatar_reader else ''
    blob = encode_session(context.storage_state(), uid)
    current=page.evaluate(IDENTITY_SCRIPT)
    again, current_name = verified_identity(current, uid)
    result=dict(platform_user_id=again, nickname=current_name, session_blob=blob)
    if avatar and _avatar_url(identity.get('avatar_url'))==_avatar_url(current.get('avatar_url')):
        result['avatar_data']=avatar
    return result


def make_kuaishou_provider(engine, paths):
    from yijianjifa.features.accounts.service import AccountService
    from yijianjifa.features.accounts.session_store import SessionStore
    from .chromium_publish import account_lock, browser_options
    service = AccountService(engine); store = SessionStore(paths.data_dir / 'profiles')

    def run(account_id, cancel, *, keep_open=False):
        if keep_open and not account_id: raise ValueError('backend requires existing account')
        from playwright.sync_api import sync_playwright
        # Configure the bundled browser path before the Node driver snapshots
        # its environment. Frozen Playwright otherwise defaults to .local-browsers.
        options = browser_options('visible')
        account = service.get(account_id) if account_id else None
        if account and account.platform != PLATFORM: raise ValueError('account platform mismatch')
        expected = account.platform_user_id if account else None
        lock = account_lock(account_id or 'kuaishou:new-account')
        if not lock.acquire(blocking=False): raise ValueError('account busy')
        temp = None; context = None; result = None
        try:
            if account: profile = chromium_profile(store, expected)
            else:
                temp = Path(tempfile.mkdtemp(prefix='.kuaishou-login-', dir=store.root))
                temp.chmod(0o700); profile = temp
            with sync_playwright() as pw:
                context = pw.chromium.launch_persistent_context(str(profile), **options)
                context.set_default_timeout(5000)
                if account: restore_session(context, store, account, profile)
                page = context.pages[0] if context.pages else context.new_page()
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
                page.goto(HOME, wait_until='domcontentloaded', timeout=30000)
                deadline = time.monotonic() + 600
                clicked_login = False
                while not cancel.is_set() and not page.is_closed() and (result is not None or time.monotonic() < deadline):
                    try:
                        result = observe_session(page, context, expected, avatar_reader=read_avatar)
                        if cancel.is_set():
                            result = None
                            break
                        if not keep_open: break
                    except AccountIdentityMismatch: raise
                    except ValueError: pass
                    except Exception:
                        if page.is_closed(): break
                    if not clicked_login and page.url == HOME:
                        link = page.get_by_role('link', name='立即登录', exact=True)
                        if link.count() == 1 and link.is_visible():
                            link.click(); clicked_login = True
                    try: page.wait_for_timeout(750)
                    except Exception: break
                try: context.close()
                except Exception:
                    if not page.is_closed(): raise
                context = None
            # A new account's browser remains temporary. Only LoginFlow may
            # persist its archive after the final cancellation/identity checks.
            # A later relogin restores that accepted archive into its own profile.
            return None if cancel.is_set() else result
        finally:
            if context:
                try: context.close()
                except Exception: pass
            if temp: shutil.rmtree(temp, ignore_errors=True)
            lock.release()
    def login(account_id, cancel):
        return run(account_id, cancel)
    def open_backend(account_id, cancel):
        # Viewing a saved account may refresh its last verified identity and
        # session, but never creates an account or checks publishing capability.
        return run(account_id, cancel, keep_open=True)
    login.open_backend = open_backend
    from .session_check import make_session_checker
    login.check_session = make_session_checker(engine, paths, platform=PLATFORM, home=HOME, origins=ORIGINS,
        profile_for=chromium_profile, restore=restore_session, expression=IDENTITY_SCRIPT,
        verify=lambda value: verified_identity(value, None),
        # Official 6551.e689a430.js bootstrap reads the current account with
        # an empty POST. Role changes and posting use separate blocked paths.
        readonly_posts=frozenset({('cp.kuaishou.com', '/rest/v2/creator/pc/authority/account/current')}))
    return login
