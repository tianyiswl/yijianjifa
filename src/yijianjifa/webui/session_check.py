"""Bounded, read-only online identity checks in disposable account profiles.

No check persists cookies or changes the accepted account archive. The native
LoginRuntime owns any conditional login-state update after version validation.
"""
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
from urllib.parse import urlsplit

from yijianjifa.features.accounts.service import AccountService
from yijianjifa.features.accounts.session_store import SessionStore
from .session_check_diagnostics import CheckDiagnostics, error_kind, sanitize_diagnostic

CHECK_SECONDS = 25
_RETAINED = []  # An unconfirmed owned process tree retains its profile and lock.
_SKIP = {'SingletonCookie', 'SingletonLock', 'SingletonSocket', 'Cache', 'Code Cache', 'GPUCache',
         'Crashpad', 'BrowserMetrics', 'ShaderCache', 'GrShaderCache', 'DawnCache'}

SURFACE = r'''function p011ReadOnlySurface() {
 const visible=e=>{
   if(!e?.isConnected||!Array.from(e.getClientRects()).some(r=>r.width>0&&r.height>0))return false;
   for(let node=e;node;node=node.parentElement){
     const s=getComputedStyle(node);
     if(s.display==='none'||['hidden','collapse'].includes(s.visibility)||Number(s.opacity)<=0)return false;
   }
   return true;
 };
 const nodes=Array.from(document.querySelectorAll('body *')).slice(0,20000).filter(visible);
 const text=nodes.filter(e=>e.children.length===0).map(e=>(e.textContent||'').trim());
 // Bili keeps risk-captcha-adapt on BODY even without a verification widget.
 const complex=Array.from(document.querySelectorAll('iframe,[class*="captcha"],[id*="captcha"],[class*="geetest"]'))
   .some(e=>!['HTML','BODY'].includes(e.tagName)&&visible(e)&&Array.from(e.getClientRects()).some(r=>r.width>=40&&r.height>=20)&&
     /captcha|geetest|验证/i.test([e.src,e.id,e.className,e.title].join(' ')));
 const sms=Array.from(document.querySelectorAll('input')).some(e=>visible(e)&&/验证码/.test([e.placeholder,e.getAttribute('aria-label')].join(' ')));
 const loginPath=/\/login(?:\/|[-.]|$)|\/passport(?:\/|$)/.test(location.pathname);
 const loginText=text.some(t=>['扫码登录','微信扫码登录','手机号登录','登录哔哩哔哩','密码登录'].includes(t));
 const password=Array.from(document.querySelectorAll('input[type="password"]')).some(visible);
 const loginWords=/扫码登录|微信快捷登录|微信扫码登录|手机号登录|密码登录|登录视频号助手|登录哔哩哔哩/;
 const qr=e=>Array.from(e.querySelectorAll('canvas,iframe,img[class*="qrcode"],img[class*="qr-code"],[id="wx-oauth-container"]'))
   .some(n=>visible(n)&&Array.from(n.getClientRects()).some(r=>r.width>=80&&r.height>=80));
 const panels=Array.from(document.querySelectorAll('form,[role="dialog"],[class*="login-modal"],[class*="login-panel"],[class*="login-container"],[class*="login-wrap"]'));
 const loginWidget=panels.some(e=>visible(e)&&loginWords.test(e.textContent||'')&&
   (qr(e)||Array.from(e.querySelectorAll('input')).some(n=>visible(n)&&(n.type==='password'||/手机|电话|phone/i.test([n.type,n.placeholder,n.getAttribute('aria-label')].join(' '))))));
 // The official Channels OAuth component stays on /platform and renders this
 // exact title plus a visible OAuth/QR container, rather than a /login route.
 const channelsLogin=location.origin==='https://channels.weixin.qq.com'&&
   Array.from(document.querySelectorAll('.qrcode-area')).some(e=>visible(e)&&/登录视频号助手/.test(e.textContent||'')&&qr(e));
 return {origin:location.origin,login_required:(loginPath&&(loginText||password))||loginWidget||channelsLogin,challenge:complex||sms};
}'''


class _Unavailable(Exception):
    def __init__(self, reason): self.reason = reason


class _LoginRequired(_Unavailable):
    pass


def _remaining(cancel, deadline, maximum=4000):
    if cancel.is_set(): raise _Unavailable('cancelled')
    remaining = int((deadline - time.monotonic()) * 1000)
    if remaining <= 0: raise _Unavailable('timeout')
    return min(maximum, remaining)


def _evaluate(page, expression, cancel, deadline, maximum=4000):
    # evaluate() itself has no timeout. Both the expression and its deadline run
    # in this isolated browser; a timed-out fetch is abandoned when it closes.
    code = '''async timeoutMs => {
      let timer;
      try {return await Promise.race([
        Promise.resolve().then(() => (%s)()),
        new Promise((_,reject)=>{timer=setTimeout(()=>reject(Error('session-check-timeout')),timeoutMs);})
      ]);} finally {clearTimeout(timer);}
    }''' % expression
    while True:
        budget = _remaining(cancel, deadline, maximum)
        try:
            return page.evaluate(code, budget)
        except Exception as error:
            # Creator pages can replace /platform while the first identity
            # request resolves. This is navigation, not evidence of expiry or
            # a new timeout budget. Never retry arbitrary script/network errors.
            navigation = any(message in str(error) for message in (
                'Execution context was destroyed', 'Cannot find context with specified id'))
            if not navigation or page.is_closed(): raise
            page.wait_for_timeout(min(100, _remaining(cancel, deadline)))


def _clone(source, target, cancel, deadline):
    """Never follow profile symlinks or carry Chromium ownership files."""
    total = 0
    for directory, folders, files in os.walk(source, followlinks=False):
        _remaining(cancel, deadline)
        parent = Path(directory)
        folders[:] = [name for name in folders if name not in _SKIP]
        for name in folders:
            if (parent / name).is_symlink(): raise _Unavailable('session_unavailable')
            (target / (parent / name).relative_to(source)).mkdir(mode=0o700, parents=True, exist_ok=True)
        for name in files:
            if name in _SKIP: continue
            item = parent / name
            if item.is_symlink() or not item.is_file(): raise _Unavailable('session_unavailable')
            destination = target / item.relative_to(source)
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with item.open('rb') as reader, destination.open('wb') as writer:
                destination.chmod(0o600)
                while chunk := reader.read(1024 * 1024):
                    _remaining(cancel, deadline)
                    total += len(chunk)
                    if total > 256 * 1024 * 1024: raise _Unavailable('session_unavailable')
                    writer.write(chunk)


def _readonly_route(route, readonly_posts):
    request = route.request
    allowed = request.method.upper() in {'GET', 'HEAD', 'OPTIONS'}
    if not allowed and request.method.upper() == 'POST':
        # Only platform-owned, explicitly reviewed identity endpoints. The
        # literal origin/path comparison rejects suffixes and encoded aliases.
        url = urlsplit(request.url)
        allowed = url.scheme == 'https' and (url.netloc, url.path) in readonly_posts
    if allowed: route.continue_()
    else: route.abort()


def _readonly_websocket(_socket):
    # Routed sockets never reach the server unless connect_to_server is called.
    # Sync close() inside this SDK dispatcher callback would block its own loop.
    return None


def make_session_checker(engine, paths, *, platform, home, origins, profile_for, restore, expression, verify,
                         expired=None, readonly_posts=frozenset(), identity_timeout_ms=4000,
                         return_home_origins=frozenset()):
    """Bind trusted host functions; no page-supplied scripts or URLs exist."""
    def probe(account, temp, cancel, deadline, progress=None):
        """Child-only browser work; the host owns the lock and hard deadline."""
        from .chromium_publish import browser_options
        from .playwright_runtime import acquire_playwright
        context = driver = None
        result = {'state': 'unavailable', 'reason': 'identity_unavailable'}
        cleaned = True
        diagnostics = progress if isinstance(progress, CheckDiagnostics) else CheckDiagnostics(publish=progress)
        observe = diagnostics.call
        try:
            store = SessionStore(paths.data_dir / 'profiles')
            available = observe('session_read', lambda: account.session_ref and store.get(account.id, ref=account.session_ref))
            if not available:
                raise _LoginRequired('session_missing')
            source = observe('profile_read', lambda: profile_for(store, account.platform_user_id))
            temp.mkdir(mode=0o700)
            observe('clone', lambda: _clone(source, temp, cancel, deadline))
            options = browser_options('background')
            # HTTP routing cannot intercept service-worker traffic. Existing
            # workers in the cloned profile and socket HTTP tunnels must not
            # bypass this check's read-only request boundary.
            options['service_workers'] = 'block'
            options['timeout'] = _remaining(cancel, deadline, 8000)
            driver = observe('driver_start', acquire_playwright)
            context = observe('browser_launch', lambda: driver.chromium.launch_persistent_context(str(temp), **options))
            diagnostics.record('session_restore', 'started')
            context.route('**/*', lambda route: _readonly_route(route, readonly_posts))
            context.route_web_socket('**/*', _readonly_websocket)
            context.set_default_timeout(_remaining(cancel, deadline, 2500))
            restore(context, store, account, temp)
            page = context.pages[0] if context.pages else context.new_page()
            diagnostics.record('session_restore', 'completed')
            # Account verification owns readiness. Peripheral creator-page
            # scripts may never reach DOMContentLoaded after write blocking.
            observe('navigation_first', lambda: page.goto(home, wait_until='commit', timeout=_remaining(cancel, deadline, 8000)))
            previous = None
            first_verified_load = False
            returns = 0
            # SPA account stores hydrate asynchronously after navigation commits.
            # Poll for at most 8 seconds of idle waits, within the overall bound.
            for _ in range(40):
                _remaining(cancel, deadline)
                if page.is_closed(): raise _Unavailable('identity_unavailable')
                surface = observe('surface', lambda: _evaluate(page, SURFACE, cancel, deadline))
                if type(surface) is dict and surface.get('origin') in return_home_origins and returns < 3:
                    # Some normal Bili authentication redirects land on its
                    # public home. That page proves nothing; return only to the
                    # fixed creator URL, under the same deadline and write guard.
                    returns += 1
                    previous = None
                    observe('navigation_second' if first_verified_load else 'navigation_first',
                            lambda: page.goto(home, wait_until='commit', timeout=_remaining(cancel, deadline, 8000)))
                    continue
                if type(surface) is not dict or surface.get('origin') not in origins:
                    raise _Unavailable('identity_unavailable')
                if surface.get('challenge') is True: raise _Unavailable('verification_required')
                value = observe('identity_second' if first_verified_load else 'identity_first',
                                lambda: _evaluate(page, expression, cancel, deadline, identity_timeout_ms))
                _remaining(cancel, deadline)
                try:
                    uid, _ = verify(value)
                except (ValueError, TypeError, KeyError):
                    logout = (bool(expired and expired(value)) or surface.get('login_required') is True)
                    observed = 'login_required' if logout else 'unknown'
                else:
                    if uid != account.platform_user_id:
                        result = {'state': 'identity_mismatch', 'platform_user_id': uid}
                        break
                    observed = 'valid'
                if observed == 'valid' and first_verified_load:
                    result = {'state': 'valid', 'platform_user_id': account.platform_user_id}
                    break
                if observed == 'login_required' and observed == previous:
                    result = {'state': observed}
                    break
                previous = observed
                if observed == 'valid':
                    # Routing disables the HTTP cache. A second independent
                    # navigation must obtain and render the same live identity.
                    first_verified_load = True
                    previous = None
                    observe('navigation_second', lambda: page.goto(home, wait_until='commit', timeout=_remaining(cancel, deadline, 8000)))
                page.wait_for_timeout(min(200, _remaining(cancel, deadline)))
        except _Unavailable as error:
            diagnostics.record(diagnostics.stage, 'failed', error_kind(error))
            result = {'state': 'login_required' if isinstance(error, _LoginRequired) else 'unavailable', 'reason': error.reason}
        except Exception as error:
            if not diagnostics.failed: diagnostics.record(diagnostics.stage, 'failed', error_kind(error))
            timeout = isinstance(error, TimeoutError) or type(error).__name__ == 'TimeoutError' or 'session-check-timeout' in str(error)
            result = {'state': 'unavailable', 'reason': 'timeout' if timeout else 'network_unavailable'}
        finally:
            diagnostics.record('cleanup', 'started')
            context_closed = context is None
            if context is not None:
                for _ in range(2):
                    try: observe('context_close', context.close); context_closed = True; break
                    except Exception: cleaned = False
            stopped = driver is None
            if driver is not None:
                exclusive = driver.exclusive
                for _ in range(2):
                    try: observe('driver_stop', driver.stop); stopped = True; break
                    except Exception: cleaned = False
                if exclusive and stopped: context_closed = True
            if not context_closed: cleaned = False
            diagnostics.record('cleanup', 'completed' if cleaned else 'failed', None if cleaned else 'cleanup_failed')
        if not cleaned: result = {'state': 'unavailable', 'reason': 'cleanup_failed'}
        elif cancel.is_set(): result = {'state': 'unavailable', 'reason': 'cancelled'}
        return {**result, 'diagnostic': diagnostics.snapshot()}

    def check_session(account_id, cancel):
        from .chromium_publish import account_lock
        service = AccountService(engine)
        try: account = service.get(account_id)
        except (ValueError, KeyError): return {'state': 'unavailable', 'reason': 'account_unavailable'}
        if account.platform != platform: return {'state': 'unavailable', 'reason': 'platform_unavailable'}
        if cancel.is_set(): return {'state': 'unavailable', 'reason': 'cancelled'}
        lock = account_lock(account.id)
        if not lock.acquire(blocking=False): return {'state': 'unavailable', 'reason': 'account_busy'}
        deadline = time.monotonic() + CHECK_SECONDS
        diagnostics = CheckDiagnostics()
        result = {}
        def evidence(value):
            clean = sanitize_diagnostic(result.get('diagnostic'))
            if 'diagnostic' in result and clean is None:
                value = {'state': 'unavailable', 'reason': 'invalid_provider_result'}
            return {**value, 'diagnostic': clean or diagnostics.snapshot()}
        temporary = None
        cleaned = True
        try:
            store = SessionStore(paths.data_dir / 'profiles')
            available = diagnostics.call('session_read', lambda: account.session_ref and store.get(account.id, ref=account.session_ref))
            if not available:
                diagnostics.record('session_read', 'failed', 'session_missing')
                return evidence({'state': 'login_required', 'reason': 'session_missing'})
            temporary = Path(tempfile.mkdtemp(prefix='.' + platform + '-check-', dir=store.root))
            temporary.chmod(0o700)
            request = dict(version=1, platform=platform, data_dir=str(paths.data_dir),
                           account={key: getattr(account, key) for key in ('id', 'platform', 'platform_user_id', 'session_ref')},
                           directory=str(temporary), deadline=deadline, diagnostic=diagnostics.snapshot())
            result, cleaned = _execute_check(request, cancel, probe)
        except (OSError, ValueError):
            result = {'state': 'unavailable', 'reason': 'session_unavailable'}
        finally:
            if cleaned:
                if temporary is not None:
                    try: shutil.rmtree(temporary)
                    except FileNotFoundError: pass
                    except OSError: cleaned = False
                lock.release()
            else:
                _RETAINED.append((temporary, lock))
        if not cleaned: return evidence({'state': 'unavailable', 'reason': 'cleanup_failed'})
        if cancel.is_set(): return evidence({'state': 'unavailable', 'reason': 'cancelled'})
        try: current = service.get(account_id)
        except KeyError: return evidence({'state': 'unavailable', 'reason': 'account_unavailable'})
        if (current.platform, current.platform_user_id, current.session_ref) != (account.platform, account.platform_user_id, account.session_ref):
            return evidence({'state': 'unavailable', 'reason': 'session_changed'})
        if result.get('state') == 'valid' and result.get('platform_user_id') != account.platform_user_id:
            return evidence({'state': 'identity_mismatch', 'platform_user_id': result.get('platform_user_id')})
        return evidence(result)
    check_session._probe = probe
    check_session.close = close_checks
    return check_session


def _execute_check(request, cancel, probe):
    # The callable is an in-process test seam only; production reconstructs the
    # fixed platform factory in a fresh interpreter, never pickles closures.
    from .session_check_process import run_check
    return run_check(request, cancel)


def close_checks(timeout=0):
    """One bounded OS-only retry; uncertain resources remain privately owned."""
    from .session_check_process import retry_cleanup, pending_directory
    retry_cleanup(timeout)
    for temporary, lock in list(_RETAINED):
        if pending_directory(temporary): continue
        try: shutil.rmtree(temporary)
        except FileNotFoundError: pass
        except OSError: continue
        lock.release()
        _RETAINED.remove((temporary, lock))
    return not _RETAINED
