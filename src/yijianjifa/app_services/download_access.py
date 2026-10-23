"""Owned-resource access. Slow operations are called by the download runtime.

No method accepts page paths; choose_output accepts only a native picker result.
Preview validation, import and delete belong outside the bridge service lane.
"""
from pathlib import Path
import os
from sqlalchemy import select, delete

from yijianjifa.features.downloads.contracts import DownloadError
from yijianjifa.features.downloads.files import _Guard, _safe_final_name, _metadata_key
from yijianjifa.features.library.copy_service import CategoryRepository
from yijianjifa.features.library.media_storage import MediaStorageService
from yijianjifa.storage.models import DownloadCandidateRow, DownloadSubtitleOriginalRow, MediaAsset, Setting, Category


class DownloadAccess:
    def __init__(self, files, engine, paths, media):
        self.files, self.engine, self.paths, self.media = files, engine, paths, media
        self.files._revoke_callbacks.append(self._revoke)

    def choose_output(self, native_directory: Path) -> dict:
        return self.files.choose_output(native_directory)

    def read_output(self) -> dict:
        return self.files.read_output()

    def _revoke(self, resource_id):
        self.media.revoke_read('download:' + resource_id)

    def preview(self, resource_id: str) -> dict:
        with self.files.open_resource(resource_id) as bound:
            resource = bound.resource
            if resource.kind not in ('source_video','video','preview','silent_video','audio_mp3','audio_wav','cover'):
                raise DownloadError('unsupported_source')
            path = bound.path; bound.verify()
            result = self.media.grant_read(path,name=path.name,kind=resource.kind,owner='download:'+resource_id,
                verify_fd=lambda fd: self.files.verify_read_fd(bound,fd))
            try: bound.verify()
            except Exception: self._revoke(resource_id); raise
            return result

    def reveal(self, resource_id: str) -> None:
        from yijianjifa.os_integration.reveal import reveal_path
        with self.files.open_resource(resource_id) as bound:
            bound.verify(); reveal_path(bound.path); bound.verify()

    def import_video(self, resource_id: str, category_ids: tuple[str, ...], *, cancel) -> dict:
        if (type(category_ids) is not tuple or len(category_ids) > 100 or any(type(v) is not str or not 1<=len(v)<=64 for v in category_ids)
                or not set(category_ids) <= {c.id for c in CategoryRepository(self.engine).list('video')}):
            raise DownloadError('input_limit')
        if cancel.cancelled(): raise DownloadError('cancelled')
        with self.files.open_resource(resource_id,cancel=cancel) as bound:
            resource = bound.resource
            if resource.kind not in ('source_video','video','preview','silent_video'): raise DownloadError('unsupported_source')
            path = bound.path
            self.files._validate(path, resource.kind, cancel)
            if self.files._hash(bound.guard,cancel) != (resource.sha256,resource.size_bytes,bound.journal['identity']):
                raise DownloadError('identity_mismatch')
            bound.verify()
            with self.engine.connect() as connection:
                if cancel.cancelled(): raise DownloadError('cancelled')
                connection.exec_driver_sql('BEGIN IMMEDIATE')
                try:
                    # Cheap revalidation closes the gap with category removal.
                    categories = set(connection.execute(select(Category.id).where(Category.kind=='video',Category.id.in_(category_ids))).scalars())
                    if categories != set(category_ids): raise DownloadError('input_limit')
                    previous = connection.execute(select(MediaAsset.id).where(MediaAsset.kind=='video',MediaAsset.content_hash==resource.sha256)).scalar_one_or_none()
                    row = connection.execute(select(DownloadCandidateRow.__table__).where(DownloadCandidateRow.id==resource.candidate_id)).mappings().one()
                    bound.verify()
                    asset = MediaStorageService(self.engine,self.paths,kind='video').import_prepared(connection,path,resource.sha256,category_ids)
                    self.files._save(connection,'import',resource_id,{'v':1,'asset_id':asset.id,'candidate_id':resource.candidate_id,'canonical_source':row['canonical_source']})
                    bound.verify()
                    if cancel.cancelled(): raise DownloadError('cancelled')
                    connection.commit()
                except Exception: connection.rollback(); raise
                return {'asset_id':asset.id,'reused':previous is not None}

    def delete_file(self, resource_id: str, *, auxiliary_job_id: str | None = None) -> None:
        """Runtime may exempt only its DB-verified current delete job."""
        self.files.delete(resource_id,auxiliary_job_id=auxiliary_job_id)

    def export_subtitle(self, store, resource_id, origin, format, native_target, *, cancel, progress=None, expected_revision=None):
        """Native save result only; output is a distinct owned physical ID.

        Formatting writes to a pinned descriptor, then the ordinary owned-file
        preparation journal publishes exclusively to the native-selected name.
        This background entry does not accept browser-supplied paths.
        """
        target = Path(native_target)
        if (not target.is_absolute() or format not in ('txt','srt') or not _safe_final_name(target.name)
                or target.suffix.lower() != '.' + format): raise DownloadError('unsafe_source')
        if cancel.cancelled(): raise DownloadError('cancelled')
        temporary = None
        with self.files.lock, _Guard(target.parent) as parent:
            if os.name == 'nt': exists = target.exists() or target.is_symlink()
            else:
                try: os.stat(target.name,dir_fd=parent.fd,follow_symlinks=False); exists = True
                except FileNotFoundError: exists = False
            if exists: raise DownloadError('unsafe_source')
            with self.engine.connect() as connection:
                original = self.files.repository._row(connection, DownloadSubtitleOriginalRow, resource_id)
                if original['origin'] != origin: raise DownloadError('subtitle_invalid')
                self.files.repository._ownership(connection, original['candidate_id'], original['item_id'])
            grant = self.files._choose_output(target.parent,set_current=False)
            kind = 'subtitle_' + format
            temporary = self.files.allocate(original['candidate_id'],kind,item_id=original['item_id'],grant_id=grant['grant_id'])
            tid, _ = self.files._temporary(temporary)
            try:
                with self.files.open_temporary(temporary) as bound:
                    owned = bound.guard
                    try:
                        flags = os.O_WRONLY | getattr(os,'O_NOFOLLOW',0)
                        fd = os.open(temporary if os.name == 'nt' else temporary.name,flags,
                                     **({} if os.name == 'nt' else {'dir_fd':owned.parent_fd}))
                        with os.fdopen(fd,'w',encoding='utf-8',newline='\n') as output:
                            info = os.fstat(output.fileno())
                            if (info.st_dev,info.st_ino) != owned.identity: raise DownloadError('identity_mismatch')
                            store.write_to(resource_id,origin,format,output,cancel=cancel,progress=progress,expected_revision=expected_revision)
                            output.flush(); os.fsync(output.fileno())
                        owned.verify(); parent.verify()
                    except Exception:
                        owned.cleanup_owned()
                        with self.engine.begin() as connection:
                            connection.execute(delete(Setting).where(Setting.key==_metadata_key('temporary',tid)))
                        temporary = None
                        raise
                return self.files._promote(original['candidate_id'],temporary,kind,item_id=original['item_id'],cancel=cancel,native_name=target.name)
            finally:
                if temporary is not None:
                    try: self.files.release_temporary(temporary)
                    except DownloadError: pass
