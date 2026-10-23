"""The same integrated creator login and publication providers on both hosts."""


def make_creator_login_providers(engine, paths):
    from .kuaishou_auth import make_kuaishou_provider
    from .channels_auth import make_channels_provider
    from .bilibili_auth import make_bilibili_provider
    return {'kuaishou':make_kuaishou_provider(engine,paths),
            'channels':make_channels_provider(engine,paths),
            'bilibili':make_bilibili_provider(engine,paths)}


def make_creator_publish_adapters(engine, paths):
    """Construct host transports lazily; registration is not live submission."""
    from .kuaishou_multi_publish import KuaishouMultiPublishAdapter
    from .bilibili_multi_publish import BilibiliMultiPublishAdapter
    from .channels_multi_publish import ChannelsMultiPublishAdapter
    return {'kuaishou':KuaishouMultiPublishAdapter(engine,paths),
            'bilibili':BilibiliMultiPublishAdapter(engine,paths),
            'channels':ChannelsMultiPublishAdapter(engine,paths)}
