"""Startup failures must be visible without touching data or exposing exceptions."""
import sys
import pytest

if sys.platform == "win32":
    pytest.skip("POSIX/macOS startup uses fcntl; Windows uses its Qt instance lock", allow_module_level=True)
from yijianjifa.webui import mac_app


def test_duplicate_start_reports_before_touching_database(tmp_path,monkeypatch):
    notices=[]
    monkeypatch.setattr(mac_app,'startup_notice',lambda title,message:notices.append((title,message)),raising=False)
    monkeypatch.setattr(sys,'argv',['app','--data-dir',str(tmp_path)])
    db=tmp_path/'app.db';db.write_bytes(b'preserve this database')
    with mac_app.InstanceLock(tmp_path):
        result=mac_app.main()
    assert result==0
    assert len(notices)==1 and '已经运行' in notices[0][1]
    assert db.read_bytes()==b'preserve this database'
    assert not (tmp_path/'backups').exists()


def test_unusable_data_directory_reports_failure_without_exposing_private_path(tmp_path,monkeypatch,capsys):
    notices=[]
    monkeypatch.setattr(mac_app,'startup_notice',lambda title,message:notices.append((title,message)),raising=False)
    path=tmp_path/'private-token-secret';path.write_text('original')
    monkeypatch.setattr(sys,'argv',['app','--data-dir',str(path)])
    result=mac_app.main()
    assert result==1
    assert len(notices)==1 and '目录' in notices[0][1]
    assert 'private-token-secret' not in str(notices)+capsys.readouterr().err
    assert path.read_text()=='original'


def test_invalid_database_stops_startup_and_releases_instance_lock(tmp_path,monkeypatch):
    notices=[]
    monkeypatch.setattr(mac_app,'startup_notice',lambda title,message:notices.append((title,message)))
    monkeypatch.setattr(sys,'argv',['app','--data-dir',str(tmp_path)])
    db=tmp_path/'app.db';db.write_bytes(b'not a sqlite database; confidential')
    assert mac_app.main()==1
    assert len(notices)==1 and 'confidential' not in str(notices)
    assert db.read_bytes()==b'not a sqlite database; confidential'
    with mac_app.InstanceLock(tmp_path):pass
