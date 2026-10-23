"""Build pre-2026-09-28 records solely for historical recovery coverage."""
from unittest.mock import patch
from yijianjifa.features.publish.schedule import PlatformLimits

def legacy_window():
    return patch('yijianjifa.app_services.publish_planning.PlatformLimits', new=lambda *args, **kwargs: PlatformLimits(2,14))
