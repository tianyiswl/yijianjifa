from yijianjifa.features.mixing.models import MixPlan
from yijianjifa.features.mixing.planner import CompositionPlanner, SourceClip
from yijianjifa.features.finished.covers import validate_frame_time


def test_phase2_quick_defaults_and_sentence_chain_contract():
    plan = MixPlan.quick()
    assert (plan.quantity, plan.resolution, plan.frame_fit) == (10, "720p", "preserve_blur")
    assert plan.transition.mode == "none"
    assert plan.background_music.mode == "none"

    result = CompositionPlanner().plan_for_sentences(
        quantity=1,
        sentence_durations_ms=[2500],
        required_categories=set(),
        sources=[
            SourceClip("a", "ha", 1000, frozenset()),
            SourceClip("b", "hb", 1000, frozenset()),
        ],
    )
    assert sum(x.duration_ms for x in result.compositions[0].segments) == 2500


def test_phase2_cover_boundary_contract():
    assert validate_frame_time(5000, 4999).ok
    assert not validate_frame_time(5000, 5000).ok
