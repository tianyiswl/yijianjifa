"""List dates come from existing DB creation stamps, including old SQLite rows."""
from datetime import datetime, timezone
import json

import pytest

from yijianjifa.app_services.desktop_api import DesktopApi
from yijianjifa.features.finished.service import FinishedVideo
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import CopyRecord, MediaAsset


@pytest.mark.parametrize('kind', ['video', 'music', 'copy', 'finished'])
def test_list_exposes_real_creation_time_as_utc_without_changing_legacy_records(kind, db_engine, app_paths, tmp_path):
    model = CopyRecord if kind == 'copy' else FinishedVideo if kind == 'finished' else MediaAsset
    created = [datetime(2020, 1, 2, 3, 4, 5 + index, 123456) for index in range(3)]
    updated = datetime(2026, 10, 16, 12, 30)
    with session_scope(db_engine) as session:
        for index in range(3):
            values = dict(id=f'old-{index}', created_at=created[index], updated_at=updated)
            if kind == 'copy':
                values.update(title=f'Old copy {index}', voice='', body='', tags='', full='')
            else:
                suffix = '.mp3' if kind == 'music' else '.mp4'
                path = tmp_path / f'old-{index}{suffix}'
                path.write_bytes(f'synthetic local media {index}'.encode())
                values.update(display_name=f'Old media {index}{suffix}', original_path=str(path), content_hash=str(index) * 64)
                if kind == 'finished':
                    values.update(origin_type='external', duration_ms=1000, title='', body='', tags='', task_locked=False)
                else:
                    values.update(kind=kind, missing=False)
            session.add(model(**values))
    api = DesktopApi(db_engine, app_paths)
    try:
        params = dict(query='', offset=1, limit=1)
        params.update(origin='all') if kind == 'finished' else params.update(kind=kind, category_ids=[])
        response = api.dispatch(json.dumps(dict(v=1, request_id='read-creation-time',
            operation='finished.list' if kind == 'finished' else 'library.list', params=params)), trusted_origin=True)
        assert response['ok'], response
        assert response['data']['total'] == 3 and len(response['data']['items']) == 1
        item = response['data']['items'][0]
        assert item['id'] == 'old-1'
        assert item['created_at'] == '2020-01-02T03:04:06.123456+00:00'
        assert datetime.fromisoformat(item['created_at']).utcoffset() == timezone.utc.utcoffset(None)
        assert str(tmp_path) not in json.dumps(response)
        with session_scope(db_engine) as session:
            rows = [session.get(model, f'old-{index}') for index in range(3)]
            assert [row.created_at for row in rows] == created
            assert all(row.updated_at == updated for row in rows)
    finally:
        assert api.close(.1)
