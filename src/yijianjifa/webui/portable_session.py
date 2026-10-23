"""Windows identity-bound Chromium cookie archive; never part of diagnostics."""
import json

def validate(cookies, domains):
    if type(cookies) is not list or not 1 <= len(cookies) <= 500:
        raise ValueError('invalid session cookie count')
    seen=set()
    for cookie in cookies:
        if type(cookie) is not dict or not all(type(cookie.get(k)) is str for k in ('name','value','domain','path')):
            raise ValueError('invalid session cookie')
        if cookie['domain'].lstrip('.').lower() not in domains:
            raise ValueError('session cookie outside allowed domains')
        identity=tuple(cookie[k] for k in ('name','domain','path'))
        if identity in seen:raise ValueError('duplicate session cookie')
        seen.add(identity)
    return cookies

def encode_session(cookies,uid,domains):
    if not isinstance(uid,str) or not uid.isdecimal():raise ValueError('invalid identity')
    rows=[dict(c) for c in cookies if c.get('domain','').lstrip('.').lower() in domains]
    payload=dict(version=1,identity=uid,domains=sorted(domains),cookies=validate(rows,domains))
    return json.dumps(payload,separators=(',',':')).encode('utf-8')

def decode_session(blob,uid,domains):
    if type(blob) is not bytes or not 1 <= len(blob) <= 10*1024*1024:raise ValueError('invalid session archive')
    value=json.loads(blob)
    if type(value) is not dict or set(value)!={'version','identity','domains','cookies'} or value['version']!=1 or value['identity']!=uid or value['domains']!=sorted(domains):
        raise ValueError('session archive identity or scope mismatch')
    return validate(value['cookies'],domains)
