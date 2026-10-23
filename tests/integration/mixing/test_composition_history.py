from yijianjifa.features.mixing.history import CompositionHistoryRepository
from yijianjifa.features.mixing.models import Composition, SegmentUse
from yijianjifa.features.mixing.fingerprint import composition_fingerprint
from yijianjifa.features.mixing.planner import CompositionPlanner, SourceClip


def test_composition_history_persists_and_blocks_exact_prior_composition(db_engine):
    history = CompositionHistoryRepository(db_engine)
    prior = Composition((SegmentUse("a", "hash-a", 0, 1000, frozenset({"A"})),))
    fingerprint = composition_fingerprint(prior)
    history.remember(fingerprint)

    assert history.seen(fingerprint)
    result = CompositionPlanner().plan(
        quantity=1,
        required_categories={"A"},
        sources=[SourceClip("a", "hash-a", 1000, frozenset({"A"}))],
        segment_duration_ms=1000,
        historical_fingerprints=history.all_fingerprints(),
    )
    assert result.compositions == ()
    assert result.missing_count == 1
