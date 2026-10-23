"""Host publication wiring only; no browser, account login or real submission."""
import json
import sys
from types import ModuleType, SimpleNamespace

import pytest

from yijianjifa.app_services.desktop_api import DesktopApi
from yijianjifa.features.accounts.service import AccountService


@pytest.fixture
def browser_forbidden(monkeypatch):
    import playwright.sync_api

    def forbidden(*args, **kwargs):
        raise AssertionError('host registration must not open a browser')

    monkeypatch.setattr(playwright.sync_api, 'sync_playwright', forbidden)


@pytest.fixture
def creator_adapter(monkeypatch, browser_forbidden):
    created = []

    class SyntheticCreatorAdapter:
        platform_id = 'kuaishou'
        verified_capabilities = frozenset({'ordinary', 'publish_now', 'software_schedule', 'result_query'})
        public_field_schema = {'title': {'type': 'string'}, 'body': {'type': 'string'}, 'tags': {'type': 'string'}}

        def __init__(self, engine, paths):
            self.engine, self.paths = engine, paths
            self.driver = None
            created.append(self)

        def submit(self, *args, **kwargs):
            raise AssertionError('registration test must not submit')

        def reconcile(self, *args, **kwargs):
            raise AssertionError('registration test must not query a platform')

        def request_shutdown(self):
            pass

    for platform, name in [('kuaishou','Kuaishou'),('bilibili','Bilibili'),('channels','Channels')]:
        cls=type('Synthetic'+name+'Adapter',(SyntheticCreatorAdapter,),{'platform_id':platform})
        module = ModuleType('yijianjifa.webui.'+platform+'_multi_publish')
        setattr(module,name+'MultiPublishAdapter',cls)
        monkeypatch.setitem(sys.modules, module.__name__, module)
    return SyntheticCreatorAdapter, created


def make_host(name, engine, paths):
    if name == 'mac':
        if sys.platform != 'darwin':
            pytest.skip('Mac native host requires macOS; Windows host is checked separately')
        from yijianjifa.webui.douyin_auth import make_douyin_provider
        return make_douyin_provider(engine, paths, expected_uid='123')
    from yijianjifa.webui.windows_provider import make_windows_provider
    return make_windows_provider(engine, paths)


def test_creator_publication_factory_builds_all_three_with_the_exact_host_resources(
        db_engine, app_paths, creator_adapter):
    from yijianjifa.webui.creator_providers import make_creator_publish_adapters
    cls, created = creator_adapter
    result = make_creator_publish_adapters(db_engine, app_paths)
    assert set(result) == {'kuaishou','bilibili','channels'}
    assert created == list(result.values())
    assert all(isinstance(a,cls) and a.engine is db_engine and a.paths is app_paths for a in result.values())
    assert AccountService(db_engine).list() == []


@pytest.mark.parametrize('host', ['mac', 'windows'])
def test_native_hosts_add_the_creator_adapter_without_replacing_ordinary_douyin(
        db_engine, app_paths, creator_adapter, host):
    cls, _ = creator_adapter
    provider = make_host(host, db_engine, app_paths)
    assert set(provider.publish_adapters) == {'douyin', 'kuaishou','bilibili','channels','douyin:image_post'}
    assert provider.publish_adapters['douyin'].platform_id == 'douyin'
    assert provider.publish_adapters['douyin'].driver is None
    assert isinstance(provider.publish_adapters['kuaishou'], cls)
    assert provider.publish_adapters['kuaishou'].driver is None
    assert callable(provider.scheduled_publish) and callable(provider.open_backend)
    if host == 'mac':
        assert provider.publish_adapters['douyin'].expected_uid == '123'
    else:
        assert set(provider.login_providers) == {'kuaishou', 'channels', 'bilibili'}
    assert AccountService(db_engine).list() == []


@pytest.mark.parametrize('host', ['mac', 'windows'])
def test_host_catalog_advertises_code_support_without_modifying_account_capabilities(
        db_engine, app_paths, creator_adapter, host):
    accounts = AccountService(db_engine)
    saved = [accounts.create(platform, 'synthetic-' + platform, '合成 ' + platform)
             for platform in ('douyin', 'kuaishou', 'channels', 'bilibili')]
    saved = [accounts.set_login_state(a.id, 'pending_verification') for a in saved]
    before = {a.id: a.capabilities for a in saved}
    provider = make_host(host, db_engine, app_paths)
    api = DesktopApi(db_engine, app_paths, login_provider=provider)
    try:
        result = api.dispatch(json.dumps({'v': 1, 'request_id': 'host-catalog',
            'operation': 'multi_publish.catalog', 'params': {}}), trusted_origin=True)
        assert result['ok']
        catalog = {item['platform']: item for item in result['data']['items']}
        assert catalog['kuaishou']['transport_available'] and catalog['kuaishou']['ordinary_ready']
        assert catalog['kuaishou']['publish_now_ready'] and catalog['kuaishou']['query_ready']
        assert catalog['kuaishou']['schedule_ready'] is False
        for platform in ('channels', 'bilibili'):
            assert catalog[platform]['transport_available'] is True
            assert catalog[platform]['ordinary_ready'] is True
            assert catalog[platform]['schedule_ready'] is False
        assert {a.id: a.capabilities for a in accounts.list()} == before
        assert all(accounts.get(a.id).login_state == 'pending_verification' for a in saved)
    finally:
        assert api.close(1)


@pytest.mark.parametrize('host', ['mac', 'windows'])
def test_duplicate_kuaishou_official_service_is_rejected_instead_of_overriding_native_adapter(
        db_engine, app_paths, creator_adapter, host):
    provider = make_host(host, db_engine, app_paths)
    service = SimpleNamespace(**{name: lambda *args, **kwargs: None for name in
        ('binding', 'observe', 'category_choices', 'stage', 'execute_final', 'query')})
    service.category_choices = lambda: [{'id': 138, 'label': '合成分区'}]
    with pytest.raises(ValueError, match='duplicate host platform adapter'):
        DesktopApi(db_engine, app_paths, login_provider=provider, official_publish_service=service)
    assert set(provider.publish_adapters) == {'douyin', 'kuaishou','bilibili','channels','douyin:image_post'}
    assert AccountService(db_engine).list() == []


def test_actual_creator_publication_factory_is_lazy_for_all_three_platforms(
        db_engine, app_paths, browser_forbidden):
    from yijianjifa.webui.creator_providers import make_creator_publish_adapters
    from yijianjifa.webui.kuaishou_multi_publish import KuaishouMultiPublishAdapter
    from yijianjifa.webui.bilibili_multi_publish import BilibiliMultiPublishAdapter
    from yijianjifa.webui.channels_multi_publish import ChannelsMultiPublishAdapter
    result = make_creator_publish_adapters(db_engine, app_paths)
    assert set(result) == {'kuaishou','bilibili','channels'}
    for platform,cls in [('kuaishou',KuaishouMultiPublishAdapter),('bilibili',BilibiliMultiPublishAdapter),('channels',ChannelsMultiPublishAdapter)]:
        adapter=result[platform]
        assert isinstance(adapter,cls) and adapter.platform_id==platform
        assert adapter.engine is db_engine and adapter.paths is app_paths and adapter.driver is None
    assert AccountService(db_engine).list() == []
