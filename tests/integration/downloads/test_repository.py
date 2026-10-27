"""Real temporary SQLite files; no profile, user database, or network access."""
import importlib.util
import json
from pathlib import Path
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor

from alembic import command
import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from yijianjifa.storage.db import _alembic_config, create_engine_for, downgrade_to_revision, migrate_to_head, migrate_to_revision


def test_registered_dotted_model_ids_are_accepted_for_jobs_and_results(db_engine):
    from yijianjifa.features.downloads.repository import DownloadRepository
    repo = DownloadRepository(db_engine, known_model_ids=('fixture.v1',))
    with db_engine.begin() as connection:
        job = repo.create_auxiliary_job(connection, kind='model_download', model_id='fixture.v1')
        repo.record_auxiliary_job(connection, job, state='succeeded', installed_model_id='fixture.v1')
    assert repo.get_auxiliary_job(job)['installed_model_id'] == 'fixture.v1'


def rows(connection, table):
    return [dict(row) for row in connection.execute(text(f'SELECT * FROM {table}')).mappings()]


def legacy_fixture(connection):
    now = '2026-10-05 00:00:00'
    connection.execute(text("INSERT INTO accounts(id,platform,platform_user_id,nickname,remark,group_name,is_default,login_state,capability_json,created_at,updated_at,last_login_at) VALUES ('account','douyin','100','fixture','','',0,'valid','{}',:now,:now,:now)"), {'now': now})
    connection.execute(text("INSERT INTO copy_records(id,title,voice,body,tags,full,created_at,updated_at) VALUES ('copy','old title','','old body','','',:now,:now)"), {'now': now})
    connection.execute(text("INSERT INTO media_assets(id,kind,display_name,original_path,content_hash,missing,created_at,updated_at) VALUES ('media','video','fixture','/fixture.mp4','oldhash',0,:now,:now)"), {'now': now})
    connection.execute(text("INSERT INTO publish_history(id,video_hash,account_id,attempt,state,created_at) VALUES ('history','oldhash','account',1,'result_unknown',:now)"), {'now': now})
    connection.execute(text("INSERT INTO settings(key,value_json,updated_at) VALUES ('license','{\"expires_at\":\"2027-01-01\"}',:now)"), {'now': now})
    connection.execute(text("INSERT INTO task_center_records(id,kind,name,state,snapshot_json,queue_order,created_at,updated_at) VALUES ('old-task','publish','fixture','result_unknown','{}',0,:now,:now)"), {'now': now})
    connection.execute(text("INSERT INTO web_command_receipts(request_id,payload_hash,response_json) VALUES ('old-receipt','oldhash','{}')"))


def test_upgrade_preserves_existing_records(tmp_path):
    engine = create_engine_for(tmp_path / 'old.db')
    migrate_to_revision(engine, '0013_account_login_time')
    tables = ('accounts', 'copy_records', 'media_assets', 'publish_history', 'settings', 'task_center_records', 'web_command_receipts')
    with engine.begin() as connection:
        legacy_fixture(connection)
        before = {name: rows(connection, name) for name in tables}
        legacy_columns = {name: tuple(column['name'] for column in inspect(connection).get_columns(name)) for name in tables}
    migrate_to_head(engine)
    assert 'download_candidates' in inspect(engine).get_table_names(), 'download migration is missing'
    with engine.connect() as connection:
        # Newly added content identity columns are allowed; every original
        # column, row, and reference must retain exactly its original value.
        after = {name: [{column: row[column] for column in legacy_columns[name]}
                        for row in rows(connection, name)] for name in tables}
        assert after == before
        history = rows(connection, 'publish_history')[0]
        assert history['content_type'] == 'video'
        assert history['content_hash'] == history['video_hash'] == 'oldhash'
        assert history['content_id'] is None  # Old unmatched history has no fabricated video ID.
        assert connection.exec_driver_sql('PRAGMA foreign_key_check').all() == []
        assert connection.execute(text('SELECT version_num FROM alembic_version')).scalar_one() == '0019_single_category'
    engine.dispose()


def api(engine):
    assert importlib.util.find_spec('yijianjifa.features.downloads.repository') is not None, 'download repository is missing'
    from yijianjifa.features.downloads import contracts
    from yijianjifa.features.downloads.repository import DownloadRepository
    return DownloadRepository(engine), contracts


@pytest.fixture
def storage(db_engine):
    repo, contract = api(db_engine)
    return repo, contract, db_engine


def candidate(contract, number=1, **changes):
    row = contract.Candidate(f'douyin:{number}', 'douyin', str(number), 'douyin_profile:' + 'a' * 64,
                             f'https://www.douyin.com/video/{number}', 'same title', 'author')
    return replace(row, **changes)


def start(storage, *, count=1, options=None):
    repo, contract, engine = storage
    with engine.begin() as connection:
        ids = tuple(repo.save_candidate(connection, candidate(contract, i + 1)) for i in range(count))
        task_id = repo.create_batch(connection, ids, options or contract.DownloadOptions(save_video=True), 'grant_fixture',
                                   {key: contract.Region() for key in ids})
        items = rows(connection, 'download_items')
    return task_id, items


def native_backend_path(value):
    # Persist a host-native absolute fixture path; no filesystem access is needed.
    return str(Path(value).resolve())


def output(storage, item, kind='video', identity='resource1'):
    repo, contract, engine = storage
    resource = contract.OutputResource(identity, item['candidate_id'], item['id'], kind, 'b' * 64, 123, 1000)
    with engine.begin() as connection:
        repo.save_output(connection, resource, backend_path=native_backend_path('/isolated/output.mp4'))
    return resource


def test_candidate_identity_is_independent_of_title_and_direct_keys(storage):
    repo, c, engine = storage
    with engine.begin() as connection:
        first = repo.save_candidate(connection, candidate(c))
        same = repo.save_candidate(connection, candidate(c, id='other', title='changed'))
        second = repo.save_candidate(connection, candidate(c, 2))
        direct1 = repo.save_candidate(connection, c.Candidate('direct1', 'direct', '', 'direct:' + 'c' * 64, None))
        direct2 = repo.save_candidate(connection, c.Candidate('direct2', 'direct', '', 'direct:' + 'd' * 64, None))
        assert first == same
        assert len({first, second, direct1, direct2}) == 4
        assert len(rows(connection, 'download_candidates')) == 4


def test_cover_attachment_preserves_metadata_refresh_and_explicit_detach(storage):
    repo,c,engine=storage
    with engine.begin() as connection:
        cid=repo.save_candidate(connection,candidate(c))
        resource=c.OutputResource('cover_resource',cid,None,'cover','b'*64,80,None)
        repo.save_output(connection,resource,backend_path=native_backend_path('/isolated/cover.png'))
        repo.attach_cover(connection,cid,resource.id)
        repo.save_candidate(connection,candidate(c,title='new title'))
        assert rows(connection,'download_candidates')[0]['cover_resource_id']==resource.id
        repo.attach_cover(connection,cid,None)
        assert rows(connection,'download_candidates')[0]['cover_resource_id'] is None
        assert rows(connection,'download_outputs')[0]['id']==resource.id


@pytest.mark.parametrize('invalid',['foreign','preview','item'])
def test_cover_attachment_rejects_wrong_owner_or_non_image_resource(storage,invalid):
    repo,c,engine=storage
    task,items=start(storage,count=2)
    with engine.begin() as connection:
        first,second=items
        resource=c.OutputResource('cover_resource',second['candidate_id'] if invalid=='foreign' else first['candidate_id'],
            first['id'] if invalid=='item' else None,'preview' if invalid=='preview' else 'cover','b'*64,80,None)
        repo.save_output(connection,resource,backend_path=native_backend_path('/isolated/cover.png'))
        with pytest.raises(c.DownloadError):repo.attach_cover(connection,first['candidate_id'],resource.id)


def test_cover_result_never_changes_completed_resolution(storage):
    repo,c,engine=storage
    from yijianjifa.features.downloads.input import normalize_inputs
    with engine.begin() as connection:
        cid=repo.save_candidate(connection,candidate(c))
        session=repo.create_resolution(connection,normalize_inputs('https://www.douyin.com/user/a')[0])
        connection.execute(text("UPDATE download_resolutions SET completion='complete' WHERE id=:id"),{'id':session})
        job=repo.create_auxiliary_job(connection,kind='cover',candidate_id=cid)
        resource=c.OutputResource('cover_resource',cid,None,'cover','b'*64,80,None)
        repo.save_output(connection,resource,backend_path=native_backend_path('/isolated/cover.png'))
        repo.attach_cover(connection,cid,resource.id);repo.record_auxiliary_job(connection,job,state='succeeded',resource_id=resource.id)
        assert rows(connection,'download_resolutions')[0]['completion']=='complete'


def test_source_and_metadata_never_store_private_handles_or_signed_urls(storage):
    repo, c, engine = storage
    private_url = 'https://public.example/movie.mp4?token=PRIVATE_TOKEN'
    with engine.begin() as connection:
        repo.save_candidate(connection, candidate(c, title=private_url + ' Cookie=PRIVATE_COOKIE; other=PRIVATE_EXTRA', author='Bearer PRIVATE_AUTH'))
        repo.save_candidate(connection, c.Candidate('direct', 'direct', '', 'direct:' + 'e' * 64, private_url))
        dump = json.dumps(rows(connection, 'download_candidates'))
        assert all(secret not in dump for secret in ('PRIVATE_TOKEN', 'PRIVATE_COOKIE', 'PRIVATE_AUTH', 'PRIVATE_EXTRA', 'token='))
        assert rows(connection, 'download_candidates')[1]['canonical_source'] is None
        with pytest.raises(c.DownloadError):
            repo.save_candidate(connection, candidate(c, 2, source_key=private_url))
        with pytest.raises(c.DownloadError):
            repo.save_candidate(connection, candidate(c, 3, canonical_source=private_url))


def test_regions_and_options_are_frozen_and_decisions_are_events(storage):
    repo, c, engine = storage
    task_id, items = start(storage, options=c.DownloadOptions(save_video=True, ocr_enabled=True, model_ids=('ocr-v1',)))
    before = repo.summary(task_id)['snapshot']
    with engine.begin() as connection:
        repo.set_region(connection, items[0]['candidate_id'], c.Region(0, 0, .5, .5))
        repo.record_decision(connection, items[0]['id'], 'original')
        assert len(rows(connection, 'download_decisions')) == 1
        with pytest.raises(IntegrityError):
            connection.execute(text("UPDATE download_items SET snapshot_json='{}'"))
        with pytest.raises(IntegrityError):
            connection.execute(text("UPDATE task_center_records SET snapshot_json='{}' WHERE id=:id"), {'id': task_id})
    assert repo.summary(task_id)['snapshot'] == before
    assert before['items'][0]['region'] == {'x': 0, 'y': .6, 'width': 1, 'height': .4}


@pytest.mark.parametrize('count', [0, 201])
def test_batch_selection_bounds_reject_before_writes(storage, count):
    repo, c, engine = storage
    with engine.begin() as connection:
        ids = tuple(repo.save_candidate(connection, candidate(c, i + 1)) for i in range(count))
        with pytest.raises(c.DownloadError):
            repo.create_batch(connection, ids, c.DownloadOptions(save_video=True), 'grant_fixture', {key: c.Region() for key in ids})
        assert rows(connection, 'task_center_records') == []
        assert rows(connection, 'download_items') == []


def test_two_hundred_items_have_every_selected_stage(storage):
    repo, c, _ = storage
    task_id, _ = start(storage, count=200, options=c.DownloadOptions(save_video=True, audio_format='wav', silent_video=True, asr_language='zh', ocr_enabled=True))
    summary = repo.summary(task_id)
    assert summary['total'] == 200
    assert summary['stages']['video']['waiting'] == 200
    assert summary['stages']['audio']['waiting'] == 200
    assert summary['stages']['asr']['waiting'] == 200
    assert summary['stages']['ocr']['waiting'] == 200
    assert summary['stages']['silent']['waiting'] == 200


def test_batch_rollback_leaves_no_partial_task(storage):
    repo, c, engine = storage
    with engine.begin() as connection:
        key = repo.save_candidate(connection, candidate(c))
    with pytest.raises(RuntimeError), engine.begin() as connection:
        repo.create_batch(connection, (key,), c.DownloadOptions(save_video=True), 'grant_fixture', {key: c.Region()})
        raise RuntimeError('rollback fixture')
    with engine.connect() as connection:
        assert rows(connection, 'task_center_records') == []
        assert rows(connection, 'download_items') == []
        assert rows(connection, 'download_stages') == []


def test_receipt_replay_and_receipt_failure_are_atomic(storage):
    from yijianjifa.app_services.library_queries import LibraryCommands
    repo, c, engine = storage
    with engine.begin() as connection:
        key = repo.save_candidate(connection, candidate(c))

    class StartCommands(LibraryCommands):
        _operations = LibraryCommands._operations | {'downloads.start'}

        @staticmethod
        def _validate(op, params):
            assert op == 'downloads.start' and set(params) == {'candidate_ids'}

        @staticmethod
        def _apply(connection, op, params):
            ids = tuple(params['candidate_ids'])
            regions = {key: repo.get_region(connection, key) for key in ids}
            return {'task_id': repo.create_batch(connection, ids, c.DownloadOptions(save_video=True), 'grant_fixture', regions)}

    raw = json.dumps({'v': 1, 'request_id': 'start_fixture', 'operation': 'downloads.start', 'params': {'candidate_ids': [key]}})
    first = StartCommands(engine).dispatch(raw, trusted_origin=True)
    replay = StartCommands(engine).dispatch(raw, trusted_origin=True)
    assert first['ok'] and replay['ok']
    assert first['data']['task_id'] == replay['data']['task_id']
    with ThreadPoolExecutor(max_workers=4) as pool:
        concurrent = list(pool.map(lambda _: StartCommands(engine).dispatch(raw, trusted_origin=True), range(4)))
    assert all(result == first for result in concurrent)
    with engine.begin() as connection:
        assert len(rows(connection, 'task_center_records')) == 1
        connection.exec_driver_sql("CREATE TRIGGER reject_receipt BEFORE INSERT ON web_command_receipts BEGIN SELECT RAISE(ABORT,'fixture'); END")
    failed = StartCommands(engine).dispatch(raw.replace('start_fixture', 'fail_fixture'), trusted_origin=True)
    assert failed['error']['code'] == 'internal_error'
    with engine.connect() as connection:
        assert len(rows(connection, 'task_center_records')) == 1
        assert len(rows(connection, 'download_items')) == 1
        assert len(rows(connection, 'web_command_receipts')) == 1


def test_all_outputs_and_stage_counts_control_item_success(storage):
    repo, c, engine = storage
    task_id, items = start(storage, options=c.DownloadOptions(save_video=True, audio_format='mp3'))
    video = output(storage, items[0])
    with engine.begin() as connection:
        repo.record_stage(connection, items[0]['id'], c.StageResult('video', 'succeeded', video.id, None))
    summary = repo.summary(task_id)
    assert summary['counts']['succeeded'] == 0
    assert summary['counts']['partial'] == 1
    assert summary['stages']['video']['succeeded'] == 1
    assert summary['stages']['audio']['waiting'] == 1
    audio = output(storage, items[0], 'audio_mp3', 'audio1')
    with engine.begin() as connection:
        repo.record_stage(connection, items[0]['id'], c.StageResult('audio', 'succeeded', audio.id, None))
    assert repo.summary(task_id)['state'] == 'succeeded'
    assert repo.summary(task_id)['counts']['succeeded'] == 1


@pytest.mark.parametrize('state,error', [('failed', 'network'), ('skipped', None), ('cancelled', 'cancelled')])
def test_terminal_results_remain_separately_counted(storage, state, error):
    repo, c, engine = storage
    task_id, items = start(storage)
    with engine.begin() as connection:
        repo.record_stage(connection, items[0]['id'], c.StageResult('video', state, None, error))
    summary = repo.summary(task_id)
    assert summary['state'] == state
    assert summary['counts'][state] == 1
    assert summary['counts']['succeeded'] == 0


def test_partial_failed_and_stage_rollback_update_task_on_same_connection(storage):
    repo, c, engine = storage
    task_id, items = start(storage, options=c.DownloadOptions(save_video=True, audio_format='mp3'))
    video = output(storage, items[0])
    with engine.begin() as connection:
        repo.record_stage(connection, items[0]['id'], c.StageResult('video', 'succeeded', video.id, None))
    with pytest.raises(RuntimeError), engine.begin() as connection:
        repo.record_stage(connection, items[0]['id'], c.StageResult('audio', 'failed', None, 'network'))
        assert connection.execute(text('SELECT state FROM task_center_records WHERE id=:id'), {'id': task_id}).scalar_one() == 'partial'
        raise RuntimeError('rollback fixture')
    assert repo.summary(task_id)['stages']['audio']['waiting'] == 1
    with engine.begin() as connection:
        repo.record_stage(connection, items[0]['id'], c.StageResult('audio', 'failed', None, 'network'))
    assert repo.summary(task_id)['counts']['partial'] == 1


def test_skip_is_append_only_and_preserves_snapshot(storage):
    repo, _, engine = storage
    task_id, items = start(storage)
    snapshot = repo.summary(task_id)['snapshot']
    with engine.begin() as connection:
        repo.record_decision(connection, items[0]['id'], 'skip')
        assert rows(connection, 'download_decisions')[0]['decision'] == 'skip'
        with pytest.raises(IntegrityError):
            connection.execute(text("UPDATE download_decisions SET decision='original'"))
    assert repo.summary(task_id)['state'] == 'skipped'
    assert repo.summary(task_id)['snapshot'] == snapshot


def test_auxiliary_job_and_resolution_completion_survive_reopening(storage):
    repo, c, engine = storage
    with engine.begin() as connection:
        key = repo.save_candidate(connection, candidate(c))
        session = repo.create_resolution(connection, c.SourceInput('douyin_profile', 'douyin_profile:' + 'a' * 64, 'https://www.douyin.com/user/fixture'))
        job = repo.create_auxiliary_job(connection, kind='resolve', session_id=session)
        repo.record_auxiliary_job(connection, job, state='interrupted', completion='limited', error_code='cancelled')
        repo.link_source(connection, key, c.SourceInput('douyin_profile', 'douyin_profile:' + 'a' * 64, 'https://www.douyin.com/user/fixture'), session_id=session)
    reopened, _ = api(engine)
    result = reopened.get_auxiliary_job(job)
    assert result['state'] == 'interrupted' and result['completion'] == 'limited'
    assert result['session_id'] == session
    with engine.connect() as connection:
        assert rows(connection, 'download_sources')[0]['candidate_id'] == key


def test_auxiliary_preview_result_is_safe_and_recoverable(storage):
    repo, c, engine = storage
    with engine.begin() as connection:
        key = repo.save_candidate(connection, candidate(c))
        job = repo.create_auxiliary_job(connection, kind='preview', candidate_id=key)
        repo.save_output(connection, c.OutputResource('preview-job', key, None, 'preview', 'b' * 64, 12), backend_path=native_backend_path('/isolated/preview.png'))
        repo.record_auxiliary_job(connection, job, state='succeeded', resource_id='preview-job')
    assert api(engine)[0].get_auxiliary_job(job)['resource_id'] == 'preview-job'


def test_outputs_have_real_candidate_and_optional_item_foreign_keys(storage):
    repo, c, engine = storage
    task_id, items = start(storage, count=2)
    preview = c.OutputResource('preview', items[0]['candidate_id'], None, 'preview', 'b' * 64, 5, None)
    with engine.connect() as connection:
        connection.exec_driver_sql('PRAGMA foreign_keys=ON')
        connection.commit()
        connection.begin()
        assert connection.exec_driver_sql('PRAGMA foreign_keys').scalar_one() == 1
        repo.save_output(connection, preview, backend_path=native_backend_path('/isolated/preview.png'))
        with pytest.raises(IntegrityError):
            connection.execute(text("INSERT INTO download_outputs(id,candidate_id,item_id,kind,sha256,size_bytes,backend_path,created_at) VALUES ('bad','missing',NULL,'preview',:sha,1,'/bad','2026-10-05 00:00:00')"), {'sha': 'b' * 64})
        with pytest.raises(IntegrityError):
            connection.execute(text("INSERT INTO download_outputs(id,candidate_id,item_id,kind,sha256,size_bytes,backend_path,created_at) VALUES ('wrong-owner',:candidate,:item,'preview',:sha,1,'/bad','2026-10-05 00:00:00')"), {'sha': 'b' * 64, 'candidate': items[0]['candidate_id'], 'item': items[1]['id']})
        with pytest.raises(c.DownloadError):
            repo.save_output(connection, replace(preview, id='wrong', item_id=items[1]['id']), backend_path=native_backend_path('/bad'))
        with pytest.raises(IntegrityError):
            connection.execute(text("UPDATE download_candidates SET cover_resource_id='missing' WHERE id=:id"), {'id': items[0]['candidate_id']})
        connection.commit()
    assert repo.get_output('preview') == preview
    assert repo.output_path('preview') == native_backend_path('/isolated/preview.png')


def test_frozen_outputs_are_required_even_if_stage_row_is_missing(storage):
    repo, c, engine = storage
    task_id, items = start(storage, options=c.DownloadOptions(save_video=True, audio_format='mp3'))
    video = output(storage, items[0])
    with engine.begin() as connection:
        repo.record_stage(connection, items[0]['id'], c.StageResult('video', 'succeeded', video.id))
        connection.execute(text("DELETE FROM download_stages WHERE stage='audio'"))
    summary = repo.summary(task_id)
    assert summary['counts']['succeeded'] == 0
    assert summary['stages']['audio']['interrupted'] == 1


def test_fk_off_writes_validate_missing_and_mismatched_references(storage):
    repo, c, engine = storage
    _, items = start(storage, count=2)
    with engine.begin() as connection:
        assert connection.exec_driver_sql('PRAGMA foreign_keys').scalar_one() == 0
        with pytest.raises(c.DownloadError):
            repo.save_output(connection, c.OutputResource('unknown', 'missing', None, 'preview', 'b' * 64, 1), backend_path=native_backend_path('/isolated/preview.png'))
        with pytest.raises(c.DownloadError):
            repo.create_auxiliary_job(connection, kind='cover', candidate_id='missing')
        with pytest.raises(c.DownloadError):
            repo.create_batch(connection, ('missing',), c.DownloadOptions(), 'grant_fixture', {'missing': c.Region()})
        connection.execute(text("INSERT INTO download_auxiliary_jobs(id,session_id,candidate_id,kind,state,completion,created_at,updated_at) VALUES ('orphan','missing',NULL,'resolve','waiting','partial','2026-10-05','2026-10-05')"))
        with pytest.raises(c.DownloadError):
            repo.record_auxiliary_job(connection, 'orphan', state='running')
        assert rows(connection, 'download_auxiliary_jobs')[0]['state'] == 'waiting'
        assert rows(connection, 'download_outputs') == []


@pytest.mark.parametrize('stage,state,resource,error', [('invented', 'failed', None, 'network'), ('video', 'succeeded', None, None), ('video', 'failed', None, 'Cookie=PRIVATE'), ('video', 'running', None, 'network')])
def test_invalid_stage_results_leave_state_unchanged(storage, stage, state, resource, error):
    repo, c, engine = storage
    task_id, items = start(storage)
    with engine.begin() as connection:
        with pytest.raises(c.DownloadError):
            repo.record_stage(connection, items[0]['id'], c.StageResult(stage, state, resource, error))
    assert repo.summary(task_id)['state'] == 'waiting'


def test_model_required_can_wait_without_completing_an_output(storage):
    repo, c, engine = storage
    task_id, items = start(storage, options=c.DownloadOptions(save_video=False, asr_language='zh'))
    with engine.begin() as connection:
        repo.record_stage(connection, items[0]['id'], c.StageResult('asr', 'waiting', None, 'model_required'))
    summary = repo.summary(task_id)
    assert summary['counts']['waiting'] == 1
    assert summary['items'][0]['stages']['asr']['error_code'] == 'model_required'
    assert summary['counts']['succeeded'] == 0


def test_stage_resources_must_match_item_and_frozen_output_kind(storage):
    repo, c, engine = storage
    task_id, items = start(storage, count=2, options=c.DownloadOptions(save_video=True, audio_format='wav'))
    other = output(storage, items[1], identity='other')
    wrong_kind = output(storage, items[0], kind='audio_mp3', identity='wrong-kind')
    with engine.begin() as connection:
        with pytest.raises(c.DownloadError):
            repo.record_stage(connection, items[0]['id'], c.StageResult('video', 'succeeded', other.id))
        with pytest.raises(c.DownloadError):
            repo.record_stage(connection, items[0]['id'], c.StageResult('audio', 'succeeded', wrong_kind.id))
    assert repo.summary(task_id)['counts']['waiting'] == 2


def test_original_subtitles_and_increasing_edits_are_immutable(storage):
    repo, c, engine = storage
    _, items = start(storage)
    segments = (c.SubtitleSegment('s1', 0, 1000, '原稿', .9),)
    with engine.begin() as connection:
        original = repo.save_subtitle_original(connection, items[0]['candidate_id'], items[0]['id'], 'asr', segments)
        draft = repo.create_subtitle_draft(connection, original)
        assert repo.append_subtitle_revision(connection, draft, segments) == 1
        assert repo.append_subtitle_revision(connection, draft, (replace(segments[0], text='修订'),)) == 2
        with pytest.raises(IntegrityError):
            connection.execute(text("UPDATE download_subtitle_originals SET segments_json='[]'"))
        with pytest.raises(IntegrityError):
            connection.execute(text("UPDATE download_subtitle_revisions SET segments_json='[]'"))
    with engine.connect() as connection:
        assert json.loads(rows(connection, 'download_subtitle_originals')[0]['segments_json'])[0]['text'] == '原稿'
        assert [row['revision'] for row in rows(connection, 'download_subtitle_revisions')] == [1, 2]


@pytest.mark.parametrize('changes', [{'save_video': False}, {'audio_format': 'aac'}, {'asr_language': 'fr'}, {'model_ids': ('https://private/model',)}])
def test_invalid_options_fail_before_batch_creation(storage, changes):
    _, c, _ = storage
    with pytest.raises(c.DownloadError):
        c.DownloadOptions(**changes)


@pytest.mark.parametrize('coordinates', [(-.1, 0, 1, 1), (0, .8, 1, .4), (0, 0, 0, 1), (float('nan'), 0, 1, 1)])
def test_invalid_regions_are_rejected(storage, coordinates):
    _, c, _ = storage
    with pytest.raises(c.DownloadError):
        c.Region(*coordinates)


def test_downgrade_upgrade_preserves_legacy_and_recreates_constraints(tmp_path):
    engine = create_engine_for(tmp_path / 'cycle.db')
    migrate_to_revision(engine, '0013_account_login_time')
    with engine.begin() as connection:
        legacy_fixture(connection)
    migrate_to_head(engine)
    repo, c = api(engine)
    with engine.begin() as connection:
        key = repo.save_candidate(connection, candidate(c))
        task_id = repo.create_batch(connection, (key,), c.DownloadOptions(), 'grant_fixture', {key: c.Region()})
        item = rows(connection, 'download_items')[0]
        repo.save_output(connection, c.OutputResource('video', key, item['id'], 'video', 'b' * 64, 12), backend_path=native_backend_path('/isolated/video.mp4'))
        repo.record_stage(connection, item['id'], c.StageResult('video', 'succeeded', 'video'))
        original = repo.save_subtitle_original(connection, key, item['id'], 'asr', (c.SubtitleSegment('s1', 0, 1000, 'fixture'),))
        draft = repo.create_subtitle_draft(connection, original)
        repo.append_subtitle_revision(connection, draft, (c.SubtitleSegment('s1', 0, 1000, 'edited'),))
        repo.record_decision(connection, item['id'], 'original')
        source = c.SourceInput('douyin_profile', 'douyin_profile:' + 'a' * 64, 'https://www.douyin.com/user/fixture')
        session_id = repo.create_resolution(connection, source)
        repo.link_source(connection, key, source, session_id=session_id)
        job = repo.create_auxiliary_job(connection, kind='preview', candidate_id=key)
        repo.save_output(connection, c.OutputResource('preview', key, None, 'preview', 'b' * 64, 12), backend_path=native_backend_path('/isolated/preview.png'))
        repo.record_auxiliary_job(connection, job, state='succeeded', resource_id='preview')
        repo.create_auxiliary_job(connection, kind='resource_import', output_resource_id='video')
        repo.create_auxiliary_job(connection, kind='subtitle_export', subtitle_original_id=original,
                                  subtitle_draft_id=draft, subtitle_revision=1, subtitle_origin='asr',
                                  export_format='srt', output_grant_id='grant_fixture')
    with engine.connect() as connection:
        connection.exec_driver_sql('PRAGMA foreign_keys=ON')
        connection.commit()
        assert connection.exec_driver_sql('PRAGMA foreign_key_check').all() == []
    downgrade_to_revision(engine, '0013_account_login_time')
    assert 'download_candidates' not in inspect(engine).get_table_names()
    migrate_to_head(engine)
    with engine.connect() as connection:
        assert rows(connection, 'accounts')[0]['login_state'] == 'valid'
        assert rows(connection, 'publish_history')[0]['state'] == 'result_unknown'
        assert rows(connection, 'settings')[0]['value_json'] == '{"expires_at":"2027-01-01"}'
        assert rows(connection, 'task_center_records')[0]['id'] == 'old-task'
        assert rows(connection, 'web_command_receipts')[0]['request_id'] == 'old-receipt'
        assert connection.exec_driver_sql('PRAGMA foreign_key_check').all() == []
        with pytest.raises(IntegrityError):
            connection.execute(text("INSERT INTO download_items(id,task_id,candidate_id,position,snapshot_json,created_at) VALUES ('orphan','missing','missing',0,'{}','2026-10-05')"))
    engine.dispose()


@pytest.fixture
def auxiliary(storage):
    from yijianjifa.features.downloads.repository import DownloadRepository
    repo, c, engine = storage
    repo = DownloadRepository(engine, known_model_ids=('fixture-model',))
    with engine.begin() as connection:
        first = repo.save_candidate(connection, candidate(c))
        second = repo.save_candidate(connection, candidate(c, 2))
        repo.save_output(connection, c.OutputResource('video-input', first, None, 'video', 'b' * 64, 12), backend_path=native_backend_path('/isolated/video.mp4'))
        repo.save_output(connection, c.OutputResource('wrong-owner-video', second, None, 'video', 'c' * 64, 12), backend_path=native_backend_path('/isolated/other.mp4'))
        original = repo.save_subtitle_original(connection, first, None, 'asr', (c.SubtitleSegment('s1', 0, 1000, '原稿'),))
        draft = repo.create_subtitle_draft(connection, original)
        repo.append_subtitle_revision(connection, draft, (c.SubtitleSegment('s1', 0, 1000, '编辑稿'),))
        connection.execute(text("INSERT INTO categories(id,kind,name,created_at) VALUES ('video-category','video','fixture','2026-10-05')"))
        connection.execute(text("INSERT INTO categories(id,kind,name,created_at) VALUES ('audio-category','audio','fixture','2026-10-05')"))
        connection.execute(text("INSERT INTO media_assets(id,kind,display_name,original_path,content_hash,missing,created_at,updated_at) VALUES ('library-result','video','fixture','/isolated/video.mp4',:sha,0,'2026-10-05','2026-10-05')"), {'sha': 'b' * 64})
        connection.execute(text("INSERT INTO media_assets(id,kind,display_name,original_path,content_hash,missing,created_at,updated_at) VALUES ('wrong-library','video','fixture','/isolated/other.mp4',:sha,0,'2026-10-05','2026-10-05')"), {'sha': 'c' * 64})
    params = {
        'resource_import': dict(output_resource_id='video-input', category_ids=('video-category',)),
        'subtitle_export': dict(subtitle_original_id=original, subtitle_draft_id=draft, subtitle_revision=1,
                                subtitle_origin='asr', export_format='srt', output_grant_id='native-export-grant'),
        'model_download': dict(model_id='fixture-model'),
        'model_import': dict(model_id='fixture-model', input_grant_id='native-model-grant'),
    }
    return repo, c, engine, params, first, second


@pytest.mark.parametrize('kind', ['resource_import', 'subtitle_export', 'model_download', 'model_import'])
def test_approved_auxiliary_jobs_persist_safe_owners_and_results(auxiliary, kind):
    from yijianjifa.features.downloads.repository import DownloadRepository
    repo, c, engine, params, first, _ = auxiliary
    with engine.begin() as connection:
        before = len(rows(connection, 'download_candidates'))
        job = repo.create_auxiliary_job(connection, kind=kind, **params[kind])
        if kind == 'resource_import':
            repo.record_auxiliary_job(connection, job, state='succeeded', library_id='library-result')
        elif kind == 'subtitle_export':
            repo.save_output(connection, c.OutputResource('subtitle-result', first, None, 'subtitle_srt', 'd' * 64, 12), backend_path=native_backend_path('/isolated/export.srt'))
            repo.record_auxiliary_job(connection, job, state='succeeded', resource_id='subtitle-result')
            repo.append_subtitle_revision(connection, params[kind]['subtitle_draft_id'], (c.SubtitleSegment('s1', 0, 1000, '后来版本'),))
        else:
            repo.record_auxiliary_job(connection, job, state='succeeded', installed_model_id='fixture-model')
        assert len(rows(connection, 'download_candidates')) == before
    reopened = DownloadRepository(engine, known_model_ids=('fixture-model',))
    result = reopened.get_auxiliary_job(job)
    assert result['kind'] == kind and result['state'] == 'succeeded'
    for field, value in params[kind].items():
        assert result[field] == value
    if kind == 'resource_import':
        assert result['library_id'] == 'library-result'
        assert result['candidate_id'] == first
    elif kind == 'subtitle_export':
        assert result['resource_id'] == 'subtitle-result'
        assert result['subtitle_revision'] == 1
        assert result['candidate_id'] == first
    else:
        assert result['candidate_id'] is None and result['session_id'] is None
        assert result['installed_model_id'] == 'fixture-model'
    assert '/isolated/' not in json.dumps(result)
    assert all(field not in result for field in ('backend_path', 'handle', 'input_path', 'output_path', 'metadata'))


def test_original_subtitle_export_has_its_own_zero_revision(auxiliary):
    repo, _, engine, params, _, _ = auxiliary
    original = params['subtitle_export']['subtitle_original_id']
    with engine.begin() as connection:
        job = repo.create_auxiliary_job(connection, kind='subtitle_export', subtitle_original_id=original,
                                       subtitle_revision=0, subtitle_origin='asr', export_format='txt', output_grant_id='native-export')
        repo.record_auxiliary_job(connection, job, state='interrupted', error_code='cancelled')
    result = repo.get_auxiliary_job(job)
    assert result['subtitle_draft_id'] is None and result['subtitle_revision'] == 0
    assert result['state'] == 'interrupted'


@pytest.mark.parametrize('kind,changes', [
    ('resource_import', {'output_resource_id': 'missing'}),
    ('resource_import', {'category_ids': ('missing',)}),
    ('resource_import', {'category_ids': ('audio-category',)}),
    ('resource_import', {'category_ids': ('video-category', 'video-category')}),
    ('resource_import', {'model_id': 'fixture-model'}),
    ('subtitle_export', {'subtitle_original_id': 'missing'}),
    ('subtitle_export', {'subtitle_revision': 99}),
    ('subtitle_export', {'subtitle_origin': 'ocr'}),
    ('subtitle_export', {'export_format': 'html'}),
    ('subtitle_export', {'output_grant_id': '/private/native/export.srt'}),
    ('subtitle_export', {'output_resource_id': 'video-input'}),
    ('model_download', {'model_id': 'unknown'}),
    ('model_download', {'model_id': 'https://private/model?token=secret'}),
    ('model_download', {'candidate_id': 'douyin:1'}),
    ('model_import', {'input_grant_id': None}),
    ('model_import', {'input_grant_id': '/private/native/model'}),
])
def test_auxiliary_owners_and_operation_fields_reject_before_mutation(auxiliary, kind, changes):
    repo, c, engine, params, _, _ = auxiliary
    with engine.begin() as connection:
        arguments = {**params[kind], **changes}
        with pytest.raises(c.DownloadError):
            repo.create_auxiliary_job(connection, kind=kind, **arguments)
        assert rows(connection, 'download_auxiliary_jobs') == []


def test_auxiliary_result_ownership_and_types_cannot_be_mixed(auxiliary):
    repo, c, engine, params, _, second = auxiliary
    with engine.begin() as connection:
        import_job = repo.create_auxiliary_job(connection, kind='resource_import', **params['resource_import'])
        export_job = repo.create_auxiliary_job(connection, kind='subtitle_export', **params['subtitle_export'])
        model_job = repo.create_auxiliary_job(connection, kind='model_download', **params['model_download'])
        repo.save_output(connection, c.OutputResource('wrong-subtitle-owner', second, None, 'subtitle_srt', 'd' * 64, 12), backend_path=native_backend_path('/isolated/export.srt'))
        for job, result in ((import_job, {'library_id': 'wrong-library'}),
                            (import_job, {'installed_model_id': 'fixture-model'}),
                            (export_job, {'resource_id': 'wrong-subtitle-owner'}),
                            (model_job, {'installed_model_id': 'unknown'}),
                            (model_job, {'library_id': 'library-result'})):
            with pytest.raises(c.DownloadError):
                repo.record_auxiliary_job(connection, job, state='succeeded', **result)
        assert all(row['state'] == 'waiting' for row in rows(connection, 'download_auxiliary_jobs'))


@pytest.mark.parametrize('kind', ['resource_import', 'subtitle_export', 'model_download', 'model_import'])
def test_auxiliary_job_receipt_failure_rolls_back_and_replays_once(auxiliary, kind):
    from yijianjifa.app_services.library_queries import LibraryCommands
    repo, _, engine, params, _, _ = auxiliary

    class AuxiliaryCommands(LibraryCommands):
        _operations = LibraryCommands._operations | {'fixture.auxiliary'}

        @staticmethod
        def _validate(op, p):
            assert op == 'fixture.auxiliary' and p == {'kind': kind}

        @staticmethod
        def _apply(connection, op, p):
            return {'job_id': repo.create_auxiliary_job(connection, kind=kind, **params[kind])}

    raw = json.dumps({'v': 1, 'request_id': 'auxiliary-receipt', 'operation': 'fixture.auxiliary', 'params': {'kind': kind}})
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TRIGGER reject_aux_receipt BEFORE INSERT ON web_command_receipts BEGIN SELECT RAISE(ABORT,'fixture'); END")
    failed = AuxiliaryCommands(engine).dispatch(raw, trusted_origin=True)
    assert failed['error']['code'] == 'internal_error'
    with engine.begin() as connection:
        assert rows(connection, 'download_auxiliary_jobs') == []
        assert rows(connection, 'web_command_receipts') == []
        connection.exec_driver_sql('DROP TRIGGER reject_aux_receipt')
    first = AuxiliaryCommands(engine).dispatch(raw, trusted_origin=True)
    replay = AuxiliaryCommands(engine).dispatch(raw, trusted_origin=True)
    assert first['ok'] and replay == first
    with engine.connect() as connection:
        assert len(rows(connection, 'download_auxiliary_jobs')) == 1
        assert len(rows(connection, 'web_command_receipts')) == 1


def test_model_jobs_require_a_finite_backend_manifest_registration(storage):
    from yijianjifa.features.downloads.repository import DownloadRepository
    repo, c, engine = storage
    with engine.begin() as connection:
        with pytest.raises(c.DownloadError) as error:
            repo.create_auxiliary_job(connection, kind='model_download', model_id='unregistered')
        assert error.value.code == 'model_unknown'
    for ids in (('same', 'same'), ('/private/model',), ['safe']):
        with pytest.raises(c.DownloadError):
            DownloadRepository(engine, known_model_ids=ids)


def test_old_unreleased_auxiliary_schema_is_detected_without_mutating(storage):
    repo, c, engine = storage
    with engine.begin() as connection:
        connection.exec_driver_sql('DROP TABLE download_auxiliary_jobs')
        connection.exec_driver_sql('CREATE TABLE download_auxiliary_jobs(id TEXT PRIMARY KEY,kind TEXT,state TEXT)')
        connection.exec_driver_sql("INSERT INTO download_auxiliary_jobs VALUES ('old-job','resolve','waiting')")
        with pytest.raises(c.DownloadError) as error:
            repo.create_auxiliary_job(connection, kind='model_download', model_id='fixture-model')
        assert error.value.code == 'metadata_missing'
        assert rows(connection, 'download_auxiliary_jobs') == [{'id': 'old-job', 'kind': 'resolve', 'state': 'waiting'}]
    with pytest.raises(c.DownloadError):
        repo.get_auxiliary_job('old-job')


def test_model_jobs_work_with_no_candidate_or_resolution_records(storage):
    from yijianjifa.features.downloads.repository import DownloadRepository
    _, _, engine = storage
    repo = DownloadRepository(engine, known_model_ids=('fixture-model',))
    with engine.begin() as connection:
        first = repo.create_auxiliary_job(connection, kind='model_download', model_id='fixture-model')
        second = repo.create_auxiliary_job(connection, kind='model_import', model_id='fixture-model', input_grant_id='native-model')
        repo.record_auxiliary_job(connection, first, state='succeeded', installed_model_id='fixture-model')
        repo.record_auxiliary_job(connection, second, state='interrupted', error_code='cancelled')
        assert rows(connection, 'download_candidates') == []
        assert rows(connection, 'download_resolutions') == []
    assert repo.get_auxiliary_job(first)['installed_model_id'] == 'fixture-model'
    assert repo.get_auxiliary_job(second)['candidate_id'] is None


def test_deleted_import_category_can_fail_without_success_or_new_references(auxiliary):
    repo, c, engine, params, _, _ = auxiliary
    with engine.begin() as connection:
        job = repo.create_auxiliary_job(connection, kind='resource_import', **params['resource_import'])
        connection.execute(text("DELETE FROM categories WHERE id='video-category'"))
        with pytest.raises(c.DownloadError):
            repo.record_auxiliary_job(connection, job, state='succeeded', library_id='library-result')
        assert rows(connection, 'download_auxiliary_jobs')[0]['state'] == 'waiting'
        repo.record_auxiliary_job(connection, job, state='failed', error_code='source_missing')
    result = repo.get_auxiliary_job(job)
    assert result['state'] == 'failed' and result['library_id'] is None
    assert result['category_ids'] == ('video-category',)


def test_auxiliary_results_and_status_roll_back_together(auxiliary):
    repo, _, engine, params, _, _ = auxiliary
    with engine.begin() as connection:
        job = repo.create_auxiliary_job(connection, kind='resource_import', **params['resource_import'])
    with pytest.raises(RuntimeError), engine.begin() as connection:
        repo.record_auxiliary_job(connection, job, state='succeeded', library_id='library-result')
        assert rows(connection, 'download_auxiliary_jobs')[0]['library_id'] == 'library-result'
        raise RuntimeError('rollback fixture')
    result = repo.get_auxiliary_job(job)
    assert result['state'] == 'waiting' and result['library_id'] is None


def test_auxiliary_inputs_are_immutable_and_new_references_have_real_fks(auxiliary):
    repo, _, engine, params, _, _ = auxiliary
    with engine.connect() as connection:
        connection.exec_driver_sql('PRAGMA foreign_keys=ON')
        connection.commit()
        connection.begin()
        job = repo.create_auxiliary_job(connection, kind='resource_import', **params['resource_import'])
        repo.create_auxiliary_job(connection, kind='subtitle_export', **params['subtitle_export'])
        model_job = repo.create_auxiliary_job(connection, kind='model_download', **params['model_download'])
        with pytest.raises(IntegrityError):
            connection.execute(text('UPDATE download_auxiliary_jobs SET output_resource_id=:resource WHERE id=:id'), {'resource': 'wrong-owner-video', 'id': job})
        with pytest.raises(IntegrityError):
            connection.execute(text("UPDATE download_auxiliary_jobs SET library_id='missing' WHERE id=:id"), {'id': job})
        with pytest.raises(IntegrityError):
            connection.execute(text("UPDATE download_auxiliary_jobs SET installed_model_id='wrong' WHERE id=:id"), {'id': model_job})
        with pytest.raises(IntegrityError):
            connection.execute(text("INSERT INTO download_auxiliary_jobs(id,kind,state,completion,output_resource_id,created_at,updated_at) VALUES ('missing-input','resource_import','waiting','partial','missing','2026-10-05','2026-10-05')"))
        with pytest.raises(IntegrityError):
            connection.execute(text("INSERT INTO download_auxiliary_jobs(id,kind,state,completion,subtitle_draft_id,subtitle_revision,created_at,updated_at) VALUES ('missing-revision','subtitle_export','waiting','partial',:draft,99,'2026-10-05','2026-10-05')"), {'draft': params['subtitle_export']['subtitle_draft_id']})
        assert connection.exec_driver_sql('PRAGMA foreign_key_check').all() == []
        connection.commit()


def test_new_import_jobs_are_single_category_while_legacy_snapshots_remain_readable(auxiliary):
    from sqlalchemy import insert, select
    from yijianjifa.storage.models import DownloadAuxiliaryJobRow
    repo, c, engine, params, _, _ = auxiliary
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO categories(id,kind,name,created_at) VALUES ('second-video','video','second','2026-10-05')"))
        with pytest.raises(c.DownloadError) as error:
            repo.create_auxiliary_job(connection, kind='resource_import', output_resource_id='video-input',
                                      category_ids=('video-category', 'second-video'))
        assert error.value.code == 'single_category_only'
        job = repo.create_auxiliary_job(connection, kind='resource_import', **params['resource_import'])
        row = dict(connection.execute(select(DownloadAuxiliaryJobRow.__table__).where(DownloadAuxiliaryJobRow.id == job)).mappings().one())
        # Seed an old immutable snapshot as a separate historical row.
        row.update(id='legacy-multi-category', category_ids_json='["video-category","second-video"]')
        connection.execute(insert(DownloadAuxiliaryJobRow).values(**row))
    assert repo.get_auxiliary_job('legacy-multi-category')['category_ids'] == ('video-category', 'second-video')
    with engine.begin() as connection:
        repo.record_auxiliary_job(connection, 'legacy-multi-category', state='failed', error_code='single_category_only')
    legacy = repo.get_auxiliary_job('legacy-multi-category')
    assert legacy['state'] == 'failed' and legacy['error_code'] == 'single_category_only'
    assert legacy['category_ids'] == ('video-category', 'second-video')
