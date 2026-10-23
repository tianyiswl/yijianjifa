"""Actual distribution requests through DesktopApi, using isolated synthetic data."""
import copy
import json
from pathlib import Path
import subprocess

import pytest

from yijianjifa.app_services.desktop_api import DesktopApi
from yijianjifa.features.accounts.service import AccountService
from yijianjifa.features.finished.service import FinishedVideoService


@pytest.mark.parametrize('scenario',['video','video_shell','platform_dropdown','platform_clear','platform_toggle'])
def test_ui_selection_changes_prepare_only_current_scope(db_engine,app_paths,tmp_path,scenario):
    accounts=[AccountService(db_engine).create(platform,'synthetic-'+str(i),'合成账号'+str(i))
              for i,platform in enumerate(('channels','bilibili','bilibili'))]
    videos=[]
    for i in range(2):
        path=tmp_path/f'owned-{i}.mp4';path.write_bytes(f'offline synthetic {i}'.encode())
        videos.append(FinishedVideoService(db_engine).import_external(path,duration_ms=3000,title=f'合成标题{i}'))
    script=Path(__file__).parents[2]/'webui'/'support'/'multi_publish_scope.cjs'
    result=subprocess.run(['node',str(script)],input=json.dumps({'scenario':scenario,'videoIds':[v.id for v in videos],'accountIds':[a.id for a in accounts]}),text=True, encoding="utf-8",capture_output=True,check=True)
    ui=json.loads(result.stdout)
    api=DesktopApi(db_engine,app_paths,publish_adapters={})
    def dispatch(operation,params,rid):
        return api.dispatch(json.dumps({'v':1,'request_id':rid,'operation':'multi_publish.'+operation,'params':params}),trusted_origin=True)
    try:
        response=dispatch('prepare',ui['first'],'scoped-plan')
        assert response['ok'],response
        assert response['data']['summary']['targets']==1
        assert response['data']['targets'][0]['platform']=='channels'
        for i,settings in enumerate(ui['saved']):
            assert dispatch('settings.save',{'settings':settings},f'scoped-settings-{i}')['ok']
        # The public boundary remains strict: replaying obsolete scope is rejected,
        # including an empty form entry created merely by opening an old video.
        stale=copy.deepcopy(ui['first'])
        if scenario.startswith('video'):stale['fields_by_video'][videos[0].id]={}
        else:stale['distribution_by_platform']['bilibili']='whole_batch'
        rejection=dispatch('prepare',stale,'obsolete-scope')
        assert rejection['error']['code']=='invalid_request'
        assert rejection['error']['message']=='请求格式不正确。'
        assert not any(op in ui['operations'] for op in ('multi_publish.confirm','multi_publish.execution.start'))
    finally:assert api.close(3)
