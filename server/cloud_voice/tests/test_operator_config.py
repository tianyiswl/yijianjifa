import json
import os
from pathlib import Path

import pytest

from yijianjifa_cloud_voice.auth import _check_password
from yijianjifa_cloud_voice.operator import create_operator_config, load_operator_config, validate_port
from yijianjifa_cloud_voice.operator_web import validate_origin
from yijianjifa_cloud_voice.store import Store


def test_operator_configuration_is_private_and_has_no_supplier_credentials(tmp_path):
    database=tmp_path/'isolated.db';Store(database)
    destination=tmp_path/'private'/'operator.json'
    create_operator_config(destination,database,'private-owner-password-123',origin='http://127.0.0.1:18771',test_mode=True)
    assert destination.stat().st_mode&0o777==0o600
    assert destination.parent.stat().st_mode&0o777==0o700
    payload=json.loads(destination.read_text())
    assert 'private-owner-password-123' not in destination.read_text()
    assert _check_password('private-owner-password-123',payload['password_hash'])
    assert set(payload)=={'kind','database','password_hash','operator_code_key','origin','test_mode'}
    assert load_operator_config(destination)['test_mode'] is True
    original=destination.read_bytes()
    with pytest.raises((ValueError,FileExistsError)):
        create_operator_config(destination,database,'different-owner-password',origin='http://127.0.0.1:18771')
    assert destination.read_bytes()==original


def test_shared_or_symbolic_configuration_is_refused(tmp_path):
    database=tmp_path/'isolated.db';Store(database)
    destination=tmp_path/'private'/'operator.json'
    create_operator_config(destination,database,'private-owner-password-123',origin='http://127.0.0.1:18771')
    os.chmod(destination,0o644)
    with pytest.raises(ValueError):load_operator_config(destination)
    os.chmod(destination,0o600)
    link=tmp_path/'link.json';link.symlink_to(destination)
    with pytest.raises(ValueError):load_operator_config(link)


@pytest.mark.parametrize('origin',['http://39.108.82.213','https://user:secret@example.com','https://example.com/operator','https://example.com?token=x','file:///tmp/x'])
def test_operator_origin_never_allows_plaintext_public_access_or_embedded_secrets(origin):
    with pytest.raises(ValueError):validate_origin(origin)


@pytest.mark.parametrize('port',[0,-1,65536,True,'8768'])
def test_development_listener_port_is_explicit_and_valid(port):
    with pytest.raises(ValueError):validate_port(port)

@pytest.mark.parametrize('answers,expected', [
    (['short'], '经营管理密码需为6至50位'),
    (['first-private-password', 'second-private-password'], '两次密码不一致'),
])
def test_init_reports_password_validation_without_disclosing_password(tmp_path, monkeypatch, capsys, answers, expected):
    from yijianjifa_cloud_voice import operator
    database=tmp_path/'isolated.db';Store(database)
    destination=tmp_path/'private'/'operator.json'
    values=iter(answers)
    monkeypatch.setattr(operator.getpass,'getpass',lambda prompt: next(values, answers[-1]))
    assert operator.main(['init','--config',str(destination),'--database',str(database),
                          '--origin','https://39.108.82.213']) == 1
    output=capsys.readouterr()
    assert expected in output.err
    assert all(secret not in output.err+output.out for secret in answers)
    assert not destination.exists()


def test_init_existing_config_refuses_before_password_prompt(tmp_path, monkeypatch, capsys):
    from yijianjifa_cloud_voice import operator
    database=tmp_path/'isolated.db';Store(database)
    destination=tmp_path/'private'/'operator.json'
    create_operator_config(destination,database,'private-existing-password',origin='https://39.108.82.213')
    before=destination.read_bytes()
    def forbidden(prompt):
        pytest.fail('Existing config should not ask for another password')
    monkeypatch.setattr(operator.getpass,'getpass',forbidden)
    assert operator.main(['init','--config',str(destination),'--database',str(database),
                          '--origin','https://39.108.82.213']) == 1
    assert '管理配置已存在' in capsys.readouterr().err
    assert destination.read_bytes()==before


@pytest.mark.parametrize('length,accepted', [(5,False),(6,True),(50,True),(51,False)])
def test_operator_password_requested_length_boundaries(tmp_path,length,accepted):
    database=tmp_path/'isolated.db';Store(database)
    destination=tmp_path/'private'/'operator.json'
    password='a'*length
    if accepted:
        create_operator_config(destination,database,password,origin='https://39.108.82.213')
        assert _check_password(password,load_operator_config(destination)['password_hash'])
    else:
        with pytest.raises(ValueError,match='6至50位'):
            create_operator_config(destination,database,password,origin='https://39.108.82.213')
        assert not destination.exists()
