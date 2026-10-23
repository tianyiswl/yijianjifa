from yijianjifa.features.accounts.service import AccountService
from yijianjifa.features.library.copy_service import CopyDraft, CopyRepository
from yijianjifa.features.presets.models import NormalPublishPreset
from yijianjifa.features.presets.service import PresetService
from yijianjifa.features.publish.normal_service import NormalPublishService


def test_confirmed_task_not_changed_by_copy_default_account_or_preset_edit(db_engine):
    copies = CopyRepository(db_engine)
    copies.create(id="W1", title="原标题", voice="", body="原文案", tags="#原", full="")

    accounts = AccountService(db_engine)
    a1 = accounts.create("douyin", "uid-1", "账号一")
    a2 = accounts.create("douyin", "uid-2", "账号二")
    accounts.set_default("douyin", a1.id)

    presets = PresetService(db_engine)
    preset = presets.create(NormalPublishPreset(name="日更方案"))

    publishing = NormalPublishService(db_engine)
    task = publishing.confirm_publish_draft(
        {
            "platform": "douyin",
            "account_id": a1.id,
            "copy": {"id": "W1", "title": "原标题", "body": "原文案", "tags": "#原"},
            "preset": {"id": preset.id, "name": preset.name},
        }
    )
    before = task.snapshot_json

    copies.update(CopyDraft(id="W1", title="新标题", voice="", body="新文案", tags="#新", full=""))
    accounts.set_default("douyin", a2.id)
    presets.rename(preset.id, "已修改方案")

    assert publishing.get_task(task.id).snapshot_json == before
    assert publishing.get_task(task.id).snapshot["account_id"] == a1.id
    assert publishing.get_task(task.id).snapshot["copy"]["title"] == "原标题"
    assert publishing.get_task(task.id).snapshot["preset"]["name"] == "日更方案"
