"""Commerce preparation shares resource hashing and revocation with ordinary publishing."""
from .publish_preparation import PublishPreparation
from .commerce_planning import CommercePlanning


class CommercePreparation(PublishPreparation):
    def __init__(self,engine,paths,*,catalog,music,declarations=None,covers=None):
        super().__init__(engine,paths,covers=covers)
        self._planning=CommercePlanning(engine,catalog=catalog,music=music,declarations=declarations,covers=covers)

    def _preview(self,params,now):
        return self._planning.preview(params,now=now)

    def _video_ids(self,params):
        return params['plan']['video_ids']

    def _account_ids(self,params):
        return [params['account_id']]

    def _snapshot_fields(self,preview):
        return dict(kind='commerce_publish',music_choice=preview['music_choice'])
