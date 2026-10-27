from PySide6.QtCore import Qt
from yijianjifa.features.library.ui.copy_pane import CopyPane
from yijianjifa.features.library.usage_progress import UsageProgressService
from test_desktop_library import answer_dialog, draft


def pane_for(qtbot, db_engine):
    pane = CopyPane(db_engine)
    qtbot.addWidget(pane)
    return pane


def test_categories_edit_dirty_save_and_filter(qtbot, db_engine):
    p = pane_for(qtbot, db_engine)
    draft(p.repository, 'A', '甲')
    draft(p.repository, 'B', '乙')
    c = p.categories.repository.create('copy', '测试')
    p.categories.refresh()
    p.open_record('A')
    p.editor_categories.set_checked({c.id})
    assert p.dirty
    assert p.save()
    assert p.repository.category_ids('A') == {c.id}
    p.categories.set_checked({c.id})
    p.categories.apply_filter()
    assert [r[0] for r in p.list.records] == ['A']
    p.editor_categories.set_checked(set())
    answer_dialog('继续编辑')
    p.open_record('B')
    assert p.current_id == 'A'
    p.save()


def test_bulk_categories_context_rename_delete_keeps_usage(qtbot, db_engine):
    p = pane_for(qtbot, db_engine)
    for id in ['A', 'B', 'C']:
        draft(p.repository, id, id)
    c = p.categories.repository.create('copy', '分类')
    p.categories.refresh()
    p.refresh()
    p.list.selection.select_all(['A', 'B'])
    assert p.context_targets('C') == {'C'}
    assert p.context_targets('A') == {'A', 'B'}
    p.assign_categories({c.id})
    assert p.repository.category_ids('B') == {c.id}
    p.rename_record('A', '新标题')
    assert p.repository.get('A').body == '正文'
    usage = UsageProgressService(db_engine)
    usage.mark_used('mixing', 'A')
    answer_dialog('删除记录')
    p.delete_records({'A', 'B'})
    assert p.repository.get('A') is None
    assert usage.is_used('mixing', 'A')


def test_two_progress_labels_and_reset_are_independent(qtbot, db_engine):
    p = pane_for(qtbot, db_engine)
    draft(p.repository, 'A', '甲')
    progress = UsageProgressService(db_engine)
    progress.mark_used('mixing', 'A')
    progress.mark_used('finished_metadata', 'A')
    p.refresh()
    assert '1 / 1' in p.progress_labels['mixing'].text()
    answer_dialog('重新开始一轮')
    p.reset_buttons['mixing'].click()
    assert not progress.is_used('mixing', 'A')
    assert progress.is_used('finished_metadata', 'A')
    assert '0 / 1' in p.progress_labels['mixing'].text()
    assert '1 / 1' in p.progress_labels['finished_metadata'].text()


def test_media_replace_categories_preserves_files_and_other_records(qtbot, db_engine, app_paths, tmp_path):
    from yijianjifa.features.library.ui.media_pane import MediaPane
    p = MediaPane(db_engine, app_paths, 'video')
    qtbot.addWidget(p)
    c = p.categories.repository.create('video', '旧分类')
    d = p.categories.repository.create('video', '新分类')
    source = tmp_path / 'video.mp4'
    source.write_bytes(b'video')
    asset = p.service.import_media(source, keep_copy=False, category_ids=[c.id])
    p.replace_categories({asset.id}, {d.id})
    assert p.service.category_ids(asset.id) == {d.id}
    assert source.exists()
    p.replace_categories({asset.id}, set())
    assert not p.service.category_ids(asset.id)


def test_assignment_editor_is_single_choice_while_library_filter_keeps_multiple(qtbot, db_engine, monkeypatch):
    from yijianjifa.features.library.ui.categories import Categories, CategoryChecks
    categories=Categories(db_engine, 'copy')
    editor=CategoryChecks(db_engine, 'copy')
    qtbot.addWidget(categories);qtbot.addWidget(editor)
    first,second=(categories.repository.create('copy', name) for name in ('first','second'))
    categories.refresh();editor.refresh()
    editor.set_checked({first.id})
    assert editor.checked()=={first.id}
    editor.set_checked({second.id})
    assert editor.checked()=={second.id}
    editor.set_checked(set())
    assert editor.checked()==set()
    categories.set_checked({first.id,second.id})
    categories.apply_filter()
    assert categories.filter_ids=={first.id,second.id}
    monkeypatch.setattr('yijianjifa.features.library.ui.categories.choose_categories', lambda *args: {second.id})
    assert categories.destination_ids()=={second.id}
    assert categories.checked()=={first.id,second.id}
