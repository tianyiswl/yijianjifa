import os
from pathlib import Path

import pytest

from media_registry import MediaRegistry, ResourceDenied, byte_range


def test_ranges_and_invalid_inputs():
    assert byte_range(None, 10) == (0, 9, 200)
    assert byte_range('bytes=2-5', 10) == (2, 5, 206)
    assert byte_range('bytes=8-', 10) == (8, 9, 206)
    assert byte_range('bytes=-3', 10) == (7, 9, 206)
    assert byte_range('bytes=0-100', 10) == (0, 9, 206)
    for header in ('bytes=10-', 'bytes=5-2', 'bytes=0-1,4-5', 'bytes=-0', 'items=0-1'):
        with pytest.raises(ResourceDenied):
            byte_range(header, 10)


def test_only_registered_handles_and_bounded_reads(tmp_path):
    source = tmp_path / 'clip.mp4'
    source.write_bytes(b'0123456789')
    with MediaRegistry() as registry:
        token = registry.grant(source)
        assert str(source) not in token
        assert registry.read(token, 3, 4) == b'3456'
        assert registry.size(token) == 10
        for unknown in (str(source), '../clip.mp4', '%2e%2e', 'unknown'):
            with pytest.raises(ResourceDenied):
                registry.read(unknown, 0, 1)
        for offset, length in ((-1, 2), (0, 1048577), (10, 1), (0, -1)):
            with pytest.raises(ResourceDenied):
                registry.read(token, offset, length)
        registry.revoke(token)
        with pytest.raises(ResourceDenied):
            registry.read(token, 0, 1)


def test_pinned_descriptor_does_not_follow_replacement(tmp_path):
    source = tmp_path / 'clip.mp4'
    source.write_bytes(b'original')
    with MediaRegistry() as registry:
        token = registry.grant(source)
        replacement = tmp_path / 'new.mp4'
        replacement.write_bytes(b'secret!!')
        os.replace(replacement, source)
        assert registry.read(token, 0, 8) == b'original'


def test_mutation_and_symlinks_denied(tmp_path):
    source = tmp_path / 'clip.mp4'
    source.write_bytes(b'original')
    link = tmp_path / 'link.mp4'
    link.symlink_to(source)
    with MediaRegistry() as registry:
        with pytest.raises(ResourceDenied):
            registry.grant(link)
        token = registry.grant(source)
        source.write_bytes(b'changed-length')
        with pytest.raises(ResourceDenied):
            registry.read(token, 0, 2)


def test_close_revokes_all_and_prevents_new_grants(tmp_path):
    source = tmp_path / 'clip.mp4'
    source.write_bytes(b'abc')
    registry = MediaRegistry()
    token = registry.grant(source)
    registry.close()
    with pytest.raises(ResourceDenied):
        registry.read(token, 0, 1)
    with pytest.raises(ResourceDenied):
        registry.grant(source)
