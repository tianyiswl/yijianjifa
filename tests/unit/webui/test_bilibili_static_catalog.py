"""Offline user choices never masquerade as account-specific platform IDs."""
import copy
import pytest

from yijianjifa.app_services import bilibili_static_catalog as c


def test_public_names_are_local_choices_without_human_platform_ids():
    value=c.builtin_options()
    assert value['status']=='ready' and value['source']=='builtin'
    human=[row for row in value['category_choices'] if row['mode']=='human']
    assert len(human)==31 and any(row['choice']=='human:tech' and row['name']=='科技数码' for row in human)
    assert all('id' not in row for row in human)
    assert value['creation_statement_choices']==[
        {'choice':'no_mark','name':'不添加声明'}, {'choice':'reprint','name':'转载'},
        {'choice':'ai_generated','name':'该视频使用人工智能合成技术'},
        {'choice':'fiction_entertainment','name':'该内容仅供娱乐，请勿过分解读'}]
    assert 'schedule' not in value and 'session' not in repr(value)
    value['category_choices'].clear()
    assert c.builtin_options()['category_choices']
    assert len(value['creation_statement_choices'])==4
    assert all(row['mode']=='human' for row in c.builtin_options()['category_choices'])


def test_local_intent_keeps_frozen_input_and_requires_explicit_supported_choices():
    fields={'category_choice':'human:tech','creation_statement_choice':'no_mark'}
    before=copy.deepcopy(fields)
    local=c.resolve_fields(fields,local=True)
    assert local['category_id'] is None and local['category_name']=='科技数码'
    assert local['creation_statement_id']==-1
    assert fields==before
    for key,value in [('category_choice','human:invented'),('creation_statement_choice','original'),('category_choice',{}),('creation_statement_choice',True)]:
        with pytest.raises(ValueError):c.resolve_fields({**fields,key:value},local=True)


def test_actual_form_resolves_unique_names_and_basic_ids_with_actual_official_text():
    fields={'category_choice':'human:tech','creation_statement_choice':'no_mark'}
    options={'categories':[{'id':444,'name':'科技数码','mode':'human'}],
        'statements':[{'id':-1,'name':'当前平台官方无声明文案'}]}
    actual=c.resolve_fields(fields,options)
    assert actual['category_id']==444 and actual['creation_statement_name']=='当前平台官方无声明文案'
    assert 'category_id' not in fields and 'creation_statement_name' not in fields


@pytest.mark.parametrize('categories,statements',[
    ([],[{'id':-1,'name':'不声明'}]),
    ([{'id':444,'name':'科技数码','mode':'legacy'}],[{'id':-1,'name':'不声明'}]),
    ([{'id':444,'name':'科技数码','mode':'human'},{'id':445,'name':'科技数码','mode':'human'}],[{'id':-1,'name':'不声明'}]),
    ([{'id':444,'name':'科技数码','mode':'human'}],[]),
    ([{'id':444,'name':'科技数码','mode':'human'}],[{'id':-1,'name':'不声明'},{'id':-1,'name':'不声明'}]),
    ([{'id':True,'name':'科技数码','mode':'human'}],[{'id':-1,'name':'不声明'}]),
])
def test_missing_ambiguous_or_wrong_system_does_not_guess(categories,statements):
    with pytest.raises(ValueError):c.resolve_fields({'category_choice':'human:tech','creation_statement_choice':'no_mark'},dict(categories=categories,statements=statements))


def test_conflicting_concrete_fields_cannot_override_the_frozen_intent():
    for extra in ({'category_id':999},{'creation_statement_id':9},{'category_name':'其他'}, {'creation_statement_name':'其他'}):
        with pytest.raises(ValueError):c.resolve_fields({'category_choice':'human:tech','creation_statement_choice':'no_mark',**extra},local=True)


def test_ai_intent_resolves_only_the_exact_official_announcement_text_to_positive_id():
    fields={'creation_statement_choice':'ai_generated'}
    assert c.resolve_fields(fields,local=True)['creation_statement_id'] is None
    assert c.resolve_fields(fields,{'statements':[{'id':8,'name':'该视频使用人工智能合成技术'}]})['creation_statement_id']==8
    assert c.resolve_fields(fields,{'statements':[{'id':9,'name':'该内容使用人工智能合成技术'}]})['creation_statement_id']==9
    for rows in ([{'id':8,'name':'AI生成'}],[{'id':-1,'name':'该视频使用人工智能合成技术'}],
                 [{'id':8,'name':'该视频使用人工智能合成技术'},{'id':9,'name':'该视频使用人工智能合成技术'}]):
        with pytest.raises(ValueError):c.resolve_fields(fields,{'statements':rows})


def test_official_fiction_statement_does_not_guess_positive_ids_or_accept_two_ai_aliases():
    value={'creation_statement_choice':'fiction_entertainment'}
    assert c.resolve_fields(value,local=True)['creation_statement_id'] is None
    assert c.resolve_fields(value,{'statements':[{'id':77,'name':'该内容仅供娱乐，请勿过分解读'}]})['creation_statement_id']==77
    with pytest.raises(ValueError):c.resolve_fields(value,{'statements':[{'id':77,'name':'内容仅供娱乐'}]})
    with pytest.raises(ValueError):c.resolve_fields({'creation_statement_choice':'ai_generated'},{'statements':[
        {'id':8,'name':'该视频使用人工智能合成技术'},{'id':9,'name':'该内容使用人工智能合成技术'}]})


def test_legacy_intent_requires_the_exact_official_id_name_parent_and_mode():
    fields={'category_choice':'legacy:201'}
    expected={'id':201,'name':'科学科普','parent_name':'知识','mode':'legacy'}
    assert c.resolve_fields(fields,{'categories':[expected]})['category_id']==201
    for changed in ({'id':202},{'name':'科普'},{'parent_name':'其他'},{'mode':'human'}):
        with pytest.raises(ValueError):c.resolve_fields(fields,{'categories':[{**expected,**changed}]})


@pytest.mark.parametrize('saved',[
    {},
    {'category_choice':'human:tech','creation_statement_choice':'ai_generated'},
    {'category_choice':'legacy:201','creation_statement_choice':'reprint','reprint_source':'原出处'},
    {'category_id':201,'category_name':'科学科普','category_mode':'legacy','creation_statement_id':-1,'creation_statement_name':'原名称'},
])
def test_defaults_only_fill_unconfigured_fields_and_leave_saved_choices_untouched(saved):
    original={**saved,'tags':['保留标签'],'content_authorization':False}
    before=copy.deepcopy(original)
    result=c.with_defaults(original)
    assert original==before and result is not original
    for key,value in saved.items():assert result[key]==value
    if not saved:assert all(result[key]==value for key,value in c.DEFAULT_FIELDS.items())
    else:assert result==before
    result['tags'].append('不改变原始资料')
    assert original['tags']==['保留标签']
    assert result['content_authorization'] is False


def test_each_default_is_independent_and_legacy_choices_remain_valid_without_being_offered():
    assert c.with_defaults({'category_choice':'human:tech'})=={
        'category_choice':'human:tech','creation_statement_choice':'no_mark'}
    assert c.with_defaults({'creation_statement_id':8})=={
        'category_choice':'human:knowledge','creation_statement_id':8}
    assert c.valid_category_choice('legacy:201')
    assert not any(row['choice'].startswith('legacy:') for row in c.category_choices())
    value=c.resolve_fields(c.with_defaults({}),local=True)
    assert value['category_name']=='知识' and value['category_mode']=='human'
    assert value['category_id'] is None and value['creation_statement_id']==-1
