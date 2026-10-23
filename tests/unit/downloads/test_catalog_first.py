from dataclasses import replace
import pytest
from yijianjifa.features.downloads.contracts import DownloadError
from yijianjifa.features.downloads.input import normalize_inputs
from yijianjifa.features.downloads.network import SourceRegistry
from yijianjifa.features.downloads.providers import DouyinProvider

class Token:
    def cancelled(self): return False

def work(i, author='a', media=False):
    video={'duration':1200}
    if media: video['play_addr']={'url_list':['https://example.com/movie.mp4?secret=PRIVATE']}
    return {'aweme_id':str(i),'author':{'sec_uid':author,'nickname':'name'},'video':video}

def post(rows, more=False, cursor=10, request=0, author='a'):
    return {'status_code':0,'aweme_list':rows,'has_more':more,'max_cursor':cursor,
            '_catalog_source':author,'_catalog_request_cursor':str(request)}

class Reader:
    def __init__(self, payload, details=None): self.payload=payload;self.details=details;self.calls=[]
    def read(self,url,*,cancel):
        self.calls.append(url)
        return self.details if '/video/' in url else self.payload
    def read_next(self,url,*,cancel):
        self.calls.append(url)
        if isinstance(self.payload,Exception): raise self.payload
        return self.payload

def setup(payload, details=None):
    reader=Reader(payload,details)
    return DouyinProvider(SourceRegistry(),reader),reader,normalize_inputs('https://www.douyin.com/user/a')[0]

def test_catalog_without_media_and_selected_only_detail():
    p,r,s=setup(post([work(1),work(2)]),{'aweme_detail':work(2,media=True)})
    page=p.resolve(s,None,cancel=Token())
    assert len(page.candidates)==2 and page.completion=='complete' and len(r.calls)==1
    p.media(page.candidates[1],cancel=Token())
    assert r.calls==[s.canonical_source,'https://www.douyin.com/video/2']
    assert 'PRIVATE' not in repr(page)


def test_cover_uses_only_owned_catalog_metadata_and_revokes_private_source():
    row=work(1,media=True);row['video']['cover']={'url_list':['https://example.com/cover.png?sig=PRIVATE_COVER']}
    p,r,s=setup(post([row]),{'aweme_detail':row})
    candidate=p.resolve(s,None,cancel=Token()).candidates[0]
    source=p.cover(candidate,cancel=Token())
    assert p.registry.lookup(source)[0].endswith('sig=PRIVATE_COVER')
    assert r.calls==[s.canonical_source] and 'PRIVATE_COVER' not in repr(candidate)
    for forged in (replace(candidate),replace(candidate,work_id='2')):
        with pytest.raises(DownloadError):p.cover(forged,cancel=Token())
    p.close()
    with pytest.raises(DownloadError):p.registry.lookup(source)


@pytest.mark.parametrize('cover',[None,{},'invalid',{'url_list':[]},{'url_list':[7]},{'url_list':['https://example.com/'+'x'*4097]}])
def test_missing_or_malformed_cover_does_not_remove_downloadable_candidate(cover):
    row=work(1,media=True);row['video']['cover']=cover
    p,r,s=setup(post([row]),{'aweme_detail':row})
    candidate=p.resolve(s,None,cancel=Token()).candidates[0]
    assert p.cover(candidate,cancel=Token()) is None
    assert p.media(candidate,cancel=Token()) and len(r.calls)==2


def test_unsafe_cover_source_fails_only_cover_and_cancel_is_observed():
    row=work(1,media=True);row['video']['cover']={'url_list':['http://127.0.0.1/private']}
    p,r,s=setup(post([row]),{'aweme_detail':row});candidate=p.resolve(s,None,cancel=Token()).candidates[0]
    with pytest.raises(DownloadError) as error:p.cover(candidate,cancel=Token())
    assert error.value.code=='unsafe_source' and p.media(candidate,cancel=Token())
    with pytest.raises(DownloadError) as error:p.cover(candidate,cancel=type('Cancelled',(),{'cancelled':lambda self:True})())
    assert error.value.code=='cancelled'

@pytest.mark.parametrize('detail',[work(2,media=True),work(1,'b',True)])
def test_selected_detail_must_match_id_and_author(detail):
    p,r,s=setup(post([work(1)]),{'aweme_detail':detail})
    candidate=p.resolve(s,None,cancel=Token()).candidates[0]
    with pytest.raises(DownloadError): p.media(candidate,cancel=Token())
    assert not p._media

def test_forged_candidate_never_navigates():
    p,r,s=setup(post([work(1)]))
    candidate=p.resolve(s,None,cancel=Token()).candidates[0]
    for forged in [replace(candidate),replace(candidate,work_id='2'),replace(candidate,canonical_source='https://www.douyin.com/video/2')]:
        with pytest.raises(DownloadError): p.media(forged,cancel=Token())
    assert len(r.calls)==1

@pytest.mark.parametrize('patch',[{'status_code':1},{'_catalog_source':'b'},{'aweme_list':[work(1,'b')]},{'_catalog_source':None}])
def test_catalog_fail_closed(patch):
    p,r,s=setup(dict(post([work(1)]),**patch))
    with pytest.raises(DownloadError): p.resolve(s,None,cancel=Token())

@pytest.mark.parametrize('payload,expected',[(post([]),'complete'),(post([work(1)],cursor=None),'partial'),(post([work(i) for i in range(501)]),'limited')])
def test_completion(payload,expected):
    p,r,s=setup(payload)
    page=p.resolve(s,None,cancel=Token())
    while page.has_more: page=p.resolve(s,page.cursor,cancel=Token())
    assert page.completion==expected

def test_cursor_survives_wrong_source_and_failed_read_but_is_one_shot():
    p,r,s=setup(post([work(1)],True,10))
    first=p.resolve(s,None,cancel=Token())
    with pytest.raises(DownloadError): p.resolve(normalize_inputs('https://www.douyin.com/user/b')[0],first.cursor,cancel=Token())
    r.payload=DownloadError('network')
    with pytest.raises(DownloadError): p.resolve(s,first.cursor,cancel=Token())
    r.payload=post([work(2)],False,20,10)
    second=p.resolve(s,first.cursor,cancel=Token())
    assert second.completion=='complete'
    with pytest.raises(DownloadError): p.resolve(s,first.cursor,cancel=Token())

def test_duplicate_terminal_and_nonadvancing_cursor_never_complete():
    for payload in [post([work(1)],False,20,10),post([work(2)],False,10,10)]:
        p,r,s=setup(post([work(1)],True,10))
        first=p.resolve(s,None,cancel=Token());r.payload=payload
        page=p.resolve(s,first.cursor,cancel=Token())
        assert page.completion=='partial' and not page.has_more

def test_native_post_requires_exact_endpoint_and_query_binding():
    from yijianjifa.features.downloads.browser import select_profile_metadata
    source='https://www.douyin.com/user/a'
    endpoint='https://www.douyin.com/aweme/v1/web/aweme/post/?sec_user_id=a&max_cursor=0'
    payload={'status_code':0,'aweme_list':[],'has_more':False,'max_cursor':0}
    result=select_profile_metadata(payload,endpoint,source)
    assert result['_catalog_source']=='a'
    for bad in [endpoint.replace('/post/','/detail/'),endpoint.replace('sec_user_id=a','sec_user_id=b'),endpoint.replace('&max_cursor=0',''),endpoint+'&sec_user_id=b']:
        assert select_profile_metadata(payload,bad,source) is None

def test_missing_terminal_proof_stays_partial_and_missing_request_fails():
    payload=post([work(1)]);payload.pop('has_more')
    p,r,s=setup(payload)
    assert p.resolve(s,None,cancel=Token()).completion=='partial'
    payload=post([work(1)]);payload.pop('_catalog_request_cursor')
    p,r,s=setup(payload)
    with pytest.raises(DownloadError): p.resolve(s,None,cancel=Token())


def test_cursor_ttl_and_eight_session_bound(monkeypatch):
    from yijianjifa.features.downloads import providers
    clock=[1000];monkeypatch.setattr(providers.time,'monotonic',lambda:clock[0])
    p,r,s=setup(post([work(1)],True))
    pages=[p.resolve(s,None,cancel=Token()) for _ in range(8)]
    with pytest.raises(DownloadError) as error: p.resolve(s,None,cancel=Token())
    assert error.value.code=='input_limit'
    clock[0]+=901
    with pytest.raises(DownloadError): p.resolve(s,pages[0].cursor,cancel=Token())
    assert p.resolve(s,None,cancel=Token()).has_more


def test_malformed_followup_is_atomic_and_retryable():
    p,r,s=setup(post([work(1)],True,10))
    first=p.resolve(s,None,cancel=Token())
    r.payload=post([work(2),work(3,'b')],False,20,10)
    with pytest.raises(DownloadError): p.resolve(s,first.cursor,cancel=Token())
    assert len(p._owned)==1
    r.payload=post([work(2)],False,20,10)
    assert [c.work_id for c in p.resolve(s,first.cursor,cancel=Token()).candidates]==['2']


def test_image_entries_skip_and_bad_display_fields_fail_closed():
    image=dict(work(2),images=[{}])
    p,r,s=setup(post([image,dict(work(1),create_time=1)]))
    page=p.resolve(s,None,cancel=Token())
    assert [c.work_id for c in page.candidates]==['1']
    assert page.candidates[0].published_at=='1970-01-01T00:00:01+00:00'
    p,r,s=setup(post([dict(work(1),desc={'secret':'bad'})]))
    with pytest.raises(DownloadError): p.resolve(s,None,cancel=Token())

def test_image_only_advancing_page_can_reach_later_video():
    p,r,s=setup(post([dict(work(1),images=[{}])],True,10))
    first=p.resolve(s,None,cancel=Token())
    assert first.candidates==() and first.has_more and first.completion=='partial'
    r.payload=post([work(2)],False,20,10)
    second=p.resolve(s,first.cursor,cancel=Token())
    assert [c.work_id for c in second.candidates]==['2'] and second.completion=='complete'


def test_terminal_image_only_page_is_complete_but_duplicates_are_not():
    p,r,s=setup(post([dict(work(1),images=[{}])],False,10))
    page=p.resolve(s,None,cancel=Token())
    assert page.candidates==() and not page.has_more and page.completion=='complete'
    p,r,s=setup(post([dict(work(1),images=[{}])],True,10))
    first=p.resolve(s,None,cancel=Token())
    r.payload=post([dict(work(1),images=[{}])],False,20,10)
    assert p.resolve(s,first.cursor,cancel=Token()).completion=='partial'


def test_native_identity_budget_limits_image_pages_and_preserves_buffer():
    p,r,s=setup(post([dict(work(i),images=[{}]) for i in range(1,500)]+[work(500),work(501)],True,10))
    page=p.resolve(s,None,cancel=Token())
    assert [c.work_id for c in page.candidates]==['500']
    assert page.completion=='limited' and not page.has_more and len(r.calls)==1
    p,r,s=setup(post([work(i) for i in range(1,501)],True,10))
    page=p.resolve(s,None,cancel=Token()); ids=[]
    while True:
        ids.extend(c.work_id for c in page.candidates)
        assert page.completion=='limited'
        if not page.has_more: break
        page=p.resolve(s,page.cursor,cancel=Token())
    assert len(ids)==500 and len(r.calls)==1


def test_advancing_empty_native_pages_stop_at_25_reads():
    p,r,s=setup(post([],True,1))
    page=p.resolve(s,None,cancel=Token())
    for n in range(2,26):
        assert page.has_more
        r.payload=post([],True,n,n-1)
        page=p.resolve(s,page.cursor,cancel=Token())
    assert page.completion=='limited' and not page.has_more and len(r.calls)==25


def test_buffered_delivery_does_not_spend_native_reads():
    p,r,s=setup(post([work(i) for i in range(1,42)],True,1))
    page=p.resolve(s,None,cancel=Token())
    for _ in range(2): page=p.resolve(s,page.cursor,cancel=Token())
    assert len(r.calls)==1 and page.has_more
    for n in range(2,26):
        r.payload=post([],True,n,n-1)
        page=p.resolve(s,page.cursor,cancel=Token())
    assert len(r.calls)==25 and page.completion=='limited' and not page.has_more


def test_image_only_native_budget_stops_continuation():
    p,r,s=setup(post([dict(work(i),images=[{}]) for i in range(1,251)],True,1))
    page=p.resolve(s,None,cancel=Token())
    r.payload=post([dict(work(i),images=[{}]) for i in range(251,502)],True,2,1)
    page=p.resolve(s,page.cursor,cancel=Token())
    assert page.candidates==() and page.completion=='limited' and not page.has_more
    assert len(r.calls)==2


def test_failed_native_attempts_preserve_cursor_but_consume_read_budget():
    p,r,s=setup(post([],True,1))
    page=p.resolve(s,None,cancel=Token()); cursor=page.cursor
    r.payload=DownloadError('metadata_missing')
    for _ in range(24):
        with pytest.raises(DownloadError): p.resolve(s,cursor,cancel=Token())
    r.payload=post([work(1)],True,2,1)
    page=p.resolve(s,cursor,cancel=Token())
    assert page.candidates==() and page.completion=='limited' and not page.has_more
    assert len(r.calls)==25
