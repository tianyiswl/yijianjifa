"""Current public B站 statement labels keep exact, observed AI attribution."""
from copy import deepcopy

import pytest

from yijianjifa.webui import bilibili_publish_form as form


def current_options(ai_id=1):
    # Public labels observed from the ordinary posting page on 2026-10-24.
    pre = {'type_mode': 1, 'neutral_mark': {'mark_list': [
        {'id': -1, 'content': '内容无需标注'},
        {'id': ai_id, 'content': '含AI生成内容'},
        {'id': 2, 'content': '含虚构演绎内容'},
        {'id': 3, 'content': '内容含营销信息'},
        {'id': 4, 'content': '个人观点，仅供参考'},
        {'id': -2, 'content': '内容为转载'},
    ]}}
    return form.safe_options(pre, [
        {'id': 1010, 'name': '知识'},
        {'id': 1012, 'name': '科技数码'},
    ])


def intent():
    return {'execution_mode': 'publish_now', 'scheduled_at': None, 'fields': {
        'title': '完整操作演示', 'body': '软件实录，配音与封面使用AI制作。',
        'tags': '#自动剪辑 #自动发布', 'category_choice': 'human:tech',
        'creation_statement_choice': 'ai_generated', 'content_authorization': True,
    }}


@pytest.mark.parametrize('observed_id', [1, 87])
def test_current_ai_label_resolves_observed_id_and_keeps_original_intent(observed_id):
    payload = intent()
    frozen = deepcopy(payload)
    options = current_options(observed_id)
    actual = form.validate_form(payload, options=options)
    assert actual['category_id'] == 1012
    assert actual['creation_statement_id'] == observed_id
    assert actual['creation_statement_name'] == '含AI生成内容'
    assert actual['copyright'] == 1
    assert actual['content_authorization'] is True
    assert actual['dtime'] == 0
    assert form.verify_options(options, actual)
    assert payload == frozen


@pytest.mark.parametrize('extra', [
    {'id': 88, 'name': '含AI生成内容'},
    {'id': 89, 'name': '该视频使用人工智能合成技术'},
    {'id': 90, 'name': '该内容使用人工智能合成技术'},
])
def test_multiple_current_or_old_ai_choices_remain_ambiguous(extra):
    options = current_options()
    options['statements'].append(extra)
    with pytest.raises(ValueError, match='声明.*不存在或不唯一'):
        form.validate_form(intent(), options=options)


@pytest.mark.parametrize('replacement', ['个人观点，仅供参考', '含AI生成内容（未经核验的新名称）'])
def test_current_ai_intent_never_guesses_an_id_or_fuzzy_label(replacement):
    options = current_options()
    options['statements'][1]['name'] = replacement
    with pytest.raises(ValueError, match='声明.*不存在或不唯一'):
        form.validate_form(intent(), options=options)
