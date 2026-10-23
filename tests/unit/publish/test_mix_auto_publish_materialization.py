from yijianjifa.features.mixing.models import MixAutoPublishIntent
from yijianjifa.features.publish.normal_service import NormalPublishService


def test_failed_mix_does_not_shift_or_fill_auto_publish_slots(db_engine):
    service = NormalPublishService(db_engine)
    intent = MixAutoPublishIntent(enabled=True, account_id="A1", chain_mode="per_item")
    result = service.materialize_mix_auto_publish(
        intent,
        successful_outputs={0: "v1", 2: "v3"},
        planned_times=[100, 200, 300],
    )
    assert [(x.video_id, x.planned_time) for x in result] == [("v1", 100), ("v3", 300)]


def test_materialized_mix_auto_publish_contains_no_commerce_location_or_declaration(db_engine):
    service = NormalPublishService(db_engine)
    intent = MixAutoPublishIntent(enabled=True, account_id="A1", chain_mode="whole_batch")
    item = service.materialize_mix_auto_publish(intent, successful_outputs={0: "v1"}, planned_times=[100])[0]
    assert item.platform == "douyin"
    assert item.account_id == "A1"
    assert item.content_type == "normal_video"
    assert not hasattr(item, "location")
    assert not hasattr(item, "declaration")
    assert not hasattr(item, "commerce")
