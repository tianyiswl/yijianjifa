"""V2 signing utility for operator-managed service execution only."""
from dataclasses import replace
import hashlib
import hmac
import secrets
import time
from ..official_http import ProtocolError, text


class BilibiliSigner:
    def __init__(self,client_id,app_secret,access_token,*,clock=time.time,nonce=lambda:secrets.token_hex(16)):
        self.issuer=text(client_id);self._client_id=self.issuer;self._secret=text(app_secret).encode();self._token=text(access_token)
        self._clock,self._nonce=clock,nonce
    def __repr__(self):return 'BilibiliSigner(<redacted>)'
    def sign(self,request):
        if request.host!='member.bilibili.com' or any(k.lower().startswith('x-bili-') or k.lower() in {'authorization','access-token'} for k in request.headers):raise ProtocolError()
        headers={'x-bili-accesskeyid':self._client_id,'x-bili-content-md5':hashlib.md5(request.body).hexdigest(),
            'x-bili-signature-method':'HMAC-SHA256','x-bili-signature-nonce':text(self._nonce()),
            'x-bili-signature-version':'2.0','x-bili-timestamp':str(int(self._clock()))}
        canonical='\n'.join(key+':'+headers[key] for key in sorted(headers))
        headers['Authorization']=hmac.new(self._secret,canonical.encode(),hashlib.sha256).hexdigest()
        headers['Access-Token']=self._token
        return replace(request,headers={**request.headers,**headers})
