import hashlib
import io

import pytest

from yijianjifa.features.downloads.contracts import DownloadError
from yijianjifa.features.downloads.network import SecureFetcher, SourceRegistry


class Cancel:
    def __init__(self, value=False):
        self.value = value

    def cancelled(self):
        return self.value


class Response(io.BytesIO):
    def __init__(self, body, *, headers=None):
        super().__init__(body)
        self.status = 200
        self.headers = headers or {}

    def getheader(self, name, default=None):
        return self.headers.get(name, default)


class Transport:
    def __init__(self, body=b'model-data', *, headers=None):
        self.body = body
        self.headers = headers
        self.calls = []

    def open(self, url):
        self.calls.append(url)
        return Response(self.body, headers=self.headers)


def manifest(body=b'model-data'):
    return {'schema_version': 1, 'models': [{
        'id': 'sample', 'name': '测试模型', 'filename': 'sample.bin',
        'version': 'test-v1', 'kind': 'asr', 'size_bytes': len(body),
        'sha256': hashlib.sha256(body).hexdigest(),
        'url': 'https://example.com/sample.bin',
        'license': {'spdx': 'MIT', 'source': 'https://example.com/LICENSE'},
    }]}


def test_require_model_never_downloads(tmp_path):
    from yijianjifa.features.downloads.models import ModelStore
    transport = Transport()
    registry = SourceRegistry()
    store = ModelStore(tmp_path / 'models', manifest=manifest(), registry=registry,
                       fetcher=SecureFetcher(registry, transport=transport))
    assert store.list()[0]['state'] == 'missing'
    with pytest.raises(DownloadError) as error:
        store.require('sample')
    assert error.value.code == 'model_required'
    assert transport.calls == []
    assert not (tmp_path / 'models').exists()


def store_for(tmp_path, *, body=b'model-data', headers=None, **kwargs):
    from yijianjifa.features.downloads.models import ModelStore
    registry = SourceRegistry()
    transport = Transport(body, headers=headers)
    store = ModelStore(tmp_path / 'models', manifest=manifest(), registry=registry,
                       fetcher=SecureFetcher(registry, transport=transport), **kwargs)
    return store, transport


@pytest.mark.parametrize('body', [b'model-datx', b'model', b'not-a-model'])
def test_require_rejects_corrupt_or_truncated_existing_model_without_network(tmp_path, body):
    store, transport = store_for(tmp_path)
    store.root.mkdir()
    (store.root / 'sample.bin').write_bytes(body)
    with pytest.raises(DownloadError) as error:
        store.require('sample')
    assert error.value.code == 'model_invalid'
    assert transport.calls == []
    assert store.list()[0]['state'] == 'invalid'
    assert (store.root / 'sample.bin').read_bytes() == body


@pytest.mark.parametrize('operation', ['require', 'install', 'import'])
def test_unknown_model_is_finite_and_never_downloads(tmp_path, operation):
    store, transport = store_for(tmp_path)
    with pytest.raises(DownloadError) as error:
        if operation == 'require':
            store.require('../SECRET')
        elif operation == 'install':
            store.install('../SECRET', cancel=Cancel(), progress=lambda p: None)
        else:
            store.import_file('../SECRET', tmp_path / 'SECRET')
    assert error.value.code == 'model_unknown'
    assert 'SECRET' not in str(error.value)
    assert transport.calls == []


@pytest.mark.parametrize('headers', [None, {'Content-Length': '10'}])
def test_explicit_install_verifies_known_and_unknown_http_size(tmp_path, headers):
    store, transport = store_for(tmp_path, headers=headers)
    events = []
    store.install('sample', cancel=Cancel(), progress=events.append)
    assert store.require('sample').read_bytes() == b'model-data'
    assert store.list()[0]['state'] == 'ready'
    assert transport.calls == ['https://example.com/sample.bin']
    assert events[-1].stage == 'model_verify' and events[-1].current == 10
    assert sorted(p.name for p in store.root.iterdir()) == ['sample.bin']
    assert all(not isinstance(value, __import__('pathlib').Path)
               for row in store.list() for value in row.values())
    assert 'url' not in store.list()[0] and 'filename' not in store.list()[0]


@pytest.mark.parametrize('body', [b'model-datx', b'model', b'not-a-model'])
def test_bad_download_preserves_valid_old_model_and_cleans_temporary(tmp_path, body):
    store, transport = store_for(tmp_path, body=body)
    store.root.mkdir()
    old = store.root / 'sample.bin'
    old.write_bytes(b'model-data')
    with pytest.raises(DownloadError) as error:
        store.install('sample', cancel=Cancel(), progress=lambda p: None)
    assert error.value.code in ('model_invalid', 'size_limit')
    assert store.require('sample') == old and old.read_bytes() == b'model-data'
    assert sorted(p.name for p in store.root.iterdir()) == ['sample.bin']


def test_cancel_after_hash_does_not_replace_valid_old_model(tmp_path):
    store, transport = store_for(tmp_path)
    store.root.mkdir()
    old = store.root / 'sample.bin'
    old.write_bytes(b'model-data')
    before = old.stat().st_ino
    cancel = Cancel()
    def progress(event):
        if event.stage == 'model_verify' and event.current == event.total:
            cancel.value = True
    with pytest.raises(DownloadError) as error:
        store.install('sample', cancel=cancel, progress=progress)
    assert error.value.code == 'cancelled'
    assert old.stat().st_ino == before and old.read_bytes() == b'model-data'
    assert sorted(p.name for p in store.root.iterdir()) == ['sample.bin']


@pytest.mark.parametrize('operation', ['install', 'import'])
def test_disk_space_failure_does_not_download_or_modify_old_model(tmp_path, operation):
    store, transport = store_for(tmp_path, free_space=lambda p: 0)
    store.root.mkdir()
    old = store.root / 'sample.bin'
    old.write_bytes(b'model-data')
    selected = tmp_path / 'chosen-model.bin'
    selected.write_bytes(b'model-data')
    with pytest.raises(DownloadError) as error:
        if operation == 'install':
            store.install('sample', cancel=Cancel(), progress=lambda p: None)
        else:
            store.import_file('sample', selected)
    assert error.value.code == 'disk_space'
    assert transport.calls == []
    assert old.read_bytes() == selected.read_bytes() == b'model-data'
    assert sorted(p.name for p in store.root.iterdir()) == ['sample.bin']


def test_import_copies_and_verifies_without_moving_original_or_network(tmp_path):
    store, transport = store_for(tmp_path)
    selected = tmp_path / 'user-original.bin'
    selected.write_bytes(b'model-data')
    original_stat = selected.stat()
    store.import_file('sample', selected)
    assert selected.read_bytes() == b'model-data'
    assert selected.stat().st_ino == original_stat.st_ino
    assert store.require('sample').read_bytes() == b'model-data'
    assert store.require('sample').stat().st_ino != original_stat.st_ino
    assert transport.calls == []
    assert sorted(p.name for p in store.root.iterdir()) == ['sample.bin']


@pytest.mark.parametrize('operation', ['install', 'import'])
def test_model_activation_flushes_writable_temporary_before_publication(tmp_path, monkeypatch, operation):
    import os
    import yijianjifa.features.downloads.models as models
    store, transport = store_for(tmp_path)
    selected = tmp_path / 'user-original.bin'
    selected.write_bytes(b'model-data')
    synced = []
    real_fsync = os.fsync
    def writable_sync(descriptor):
        # Windows _commit rejects read-only descriptors. A zero-byte write
        # checks the same access requirement on POSIX without altering data.
        assert os.write(descriptor, b'') == 0
        assert not (store.root / 'sample.bin').exists()
        synced.append(os.fstat(descriptor).st_ino)
        return real_fsync(descriptor)
    monkeypatch.setattr(models.os, 'fsync', writable_sync)
    if operation == 'install':
        store.install('sample', cancel=Cancel(), progress=lambda event: None)
    else:
        store.import_file('sample', selected)
    destination = store.require('sample')
    assert synced == [destination.stat().st_ino]
    assert destination.read_bytes() == selected.read_bytes() == b'model-data'
    assert transport.calls == (['https://example.com/sample.bin'] if operation == 'install' else [])


@pytest.mark.parametrize('body', [b'model-datx', b'model', b'not-a-model'])
def test_bad_offline_import_preserves_valid_old_model(tmp_path, body):
    store, transport = store_for(tmp_path)
    store.root.mkdir()
    old = store.root / 'sample.bin'
    old.write_bytes(b'model-data')
    selected = tmp_path / 'wrong-file.bin'
    selected.write_bytes(body)
    with pytest.raises(DownloadError) as error:
        store.import_file('sample', selected)
    assert error.value.code == 'model_invalid'
    assert selected.read_bytes() == body and store.require('sample').read_bytes() == b'model-data'
    assert transport.calls == []
    assert sorted(p.name for p in store.root.iterdir()) == ['sample.bin']


@pytest.mark.parametrize('phase', ['copy', 'verify'])
def test_offline_import_cancellation_preserves_both_files(tmp_path, phase):
    store, transport = store_for(tmp_path)
    store.root.mkdir()
    old = store.root / 'sample.bin'
    old.write_bytes(b'model-data')
    selected = tmp_path / 'user-original.bin'
    selected.write_bytes(b'model-data')
    before = old.stat().st_ino
    cancel = Cancel()
    def progress(event):
        if event.stage == ('model_import' if phase == 'copy' else 'model_verify'):
            cancel.value = True
    with pytest.raises(DownloadError) as error:
        store.import_file('sample', selected, cancel=cancel, progress=progress)
    assert error.value.code == 'cancelled'
    assert old.stat().st_ino == before
    assert old.read_bytes() == selected.read_bytes() == b'model-data'
    assert transport.calls == []
    assert sorted(p.name for p in store.root.iterdir()) == ['sample.bin']


def test_cancel_waiting_for_shared_network_slot_never_downloads(tmp_path):
    import threading
    slots = threading.BoundedSemaphore(1)
    slots.acquire()
    store, transport = store_for(tmp_path, network_slots=slots)
    cancel = Cancel()
    errors = []
    started = threading.Event()
    def install():
        started.set()
        try:
            store.install('sample', cancel=cancel, progress=lambda p: None)
        except DownloadError as error:
            errors.append(error.code)
    thread = threading.Thread(target=install)
    thread.start()
    try:
        assert started.wait(1)
        cancel.value = True
        thread.join(1)
        assert not thread.is_alive() and errors == ['cancelled']
        assert transport.calls == []
        assert not (store.root / 'sample.bin').exists()
    finally:
        slots.release()
        thread.join(1)


@pytest.mark.parametrize('field,value', [
    ('filename', '../SECRET'), ('sha256', 'abc'), ('size_bytes', True),
    ('url', 'http://127.0.0.1/SECRET'),
])
def test_invalid_manifest_rejected_without_network(tmp_path, field, value):
    from yijianjifa.features.downloads.models import ModelStore
    data = manifest()
    data['models'][0][field] = value
    with pytest.raises(DownloadError) as error:
        ModelStore(tmp_path / 'models', manifest=data)
    assert error.value.code == 'model_invalid' and 'SECRET' not in str(error.value)


def test_default_model_manifest_has_usable_official_models_without_init_download(tmp_path, monkeypatch):
    from yijianjifa.features.downloads.models import ModelStore
    store = ModelStore(tmp_path / 'models')
    rows = store.list()
    assert len(rows) == 4 and all(row['state'] == 'missing' for row in rows)
    assert all(row['size_bytes'] > 0 and row['license']['spdx'] in ('MIT', 'Apache-2.0') for row in rows)
    assert not store.root.exists()


def test_changed_same_size_model_invalidates_cached_verification(tmp_path):
    import os
    store, transport = store_for(tmp_path)
    selected = tmp_path / 'original.bin'
    selected.write_bytes(b'model-data')
    store.import_file('sample', selected)
    path = store.require('sample')
    before = path.stat()
    path.write_bytes(b'model-datx')
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    with pytest.raises(DownloadError) as error:
        store.require('sample')
    assert error.value.code == 'model_invalid'
    assert transport.calls == []


def test_default_network_limit_is_shared_by_multiple_model_stores(tmp_path):
    import threading
    from yijianjifa.features.downloads.models import ModelStore
    gate = threading.Event()
    condition = threading.Condition()
    calls, errors = [], []
    class GateTransport:
        def open(self, url):
            with condition:
                calls.append(url)
                condition.notify_all()
            assert gate.wait(2)
            return Response(b'model-data')
    stores, tokens, threads = [], [], []
    for index in range(3):
        registry = SourceRegistry()
        stores.append(ModelStore(tmp_path / str(index), manifest=manifest(), registry=registry,
                                 fetcher=SecureFetcher(registry, transport=GateTransport())))
        tokens.append(Cancel())
    def install(index):
        try:
            stores[index].install('sample', cancel=tokens[index], progress=lambda p: None)
        except DownloadError as error:
            errors.append(error.code)
    try:
        for index in range(2):
            thread = threading.Thread(target=install, args=(index,))
            threads.append(thread)
            thread.start()
        with condition:
            assert condition.wait_for(lambda: len(calls) == 2, timeout=1)
        third = threading.Thread(target=install, args=(2,))
        threads.append(third)
        third.start()
        with condition:
            assert not condition.wait_for(lambda: len(calls) == 3, timeout=.15)
        tokens[2].value = True
        third.join(1)
        assert not third.is_alive() and errors == ['cancelled']
    finally:
        gate.set()
        for thread in threads:
            thread.join(2)
    assert len(calls) == 2 and all(not thread.is_alive() for thread in threads)
    assert all(stores[index].require('sample').read_bytes() == b'model-data' for index in (0, 1))
    assert not (stores[2].root / 'sample.bin').exists()


def test_default_root_obeys_isolated_test_data_dir(tmp_path, monkeypatch):
    from yijianjifa.features.downloads.models import ModelStore
    monkeypatch.setenv('YJJF_TEST_DATA_DIR', str(tmp_path / 'isolated-data'))
    store = ModelStore()
    assert store.root == tmp_path / 'isolated-data' / 'models'
    assert all(row['state'] == 'missing' for row in store.list())
    assert not (tmp_path / 'isolated-data').exists()


@pytest.mark.parametrize('initial,updated,expected_code', [
    (b'model-data', b'model-datx', 'model_invalid'),
    (b'model-datx', b'model-data', None),
])
def test_windows_creation_time_signature_never_reuses_digest_cache(
        tmp_path, monkeypatch, initial, updated, expected_code):
    import os
    import sys
    from yijianjifa.features.downloads.models import ModelStore
    # Simulate CPython 3.12 Windows creation-time semantics on this Mac host.
    # This is not execution on Windows or a filesystem compatibility claim.
    monkeypatch.setattr(sys, 'platform', 'win32')
    store, transport = store_for(tmp_path)
    store.root.mkdir()
    path = store.root / 'sample.bin'
    path.write_bytes(initial)
    creation_ns = path.stat().st_ctime_ns
    native_signature = ModelStore._signature
    def windows_signature(target):
        signature = native_signature(target)
        return signature[:-1] + (creation_ns,)
    monkeypatch.setattr(ModelStore, '_signature', staticmethod(windows_signature))
    before = path.stat()
    if initial == b'model-data':
        assert store.require('sample') == path
    else:
        with pytest.raises(DownloadError) as error:
            store.require('sample')
        assert error.value.code == 'model_invalid'
    path.write_bytes(updated)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    if expected_code is None:
        assert store.require('sample').read_bytes() == b'model-data'
        assert store.list()[0]['state'] == 'ready'
    else:
        with pytest.raises(DownloadError) as error:
            store.require('sample')
        assert error.value.code == expected_code
        assert store.list()[0]['state'] == 'invalid'
    assert transport.calls == []


@pytest.mark.parametrize('operation', ['install', 'import'])
@pytest.mark.parametrize('primary_code', ['cancelled', 'model_invalid'])
def test_temporary_unlink_failure_keeps_primary_error_and_records_owned_file(
        tmp_path, monkeypatch, operation, primary_code):
    import errno
    from pathlib import Path
    import traceback
    body = b'model-data' if primary_code == 'cancelled' else b'model-datx'
    store, transport = store_for(tmp_path, body=body)
    store.root.mkdir()
    old = store.root / 'sample.bin'
    old.write_bytes(b'model-data')
    old_inode = old.stat().st_ino
    selected = tmp_path / 'private-original.bin'
    selected.write_bytes(body)
    selected_inode = selected.stat().st_ino
    cancel = Cancel()
    def progress(event):
        if primary_code == 'cancelled' and event.stage == 'model_verify':
            cancel.value = True
    original_unlink = Path.unlink
    def occupied_unlink(path, *args, **kwargs):
        if path.name.startswith('.model-') and path.suffix == '.part':
            raise PermissionError(errno.EACCES, 'private SECRET cleanup detail', str(path))
        return original_unlink(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'unlink', occupied_unlink)
        with pytest.raises(DownloadError) as error:
            if operation == 'install':
                store.install('sample', cancel=cancel, progress=progress)
            else:
                store.import_file('sample', selected, cancel=cancel, progress=progress)
    assert error.value.code == primary_code
    displayed = ''.join(traceback.format_exception(error.value))
    assert 'SECRET' not in displayed and str(store.root) not in displayed
    assert old.stat().st_ino == old_inode and old.read_bytes() == b'model-data'
    assert selected.stat().st_ino == selected_inode and selected.read_bytes() == body
    remaining = tuple(path for path in store.root.iterdir() if path != old)
    assert len(remaining) == 1 and remaining[0].name.startswith('.model-')
    assert store.pending_cleanup() == remaining
    assert store.require('sample') == old
    assert (transport.calls == []) == (operation == 'import')
