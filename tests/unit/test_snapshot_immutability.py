from yijianjifa.core.snapshots import FrozenSnapshot


def test_snapshot_does_not_change_when_source_mapping_changes():
    source = {"title": "A", "tags": ["x"]}
    snap = FrozenSnapshot.from_mapping(source)
    source["title"] = "B"
    source["tags"].append("y")
    assert snap.data == {"title": "A", "tags": ["x"]}
