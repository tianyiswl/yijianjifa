from yijianjifa.features.notifications.service import NotificationService, user_status_label


def test_status_labels_are_not_collapsed_to_success():
    assert user_status_label("mix_completed") == "制作完成"
    assert user_status_label("platform_scheduled") == "平台定时已设置"
    assert user_status_label("published") == "实际已发布"
    assert user_status_label("result_unknown") == "结果待确认"


def test_batch_summary_creates_one_persistent_notification_not_one_per_item(db_engine):
    service = NotificationService(db_engine, sound_enabled=True)
    note = service.emit_batch_summary(
        task_id="M1",
        status="mix_completed",
        success_count=8,
        failure_count=2,
    )

    history = service.history_for("M1")
    assert len(history) == 1
    assert history[0].id == note.id
    assert "成功 8" in history[0].message
    assert "失败 2" in history[0].message
    assert service.sound_play_count == 1


def test_sound_off_keeps_visible_action_required_notification(db_engine):
    service = NotificationService(db_engine, sound_enabled=False)
    service.emit_action_required("P1", "需要重新登录")

    history = service.history_for("P1")
    assert len(history) == 1
    assert history[0].message == "需要重新登录"
    assert history[0].deep_link == "task:P1"
    assert service.sound_play_count == 0
