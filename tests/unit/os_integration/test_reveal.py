from yijianjifa.os_integration.reveal import reveal_path


def test_reveal_missing_path_returns_false(tmp_path):
    assert reveal_path(tmp_path / "missing.mp4", dry_run=True) is False


def test_reveal_existing_path_dry_run_returns_true(tmp_path):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"x")
    assert reveal_path(path, dry_run=True) is True
