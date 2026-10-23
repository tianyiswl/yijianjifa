"""Human labels are independent of the immutable file format and path."""
from pathlib import Path

MEDIA_SUFFIXES={'.mp4','.mov','.mkv','.avi','.m4v','.webm','.mp3','.wav','.m4a','.aac','.flac','.ogg'}

def name_only(value):
    name=str(value).strip()
    suffix=Path(name).suffix.lower()
    if suffix in MEDIA_SUFFIXES:name=name[:-len(suffix)].strip()
    if name.lower() in MEDIA_SUFFIXES or not name:raise ValueError('名称不能为空。')
    return name

def presentation(record):
    return {'name':name_only(record.display_name),'format':record.effective_path.suffix.removeprefix('.').upper() or '未知格式'}
