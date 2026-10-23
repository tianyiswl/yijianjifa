from pathlib import Path

from yijianjifa.core.settings import GlobalSettings
from yijianjifa.features.mixing.queue import MixQueue
from yijianjifa.features.notifications.service import NotificationService


class NoopExecutor:
    def render_item(self, task_id, item_index, snapshot):
        return None


def test_changing_output_directory_only_affects_new_batches(db_engine, tmp_path):
    settings = GlobalSettings(db_engine, default_output_directory=tmp_path / "default")
    queue = MixQueue(db_engine, NoopExecutor(), global_settings=settings)
    old = tmp_path / "old"
    new = tmp_path / "new"

    settings.output_directory = old
    first = queue.confirm_batch({"quantity": 1})
    settings.output_directory = new
    second = queue.confirm_batch({"quantity": 1})

    assert first.snapshot["output_directory"] == str(old)
    assert second.snapshot["output_directory"] == str(new)
    assert first.snapshot["output_directory"] == str(old)


def test_sound_off_keeps_visible_notifications(db_engine, tmp_path):
    settings = GlobalSettings(db_engine, default_output_directory=tmp_path)
    settings.notification_sound_enabled = False
    notification_service = NotificationService(db_engine, sound_enabled=settings.notification_sound_enabled)

    notification_service.emit_action_required("T1", "需要登录")

    assert notification_service.history_for("T1")
    assert notification_service.sound_play_count == 0


def test_global_settings_persist_across_reopen(db_engine, tmp_path):
    one = GlobalSettings(db_engine, default_output_directory=tmp_path / "default")
    one.output_directory = tmp_path / "chosen"
    one.notification_sound_enabled = False

    two = GlobalSettings(db_engine, default_output_directory=tmp_path / "other")
    assert two.output_directory == tmp_path / "chosen"
    assert two.notification_sound_enabled is False
