import hashlib
import ipaddress
import re
from urllib.parse import urlsplit, urlunsplit
from .contracts import DownloadError, SourceInput


def validate_url(url: str, *, code='unsafe_source'):
    try:
        if any(ord(c) < 32 or ord(c) == 127 for c in url): raise ValueError()
        parts = urlsplit(url)
        if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username is not None or parts.password is not None: raise ValueError()
        if parts.port not in (None, 443 if parts.scheme == 'https' else 80): raise ValueError()
        host = parts.hostname
        if '%' in host or '\\' in url: raise ValueError()
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if host.lower() == 'localhost' or host.lower().endswith(('.localhost', '.local')): raise ValueError()
            host.encode('idna')
        else:
            if not address.is_global: raise ValueError()
        return parts
    except (ValueError, UnicodeError):
        raise DownloadError(code) from None


def normalize_inputs(raw: str) -> tuple[SourceInput, ...]:
    if len(raw.encode('utf-8')) > 6144 or any(ord(c)<32 and c not in '\n\r\t' or ord(c)==127 for c in raw):
        raise DownloadError('input_limit')
    urls = re.findall(r'https?://[^\s<>"，。]+', raw)
    if not urls or len(urls)>20: raise DownloadError('input_limit')
    result = {}; profiles = 0
    for url in urls:
        parts = validate_url(url, code='input_limit')
        host = parts.hostname.lower()
        kind = 'direct'
        canonical = urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ''))
        if host in ('douyin.com', 'www.douyin.com', 'v.douyin.com'):
            if parts.path.startswith('/user/'):
                kind = 'douyin_profile'
            elif host == 'v.douyin.com' or parts.path.startswith('/video/'):
                kind = 'douyin_video'
            else: raise DownloadError('input_limit')
            canonical = urlunsplit(('https',host,parts.path,'',''))
        elif not parts.path.lower().endswith('.mp4'): raise DownloadError('input_limit')
        key = kind + ':' + hashlib.sha256(canonical.encode()).hexdigest()
        result[key] = SourceInput(kind,key,canonical)
    profiles = sum(s.kind=='douyin_profile' for s in result.values())
    if profiles and len(result)!=1: raise DownloadError('input_limit')
    return tuple(result.values())
