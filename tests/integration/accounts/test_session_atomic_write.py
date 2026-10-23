"""Synthetic session bytes only; failures must not destroy prior login state."""
import os
from pathlib import Path
import pytest
from yijianjifa.features.accounts.session_store import SessionStore
from yijianjifa.features.publish.platforms.douyin.browser_session import build_browser_session_config


def test_failed_atomic_replacement_retains_existing_session(tmp_path,monkeypatch):
    store=SessionStore(tmp_path/'profiles');store.put('A',b'old synthetic session')
    def fail(*args,**kwargs):raise OSError('simulated disk failure')
    monkeypatch.setattr(os,'replace',fail)
    with pytest.raises(OSError):store.put('A',b'new synthetic session')
    assert store.get('A')==b'old synthetic session'
    assert [p.name for p in store.profile_dir('A').iterdir()]==['session.bin']


@pytest.mark.skipif(os.name=='nt',reason='POSIX Mac permission boundary')
def test_private_permissions_apply_before_new_session_becomes_visible(tmp_path,monkeypatch):
    store=SessionStore(tmp_path/'profiles');replace=os.replace;observed=[]
    def inspect_replace(source,target):
        source,target=Path(source),Path(target)
        observed.append((source.stat().st_mode&0o777,target.parent.stat().st_mode&0o777))
        assert source.read_bytes()==b'synthetic session'
        assert not target.exists()
        replace(source,target)
    monkeypatch.setattr(os,'replace',inspect_replace)
    store.put('A',b'synthetic session')
    assert observed==[(0o600,0o700)]
    assert store.get('A')==b'synthetic session'


@pytest.mark.skipif(os.name=='nt',reason='POSIX Mac permission boundary')
def test_browser_profile_is_private_before_browser_start(tmp_path):
    store=SessionStore(tmp_path/'profiles')
    config=build_browser_session_config(store,'A')
    assert config.user_data_dir.stat().st_mode&0o777==0o700
    assert store.root.stat().st_mode&0o777==0o700


def test_failed_flush_removes_temporary_file_and_preserves_old_session(tmp_path,monkeypatch):
    store=SessionStore(tmp_path/'profiles');store.put('A',b'old synthetic session')
    def fail(fd):raise OSError('simulated full disk')
    monkeypatch.setattr(os,'fsync',fail)
    with pytest.raises(OSError):store.put('A',b'new synthetic session')
    assert store.get('A')==b'old synthetic session'
    assert [p.name for p in store.profile_dir('A').iterdir()]==['session.bin']


@pytest.mark.skipif(os.name=='nt',reason='POSIX Mac permission boundary')
def test_permission_failure_does_not_replace_existing_session(tmp_path,monkeypatch):
    store=SessionStore(tmp_path/'profiles');store.put('A',b'old synthetic session')
    chmod=Path.chmod
    def fail_profile(path,mode,*args,**kwargs):
        if path==store.profile_dir('A'):raise PermissionError('synthetic denied')
        return chmod(path,mode,*args,**kwargs)
    monkeypatch.setattr(Path,'chmod',fail_profile)
    with pytest.raises(PermissionError):store.put('A',b'new synthetic session')
    assert store.get('A')==b'old synthetic session'
