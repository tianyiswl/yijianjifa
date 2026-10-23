import pytest
from pydantic import ValidationError

from yijianjifa.features.mixing.models import MixAutoPublishIntent


def test_mix_auto_publish_defaults_are_restricted_and_disabled():
    intent = MixAutoPublishIntent()
    assert intent.enabled is False
    assert intent.platform == "douyin"
    assert intent.account_id is None
    assert intent.content_type == "normal_video"
    assert intent.publish_mode == "scheduled_only"
    assert intent.first_delay_hours == 3
    assert intent.interval_hours == 3
    assert intent.chain_mode == "per_item"


def test_enabled_mix_auto_publish_requires_one_account():
    with pytest.raises(ValidationError):
        MixAutoPublishIntent(enabled=True)
    intent = MixAutoPublishIntent(enabled=True, account_id="DY001")
    assert intent.account_id == "DY001"


def test_mix_auto_publish_cannot_be_changed_to_immediate_or_commerce():
    with pytest.raises(ValidationError):
        MixAutoPublishIntent(publish_mode="immediate")
    with pytest.raises(ValidationError):
        MixAutoPublishIntent(content_type="commerce")
