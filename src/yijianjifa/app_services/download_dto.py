"""Committed download results, deliberately excluding snapshots and filesystem paths."""
import json
from sqlalchemy import select
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import DownloadOutputRow,DownloadSubtitleOriginalRow,Setting
from yijianjifa.features.tasks.service import TaskCenterRow
from yijianjifa.features.downloads.repository import DownloadRepository
from .download_commands import MESSAGES,local_message

LABELS={'video':'视频','source_video':'下载原件','audio_mp3':'MP3 音频','audio_wav':'WAV 音频','silent_video':'静音视频','asr':'语音字幕原稿','ocr':'画面字幕原稿','subtitle_txt':'TXT 字幕','subtitle_srt':'SRT 字幕'}

def batch_detail(engine,task_id):
    summary=DownloadRepository(engine).summary(task_id)
    frozen={i['id']:i for i in summary['snapshot']['items']}
    with session_scope(engine) as s:
        task=s.get(TaskCenterRow,task_id)
        outputs=list(s.scalars(select(DownloadOutputRow).where(DownloadOutputRow.item_id.in_(frozen))))
        originals=list(s.scalars(select(DownloadSubtitleOriginalRow).where(DownloadSubtitleOriginalRow.item_id.in_(frozen)).order_by(DownloadSubtitleOriginalRow.created_at,DownloadSubtitleOriginalRow.id)))
        deleted=set()
        for output in outputs:
            journal=s.get(Setting,'download-owned:journal:'+output.id)
            if journal:
                try:
                    saved=json.loads(journal.value_json)
                    if type(saved) is not dict or saved.get('state')=='deleted':deleted.add(output.id)
                except (ValueError,TypeError):deleted.add(output.id)
        # Keep the task state from the same projection as its stage resources.
        # A worker may commit completion between summary() and this metadata read.
        result={'task_id':task_id,'title':task.name,'state':summary['state'],'created_at':task.created_at.isoformat(),
            'total':summary['total'],'options':summary['snapshot']['options'],'counts':summary['counts'],'items':[]}
        result['source_mode']=summary['snapshot'].get('source_mode','link')
        result['source_kind']='local' if result['source_mode']=='local' else 'link'
        for item in summary['items']:
            candidate=frozen[item['id']]['candidate']
            safe={'id':item['id'],'candidate_id':item['candidate_id'],'title':candidate['title'],'state':item['state'],'region':frozen[item['id']]['region'],
                'stages':{key:{**value,'message':MESSAGES.get(value.get('error_code'),'')} for key,value in item['stages'].items()},
                'outputs':[{'id':o.id,'kind':o.kind,'label':LABELS.get(o.kind,o.kind),'size_bytes':o.size_bytes,'duration_ms':o.duration_ms} for o in outputs if o.item_id==item['id'] and o.id not in deleted],
                'subtitles':[]}
            safe['source_kind']='local' if candidate['platform']=='local' else candidate['platform']
            if result['source_mode']=='local':
                for stage in safe['stages'].values():stage['message']=local_message(stage.get('error_code'))
                for output in safe['outputs']:
                    if output['kind']=='source_video':output['label']='本地视频副本'
            versions={}
            for original in originals:
                if original.item_id!=item['id']:continue
                versions[original.origin]=versions.get(original.origin,0)+1
                safe['subtitles'].append({'resource_id':original.id,'origin':original.origin,'version':versions[original.origin]})
            result['items'].append(safe)
    return result
