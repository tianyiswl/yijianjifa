from yijianjifa.webui.publish_progress import PublishProgress

def test_progress_survives_navigation_and_has_distinct_final_status():
    p=PublishProgress();p.start('task',4);p.update('task',stage='上传视频',title='第一条',index=1)
    assert p.status()['task']['stage']=='上传视频'
    assert p.status()['task']['total']==4
    p.finish('task','result_unknown')
    assert p.status()['task']['state']=='result_unknown'
    assert p.status()['task']['running'] is False

def test_old_callback_cannot_update_a_new_task():
    p=PublishProgress();p.start('old',1);p.start('new',2);p.update('old',stage='结束')
    assert p.status()['task']['stage']=='任务已开始'

def test_finish_makes_terminal_reason_visible():
    p=PublishProgress();p.start('x',4);p.finish('x','pre_submit_failed')
    assert '失败' in p.status()['task']['stage']
