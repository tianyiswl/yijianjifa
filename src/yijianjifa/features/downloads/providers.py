from typing import Protocol
from .contracts import Candidate, CancelToken, DownloadError, MediaSource, ResolvePage, SourceInput
from .network import SourceRegistry

class Provider(Protocol):
    def resolve(self, source: SourceInput, cursor: str | None, *, cancel: CancelToken) -> ResolvePage: ...
    def media(self, candidate: Candidate, *, cancel: CancelToken) -> MediaSource: ...
    def cover(self, candidate: Candidate, *, cancel: CancelToken) -> MediaSource | None: ...

class DirectProvider:
    def __init__(self, registry: SourceRegistry):
        self.registry = registry
        self._sources: dict[str, MediaSource] = {}

    def resolve(self, source: SourceInput, cursor: str | None, *, cancel: CancelToken) -> ResolvePage:
        if cancel.cancelled(): raise DownloadError('cancelled')
        if source.kind != 'direct' or cursor is not None: raise DownloadError('unsupported_source')
        self._sources[source.source_key] = self.registry.put(source.canonical_source, headers={})
        candidate = Candidate(source.source_key, 'direct', source.source_key, source.source_key, None)
        return ResolvePage((candidate,), None, False, 'complete')

    def media(self, candidate: Candidate, *, cancel: CancelToken) -> MediaSource:
        if cancel.cancelled(): raise DownloadError('cancelled')
        try: return self._sources[candidate.id]
        except KeyError: raise DownloadError('source_missing') from None

    def cover(self, candidate: Candidate, *, cancel: CancelToken) -> MediaSource | None:
        if cancel.cancelled(): raise DownloadError('cancelled')
        return None

# Normal publicly readable response metadata only. Signed media remains in registry.
import re
import secrets
import time
from urllib.parse import urlsplit


def _display(value):
    return re.sub(r'https?://\S+', '[链接]', str(value or ''))[:500]


class DouyinProvider:
    available = False  # Only the real-sample acceptance gate may change this.

    def __init__(self, registry: SourceRegistry, reader):
        self.registry, self.reader = registry, reader
        self._sessions, self._media, self._owned = {}, {}, {}
        self._cover_urls, self._covers = {}, {}

    @staticmethod
    def _item(item, target=None):
        if not isinstance(item, dict): raise DownloadError('metadata_missing')
        work_id = item.get('aweme_id')
        if not isinstance(work_id, str) or not re.fullmatch(r'\d{1,32}', work_id):
            raise DownloadError('metadata_missing')
        author = item.get('author')
        sec_uid = author.get('sec_uid') if isinstance(author, dict) else None
        if not isinstance(sec_uid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', sec_uid):
            raise DownloadError('metadata_missing')
        if target is not None and sec_uid != target: raise DownloadError('metadata_missing')
        if any(value is not None and not isinstance(value, str) for value in (item.get('desc'), author.get('nickname'))):
            raise DownloadError('metadata_missing')
        if item.get('images') or item.get('aweme_type') in (2, 68): return None
        if not isinstance(item.get('video'), dict): raise DownloadError('metadata_missing')
        return work_id, sec_uid

    def _register(self, candidate, item):
        video = item['video']
        address = video.get('play_addr')
        urls = address.get('url_list') if isinstance(address, dict) else None
        if not isinstance(urls, list) or not urls or not isinstance(urls[0], str):
            raise DownloadError('metadata_missing')
        handle = self.registry.put(urls[0], headers={'Referer': 'https://www.douyin.com/'})
        key = id(candidate)
        old = self._media.get(key)
        if old: self.registry.revoke(old)
        self._media[key] = handle
        return handle

    def resolve(self, source, cursor, *, cancel):
        from .input import normalize_inputs
        if cancel.cancelled(): raise DownloadError('cancelled')
        if source.kind not in ('douyin_video', 'douyin_profile'): raise DownloadError('unsupported_source')
        # SourceInput is public; validate its canonical URL and source binding as well.
        if normalize_inputs(source.canonical_source)[0] != source: raise DownloadError('unsafe_source')
        now = time.monotonic()
        for key, session in list(self._sessions.items()):
            if now - session['created'] > 900: del self._sessions[key]
        if cursor is not None:
            old = self._sessions.get(cursor)
            if old is None or old['source'] != source: raise DownloadError('source_missing')
            state = dict(old, rows=list(old['rows']), seen=set(old['seen']), cursors=set(old['cursors']))
        else:
            if len(self._sessions) >= 8: raise DownloadError('input_limit')
            state = dict(source=source, rows=[], seen=set(), offset=0, created=now,
                         remote_more=False, completion='partial', native_cursor='0', cursors={'0'}, native_reads=0)
        rows, offset = state['rows'], state['offset']
        staged = []
        if cursor is None or (offset >= len(rows) and state['remote_more']):
            if state['native_reads'] >= 25:
                state['remote_more'], state['completion'] = False, 'limited'
            else:
                # Actual attempts consume the read budget even if validation fails;
                # all catalog/cursor data still commits only after successful validation.
                state['native_reads'] += 1
                if cursor is not None: old['native_reads'] = state['native_reads']
                payload = (self.reader.read_next(source.canonical_source, cancel=cancel) if cursor is not None
                           else self.reader.read(source.canonical_source, cancel=cancel))
                if not isinstance(payload, dict): raise DownloadError('metadata_missing')
                if payload.get('verification'): raise DownloadError('verification_required')
                profile = source.kind == 'douyin_profile'
                target = urlsplit(source.canonical_source).path.rstrip('/').rsplit('/', 1)[-1] if profile else None
                if profile:
                    if not re.fullmatch(r'/user/[A-Za-z0-9_-]{1,200}/?',urlsplit(source.canonical_source).path):
                        raise DownloadError('unsafe_source')
                    if (type(payload.get('status_code')) is not int or payload['status_code'] != 0
                            or payload.get('_catalog_source') != target
                            or payload.get('_catalog_request_cursor') != state['native_cursor']):
                        raise DownloadError('metadata_missing')
                    raw = payload.get('aweme_list')
                    more = payload.get('has_more')
                    if more is not None and (type(more) not in (int, bool) or more not in (0, 1)):
                        raise DownloadError('metadata_missing')
                    native_cursor = payload.get('max_cursor')
                    if native_cursor is not None and (type(native_cursor) not in (str, int) or not re.fullmatch(r'\d{1,32}', str(native_cursor))):
                        raise DownloadError('metadata_missing')
                    native_cursor = str(native_cursor) if native_cursor is not None else None
                    advancing = native_cursor is not None and native_cursor not in state['cursors']
                    # Empty terminal first page may legitimately retain cursor zero.
                    terminal_empty = raw == [] and more in (False, 0) and more is not None
                    continuous = advancing or (terminal_empty and native_cursor is not None)
                    state['remote_more'] = more in (True, 1) and continuous
                    state['completion'] = 'complete' if more is not None and not more and continuous else 'partial'
                    if native_cursor is not None:
                        state['native_cursor'] = native_cursor
                        state['cursors'].add(native_cursor)
                else:
                    from .browser import public_video_work_id
                    expected_work_id=public_video_work_id(source.canonical_source)
                    if expected_work_id is None:
                        if urlsplit(source.canonical_source).hostname != 'v.douyin.com': raise DownloadError('unsafe_source')
                        expected_work_id=payload.get('_video_work_id')
                        if (payload.get('_video_source') != source.canonical_source
                                or not isinstance(expected_work_id,str) or not re.fullmatch(r'\d{1,32}',expected_work_id)):
                            raise DownloadError('metadata_missing')
                    raw = [payload.get('aweme_detail')]
                    state['remote_more'], state['completion'] = False, 'complete'
                if not isinstance(raw, list): raise DownloadError('metadata_missing')
                newly_seen = 0
                for item in raw:
                    if cancel.cancelled(): raise DownloadError('cancelled')
                    identity = self._item(item, target)
                    # Native identity progress includes valid skipped image/note works.
                    work_id = item['aweme_id']
                    if work_id in state['seen']: continue
                    state['seen'].add(work_id); newly_seen += 1
                    if identity is None:
                        if len(state['seen']) >= 500:
                            state['remote_more'], state['completion'] = False, 'limited'
                            break
                        if not profile: raise DownloadError('unsupported_source')
                        continue
                    work_id, author_id = identity
                    if not profile and work_id != expected_work_id:
                        raise DownloadError('metadata_missing')
                    duration = item['video'].get('duration')
                    timestamp = item.get('create_time')
                    published = None
                    if type(timestamp) is int and 0 <= timestamp <= 253402300799:
                        from datetime import datetime, timezone
                        published = datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
                    candidate = Candidate('douyin:' + work_id, 'douyin', work_id, source.source_key,
                                          'https://www.douyin.com/video/' + work_id, _display(item.get('desc')),
                                          _display(item['author'].get('nickname')),
                                          duration if type(duration) is int and duration >= 0 else None, published)
                    rows.append(candidate); staged.append((candidate, author_id, item))
                    if len(rows) >= 500 or len(state['seen']) >= 500:
                        state['remote_more'], state['completion'] = False, 'limited'
                        break
                if raw and not newly_seen:
                    state['remote_more'], state['completion'] = False, 'partial'
                if state['native_reads'] >= 25:
                    state['remote_more'], state['completion'] = False, 'limited'
                if len(self._owned) + len(staged) > 4000: raise DownloadError('input_limit')
                # Commit private state only after the whole page passed validation.
                for candidate, author_id, item in staged:
                    if not profile: self._register(candidate, item)
                    self._owned[id(candidate)] = (candidate, author_id, candidate.work_id, candidate.canonical_source)
                    cover = item['video'].get('cover')
                    urls = cover.get('url_list') if type(cover) is dict else None
                    if (type(urls) is list and urls and type(urls[0]) is str
                            and 0 < len(urls[0]) <= 4096):
                        self._cover_urls[id(candidate)] = urls[0]
        end = min(offset + 20, len(rows))
        next_cursor = None
        if end < len(rows) or state['remote_more']:
            next_cursor = secrets.token_urlsafe(24)
            state['offset'] = end
            self._sessions[next_cursor] = state
        if cursor is not None: del self._sessions[cursor]
        completion = state['completion'] if end == len(rows) else ('limited' if state['completion'] == 'limited' else 'partial')
        return ResolvePage(tuple(rows[offset:end]), next_cursor, next_cursor is not None, completion)

    def _binding(self, candidate):
        binding = self._owned.get(id(candidate))
        if (binding is None or binding[0] is not candidate
                or (candidate.work_id, candidate.canonical_source) != binding[2:]):
            raise DownloadError('source_missing')
        return binding[1]

    def author_profile(self, candidate):
        return 'https://www.douyin.com/user/' + self._binding(candidate)

    def media(self, candidate, *, cancel):
        if cancel.cancelled(): raise DownloadError('cancelled')
        author_id = self._binding(candidate)
        if id(candidate) in self._media: return self._media[id(candidate)]
        payload = self.reader.read(candidate.canonical_source, cancel=cancel)
        if not isinstance(payload, dict): raise DownloadError('metadata_missing')
        if payload.get('verification'): raise DownloadError('verification_required')
        item = payload.get('aweme_detail')
        if self._item(item, author_id) != (candidate.work_id, author_id):
            raise DownloadError('metadata_missing')
        if cancel.cancelled(): raise DownloadError('cancelled')
        return self._register(candidate, item)

    def cover(self, candidate, *, cancel):
        if cancel.cancelled(): raise DownloadError('cancelled')
        self._binding(candidate)
        key = id(candidate)
        if key in self._covers: return self._covers[key]
        url = self._cover_urls.get(key)
        if url is None: return None
        # Only the already validated item's static cover. Never read a new page.
        source = self.registry.put(url, headers={'Referer': 'https://www.douyin.com/'})
        self._covers[key] = source
        return source

    def close(self):
        for handle in self._media.values(): self.registry.revoke(handle)
        for handle in self._covers.values(): self.registry.revoke(handle)
        self._covers.clear(); self._cover_urls.clear()
        self._media.clear(); self._sessions.clear(); self._owned.clear()
