from yijianjifa.features.mixing.fingerprint import composition_fingerprint
from yijianjifa.features.mixing.models import Composition, SegmentUse


def _composition(order):
    return Composition(
        segments=tuple(
            SegmentUse(
                asset_id=asset,
                asset_content_hash=f"hash-{asset}",
                source_start_ms=start,
                duration_ms=duration,
                category_ids=frozenset(),
            )
            for asset, start, duration in order
        )
    )


def test_fingerprint_changes_when_order_start_or_duration_changes():
    a = _composition([("A", 0, 5000), ("B", 0, 5000)])
    b = _composition([("B", 0, 5000), ("A", 0, 5000)])
    c = _composition([("A", 1000, 5000), ("B", 0, 5000)])
    d = _composition([("A", 0, 4500), ("B", 0, 5500)])
    assert len({composition_fingerprint(x) for x in (a, b, c, d)}) == 4


def test_fingerprint_ignores_asset_display_identity_when_content_is_same():
    a = Composition(segments=(SegmentUse("A", "same", 0, 5000, frozenset()),))
    b = Composition(segments=(SegmentUse("RENAMED", "same", 0, 5000, frozenset()),))
    assert composition_fingerprint(a) == composition_fingerprint(b)
