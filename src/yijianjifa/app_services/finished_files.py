"""Native-selected finished imports and exports; no JavaScript file paths."""
from pathlib import Path
import math
import tempfile
import threading
from sqlalchemy import select
from yijianjifa.storage.db import session_scope
from yijianjifa.features.finished.service import FinishedVideoService, FinishedVideo
from yijianjifa.features.library.media_identity import sha256_file
from yijianjifa.features.library.media_storage import _VIDEO_EXTENSIONS
from yijianjifa.features.mixing.ffmpeg_renderer import _probe


class FinishedFiles:
    def __init__(self, engine, *, probe=None):
        self.engine = engine
        self.core = FinishedVideoService(engine)
        self.probe = probe or _probe
        self.lock = threading.Lock()

    def import_files(self, selected):
        result = {'cancelled': not bool(selected), 'imported': 0, 'reused': 0, 'ids': [], 'errors': []}
        with self.lock:
            paths = []
            for raw in selected:
                path = Path(raw)
                if path.is_dir() and not path.is_symlink():
                    root = path.resolve()
                    paths.extend(p for p in sorted(root.rglob('*')) if p.is_file()
                                 and p.suffix.lower() in _VIDEO_EXTENSIONS
                                 and not p.is_symlink() and p.resolve().is_relative_to(root))
                else:
                    paths.append(path)
            for path in dict.fromkeys(paths):
                try:
                    if path.is_symlink() or not path.is_file() or path.suffix.lower() not in _VIDEO_EXTENSIONS:
                        raise ValueError('not a regular video')
                    data = self.probe(path)
                    duration = float(data['format']['duration'])
                    if not math.isfinite(duration) or duration <= 0 or not any(s.get('codec_type') == 'video' for s in data.get('streams', [])):
                        raise ValueError('no playable video')
                    digest = sha256_file(path)
                    # Repeated native selection does not manufacture duplicate records.
                    with session_scope(self.engine) as session:
                        existing = session.scalar(select(FinishedVideo).where(FinishedVideo.content_hash == digest).order_by(FinishedVideo.created_at).limit(1))
                        existing_id = existing.id if existing else None
                    if existing_id:
                        result['reused'] += 1
                        result['ids'].append(existing_id)
                    else:
                        record = self.core.import_external(path, duration_ms=max(1, round(duration * 1000)))
                        result['imported'] += 1
                        result['ids'].append(record.id)
                except Exception:
                    result['errors'].append({'name': path.name, 'message': '无法导入，请检查视频格式、文件或权限。'})
        return result

    @staticmethod
    def validate_export(ids, include_metadata):
        if type(ids) is not list or not 1 <= len(ids) <= 200 or any(type(i) is not str or not 1 <= len(i) <= 64 for i in ids) or type(include_metadata) is not bool:
            raise ValueError('导出选择无效，请每次选择1至200条成品。')

    def export(self, ids, selected, *, include_metadata):
        self.validate_export(ids, include_metadata)
        result = {'cancelled': not bool(selected), 'exported': 0, 'errors': [], 'folder_name': ''}
        if not selected:
            return result
        # Each export owns a new directory, leaving earlier exports and sources intact.
        target = Path(tempfile.mkdtemp(prefix='一剪即发导出-', dir=Path(selected[0])))
        result['folder_name'] = target.name
        for pid in dict.fromkeys(ids):
            row = self.core.get(pid)
            if row is None:
                result['errors'].append({'id': pid, 'message': '成品记录不存在。'})
                continue
            try:
                self.core.export([pid], destination=target, include_metadata=include_metadata)
                result['exported'] += 1
            except Exception:
                result['errors'].append({'id': pid, 'message': '导出未完成，请检查源文件、空间及目录权限。'})
        return result
