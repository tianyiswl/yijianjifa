"""Executable official Bilibili wire client; lives in the trusted service.

An injected executor signs business requests and retains OAuth credentials.
File requests carry only their upload token. No optional cover API is enabled.
"""
from dataclasses import dataclass,field
from pathlib import Path
from ..official_http import Request,ProtocolError,envelope,json_bytes,text,reference,safe_filename,remaining


@dataclass(frozen=True)
class Upload:
    token: str = field(repr=False)


class BilibiliClient:
    platform='bilibili'
    def __init__(self,executor,*,chunk_size=10*1024*1024):
        if type(chunk_size) is not int or not 1<=chunk_size<=10*1024*1024:raise ProtocolError()
        self.executor,self.chunk_size=executor,chunk_size
        self.issuer=getattr(executor,'issuer',None)
    def _call(self,method,path,*,deadline,query=(),body=b'',file=False):
        request=Request(method,'openupos.bilivideo.com' if file else 'member.bilibili.com',path,query,body,
            {'Content-Type':'application/octet-stream' if file else 'application/json'})
        return envelope(self.executor.execute(request,deadline=deadline),'code',0)
    def identity_scopes(self,*,deadline):
        info=self._call('GET','/arcopen/fn/user/account/info',deadline=deadline).get('data')
        scopes=self._call('GET','/arcopen/fn/user/account/scopes',deadline=deadline).get('data')
        if type(info) is not dict or type(scopes) is not dict:raise ProtocolError()
        subject=text(info.get('openid'))
        if scopes.get('openid')!=subject or type(scopes.get('scopes')) is not list or any(type(s) is not str for s in scopes['scopes']):raise ProtocolError()
        return subject,frozenset(scopes['scopes'])
    def categories(self,*,deadline):
        data=self._call('GET','/arcopen/fn/archive/type/list',deadline=deadline).get('data')
        if type(data) is not list:raise ProtocolError()
        seen=set();choices=[]
        def node(row,parent):
            if type(row) is not dict or type(row.get('id')) is not int or row['id']<=0 or row['id'] in seen or type(row.get('parent')) is not int or row['parent']!=parent:raise ProtocolError()
            seen.add(row['id']);name=text(row.get('name'))
            if 'description' in row and type(row['description']) is not str:raise ProtocolError()
            return name
        for group in data:
            name=node(group,0);children=group.get('children',[])
            if type(children) is not list:raise ProtocolError()
            for child in children:
                label=node(child,group['id'])
                if child.get('children') not in (None,[]):raise ProtocolError()
                choices.append({'id':child['id'],'label':name+' / '+label})
        return choices
    def upload(self,path,*,deadline):
        path=Path(path);name=safe_filename(path.name)
        if not path.suffix or not path.is_file() or path.stat().st_size<=0:raise ProtocolError()
        data=self._call('POST','/arcopen/fn/archive/video/init',deadline=deadline,body=json_bytes({'name':name,'utype':'0'})).get('data')
        if type(data) is not dict:raise ProtocolError()
        upload=Upload(text(data.get('upload_token')))
        with path.open('rb') as stream:
            part=1
            while chunk:=stream.read(self.chunk_size):
                remaining(deadline)
                self._call('POST','/video/v2/part/upload',deadline=deadline,query=(('upload_token',upload.token),('part_number',str(part))),body=chunk,file=True)
                part+=1
        self._call('POST','/arcopen/fn/archive/video/complete',deadline=deadline,query=(('upload_token',upload.token),))
        return upload
    @staticmethod
    def submit_fields(fields):
        title=fields.get('title');tags=fields.get('tags');body=fields.get('body','');tid=fields.get('category_id');copyright=fields.get('copyright')
        if type(title) is not str or not title.strip() or len(title)>=80 or type(tags) is not str or not tags.strip() or len(tags)>=200 or type(body) is not str or len(body)>=250:raise ProtocolError('configuration_required')
        if type(tid) is not int or tid<=0 or type(copyright) not in {int,str} or copyright not in {'original','repost',1,2}:raise ProtocolError('configuration_required')
        if fields.get('cover') is not None or any(fields.get(k) is not None for k in ('location','declaration')):raise ProtocolError('configuration_required')
        result={'title':title,'tid':tid,'tag':tags,'copyright':1 if copyright in {'original',1} else 2,'desc':body}
        if result['copyright']==2:result['source']=text(fields.get('source'),limit=2000)
        elif fields.get('source'):raise ProtocolError('configuration_required')
        return result
    def final_request(self,upload,fields):
        return Request('POST','member.bilibili.com','/arcopen/fn/archive/add-by-utoken',(('upload_token',upload.token),),json_bytes(self.submit_fields(fields)),{'Content-Type':'application/json'})
    def execute_final(self,request,*,deadline):
        data=envelope(self.executor.execute(request,deadline=deadline),'code',0).get('data')
        if type(data) is not dict:raise ProtocolError()
        return {'resource_id':reference(data.get('resource_id'))}
    def submit(self,upload,fields,*,deadline):return self.execute_final(self.final_request(upload,fields),deadline=deadline)
    def query(self,receipt,*,deadline):
        resource=reference(receipt.get('resource_id'))
        data=self._call('GET','/arcopen/fn/archive/view',deadline=deadline,query=(('resource_id',resource),)).get('data')
        if type(data) is not dict or data.get('resource_id')!=resource:raise ProtocolError()
        info=data.get('addit_info')
        if type(info) is not dict:return 'result_unknown'
        state=info.get('state')
        return 'published' if type(state) is int and state==0 else 'result_unknown'
