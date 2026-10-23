"""Official Kwai wire client for the trusted service, with explicit TLS gateways.

Injected business executor adds app_id/access_token server-side. Tokens never
enter adapter payloads, public receipts, settings or native grant observations.
"""
from dataclasses import dataclass,field,replace
from pathlib import Path
import re
import secrets
from ..official_http import Request,ProtocolError,envelope,text,reference,safe_filename,remaining


@dataclass(frozen=True)
class Upload:
    token: str = field(repr=False)
    gateway: str = field(repr=False)


class KuaishouClient:
    platform='kuaishou'
    def __init__(self,executor,*,gateway_hosts=(),chunk_size=10*1024*1024):
        if type(chunk_size) is not int or not 1<=chunk_size<=10*1024*1024:raise ProtocolError()
        self.executor,self.gateway_hosts,self.chunk_size=executor,frozenset(gateway_hosts),chunk_size
        self.issuer=getattr(executor,'issuer',None)
    def _call(self,method,path,*,deadline,query=(),body=b'',gateway=None):
        request=Request(method,gateway or 'open.kuaishou.com',path,query,body,{'Content-Type':'application/octet-stream' if gateway else 'application/json'})
        return envelope(self.executor.execute(request,deadline=deadline),'result',1)
    def _gateway(self,value):
        if type(value) is not str or not re.fullmatch(r'[a-z0-9.-]+',value) or value not in self.gateway_hosts:raise ProtocolError('configuration_required')
        return value
    def resume_state(self,upload,sizes,*,deadline):
        response=self._call('GET','/api/upload/resume',deadline=deadline,query=(('upload_token',upload.token),),gateway=upload.gateway)
        existed,index,parts,size=(response.get(k) for k in ('existed','fragment_index','fragment_list','fragment_index_bytes'))
        if type(existed) is not bool or type(index) is not int or type(parts) is not list or type(size) is not int or size<0:raise ProtocolError()
        if any(type(p) is not int or not 0<=p<len(sizes) for p in parts) or len(set(parts))!=len(parts):raise ProtocolError()
        if not existed:
            if index!=-1 or parts or size!=0:raise ProtocolError()
        elif not parts or index!=max(parts) or size!=sizes[index]:raise ProtocolError()
        return frozenset(parts)
    def upload(self,path,*,deadline):
        path=Path(path)
        if not path.is_file() or path.stat().st_size<=0:raise ProtocolError()
        start=self._call('POST','/openapi/photo/start_upload',deadline=deadline)
        upload=Upload(text(start.get('upload_token')),self._gateway(start.get('endpoint')))
        length=path.stat().st_size;sizes=[min(self.chunk_size,length-offset) for offset in range(0,length,self.chunk_size)]
        # A fresh upload token must have no prior fragments. Resume cannot assert
        # equality of opaque remote checksums to our content, so do not reuse it.
        if self.resume_state(upload,sizes,deadline=deadline):raise ProtocolError()
        with path.open('rb') as stream:
            for index,size in enumerate(sizes):
                remaining(deadline);chunk=stream.read(size)
                if len(chunk)!=size:raise ProtocolError()
                response=self._call('POST','/api/upload/fragment',deadline=deadline,query=(('fragment_id',str(index)),('upload_token',upload.token)),body=chunk,gateway=upload.gateway)
                if type(response.get('size')) is not int or response['size']!=size:raise ProtocolError()
                text(response.get('checksum'))
        self._call('POST','/api/upload/complete',deadline=deadline,query=(('fragment_count',str(len(sizes))),('upload_token',upload.token)),gateway=upload.gateway)
        return upload
    @staticmethod
    def caption(fields):
        if any(type(fields.get(k,'')) is not str for k in ('title','body','tags')) or not fields.get('title','').strip():raise ProtocolError('configuration_required')
        if any(fields.get(k) is not None for k in ('location','declaration')) or any(k in fields for k in ('category_id','copyright','source')):raise ProtocolError('configuration_required')
        return '\n'.join(fields.get(k,'') for k in ('title','body','tags') if fields.get(k,''))
    def final_request(self,upload,caption,cover):
        if type(caption) is not str or not caption or len(caption)>30002 or '\x00' in caption:raise ProtocolError('configuration_required')
        cover=Path(cover);name=safe_filename(cover.name)
        raw=cover.read_bytes()
        if not raw or len(raw)>=10_000_000:raise ProtocolError('configuration_required')
        boundary='p011'+secrets.token_hex(16)
        body=(f'--{boundary}\r\nContent-Disposition: form-data; name="caption"\r\n\r\n'.encode()+caption.encode()+
            f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="cover"; filename="{name}"\r\nContent-Type: image/png\r\n\r\n'.encode()+raw+f'\r\n--{boundary}--\r\n'.encode())
        return Request('POST','open.kuaishou.com','/openapi/photo/publish',(('upload_token',upload.token),),body,{'Content-Type':'multipart/form-data; boundary='+boundary})
    def execute_final(self,request,*,deadline):
        data=envelope(self.executor.execute(request,deadline=deadline),'result',1).get('video_info')
        if type(data) is not dict or type(data.get('pending')) is not bool:raise ProtocolError()
        return {'photo_id':reference(data.get('photo_id'))}
    def submit(self,upload,caption,cover,*,deadline):return self.execute_final(self.final_request(upload,caption,cover),deadline=deadline)
    def query(self,receipt,*,deadline):
        photo=reference(receipt.get('photo_id'))
        data=self._call('GET','/openapi/photo/info',deadline=deadline,query=(('photo_id',photo),)).get('video_info')
        if type(data) is not dict or data.get('photo_id')!=photo:raise ProtocolError()
        pending=data.get('pending')
        if type(pending) is not bool:return 'result_unknown'
        return 'processing' if pending else 'platform_accepted'


class KuaishouAuthorizedExecutor:
    """Operator-side OAuth query decoration, never a desktop credential API."""
    def __init__(self,executor,app_id,access_token):
        self._executor,self._app_id,self._token=executor,text(app_id),text(access_token)
        self.issuer=self._app_id
    def __repr__(self):return 'KuaishouAuthorizedExecutor(<redacted>)'
    def execute(self,request,*,deadline):
        if request.host=='open.kuaishou.com':
            if any(key in {'app_id','access_token'} for key,_ in request.query):raise ProtocolError()
            request=replace(request,query=request.query+(('app_id',self._app_id),('access_token',self._token)))
        return self._executor.execute(request,deadline=deadline)
