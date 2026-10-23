import importlib
import importlib.util
import json

import pytest


def catalog_type():
    name = 'yijianjifa.features.publish.platforms.catalog'
    assert importlib.util.find_spec(name) is not None, 'platform capability catalog is missing'
    return importlib.import_module(name).PlatformCatalog


def test_catalog_keeps_unintegrated_platforms_disabled_without_faking_capabilities():
    catalog = catalog_type()()
    rows = {row['platform']: row for row in catalog.list()}
    assert {key: row['label'] for key, row in rows.items()} == {
        'douyin': '抖音', 'kuaishou': '快手', 'channels': '视频号', 'bilibili': 'B站'}
    assert all(not row['transport_available'] for row in rows.values())
    assert rows['douyin']['verified_capabilities'] == ['ordinary', 'platform_schedule']
    for platform in ('kuaishou', 'channels', 'bilibili'):
        assert rows[platform]['verified_capabilities'] == []
        assert 'ordinary' in rows[platform]['pending_capabilities']
    assert 'publish_now' in rows['douyin']['pending_capabilities']
    assert 'custom_cover' in rows['douyin']['pending_capabilities']


def test_catalog_routes_exact_host_adapter_without_invoking_it_or_leaking_private_fields():
    class Adapter:
        platform_id = 'kuaishou'
        token = 'PRIVATE-TOKEN'
        path = '/private/profile'

        def submit(self, *args):
            raise AssertionError('catalog must not submit')

        def reconcile(self, *args):
            raise AssertionError('catalog must not query')

    adapter = Adapter()
    catalog = catalog_type()(transports={'kuaishou': adapter})
    assert catalog.transport_for('kuaishou') is adapter
    assert catalog.get('kuaishou')['transport_available'] is True
    assert catalog.get('douyin')['transport_available'] is False
    public = json.dumps(catalog.list())
    assert 'PRIVATE-TOKEN' not in public and '/private/profile' not in public
    with pytest.raises(ValueError):
        catalog.transport_for('douyin')
    for platform in ('wechat', 'unknown', '', 'Douyin'):
        with pytest.raises(ValueError):
            catalog.get(platform)
        with pytest.raises(ValueError):
            catalog.transport_for(platform)


@pytest.mark.parametrize('value', [True, False, {'available': True}, object()])
def test_catalog_rejects_frontend_availability_flags(value):
    with pytest.raises(ValueError):
        catalog_type()(transports={'douyin': value})


def test_catalog_accepts_host_callable_but_rejects_cross_platform_adapter():
    transport = lambda *args: None
    catalog = catalog_type()(transports={'douyin': transport})
    assert catalog.transport_for('douyin') is transport
    with pytest.raises(ValueError):
        catalog_type()(transports={'wechat': transport})

    class WrongAdapter:
        platform_id = 'douyin'
        def submit(self): pass
        def reconcile(self): pass

    with pytest.raises(ValueError):
        catalog_type()(transports={'kuaishou': WrongAdapter()})
