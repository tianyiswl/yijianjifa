"""Native cookie archive, bound to a separately verified identity and domain set.

No browser store access or identity verification occurs here. Callers must only
supply the isolated auth window's cookies, never another browser's profile.
"""
import re


def _scope(identity, domains):
    if not isinstance(identity,str) or not identity.strip() or len(identity)>128:
        raise ValueError('invalid session identity')
    domains=frozenset(domains)
    if not domains or any(not isinstance(d,str) or not re.fullmatch(r'[a-z0-9]+(?:[a-z0-9.-]*[a-z0-9])?',d) for d in domains):
        raise ValueError('invalid session domains')
    return domains


def _validate(cookies,domains):
    if not 1<=len(cookies)<=500:
        raise ValueError('invalid session cookie count')
    seen=set()
    for cookie in cookies:
        if cookie is None or str(cookie.domain()).lstrip('.').lower() not in domains:
            raise ValueError('session cookie outside allowed domains')
        key=(str(cookie.name()),str(cookie.domain()),str(cookie.path()))
        if key in seen: raise ValueError('duplicate session cookie')
        seen.add(key)


def encode_cookies(cookies, *, identity, domains):
    import Foundation as F
    domains=_scope(identity,domains)
    _validate(cookies,domains)
    payload=dict(version=1,identity=identity,domains=sorted(domains),
                 cookies=[dict(cookie.properties()) for cookie in cookies])
    data,error=F.NSPropertyListSerialization.dataWithPropertyList_format_options_error_(
        payload,F.NSPropertyListBinaryFormat_v1_0,0,None)
    if error is not None or data is None or len(data)>10*1024*1024:
        raise ValueError('session archive encoding failed')
    return bytes(data)


def decode_cookies(blob, *, identity, domains):
    import Foundation as F
    domains=_scope(identity,domains)
    if type(blob) is not bytes or not 1<=len(blob)<=10*1024*1024:
        raise ValueError('invalid session archive')
    try:
        data=F.NSData.dataWithBytes_length_(blob,len(blob))
        payload,_,error=F.NSPropertyListSerialization.propertyListWithData_options_format_error_(data,0,None,None)
        if error is not None or not isinstance(payload,F.NSDictionary):
            raise ValueError('invalid session archive')
        if set(payload)!={'version','identity','domains','cookies'} or payload['version']!=1 or payload['identity']!=identity or set(payload['domains'])!=domains:
            raise ValueError('session archive identity or scope mismatch')
        properties=payload['cookies']
        if not 1<=len(properties)<=500: raise ValueError('invalid session cookie count')
        cookies=[F.NSHTTPCookie.cookieWithProperties_(p) for p in properties]
        _validate(cookies,domains)
        return cookies
    except Exception:
        raise ValueError('session archive rejected') from None
