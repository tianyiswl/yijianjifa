"""Versioned subtitle resources, independent of disk output file identities.

Writes use the receipt owner's transaction. Disk export is a background action
on a native-selected destination; never call it from a bridge write transaction.
"""
from dataclasses import asdict
import base64
import hmac
import json
import os
from pathlib import Path
import secrets
import stat

from sqlalchemy import select

from yijianjifa.storage.models import (
    DownloadCandidateRow, DownloadSubtitleOriginalRow,
    DownloadSubtitleDraftRow, DownloadSubtitleRevisionRow,
)
from .contracts import DownloadError, SubtitleSegment

ORIGINS = ('platform_caption', 'embedded_caption', 'asr', 'ocr')
BUDGET = 12288
_MAX_DOCUMENT_BYTES = 32 * 1024 * 1024


def _encoded(value):
    try:
        return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (ValueError, TypeError, UnicodeError):
        raise DownloadError('subtitle_invalid') from None


def _segments(document, duration_ms=None):
    if type(document) is not list or len(document) > 100000:
        raise DownloadError('subtitle_invalid')
    result = []
    seen = set()
    previous_end = 0
    try:
        for row in document:
            if type(row) is not dict or set(row) != {'id', 'start_ms', 'end_ms', 'text', 'confidence'}:
                raise DownloadError('subtitle_invalid')
            segment = SubtitleSegment(**row)
            if (segment.id in seen or segment.start_ms < previous_end
                    or segment.end_ms > 7 * 24 * 3600000
                    or duration_ms is not None and segment.end_ms > duration_ms
                    or '\x00' in segment.text):
                raise DownloadError('subtitle_invalid')
            _encoded(row)
            seen.add(segment.id)
            previous_end = segment.end_ms
            result.append(segment)
    except (TypeError, ValueError, DownloadError):
        raise DownloadError('subtitle_invalid') from None
    return tuple(result)


class SubtitleStore:
    def __init__(self, repository):
        self.repository = repository
        self.engine = repository.engine
        self._cursor_key = secrets.token_bytes(32)

    def _document(self, connection, resource_id, origin):
        if type(resource_id) is not str or len(resource_id) > 64 or origin not in ORIGINS:
            raise DownloadError('subtitle_invalid')
        original = self.repository._row(connection, DownloadSubtitleOriginalRow, resource_id)
        self.repository._ownership(connection, original['candidate_id'], original['item_id'])
        if original['origin'] != origin:
            raise DownloadError('subtitle_invalid')
        candidate = self.repository._row(connection, DownloadCandidateRow, original['candidate_id'])
        drafts = list(connection.execute(select(DownloadSubtitleDraftRow.__table__).where(
            DownloadSubtitleDraftRow.original_id == resource_id)).mappings())
        if len(drafts) > 1:
            raise DownloadError('subtitle_invalid')
        draft_id = drafts[0]['id'] if drafts else None
        latest = connection.execute(select(DownloadSubtitleRevisionRow.__table__).where(
            DownloadSubtitleRevisionRow.draft_id == draft_id).order_by(
                DownloadSubtitleRevisionRow.revision.desc()).limit(1)).mappings().first() if draft_id else None
        encoded = latest['segments_json'] if latest else original['segments_json']
        try:
            if type(encoded) is not str or len(encoded.encode('utf-8')) > _MAX_DOCUMENT_BYTES:
                raise DownloadError('input_limit')
            segments = _segments(json.loads(encoded), candidate['duration_ms'])
        except (ValueError, UnicodeError):
            raise DownloadError('subtitle_invalid') from None
        return original, draft_id, latest['revision'] if latest else 0, segments

    def _cursor(self, resource_id, origin, revision, offset):
        body = _encoded([resource_id, origin, revision, offset])
        signature = hmac.digest(self._cursor_key, body, 'sha256')
        return base64.urlsafe_b64encode(signature + body).decode('ascii').rstrip('=')

    def _offset(self, cursor, resource_id, origin, revision):
        if cursor is None:
            return 0
        if type(cursor) is not str or len(cursor) > 512:
            raise DownloadError('subtitle_invalid')
        try:
            decoded = base64.b64decode(cursor + '=' * (-len(cursor) % 4), altchars=b'-_', validate=True)
            signature, body = decoded[:32], decoded[32:]
            if not hmac.compare_digest(signature, hmac.digest(self._cursor_key, body, 'sha256')):
                raise ValueError()
            value = json.loads(body)
            if type(value) is not list or len(value) != 4 or value[:2] != [resource_id, origin]:
                raise ValueError()
            if type(value[2]) is not int or type(value[3]) is not int or value[3] < 0:
                raise ValueError()
            if value[2] != revision:
                raise DownloadError('subtitle_conflict')
            return value[3]
        except (ValueError, TypeError, UnicodeError):
            raise DownloadError('subtitle_invalid') from None

    def read(self, resource_id, origin, cursor, *, budget_bytes=BUDGET):
        if type(budget_bytes) is not int or not 512 <= budget_bytes <= BUDGET:
            raise DownloadError('input_limit')
        with self.engine.connect() as connection:
            original, draft_id, revision, segments = self._document(connection, resource_id, origin)
        offset = self._offset(cursor, resource_id, origin, revision)
        if offset > len(segments):
            raise DownloadError('subtitle_invalid')
        response = {'resource_id': resource_id, 'origin': origin, 'draft_id': draft_id,
                    'revision': revision, 'total': len(segments), 'offset': offset,
                    'segments': [], 'next_cursor': None}
        # Account for the cursor and complete response, not just text lengths.
        for index in range(offset, len(segments)):
            response['segments'].append(asdict(segments[index]))
            response['next_cursor'] = self._cursor(resource_id, origin, revision, index + 1) if index + 1 < len(segments) else None
            if len(_encoded(response)) > budget_bytes:
                response['segments'].pop()
                if not response['segments']:
                    raise DownloadError('input_limit')
                response['next_cursor'] = self._cursor(resource_id, origin, revision, index)
                break
        return response

    def save(self, connection, resource_id, origin, expected_revision, patches):
        self.repository._write(connection)
        if (type(expected_revision) is not int or expected_revision < 0
                or type(patches) is not tuple or not 1 <= len(patches) <= 200):
            raise DownloadError('subtitle_invalid')
        params = {'resource_id': resource_id, 'origin': origin, 'expected_revision': expected_revision, 'patches': patches}
        if len(_encoded(params)) > BUDGET:
            raise DownloadError('input_limit')
        original, draft_id, revision, segments = self._document(connection, resource_id, origin)
        if expected_revision != revision:
            raise DownloadError('subtitle_conflict')
        changes = {}
        known_ids = {segment.id for segment in segments}
        for patch in patches:
            if (type(patch) is not dict or not {'id'} < set(patch)
                    or not set(patch) <= {'id', 'text', 'start_ms', 'end_ms'}
                    or type(patch.get('id')) is not str or patch['id'] not in known_ids
                    or patch['id'] in changes):
                raise DownloadError('subtitle_invalid')
            changes[patch['id']] = patch
        document = [asdict(segment) | changes.get(segment.id, {}) for segment in segments]
        candidate = self.repository._row(connection, DownloadCandidateRow, original['candidate_id'])
        changed = _segments(document, candidate['duration_ms'])
        # Saving must not make a previously readable document impossible to
        # reopen. Include revision/draft/cursor metadata in each segment test.
        if len(_encoded(document)) > _MAX_DOCUMENT_BYTES:
            raise DownloadError('input_limit')
        for index, segment in enumerate(changed):
            single = {'resource_id': resource_id, 'origin': origin,
                      'draft_id': draft_id or 'draft_' + '0' * 32,
                      'revision': revision + 1, 'total': len(changed), 'offset': index,
                      'segments': [asdict(segment)], 'next_cursor': self._cursor(
                          resource_id, origin, revision + 1, index + 1) if index + 1 < len(changed) else None}
            if len(_encoded(single)) > BUDGET:
                raise DownloadError('input_limit')
        if draft_id is None:
            draft_id = self.repository.create_subtitle_draft(connection, resource_id)
        next_revision = self.repository.append_subtitle_revision(connection, draft_id, changed)
        return {'revision': next_revision, 'saved_count': len(patches), 'draft_id': draft_id}

    @staticmethod
    def _time(milliseconds):
        seconds, ms = divmod(milliseconds, 1000)
        minutes, second = divmod(seconds, 60)
        hour, minute = divmod(minutes, 60)
        return f'{hour:02}:{minute:02}:{second:02},{ms:03}'

    def export(self, resource_id, origin, format, destination: Path, *, cancel=None, progress=None,
               expected_revision=None):
        path = Path(destination)
        if not path.is_absolute() or path.suffix.lower() != '.' + format:
            raise DownloadError('unsafe_source')
        temporary = None
        try:
            # Host owns this native-selected path grant. Independently reject
            # existing files/links and reparse parents before writing.
            for parent in (path.parent, *path.parent.parents):
                info = parent.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                    raise DownloadError('unsafe_source')
            if path.exists() or path.is_symlink():
                raise DownloadError('unsafe_source')
            temporary = path.parent / ('.p011-subtitle-' + secrets.token_hex(16) + '.tmp')
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as output:
                self.write_to(resource_id, origin, format, output, cancel=cancel,
                              progress=progress, expected_revision=expected_revision)
                output.flush()
                os.fsync(output.fileno())
            if cancel is not None and cancel.cancelled():
                raise DownloadError('cancelled')
            # Hard-link publication is atomic and never overwrites. Native
            # export grants are rechecked by Access around this operation.
            os.link(temporary, path)
        except OSError:
            raise DownloadError('unsafe_source') from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    # This backend helper may leave only its own temp when
                    # locked; never replace the primary error with raw paths.
                    pass

    def write_to(self, resource_id, origin, format, output, *, cancel=None, progress=None,
                 expected_revision=None):
        """Render to a backend-owned text stream, without opening any path.

        Native Access pins the parent and creates/publishes/removes its own
        temporary files by descriptor. This is the production export handoff.
        """
        if format not in ('txt', 'srt'):
            raise DownloadError('subtitle_invalid')
        with self.engine.connect() as connection:
            _, _, revision, segments = self._document(connection, resource_id, origin)
        if expected_revision is not None and expected_revision != revision:
            raise DownloadError('subtitle_conflict')
        from .contracts import Progress
        for index, segment in enumerate(segments):
            if cancel is not None and cancel.cancelled():
                raise DownloadError('cancelled')
            if format == 'txt':
                output.write(segment.text + '\n')
            else:
                text = '\n'.join(line for line in segment.text.splitlines() if line.strip())
                output.write(f'{index + 1}\n{self._time(segment.start_ms)} --> {self._time(segment.end_ms)}\n{text}\n\n')
            if progress is not None:
                progress(Progress('export', index + 1, len(segments)))
        if cancel is not None and cancel.cancelled():
            raise DownloadError('cancelled')
