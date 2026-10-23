"""Read-only finite differences never authorize a changed form or clip."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from test_channels_multi_publish import case, module
from test_channels_publish_form import live_form
from test_channels_dom_visibility import offline_browser, offline_page, mount_forms
from yijianjifa.webui import channels_publish_form as form


@pytest.mark.parametrize('field,value', [
    ('url', 'http://SECRET.invalid/media'),
    ('md5sum', None),
    ('fileSize', 'SECRET-size'),
    ('width', 0),
    ('height', False),
    ('videoPlayLen', None),
    ('thumbUrl', 'http://SECRET.invalid/thumb'),
    ('mediaType', 2),
])
def test_last_form_observation_identifies_media_field_without_its_value(case, field, value):
    adapter, payload, _, _, _, media = case
    fields = form.validate_form(payload)
    raw = live_form(fields, media)
    s = SimpleNamespace(payload=payload, owned_media=deepcopy(media), owned_declaration=None)
    raw['media'][field] = value
    adapter._observe_form_gates(s, raw, fields)
    observation = s.form_observation
    assert observation['media_invalid_fields'] == [field]
    assert observation['frozen_media_changed_fields'] == [field]
    assert observation['checks']['owned_media'] is False
    assert observation['checks']['frozen_media_matches'] is False
    assert not form.verify_form(raw, fields, s.owned_media, payload['platform_user_id'])
    assert all(secret not in json.dumps(observation) for secret in ('SECRET', 'owned.mp4', 'owned-uuid', 'finder-test'))
    adapter.close()


def test_missing_media_is_a_finite_record_failure(case):
    adapter, payload, _, _, _, media = case
    fields = form.validate_form(payload); raw = live_form(fields, media); raw['media'] = None
    s = SimpleNamespace(payload=payload, owned_media=media, owned_declaration=None)
    adapter._observe_form_gates(s, raw, fields)
    assert s.form_observation['media_invalid_fields'] == ['record']
    assert s.form_observation['checks']['owned_media'] is False
    adapter.close()


@pytest.mark.parametrize('caption,shape', [
    (' Title\nBody #topic ', 'matches_trimmed'),
    ('Title\nBody\u00a0#topic', 'matches_nbsp_as_space'),
    ('Title Body #topic', 'matches_whitespace_collapsed'),
])
def test_caption_difference_shape_preserves_internal_content(case, caption, shape):
    adapter, payload, _, _, _, media = case
    payload['fields'].update(title='Title', body='Body', tags='#topic')
    fields = form.validate_form(payload); raw = live_form(fields, media)
    raw.update(caption=caption, state_caption=caption)
    s = SimpleNamespace(payload=payload, owned_media=media, owned_declaration=None)
    adapter._observe_form_gates(s, raw, fields)
    result = s.form_observation['caption_shape']
    assert result['dom_' + shape] is True and result['state_' + shape] is True
    assert result['dom_state_equal'] is True
    accepted = shape == 'matches_trimmed'
    assert s.form_observation['checks']['caption_matches'] is accepted
    assert s.form_observation['checks']['state_caption_matches'] is accepted
    assert form.verify_form(raw, fields, media, payload['platform_user_id']) is accepted
    assert all(type(v) is bool for v in result.values())
    assert all(text not in json.dumps(s.form_observation) for text in ('Title', 'Body', '#topic'))
    adapter.close()


def test_unexpected_diagnostic_keys_and_values_cannot_escape_to_log(case):
    adapter, payload, _, _, _, media = case
    fields = form.validate_form(payload); raw = live_form(fields, media)
    raw.update(extras_checks={'SECRET-name': True, 'no_bgm': 'SECRET-content'},
               clip_bindings={'SECRET-id': True, 'record_ticket_matches': 'SECRET-ticket'},
               caption='SECRET-caption', state_caption=None)
    raw['media']['SECRET-ref'] = 'SECRET-media'
    s = SimpleNamespace(payload=payload, owned_media=media, owned_declaration=None)
    adapter._observe_form_gates(s, raw, fields)
    observation = s.form_observation
    assert observation['extras_checks']['no_bgm'] == 'unknown'
    assert observation['clip_bindings']['record_ticket_matches'] == 'unknown'
    assert observation['caption_shape']['state_is_text'] is False
    assert 'SECRET' not in json.dumps(observation)
    adapter.close()


@pytest.mark.parametrize('field,key,value', [
    ('vm', 'componentId', 'no_component'),
    ('vm', 'member', 'no_member'),
    ('vm', 'isForVip', 'ordinary_mode'),
    ('vm', 'bgmData', 'no_bgm'),
    ('vm', 'extReading', 'no_external_link'),
    ('desc', 'location', 'empty_location'),
    ('desc', 'topic', 'no_collection'),
    ('desc', 'event', 'empty_event'),
    ('desc', 'mentionedUser', 'no_mentions'),
    ('temp', 'longitude', 'neutral_longitude'),
    ('temp', 'latitude', 'neutral_latitude'),
    ('temp', 'feedLongitude', 'neutral_feed_longitude'),
    ('temp', 'feedLatitude', 'neutral_feed_latitude'),
    ('temp', 'originalFlag', 'neutral_original_flag'),
])
def test_real_read_script_reports_specific_extra_as_boolean_without_leaking_value(offline_page, field, key, value):
    mount_forms(offline_page, [''])
    offline_page.evaluate('''args => {
      const vm=document.querySelector('#wujie-host').shadowRoot.querySelector('.post-create').__vue__;
      const state=vm.$refs.postCreate.combinedStore;
      const target=args.field==='vm'?vm:args.field==='desc'?state.postObjDesc:state.tempPost;
      const values={componentId:'SECRET',member:{memberLevel:1},isForVip:true,bgmData:{url:'SECRET'},
        extReading:{link:'SECRET'},location:{uid:'SECRET'},topic:{collectionId:'SECRET'},event:{id:'SECRET'},
        mentionedUser:[{id:'SECRET'}],longitude:1,latitude:1,feedLongitude:1,feedLatitude:1,originalFlag:1};
      target[args.key]=values[args.key];
    }''', dict(field=field, key=key))
    raw = offline_page.evaluate(form.READ_FORM)
    assert raw['no_extras'] is False and raw['extras_checks'][value] is False
    assert all(type(v) is bool for v in raw['extras_checks'].values())
    assert 'SECRET' not in json.dumps(raw['extras_checks'])


@pytest.mark.parametrize('ticket,media_task,expected', [
    ('SECRET-ticket', 'SECRET-ticket', True),
    ('SECRET-ticket', 'SECRET-other', False),
    ('', 'SECRET-ticket', False),
    (None, None, False),
    (42, 42, False),
])
def test_clip_association_is_only_a_boolean_and_never_clears_clip_gate(offline_page, ticket, media_task, expected):
    mount_forms(offline_page, [''])
    offline_page.evaluate('''args => {
      const state=document.querySelector('#wujie-host').shadowRoot.querySelector('.post-create').__vue__.$refs.postCreate.combinedStore;
      state.asyncClip=true;state.createPostStatus={clipTicket:{draftId:args.ticket}};
      state.postObjDesc.media[0].urlCdnTaskId=args.media_task;
    }''', dict(ticket=ticket, media_task=media_task))
    raw = offline_page.evaluate(form.READ_FORM)
    assert raw['clip_bindings']['async_processing'] is True
    assert raw['clip_bindings']['record_ticket_matches'] is expected
    assert 'SECRET' not in json.dumps(raw['clip_bindings'])
    # The existing raw clip gate is retained; matching IDs grant no permission.
    if type(ticket) is str and ticket:
        assert raw['clip_id'] == ticket
