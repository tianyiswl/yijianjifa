"""Windows uses the same identity-checked isolated Chromium publishing service."""
def make_windows_provider(engine,paths):
    from .chromium_publish import make_chromium_login,make_chromium_transport
    provider=make_chromium_login(engine,paths)
    provider.scheduled_publish=make_chromium_transport(engine,paths)
    from .chromium_multi_publish import ChromiumMultiPublishAdapter
    from .douyin_image_publish import DouyinImagePostAdapter
    from .creator_providers import make_creator_publish_adapters
    provider.publish_adapters={'douyin':ChromiumMultiPublishAdapter(engine,paths),
                               'douyin:image_post':DouyinImagePostAdapter(engine,paths),
                               **make_creator_publish_adapters(engine,paths)}
    provider.open_backend=make_chromium_login(engine,paths,backend=True)
    from .chromium_probe import ChromiumProbe
    probe=ChromiumProbe(engine,paths)
    def metadata(*args,**kwargs):return probe.metadata(*args,**kwargs)
    metadata.close=probe.close
    provider.metadata=metadata
    provider.check_capabilities=probe.capability
    from .creator_providers import make_creator_login_providers
    provider.login_providers=make_creator_login_providers(engine,paths)
    return provider
