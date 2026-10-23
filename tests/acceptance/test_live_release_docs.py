from pathlib import Path


def test_live_douyin_checklist_requires_explicit_authorization_and_starts_unverified():
    text = Path('docs/acceptance/live-douyin-final.md').read_text(encoding='utf-8')
    assert '明确授权' in text
    assert 'UNVERIFIED' in text
    assert '| PASS |' not in text


def test_release_checklist_blocks_release_until_matrix_is_evidence_backed():
    text = Path('docs/release/V1_RELEASE_CHECKLIST.md').read_text(encoding='utf-8')
    assert '38' in text
    assert 'UNVERIFIED' in text
    assert '不得发布正式 V1' in text
