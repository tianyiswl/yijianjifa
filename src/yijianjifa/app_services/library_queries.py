from __future__ import annotations

from dataclasses import asdict
import json
import re
import uuid
import threading

from yijianjifa.features.library.copy_service import CopyRepository, CategoryRepository


class InvalidRequest(ValueError):
    pass


class NativeReadGate:
    """Native-only authority for the initial, immutable about:blank document.

    Never expose this object through js_api. The Cocoa adapter supplies view,
    frame and URL directly from WKScriptMessage; navigation must be fenced.
    """

    def __init__(self, service):
        self._service = service
        self._view = None
        self._closed = False
        self._lock = threading.RLock()

    def bind(self, view):
        with self._lock:
            if self._closed or self._view is not None or view is None:
                raise RuntimeError('gate cannot be rebound')
            self._view = view

    def receive(self, raw, *, view, main_frame, url):
        with self._lock:
            trusted = (not self._closed and self._view is not None
                       and view is self._view and main_frame is True
                       and url == 'about:blank')
            return self._service.dispatch(raw, trusted_origin=trusted)

    def close(self):
        with self._lock:
            self._closed = True
            self._view = None


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InvalidRequest()
        result[key] = value
    return result


class LibraryQueries:
    """Version-1 read-only service contract, not a complete browser transport.

    trusted_origin must come from the native adapter, never from a JS parameter.
    No writes, platform secrets, native path access or generic dispatch are exposed.
    """

    def __init__(self, engine):
        self._copies = CopyRepository(engine)
        self._categories = CategoryRepository(engine)

    def dispatch(self, raw: str, *, trusted_origin: bool = False) -> dict:
        request_id = None
        if trusted_origin is not True:
            return self._error('origin_denied', request_id)
        try:
            if type(raw) is not str or len(raw.encode('utf-8')) > 16384:
                raise InvalidRequest()
            payload = json.loads(raw, object_pairs_hook=_unique_object)
            if type(payload) is not dict or set(payload) != {'v', 'request_id', 'operation', 'params'}:
                raise InvalidRequest()
            if type(payload['v']) is not int or payload['v'] != 1:
                raise InvalidRequest()
            rid, operation, params = payload['request_id'], payload['operation'], payload['params']
            if type(rid) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', rid):
                raise InvalidRequest()
            if type(operation) is not str or len(operation) > 64 or type(params) is not dict:
                raise InvalidRequest()
            request_id = rid
            if operation == 'copy.get':
                if set(params) != {'id'} or type(params['id']) is not str or not 1 <= len(params['id']) <= 64:
                    raise InvalidRequest()
            elif operation == 'category.list':
                if set(params) != {'kind', 'offset', 'limit'} or params['kind'] not in ('video', 'audio', 'music', 'copy', 'finished'):
                    raise InvalidRequest()
                if type(params['offset']) is not int or not 0 <= params['offset'] <= 1000000:
                    raise InvalidRequest()
                if type(params['limit']) is not int or not 1 <= params['limit'] <= 100:
                    raise InvalidRequest()
            else:
                return self._error('unknown_operation', request_id)
        except (ValueError, TypeError, UnicodeError, RecursionError):
            return self._error('invalid_request', request_id)

        try:
            if operation == 'copy.get':
                copy = self._copies.get(params['id'])
                if copy is None:
                    return self._error('not_found', request_id)
                data = asdict(copy)  # CopyDraft contains only the six user-visible fields.
            else:
                rows = self._categories.list('music' if params['kind']=='audio' else params['kind'])
                start = params['offset']
                data = {'items': [asdict(x) for x in rows[start:start+params['limit']]], 'total': len(rows)}
            if len(json.dumps(data, ensure_ascii=False).encode('utf-8')) > 2 * 1024 * 1024:
                return self._error('response_too_large', request_id)
            return {'v': 1, 'request_id': request_id, 'ok': True, 'data': data}
        except Exception:
            # Never forward a storage exception/traceback or session path to the page.
            return self._error('internal_error', request_id)

    @staticmethod
    def _error(code, request_id):
        messages = {
            'origin_denied': '此页面无权读取资料。',
            'invalid_request': '请求格式不正确。',
            'unknown_operation': '不支持此操作。',
            'not_found': '这条资料已不存在，请刷新列表。',
            'response_too_large': '资料内容过大，请缩小读取范围。',
            'internal_error': '读取资料失败，请稍后重试。',
            'publish_metadata_unavailable': '未能读取后台信息：窗口可能已关闭、登录已失效或页面尚未就绪。请重试；如出现上传页，请按提示上传内置测试片。',
            'platform_not_integrated': '此平台的登录和发布通道尚未接入。',
            'app_closing': '客户端正在退出，请稍候。',
            'empty_copy_selection': '当前没有可用文案，请在文案范围选择“不使用文案”，或添加文案后重试。',
            'single_category_only': '每条内容只能选择一个分类。',
        }
        return {'v': 1, 'request_id': request_id, 'ok': False,
                'error': {'code': code, 'message': messages[code],
                          'retryable': code == 'internal_error', 'diagnostic_id': uuid.uuid4().hex}}


class CommandRejected(ValueError):
    pass


class ContextualCommandRejected(CommandRejected):
    """Opt-in message built only from trusted local, already sanitized context."""
    def __init__(self, code, message):
        if type(message) is not str or not 1 <= len(message) <= 1024:
            raise ValueError('invalid local diagnostic')
        super().__init__(code)
        self.public_message = message


class LibraryCommands(LibraryQueries):
    """Finite local writes; core changes and receipt commit in one SQLite transaction.

    Not installed on the read-only native bridge. Request IDs must be unique for
    each new intent and retained across retries, including application restarts.
    """
    _operations = {'copy.save', 'copy.delete', 'category.create', 'category.rename', 'category.delete'}

    def __init__(self, engine):
        super().__init__(engine)
        self._engine = engine

    def dispatch(self, raw, *, trusted_origin=False):
        import hashlib
        from sqlalchemy import text
        rid = None
        if trusted_origin is not True:
            return self._error('origin_denied', rid)
        try:
            if type(raw) is not str or len(raw.encode('utf-8')) > 16384:
                raise InvalidRequest()
            payload = json.loads(raw, object_pairs_hook=_unique_object)
            if type(payload) is not dict or set(payload) != {'v','request_id','operation','params'}:
                raise InvalidRequest()
            if type(payload['v']) is not int or payload['v'] != 1:
                raise InvalidRequest()
            rid = payload['request_id']
            if type(rid) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', rid):
                rid = None
                raise InvalidRequest()
            op, params = payload['operation'], payload['params']
            if type(op) is not str or len(op) > 64 or type(params) is not dict:
                raise InvalidRequest()
            if op not in self._operations:
                return super().dispatch(raw, trusted_origin=True)
            self._validate(op, params)
            digest = hashlib.sha256(json.dumps({'v':1, 'operation':op, 'params':params}, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()
        except CommandRejected as exc:
            return self._error(str(exc), rid)
        except (ValueError, TypeError, UnicodeError, RecursionError):
            return self._error('invalid_request', rid)
        try:
            with self._engine.connect() as connection:
                # Serialize the read-before-write decision across app instances.
                connection.exec_driver_sql('BEGIN IMMEDIATE')
                try:
                    row = connection.execute(text('SELECT payload_hash, response_json FROM web_command_receipts WHERE request_id=:id'), {'id':rid}).first()
                    if row:
                        result = json.loads(row[1]) if row[0] == digest else self._error('request_conflict', rid)
                    else:
                        data = self._apply(connection, op, params)
                        result = {'v':1, 'request_id':rid, 'ok':True, 'data':data}
                        connection.execute(text('INSERT INTO web_command_receipts(request_id,payload_hash,response_json) VALUES (:id,:hash,:response)'),
                                           {'id':rid, 'hash':digest, 'response':json.dumps(result,ensure_ascii=False)})
                    connection.commit()
                    return result
                except Exception:
                    connection.rollback()
                    raise
        except CommandRejected as exc:
            result = self._error(str(exc), rid)
            if type(exc) is ContextualCommandRejected:
                result['error']['message'] = exc.public_message
            return result
        except KeyError:
            return self._error('not_found', rid)
        except Exception:
            return self._error('internal_error', rid)

    @staticmethod
    def _validate(op, p):
        def identity(value):
            return type(value) is str and 1 <= len(value) <= 64
        if op == 'copy.save':
            if set(p) != {'id','title','voice','body','tags','full','category_ids'}:
                raise InvalidRequest()
            if p['id'] is not None and not identity(p['id']):
                raise InvalidRequest()
            if not all(type(p[k]) is str for k in ('title','voice','body','tags','full')):
                raise InvalidRequest()
            ids = p['category_ids']
            if type(ids) is not list or len(ids) > 100 or not all(identity(x) for x in ids):
                raise InvalidRequest()
            if len(ids) > 1:
                raise CommandRejected('single_category_only')
        elif op == 'category.create':
            if set(p) != {'kind','name'} or p['kind'] not in ('video','audio','music','copy','finished'):
                raise InvalidRequest()
        elif op == 'category.rename':
            if set(p) != {'id','name'} or not identity(p['id']):
                raise InvalidRequest()
        elif set(p) != {'id'} or not identity(p['id']):
            raise InvalidRequest()
        if op in ('category.create','category.rename'):
            if type(p['name']) is not str or not 1 <= len(p['name'].strip()) <= 255:
                raise InvalidRequest()

    @staticmethod
    def _apply(connection, op, p):
        from yijianjifa.features.library.copy_service import CopyDraft
        copies = CopyRepository(connection)
        categories = CategoryRepository(connection)
        if op == 'copy.save':
            valid = {r.id for r in categories.list('copy')}
            if not set(p['category_ids']) <= valid:
                raise CommandRejected('invalid_category')
            fields = {k:p[k] for k in ('title','voice','body','tags','full')}
            if p['id'] is None:
                if not any(v.strip() for v in fields.values()):
                    raise CommandRejected('empty_copy')
                record = copies.create(id='W'+uuid.uuid4().hex[:12].upper(), **fields)
            else:
                record = copies.update(CopyDraft(id=p['id'], **fields))
            copies.set_categories(record.id, p['category_ids'])
            return asdict(record)
        if op == 'copy.delete':
            # Reuse the existing unfinished-task reference guard.
            try:
                copies.delete(p['id'])
            except RuntimeError as exc:
                raise CommandRejected('delete_blocked') from exc
            return {'id':p['id']}
        if op == 'category.create':
            return asdict(categories.create('music' if p['kind']=='audio' else p['kind'], p['name']))
        if op == 'category.rename':
            return asdict(categories.rename(p['id'], p['name']))
        categories.delete(p['id'])
        return {'id':p['id']}

    @staticmethod
    def _error(code, request_id):
        extra = {'inspection_expired':'本次检查准备已过期，请重新选择并检查。',
                 'inspection_unavailable':'此平台暂不支持仅检查，请选择快手、B站或视频号。',
                 'inspection_selection_required':'仅检查需选择 1 条视频和 1 个账号。',
                 'inspection_configuration_required':'发布资料尚未设置完整，请补齐后再检查。',
                 'inspection_busy':'已有检查或发布正在使用账号，请完成或取消后再操作。',
                 'cover_staging_full':'本次发布封面缓存已满。已应用封面保持不变，请先保存当前任务，再重启程序后继续设置其他封面。','cover_stale':'封面预览已过期或成品封面已改变，请重新生成预览。',
                 'assignment_stale':'预览已过期或资料、成品信息、使用进度发生变化，请重新预览。',
                 'assignment_no_external':'请选择外部导入的成品；自制成品保留制作时的文案对应。',
                 'assignment_no_copy':'所选范围没有文案，请先添加文案或调整分类。',
                 'account_delete_blocked':'账号仍被未完成任务使用，请先处理或取消相关任务。',
                 'account_window_active':'请先关闭登录或账号后台窗口。',
                 'delete_blocked':'资料被未完成任务引用或任务记录异常，请先处理任务。',
                 'request_conflict':'此请求编号已用于其他操作，请刷新后重新提交。',
                 'publish_schedule_conflict':'该账号在所选时间已有其他发布安排。请到任务记录查看原目标，处理已有安排或选择其他时间；本次目标未修改，也未提交平台。',
                 'invalid_category':'所选分类已失效，请重新选择。',
                 'empty_copy':'请先填写文案内容。'}
        if code not in extra:
            return LibraryQueries._error(code, request_id)
        return {'v':1,'request_id':request_id,'ok':False,
                'error':{'code':code,'message':extra[code],'retryable':False,'diagnostic_id':uuid.uuid4().hex}}
