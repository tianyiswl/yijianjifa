"""B站 creator login through its own official window, separate from publishing.

The creator page's public 087b8a68 assets (checked 2026-10-07) bind the
official nav response to header.navData and then store.state.upInfo. The
connected header is required; store initialization is not proof of login.
Read only those existing page fields; never request identity endpoints or infer a
successful login from a nickname, URL, or cookie.
"""
import re

from yijianjifa.features.accounts.service import AccountIdentityMismatch
from .creator_auth import CreatorAuthSpec, make_creator_provider

PLATFORM = 'bilibili'
HOME = 'https://member.bilibili.com/platform/home'
DOMAINS = frozenset({'bilibili.com', 'member.bilibili.com',
                     'passport.bilibili.com', 'api.bilibili.com'})
ORIGINS = frozenset({'https://member.bilibili.com', 'https://passport.bilibili.com'})

# 5434.087b8a68.js: the connected header loads navData, dispatches setUpInfo,
# and binds navData.face to a.avatar > BImg. 950.087b8a68.js stores upInfo.
# Its official getUserInfo implementation requires nav.data.isLogin and reads
# mid/uname/face. BImg may append a CDN thumbnail suffix to the face URL.
IDENTITY_SCRIPT = r'''() => {
 if (location.origin !== 'https://member.bilibili.com') return null;
 // Vue replaces the initial #creativecenter-v3 mount placeholder with #app.
 const root = document.querySelector('#app')?.__vue__;
 if (!root) return null;
 const info = root?.$store?.state?.upInfo;
 const id = value => typeof value === 'string' ? value :
   (Number.isSafeInteger(value) && value > 0 ? String(value) : null);
 const valid = data => data && data.isLogin === true &&
   /^[1-9][0-9]{0,31}$/.test(id(data.mid)||'') &&
   typeof data.uname === 'string' && data.uname.trim() && data.uname.trim().length<=255;
 const queue = root ? [root] : [], seen = new Set();
 let header = null, conflicting = false;
 while (queue.length && seen.size < 500) {
   const vm = queue.shift(); if (!vm || seen.has(vm)) continue; seen.add(vm);
   const data = vm.navData;
   if (vm.$el?.isConnected && data !== undefined && data !== null) {
     if (!valid(data) || (header &&
       (id(data.mid)!==id(header.navData.mid)||data.uname!==header.navData.uname))) conflicting=true;
     else header=vm;
   }
   if (Array.isArray(vm.$children)) queue.push(...vm.$children);
 }
 // A natural nav response already reached a connected account header. The
 // optional store copy may still be uninitialized on a newly mounted route.
 // Any populated copy or additional connected header must agree exactly.
 if (!header || conflicting || queue.length) return null;
 const uid=id(header.navData.mid), nickname=header.navData.uname;
 // Official store state starts with upInfo:{} (950.087b8a68). It only
 // becomes an identity copy once nav identity fields have been populated.
 const populatedInfo=info!==undefined && info!==null &&
   (typeof info!=='object'||Array.isArray(info)||['isLogin','mid','uname'].some(key=>key in info));
 if (populatedInfo &&
   (!valid(info)||id(info.mid)!==uid||info.uname!==nickname)) return null;
 let avatar = '';
 try {
   const image = header.$el.querySelector('a.avatar img');
   const actual = image?.currentSrc || image?.src;
   const face = header.navData.face;
   if (typeof actual === 'string' && actual.length <= 4096 &&
       typeof face === 'string' && face.length <= 4096 && face) {
     const target = new URL(face, location.href), loaded = new URL(actual, location.href);
     if (target.protocol === 'http:') target.protocol = 'https:';
     if (loaded.protocol === 'http:') loaded.protocol = 'https:';
     if (target.protocol === 'https:' && !target.username && !target.password &&
         loaded.origin === target.origin && loaded.search === target.search &&
         (loaded.pathname === target.pathname || loaded.pathname.startsWith(target.pathname+'@')))
       avatar = loaded.href;
   }
 } catch (_) {}
 return {origin:location.origin, logged_in:true, uid, nickname,
         profile_uid:id(header.navData.mid), avatar_url:avatar};
}'''


def verified_identity(value, expected_uid=None):
    if (not isinstance(value, dict)
            or value.get('origin') != 'https://member.bilibili.com'
            or value.get('logged_in') is not True):
        raise ValueError('identity unavailable')
    uid=value.get('uid');name=value.get('nickname')
    if (type(uid) is not str or not re.fullmatch(r'[1-9][0-9]{0,31}', uid)
            or value.get('profile_uid') != uid
            or type(name) is not str or not 1 <= len(name.strip()) <= 255):
        raise ValueError('identity unavailable or changed')
    if expected_uid is not None and expected_uid != uid:
        raise AccountIdentityMismatch('account identity mismatch')
    return uid, name.strip()


SPEC = CreatorAuthSpec(PLATFORM, HOME, DOMAINS, ORIGINS,
                       IDENTITY_SCRIPT, verified_identity,
                       return_home_origins=frozenset({'https://www.bilibili.com'}))


def make_bilibili_provider(engine, paths):
    return make_creator_provider(engine, paths, SPEC)
