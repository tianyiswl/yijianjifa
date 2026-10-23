from yijianjifa.app_services import channels_declaration_catalog as catalog
from yijianjifa.webui.channels_multi_publish import ChannelsMultiPublishAdapter


def test_catalog_has_local_semantics_without_platform_ids_or_original_rights():
    rows = catalog.choices()
    assert rows == [{'choice': 'none', 'name': '不添加内容声明'},
                    {'choice': 'ai_generated', 'name': '内容为 AI 技术制作，虚构内容请谨慎甄别'}]
    assert all(set(row) == {'choice', 'name'} for row in rows)
    assert catalog.valid_choice('none') and catalog.valid_choice('ai_generated')
    assert not catalog.valid_choice('fictional_performance') and not catalog.valid_choice(True)
    rows[1]['name'] = 'foreign'
    assert catalog.choices()[1]['name'] == catalog.AI_LABEL
    schema = ChannelsMultiPublishAdapter.public_field_schema
    assert schema['content_declaration_choice']['choices'] == catalog.choices()
    assert schema['channels_original']['default'] is False
    assert schema['content_declaration_choice']['evidence_status'] == 'partial'
