"""Bounded public CDN avatar fetch; no account credentials or redirects."""
import base64
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler


def avatar_url_allowed(url):
    if not isinstance(url,str) or len(url)>4096:return False
    try:
        p=urlsplit(url)
        return p.scheme=='https' and p.port in (None,443) and not p.username and not p.password and any(
            p.hostname==domain or (p.hostname or '').endswith('.'+domain)
            for domain in ('douyinpic.com','douyincdn.com','byteimg.com'))
    except ValueError:return False


def image_data(data):
    """Encode a bounded PNG/JPEG/WebP; never retain arbitrary response bodies."""
    if type(data) is not bytes or not 0 < len(data) <= 512*1024:return ''
    kind='png' if data.startswith(b'\x89PNG\r\n\x1a\n') else 'jpeg' if data.startswith(b'\xff\xd8\xff') else 'webp' if data[:4]==b'RIFF' and data[8:12]==b'WEBP' else None
    return 'data:image/'+kind+';base64,'+base64.b64encode(data).decode('ascii') if kind else ''


def fetch_avatar(url):
    if not avatar_url_allowed(url):return ''
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self,*args):return None
    try:
        with build_opener(NoRedirect).open(Request(url,headers={'User-Agent':'YiJianJiFa/1.0'}),timeout=4) as response:
            data=response.read(512*1024+1)
        return image_data(data)
    except Exception:return ''
