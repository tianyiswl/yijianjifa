"""Durable, paused commerce tasks. No platform adapter is invoked."""
import secrets
from .publish_confirmation import PublishConfirmation
from .commerce_preparation import CommercePreparation
from yijianjifa.features.commerce.service import CommerceTaskRow


class CommerceConfirmation(PublishConfirmation):
    snapshot_kind='commerce_publish'
    receipt_prefix='commerce-confirm:'
    task_kind='commerce'
    task_name='团购带货 · 本地待执行'

    def __init__(self,engine,paths,preparation,*,choose=None):
        super().__init__(engine,paths,preparation)
        self._choose=choose or secrets.choice

    def _capture_locked(self,connection,params,now):
        planning=self.preparation._planning
        return CommercePreparation(connection,self.paths,catalog=planning.catalog,music=planning.music,declarations=planning.declarations,covers=self.preparation.covers)._capture(params,now)

    def _freeze_specific(self,snapshot):
        for item in snapshot['items']:
            music=item.get('music_override',snapshot['music_choice']);rows=music['candidates']
            if music['mode']=='none':item['platform_music']=None
            elif music['mode']=='fixed':item['platform_music']=dict(rows[0])
            else:
                selected=self._choose(rows)
                if selected not in rows:raise ValueError('music choice outside confirmed pool')
                item['platform_music']=dict(selected)

    def _task_row(self,task_id,frozen):
        return CommerceTaskRow(id=task_id,account_id=frozen.data['accounts'][0]['id'],state='paused',snapshot_json=frozen.json)
