from yijianjifa.features.mixing.planner import CompositionPlanner, SourceClip


def test_short_sources_chain_within_one_sentence_without_shortening_sentence():
    sources = [
        SourceClip("a", "ha", 1000, frozenset()),
        SourceClip("b", "hb", 1000, frozenset()),
    ]
    result = CompositionPlanner().plan_for_sentences(
        quantity=1,
        sentence_durations_ms=[2500],
        required_categories=set(),
        sources=sources,
    )
    composition = result.compositions[0]
    assert sum(s.duration_ms for s in composition.segments) == 2500
    assert len(composition.segments) >= 3
    assert {s.sentence_index for s in composition.segments} == {0}


def test_more_required_categories_than_sentences_uses_multiple_visuals_inside_sentence():
    sources = [
        SourceClip("a", "ha", 5000, frozenset({"A"})),
        SourceClip("b", "hb", 5000, frozenset({"B"})),
        SourceClip("c", "hc", 5000, frozenset({"C"})),
    ]
    result = CompositionPlanner().plan_for_sentences(
        quantity=1,
        sentence_durations_ms=[3000],
        required_categories={"A", "B", "C"},
        sources=sources,
    )
    composition = result.compositions[0]
    assert {"A", "B", "C"} <= composition.covered_categories
    assert len(composition.segments) >= 3
    assert all(s.sentence_index == 0 for s in composition.segments)
    assert sum(s.duration_ms for s in composition.segments) == 3000


def test_each_sentence_starts_a_new_visual_segment():
    sources = [SourceClip("a", "ha", 5000, frozenset())]
    result = CompositionPlanner().plan_for_sentences(
        quantity=1,
        sentence_durations_ms=[1000, 1200],
        required_categories=set(),
        sources=sources,
    )
    composition = result.compositions[0]
    starts = [s for s in composition.segments if s.sentence_index in {0, 1}]
    assert [s.sentence_index for s in starts] == [0, 1]
    assert [s.duration_ms for s in starts] == [1000, 1200]


def test_seeded_batch_varies_openings_despite_different_copy_lengths():
    sources = [SourceClip(str(i), str(i), 30000, frozenset({'A'})) for i in range(14)]
    planner = CompositionPlanner()
    results = []
    for index in range(10):
        args = dict(quantity=1, sentence_durations_ms=[3000, 3000, 1000 + index * 100],
                    required_categories={'A'}, sources=sources,
                    variation_seed=731, variant_index=index)
        result = planner.plan_for_sentences(**args).compositions[0]
        assert result == planner.plan_for_sentences(**args).compositions[0]
        assert len({s.asset_id for s in result.segments}) == 3
        assert sum(s.duration_ms for s in result.segments) == 7000 + index * 100
        assert all(0 <= s.source_start_ms <= 30000 - s.duration_ms for s in result.segments)
        results.append(result)
    assert len({r.segments[0].asset_id for r in results}) == 10
    assert len({r.segments[0].source_start_ms for r in results}) > 5


def test_seeded_plan_preserves_category_coverage_with_short_sources():
    sources = [SourceClip('a', 'a', 500, frozenset({'A'})),
               SourceClip('b', 'b', 800, frozenset({'B'}))]
    result = CompositionPlanner().plan_for_sentences(quantity=1,
        sentence_durations_ms=[2000, 1500], required_categories={'A','B'},
        sources=sources, variation_seed=11, variant_index=4).compositions[0]
    assert result.covered_categories == {'A', 'B'}
    assert sum(s.duration_ms for s in result.segments) == 3500
    assert all(a.asset_id != b.asset_id for a,b in zip(result.segments,result.segments[1:]))
