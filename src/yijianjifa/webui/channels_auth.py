"""Read the current, user-selected identity from the official video-channel page.

The 2026-10-07 official index.dd23013a.js exposes window._store.userStore.
Its getAuthData action writes finderUser to finder; hasLogin checks its stable
finderUsername. AccountInfo in the connected sidebar binds that same finder.
Only existing page state is read; no private request, upload or post is made.
"""
from yijianjifa.features.accounts.service import AccountIdentityMismatch
from .creator_auth import CreatorAuthSpec, make_creator_provider


PLATFORM = 'channels'
HOME = 'https://channels.weixin.qq.com/platform'
DOMAINS = frozenset({'channels.weixin.qq.com', 'weixin.qq.com'})
ORIGINS = frozenset({'https://channels.weixin.qq.com'})


IDENTITY_SCRIPT = r'''() => {
 if (location.origin !== 'https://channels.weixin.qq.com') return null;
 if (!(location.pathname === '/platform' || location.pathname.startsWith('/platform/')) ||
     /^\/platform\/login(?:\/|-|$)/.test(location.pathname)) return null;
 const user = window._store?.userStore;
 if (!user || user.hasLogin !== true || user.isExpired !== false) return null;
 const finder = user.finder;
 const uid = finder?.finderUsername, nickname = finder?.nickname;
 if (typeof uid !== 'string' || !uid || uid.length > 128 || /\s|[\u0000-\u001f\u007f]/.test(uid) ||
     typeof nickname !== 'string' || !nickname.trim() || nickname.trim().length > 255) return null;
 const footer = document.querySelector('.side-bar-footer');
 const root = document.querySelector('#app')?.__vue__;
 if (!footer?.isConnected || !root) return null;
 const queue = [root], seen = new Set();
 let profile = null;
 while (queue.length && seen.size < 500) {
   const vm = queue.shift();
   if (!vm || seen.has(vm)) continue;
   seen.add(vm);
   if (vm.$el?.isConnected && footer.contains(vm.$el) && vm.finder) {
     if (vm.finder.finderUsername !== uid || vm.finder.nickname !== nickname) return null;
     profile = vm.finder;
   }
   if (Array.isArray(vm.$children)) queue.push(...vm.$children);
 }
 if (!profile) return null;
 return {origin:location.origin, logged_in:true, expired:false, uid, nickname,
         profile_uid:profile.finderUsername,
         avatar_url:typeof finder.headImgUrl === 'string' && finder.headImgUrl.length <= 4096 ? finder.headImgUrl : ''};
}'''


def verified_identity(value, expected_uid=None):
    if (type(value) is not dict or value.get('origin') != 'https://channels.weixin.qq.com'
            or value.get('logged_in') is not True or value.get('expired') is not False):
        raise ValueError('identity unavailable')
    uid = value.get('uid'); nickname = value.get('nickname')
    # finderUsername is an opaque platform ID, not a nickname or a cookie.
    # No numeric-only pattern is invented for this platform's identity.
    if (type(uid) is not str or not 1 <= len(uid) <= 128
            or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in uid)
            or value.get('profile_uid') != uid
            or type(nickname) is not str or not 1 <= len(nickname.strip()) <= 255):
        raise ValueError('identity unavailable or changed')
    if expected_uid is not None and uid != expected_uid:
        raise AccountIdentityMismatch('account identity mismatch')
    return uid, nickname.strip()


SPEC = CreatorAuthSpec(
    platform=PLATFORM, home=HOME, domains=DOMAINS, origins=ORIGINS,
    identity_script=IDENTITY_SCRIPT, verify_identity=verified_identity,
    # Official 404.b0af01fb.js UserService.getAuthData only reads finderUser;
    # getLoginType reads get_auth_info first. No other POST (including switch,
    # notification_read or post_create) is allowed.
    readonly_identity_posts=frozenset({
        ('channels.weixin.qq.com', '/cgi-bin/mmfinderassistant-bin/auth/auth_data'),
        ('channels.weixin.qq.com', '/cgi-bin/mmfinderassistant-bin/auth/get_auth_info'),
    }),
)


def make_channels_provider(engine, paths):
    return make_creator_provider(engine, paths, SPEC)
