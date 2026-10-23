import pytest
from yijianjifa.features.downloads.input import normalize_inputs
from yijianjifa.features.downloads.contracts import DownloadError


def test_share_inputs_dedupe_and_bound():
    assert len(normalize_inputs('分享 https://v.douyin.com/abc/ https://v.douyin.com/abc/')) == 1
    for raw in [' '.join(f'https://example.com/{i}.mp4' for i in range(21)), 'x' * 6145,
                'https://example.com/a.mp4\x00', 'https://u:p@example.com/a.mp4',
                'https://example.com:444/a.mp4', 'http://127.0.0.1/a.mp4',
                'http://[::1]/a.mp4',
                'https://www.douyin.com/user/abc https://example.com/a.mp4']:
        with pytest.raises(DownloadError) as error:
            normalize_inputs(raw)
        assert error.value.code == 'input_limit'


def test_direct_input_and_candidate_hide_private_url():
    from yijianjifa.features.downloads.providers import DirectProvider
    from yijianjifa.features.downloads.network import SourceRegistry
    source = normalize_inputs('https://example.com/a.mp4?token=SECRET')[0]
    assert 'SECRET' not in repr(source)
    provider = DirectProvider(SourceRegistry())
    candidate = provider.resolve(source, None, cancel=NeverCancel()).candidates[0]
    assert candidate.canonical_source is None
    assert 'SECRET' not in repr(candidate)
    assert 'SECRET' not in source.source_key
    assert provider.media(candidate, cancel=NeverCancel()).handle


class NeverCancel:
    def cancelled(self): return False


def test_public_ipv6_allowed_and_utf8_size_bound():
    assert normalize_inputs('https://[2606:4700:4700::1111]/a.mp4')[0].kind=='direct'
    with pytest.raises(DownloadError): normalize_inputs('中'*2049)
    for host in ['[fe80::1]', '[fc00::1]', '169.254.1.1', '10.0.0.1', 'localhost']:
        with pytest.raises(DownloadError): normalize_inputs(f'https://{host}/a.mp4')
