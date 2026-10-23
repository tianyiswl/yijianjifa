from PySide6.QtCore import Qt
from sqlalchemy import select
from yijianjifa.storage.db import session_scope
from yijianjifa.features.mixing.queue import MixQueue
from yijianjifa.features.library.copy_service import CopyRepository
from yijianjifa.features.library.media_storage import MediaStorageService


def test_native_quick_defaults_submit_and_task_navigation(qtbot, db_engine, app_paths, tmp_path):
    from yijianjifa.app.main_window import MainWindow
    w = MainWindow(db_engine, app_paths)
    qtbot.addWidget(w)
    w.show()
    assert w.mixing.quantity.value() == 10
    assert w.mixing.resolution.currentData() == '1080p'
    assert w.mixing.fit.currentData() == 'preserve_blur'
    assert w.mixing.materials.checked() == set()
    path = tmp_path / 'video.mp4'
    path.write_bytes(b'test')
    MediaStorageService(db_engine, app_paths, kind='video').import_media(path, keep_copy=False, category_ids=[])
    CopyRepository(db_engine).create(id='A', title='文案', voice='', body='', tags='', full='')
    w.mix_controller.timer.stop()  # UI test inspects frozen confirmation before execution.
    w.navigation.setCurrentRow(1)
    w.mixing.quantity.setValue(2)
    w.mixing.generate.click()
    tasks = w.mix_controller.service.tasks()
    assert len(tasks) == 1 and tasks[0].quantity == 2
    assert tasks[0].snapshot['auto_publish'] is False
    w.navigation.setCurrentRow(6)
    assert w.pages.currentWidget() is w.task_page
    w.task_page.refresh()
    assert w.task_page.list.table.rowCount() == 1


def test_restart_requires_explicit_continue_and_queue_controls(qtbot, db_engine, app_paths):
    from yijianjifa.app.main_window import MainWindow
    q = MixQueue(db_engine, None)
    a = q.enqueue({'quantity': 1})
    b = q.enqueue({'quantity': 1})
    w = MainWindow(db_engine, app_paths)
    qtbot.addWidget(w)
    w.mix_controller.timer.stop()
    assert q.get(a.id).state == 'paused'
    p = w.task_page
    p.list.selection.select_all([a.id, b.id])
    p.resume_button.click()
    assert q.get(a.id).state == 'waiting'
    assert q.get(b.id).state == 'waiting'
    p.move_task(b.id, -1)
    assert q.get(b.id).position < q.get(a.id).position
    p.pause_button.click()
    assert q.get(a.id).state == 'paused'
    p.cancel_tasks({a.id}, confirm=False)
    assert q.get(a.id).state == 'cancelled'


def test_background_worker_finishes_without_blocking_ui(qtbot, db_engine, app_paths):
    from yijianjifa.features.mixing.ui.controller import MixController
    import threading
    gate = threading.Event()
    class Executor:
        def render_item(self, task_id, index, snapshot):
            assert gate.wait(5)
    c = MixController(db_engine, app_paths)
    c.service.queue.executor = Executor()
    task = c.service.queue.enqueue({'quantity': 2})
    c.tick()
    qtbot.waitUntil(lambda: c.service.queue.get(task.id).state == 'running')
    c.service.queue.request_pause(task.id)
    gate.set()
    qtbot.waitUntil(lambda: not c.busy, timeout=10000)
    assert c.service.queue.get(task.id).state == 'paused'
    assert c.service.queue.get(task.id).success_count == 1
    c.timer.stop()


def test_deferred_close_keeps_dispatcher_available(qtbot, db_engine, app_paths):
    from yijianjifa.features.mixing.ui.controller import MixController
    c = MixController(db_engine, app_paths)
    c.busy = True
    assert not c.prepare_close()
    assert c.timer.isActive()
    c.busy = False
    assert c.prepare_close()
    assert not c.timer.isActive()
