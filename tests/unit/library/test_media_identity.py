from yijianjifa.features.library.media_identity import sha256_file


def test_same_bytes_with_different_name_have_same_identity(tmp_path):
    a = tmp_path / "a.mp4"
    b = tmp_path / "renamed.mp4"
    a.write_bytes(b"same-bytes")
    b.write_bytes(b"same-bytes")
    assert sha256_file(a) == sha256_file(b)
