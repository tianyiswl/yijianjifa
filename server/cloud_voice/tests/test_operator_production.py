"""The production launcher must fail closed before opening a listener."""
import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, ProxyHandler

# Local test listeners must never use a workstation system proxy.
urlopen = build_opener(ProxyHandler({})).open

import pytest

from yijianjifa_cloud_voice.operator import create_operator_config
from yijianjifa_cloud_voice.store import Store


ENTRY = Path(__file__).parents[1] / 'deploy' / 'operator_production.py'


@pytest.fixture
def launcher():
    assert ENTRY.is_file(), 'The isolated production operator launcher is missing.'
    spec = importlib.util.spec_from_file_location('operator_production', ENTRY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def operator_config(tmp_path):
    database = tmp_path / 'voice.db'
    Store(database)
    path = tmp_path / 'private' / 'operator.json'
    create_operator_config(path, database, 'only-a-private-test-password',
                           origin='https://39.108.82.213', test_mode=False)
    return path, database


def alter_config(path, **changes):
    payload = json.loads(path.read_text())
    payload.update(changes)
    path.write_text(json.dumps(payload))


def build_for_test(launcher, operator_config):
    path, database = operator_config
    return launcher.build_server(config_path=path, expected_database=database, port=0)


def request(runtime, path):
    address = f'http://127.0.0.1:{runtime.server.effective_port}{path}'
    return urlopen(Request(address, headers={'Host': '39.108.82.213'}), timeout=3)


def test_private_production_launcher_serves_only_operator_without_issuing_credits(launcher, operator_config, monkeypatch):
    # A launcher accidentally mounting the customer API or reading its env fails.
    monkeypatch.setenv('YJJF_CLOUD_DATABASE', '/nonexistent/wrong-database.db')
    monkeypatch.setenv('YJJF_CLOUD_PROVIDER', 'invalid-unrelated-provider')
    monkeypatch.setenv('YJJF_CLOUD_API_KEY', 'unused-test-marker')
    runtime = build_for_test(launcher, operator_config)
    thread = threading.Thread(target=runtime.run, daemon=True)
    thread.start()
    try:
        with request(runtime, '/operator/') as response:
            assert response.status == 200
            assert '兑换码'.encode() in response.read()
        with request(runtime, '/operator/api/session') as response:
            payload = json.load(response)['data']
            assert payload['test_mode'] is False
            assert payload['authenticated'] is False
            assert 'Secure' in response.headers['Set-Cookie']
        with pytest.raises(HTTPError) as error:
            request(runtime, '/v1/account')
        assert error.value.code == 404
        with Store(operator_config[1]).connect() as connection:
            for table in ('accounts', 'redemption_codes', 'ledger'):
                assert connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == 0
    finally:
        runtime.close()
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert runtime.closed is True
    runtime.close()
    with pytest.raises((URLError, OSError)):
        request(runtime, '/operator/')


@pytest.mark.parametrize('changes', [
    {'test_mode': True},
    {'origin': 'http://127.0.0.1:18771'},
    {'origin': 'https://39.108.82.213:443'},
    {'origin': 'https://another.example'},
])
def test_test_mode_and_any_nonproduction_origin_are_refused(launcher, operator_config, changes):
    alter_config(operator_config[0], **changes)
    with pytest.raises(ValueError):
        build_for_test(launcher, operator_config)


def test_other_database_is_refused_before_a_listener_is_opened(launcher, operator_config, tmp_path):
    qa_database = tmp_path / 'https-acceptance' / 'voice.db'
    Store(qa_database)
    alter_config(operator_config[0], database=str(qa_database))
    with pytest.raises(ValueError):
        build_for_test(launcher, operator_config)


@pytest.mark.parametrize('target,permissions', [('file', 0o400), ('file', 0o640), ('directory', 0o750)])
def test_exact_private_permissions_are_required(launcher, operator_config, target, permissions):
    path = operator_config[0] if target == 'file' else operator_config[0].parent
    path.chmod(permissions)
    with pytest.raises(ValueError):
        build_for_test(launcher, operator_config)


def test_configuration_must_belong_to_the_running_user(launcher, operator_config, monkeypatch):
    monkeypatch.setattr(launcher.os, 'geteuid', lambda: operator_config[0].stat().st_uid + 1)
    with pytest.raises(ValueError):
        build_for_test(launcher, operator_config)


def test_symlinked_private_configuration_is_refused(launcher, operator_config, tmp_path):
    link = tmp_path / 'linked.json'
    link.symlink_to(operator_config[0])
    with pytest.raises(ValueError):
        launcher.build_server(config_path=link, expected_database=operator_config[1], port=0)


def test_missing_operator_environment_does_not_fall_back_to_supplier_configuration(launcher, monkeypatch):
    monkeypatch.delenv('YJJF_OPERATOR_CONFIG', raising=False)
    monkeypatch.setenv('YJJF_CLOUD_DATABASE', '/var/lib/yijianjifa-cloud-voice/voice.db')
    with pytest.raises(ValueError):
        launcher.build_server()


def test_environment_configuration_path_is_the_only_configuration_input(launcher, operator_config, monkeypatch):
    monkeypatch.setenv('YJJF_OPERATOR_CONFIG', str(operator_config[0]))
    runtime = launcher.build_server(expected_database=operator_config[1], port=0)
    runtime.close()


def test_unlocked_waitress_version_is_rejected_before_loading_configuration(launcher, monkeypatch):
    monkeypatch.setattr(launcher.importlib.metadata, 'version', lambda name: '0.0.0')
    with pytest.raises(RuntimeError):
        launcher.build_server()


@pytest.mark.parametrize('port', [True, '18771', -1, 65536])
def test_invalid_listener_port_is_refused(launcher, operator_config, port):
    with pytest.raises(ValueError):
        launcher.build_server(config_path=operator_config[0], expected_database=operator_config[1], port=port)


def test_production_database_never_accepts_a_different_listener_port(launcher, operator_config):
    with pytest.raises(ValueError):
        launcher.build_server(config_path=operator_config[0], port=0)


def test_main_rejects_command_line_overrides_without_echoing_them(launcher, monkeypatch, capsys):
    monkeypatch.setattr(sys, 'argv', ['operator_production.py', '--password=do-not-print-this'])
    assert launcher.main() == 2
    assert 'do-not-print-this' not in capsys.readouterr().err


def test_sigterm_drains_actual_operator_server_and_exits_cleanly(launcher, operator_config):
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    script = (
        'import importlib.util,sys;'
        f'spec=importlib.util.spec_from_file_location("operator_production",{str(ENTRY)!r});'
        'module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);'
        f'module.PRODUCTION_DATABASE={str(operator_config[1])!r};'
        f'module.PRODUCTION_PORT={port};'
        'sys.argv=["operator_production.py"];raise SystemExit(module.main())'
    )
    # The deployed venv installs the package. This source checkout is deliberately
    # not installed into the isolated local verification venv.
    environment = dict(os.environ, YJJF_OPERATOR_CONFIG=str(operator_config[0]),
                       PYTHONPATH=str(ENTRY.parents[1]))
    process = subprocess.Popen([sys.executable, '-c', script], env=environment,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 10
        ready = False
        while time.monotonic() < deadline and process.poll() is None:
            try:
                with urlopen(Request(f'http://127.0.0.1:{port}/operator/',
                                     headers={'Host': '39.108.82.213'}), timeout=.2) as response:
                    ready = response.status == 200
                    break
            except (OSError, URLError):
                time.sleep(.03)
        assert ready, 'The operator process did not become healthy.'
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, (stdout, stderr)
        assert b'Traceback' not in stderr
        with socket.socket() as probe:
            assert probe.connect_ex(('127.0.0.1', port)) != 0
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)


def test_unit_supplies_source_import_path_without_installing_business_package(tmp_path):
    unit = (ENTRY.parent / 'yijianjifa-cloud-voice-operator.service').read_text()
    environment = {'PATH': os.defpath}
    for line in unit.splitlines():
        if line.startswith('Environment='):
            key, value = line.removeprefix('Environment=').split('=', 1)
            environment[key] = value.replace('/opt/yijianjifa-cloud-voice/current', str(ENTRY.parents[1].resolve()))
    result = subprocess.run([sys.executable, '-c', 'import yijianjifa_cloud_voice.operator_web'],
                            cwd=tmp_path, env=environment, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_production_wires_separate_sibling_billing_config(launcher,operator_config):
    runtime=build_for_test(launcher,operator_config)
    try:
        assert runtime.app.supplier_balance is not None
        assert runtime.app.supplier_balance.path==operator_config[0].with_name('billing.json')
        assert runtime.app.supplier_balance.read()['status']=='unconfigured'
    finally:runtime.close()
