"""Capture only the selected avatar already loaded by a login page."""
from urllib.parse import urlsplit, urlunsplit

def _avatar_url(value):
    # Match only images already loaded by this official page. No new request or
    # account credentials are sent to an inferred CDN. Chrome upgrades HTTP
    # image URLs on HTTPS pages, so normalize that one scheme difference.
    if type(value) is not str or not 1 <= len(value) <= 4096:return None
    try:
        parsed=urlsplit('https:'+value if value.startswith('//') else value)
        if parsed.scheme not in {'http','https'} or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None,443):return None
        return urlunsplit(('https',parsed.netloc,parsed.path,parsed.query,''))
    except ValueError:return None


class LoadedAvatarCache:
    """References to completed images in one login page, never a downloader."""
    def __init__(self,frame):self.frame=frame;self.images={}

    def capture(self,request):
        try:
            if request.resource_type!='image' or request.frame!=self.frame:return
            response=request.response()
            if response is None or not 200 <= response.status < 300:return
            url=_avatar_url(response.url)
            if not url or response.url!=url:return
            length=int(response.headers.get('content-length','0'))
            if not 0 < length <= 512*1024:return
            if len(self.images)>=128:self.images.pop(next(iter(self.images)))
            self.images[url]=response
        except Exception:return

    def data_for(self,url):
        key=_avatar_url(url)
        if not key:return ''
        value=self.images.get(key)
        if value is None:return None
        if type(value) is str:return value
        from yijianjifa.features.accounts.avatar import image_data
        try:data=image_data(value.body())
        except Exception:data=''
        self.images[key]=data
        return data
