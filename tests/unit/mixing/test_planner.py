from yijianjifa.features.mixing.planner import CompositionPlanner, SourceClip


def test_each_explicit_category_appears_in_every_composition():
    sources = [
        SourceClip("a", "ha", 8000, frozenset({"A"})),
        SourceClip("b", "hb", 8000, frozenset({"B"})),
        SourceClip("c", "hc", 8000, frozenset({"C"})),
    ]
    result = CompositionPlanner().plan(
        quantity=5,
        required_categories={"A", "B", "C"},
        sources=sources,
        segment_duration_ms=1000,
    )
    assert len(result.compositions) == 5
    assert all({"A", "B", "C"} <= c.covered_categories for c in result.compositions)
    assert result.relaxed_rules is False


def test_insufficient_unique_compositions_returns_partial_without_relaxing():
    sources = [SourceClip("only", "h-only", 1000, frozenset({"A"}))]
    result = CompositionPlanner().plan(
        quantity=10,
        required_categories={"A"},
        sources=sources,
        segment_duration_ms=1000,
    )
    assert len(result.compositions) == 1
    assert result.missing_count == 9
    assert result.relaxed_rules is False


def test_missing_required_category_returns_no_compositions_without_substitution():
    sources = [SourceClip("a", "ha", 5000, frozenset({"A"}))]
    result = CompositionPlanner().plan(
        quantity=3,
        required_categories={"A", "B"},
        sources=sources,
        segment_duration_ms=1000,
    )
    assert result.compositions == ()
    assert result.missing_count == 3
    assert result.relaxed_rules is False
