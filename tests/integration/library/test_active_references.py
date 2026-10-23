import pytest

from yijianjifa.features.mixing.queue import MixQueue
from yijianjifa.features.library.copy_service import CopyRepository


def test_unfinished_mix_prevents_deleting_owned_file(media_service, db_engine, tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"active source")
    asset = media_service.import_media(source, keep_copy=True, category_ids=[])
    queue = MixQueue(db_engine, None)
    task = queue.enqueue({"quantity": 1, "segments": [{"asset_id": asset.id, "path": str(asset.managed_path)}]})
    with pytest.raises(RuntimeError, match="任务"):
        media_service.delete_media_record(asset.id)
    assert media_service.get(asset.id) is not None
    assert asset.managed_path.exists()
    queue.cancel_remaining(task.id)
    media_service.delete_media_record(asset.id)
    assert not asset.managed_path.exists()
    assert source.exists()


def test_unfinished_mix_prevents_deleting_copy_but_unrelated_copy_is_allowed(db_engine):
    repo = CopyRepository(db_engine)
    for id in ("used", "unrelated"):
        repo.create(id=id, title=id, voice="", body="", tags="", full="")
    MixQueue(db_engine, None).enqueue({"quantity": 1, "copy": {"id": "used", "title": "frozen"}})
    with pytest.raises(RuntimeError, match="任务"):
        repo.delete("used")
    repo.delete("unrelated")
    assert repo.get("used") is not None
    assert repo.get("unrelated") is None


def test_reimport_with_copy_switch_does_not_migrate_existing_record(media_service, tmp_path):
    source = tmp_path / "existing.mp4"
    source.write_bytes(b"same file")
    first = media_service.import_media(source, keep_copy=False, category_ids=[])
    again = media_service.import_media(source, keep_copy=True, category_ids=[])
    assert again.id == first.id
    assert again.managed_path is None


def test_reimport_does_not_retarget_existing_original(media_service, tmp_path):
    original = tmp_path / "source.mp4"
    duplicate = tmp_path / "duplicate.mp4"
    original.write_bytes(b"same")
    duplicate.write_bytes(b"same")
    asset = media_service.import_media(original, keep_copy=False, category_ids=[])
    again = media_service.import_media(duplicate, keep_copy=False, category_ids=[])
    assert again.id == asset.id
    assert again.original_path == original
