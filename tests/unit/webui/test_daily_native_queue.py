import json
import threading
from types import SimpleNamespace

import pytest

from yijianjifa.webui.host_commands import make_host


@pytest.mark.parametrize('operation,accepted,trusted,starts', [
    ('mix.daily.submit', True, True, 1),
    ('mix.submit', True, True, 1),
    ('task.resume', True, True, 1),
    ('mix.daily.save', True, True, 0),
    ('mix.daily.submit', False, True, 0),
    ('mix.daily.submit', True, False, 0),
])
def test_only_accepted_native_work_wakes_the_making_queue(operation, accepted, trusted, starts):
    """A persisted daily batch must run; reads/saves and rejects must not start work."""
    wakeups = []
    api = SimpleNamespace(
        dispatch=lambda *a, **kw: {'ok': accepted},
        start_queue=lambda: wakeups.append('started'),
        _error=lambda code, rid: {'ok': False, 'error': {'code': code}},
    )
    host = make_host(api=api, engine=None, window=None, webview=None, exit_started=threading.Event(),
        finished_files=None, finished_deletion=None, deletion_recovery_pending=False,
        cover_response=None, drops=None, drop_access=None, media=None, excel=None, output=None,
        voice_catalog=None, volc_config=None, voices=None, play_sound=None, copy_text=None, request_exit=None)
    host.dispatch(json.dumps({'v': 1, 'request_id': 'daily', 'operation': operation, 'params': {}}),
                  trusted_origin=trusted)
    assert len(wakeups) == starts
