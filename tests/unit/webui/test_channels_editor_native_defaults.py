"""Regress real editor padding, automatic location and same-upload processing."""
from copy import deepcopy
from types import SimpleNamespace
import pytest
from test_channels_publish_form import payload, media, live_form, page_request
from test_channels_multi_publish import case, module
from test_channels_dom_visibility import offline_browser, offline_page, mount_forms
from yijianjifa.webui import channels_publish_form as form


def test_editor_edge_newlines_match_at_form_request_and_receipt(payload,media):
    f=form.validate_form(payload);raw=live_form(f,media)
    raw.update(caption=f['caption']+'\n\n',state_caption=f['caption']+'\n\n')
    assert form.verify_form(raw,f,media,'finder-test')
    request=page_request(f,media);request['objectDesc']['description']+='\n\n'
    assert form.request_matches(request,f,media)
    assert form.verified_row([{'objectId':'123','desc':request['objectDesc'],'effectiveTime':f['effective_time'],'handleStatus':2}],{'work_id':'123'},f)


@pytest.mark.parametrize('change',['inner_newline','inner_space','other_text','immediate'])
def test_padding_tolerance_never_changes_content_or_schedule(payload,media,change):
    f=form.validate_form(payload);raw=live_form(f,media);request=page_request(f,media)
    text=f['caption']
    if change=='inner_newline':text=text.replace('\n',' ')
    if change=='inner_space':text=text.replace(' #','  #')
    if change=='other_text':text+='extra'
    raw.update(caption=text,state_caption=text);request['objectDesc']['description']=text
    if change=='immediate':raw['effective_time']=0;request['effectiveTime']=0
    assert not form.verify_form(raw,f,media,'finder-test')
    assert not form.request_matches(request,f,media)


def test_automatic_location_is_cleared_by_normal_option_click(case):
    adapter,*_=case;events=[]
    class Control:
        def count(self):return 1
        def is_visible(self):return True
        def get_by_text(self,text,exact=False):assert text=='不显示位置' and exact;return self
        def click(self):events.append('click')
    class Surface:
        def locator(self,selector):events.append(selector);return Control()
    session=SimpleNamespace()
    assert adapter._clear_location(session,Surface(),{'extras_checks':{'empty_location':False}}) is False
    assert events==[form.LOCATION_SELECT,'click',form.LOCATION_OPTION,'click']
    assert adapter._clear_location(session,Surface(),{'extras_checks':{'empty_location':True}}) is True
    assert len(events)==4
    assert adapter._clear_location(session,Surface(),{'extras_checks':{'empty_location':False}}) is False
    assert len(events)==4  # no repeated editing on stale observations
    adapter.close()


def test_processed_url_requires_native_upload_proof_and_same_ticket(media):
    frozen=dict(media,urlCdnTaskId='owned-ticket')
    processed=dict(frozen,url='https://example.invalid/processed.mp4',fileSize=None)
    raw={'media':processed,'clip_id':'owned-ticket','clip_bindings':{'record_ticket_matches':True,'processed_from_upload':True}}
    assert form.refresh_processed_media(raw,frozen)==processed
    for broken in ('proof','ticket','uuid','width','cover'):
        value=deepcopy(raw)
        if broken=='proof':value['clip_bindings']['processed_from_upload']=False
        if broken=='ticket':value['clip_id']='other'
        if broken=='uuid':value['media']['md5sum']='other'
        if broken=='width':value['media']['width']=1
        if broken=='cover':value['media']['thumbUrl']='https://example.invalid/other'
        assert form.refresh_processed_media(value,frozen) is None
    assert form.owned_media(dict(media,fileSize=None)) is None


def test_native_processed_upload_readback_is_bound_to_exact_record(offline_page):
    mount_forms(offline_page,[''])
    offline_page.evaluate('''() => {
      const vm=document.querySelector('#wujie-host').shadowRoot.querySelector('.post-create').__vue__;
      const media=vm.$refs.postCreate.combinedStore.postObjDesc.media[0];
      Object.assign(media,{md5sum:'owned-uuid',url:'https://example.invalid/processed',urlCdnTaskId:'ticket',fileSize:undefined,width:1920,height:1080,videoPlayLen:3});
      vm.$refs.postCreate.combinedStore.createPostStatus={clipTicket:{draftId:'ticket'}};
      vm.$children=[{$el:vm.$el,fileList:[{uuid:'owned-uuid',processedResponse:{url:media.url,width:1920,height:1080,duration:3}}]}];
    }''')
    raw=offline_page.evaluate(form.READ_FORM)
    assert raw['clip_bindings']['processed_from_upload'] is True
    offline_page.evaluate('''() => {
      document.querySelector('#wujie-host').shadowRoot.querySelector('.post-create').__vue__.$children[0].fileList[0].uuid='other';
    }''')
    assert offline_page.evaluate(form.READ_FORM)['clip_bindings']['processed_from_upload'] is False


def test_actual_location_selectors_clear_default_without_accepting_other_location(case,offline_page):
    adapter,*_=case
    mount_forms(offline_page,[''])
    offline_page.evaluate("""() => {
      const el=document.querySelector('#wujie-host').shadowRoot.querySelector('.post-create');
      const store=el.__vue__.$refs.postCreate.combinedStore;
      store.postObjDesc.location={city:'Fixture automatic city'};
      el.insertAdjacentHTML('beforeend','<div class="post-position-wrap"><div class="position-display-wrap">Fixture city</div><div class="location-filter-wrap" style="display:none"><div class="location-item"><div class="name">不显示位置</div></div></div></div>');
      el.querySelector('.position-display-wrap').onclick=()=>el.querySelector('.location-filter-wrap').style.display='block';
      el.querySelector('.name').onclick=()=>{store.postObjDesc.location={};el.querySelector('.location-filter-wrap').style.display='none'};
    }""")
    session=SimpleNamespace()
    raw=offline_page.evaluate(form.READ_FORM)
    assert not raw['no_extras']
    assert not adapter._clear_location(session,offline_page,raw)
    raw=offline_page.evaluate(form.READ_FORM)
    assert adapter._clear_location(session,offline_page,raw)
    assert raw['no_extras']
    adapter.close()


def test_processed_form_and_request_keep_schedule_and_original_upload(payload,media):
    f=form.validate_form(payload);frozen=dict(media,urlCdnTaskId='native-ticket')
    processed=dict(frozen,url='https://example.invalid/processed')
    del processed['fileSize']
    raw=live_form(f,processed)
    raw.update(clip_id='native-ticket',clip_bindings={'record_ticket_matches':True,'processed_from_upload':True})
    refreshed=form.refresh_processed_media(raw,frozen)
    assert refreshed==processed
    assert form.verify_form(raw,f,refreshed,'finder-test')
    request=page_request(f,processed);request['videoClipTaskId']='native-ticket'
    assert form.request_matches(request,f,refreshed)
    request['effectiveTime']=0
    assert not form.request_matches(request,f,refreshed)
    request['effectiveTime']=f['effective_time'];request['videoClipTaskId']='foreign-ticket'
    assert not form.request_matches(request,f,refreshed)
