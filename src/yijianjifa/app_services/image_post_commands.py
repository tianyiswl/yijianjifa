"""Local image-post commands. Native file grants never become JavaScript paths."""
import base64
import hashlib
import json
from io import BytesIO
import re
import threading

from yijianjifa.features.image_posts.files import ImagePostFiles
from yijianjifa.features.image_posts.service import ImagePostService
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting


def identifier(value):
    if type(value) is not str or not re.fullmatch(r'[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}', value):
        raise ValueError('选择已失效，请刷新后重新选择。')
    return value


def identifiers(values):
    if (type(values) is not list or not 1 <= len(values) <= 100
            or any(type(value) is not str for value in values) or len(set(values)) != len(values)):
        raise ValueError('请选择1至100篇图文。')
    return [identifier(value) for value in values]


class ImagePostCommands:
    def __init__(self, engine, paths):
        self.engine, self.paths = engine, paths
        self.core = ImagePostService(engine, paths)
        self.files = ImagePostFiles(engine, paths)
        self.lock = threading.RLock()

    def _thumbnail(self, path, *, summary=False):
        if not path or not path.is_file() or path.is_symlink():
            return ''
        if not path.resolve().is_relative_to(self.core.root.resolve()):
            return ''
        data = path.read_bytes()
        if len(data) > 1024 * 1024 or not data.startswith(b'\x89PNG\r\n\x1a\n'):
            return ''
        # Every bridge response is bounded, including a 100-image editor.
        # Full pixels are served only through an owned native preview grant.
        if data:
            from PIL import Image
            with Image.open(BytesIO(data)) as image:
                image.thumbnail((120, 120), Image.Resampling.LANCZOS)
                image = image.convert('RGB')
                encoded = BytesIO()
                image.save(encoded, 'JPEG', quality=65)
            return 'data:image/jpeg;base64,' + base64.b64encode(encoded.getvalue()).decode('ascii')
        return ''

    def dto(self, record, *, summary=False):
        if record is None:
            raise ValueError('这篇图文已不存在，请刷新列表。')
        images = [dict(id=image.id, position=image.position, original_name=image.original_name,
                       width=image.width, height=image.height, format=image.format,
                       thumbnail_url=self._thumbnail(image.thumbnail_path, summary=summary))
                  for image in (record.images[:1] if summary else record.images)]
        return dict(id=record.id, display_name=record.display_name, title=record.title,
                    body=record.body, tags=record.tags, content_hash=record.content_hash,
                    created_at=record.created_at.isoformat(), image_count=len(record.images), images=images,
                    thumbnail_url=images[0]['thumbnail_url'] if images else '')

    @staticmethod
    def success(rid, data):
        return {'v': 1, 'request_id': rid, 'ok': True, 'data': data}

    @staticmethod
    def error(rid, message, code='image_post_invalid'):
        return {'v': 1, 'request_id': rid, 'ok': False,
                'error': {'code': code, 'message': message, 'retryable': False}}

    @staticmethod
    def fields(params, expected):
        if set(params) != set(expected):
            raise ValueError('图文操作参数无效，请刷新后重试。')

    def dispatch(self, request):
        rid, op, p = request['request_id'], request['operation'], request['params']
        writes = {'image_posts.save', 'image_posts.delete', 'image_posts.import.confirm',
                  'image_posts.metadata.confirm'}
        try:
            with self.lock:
                key = 'image_posts.command:' + rid
                digest = hashlib.sha256(json.dumps([op, p], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                if op in writes:
                    with session_scope(self.engine) as db:
                        previous = db.get(Setting, key)
                        if previous:
                            receipt = json.loads(previous.value_json)
                            if receipt['digest'] != digest:
                                raise ValueError('这次操作已改变，请重新操作。')
                            return receipt['response']
                if op == 'image_posts.list':
                    self.fields(p, {'query', 'offset', 'limit'})
                    if (type(p['query']) is not str or len(p['query']) > 200 or
                            type(p['offset']) is not int or p['offset'] < 0 or
                            type(p['limit']) is not int or not 1 <= p['limit'] <= 100):
                        raise ValueError('列表筛选无效，请重新选择。')
                    records, total = self.core.list(**p)
                    items, size = [], 0
                    for record in records:
                        item = self.dto(record, summary=True)
                        item_bytes = len(json.dumps(item, ensure_ascii=False).encode('utf-8'))
                        if items and size + item_bytes > 1800000:
                            break
                        items.append(item)
                        size += item_bytes
                    data = {'items': items, 'total': total}
                elif op == 'image_posts.get':
                    self.fields(p, {'id'})
                    data = self.dto(self.core.get(identifier(p['id'])))
                elif op == 'image_posts.save':
                    self.fields(p, {'id', 'display_name', 'title', 'body', 'tags', 'image_ids'})
                    for name in ('display_name', 'title', 'body', 'tags'):
                        if type(p[name]) is not str or len(p[name]) > 10000:
                            raise ValueError('文案内容过长，请缩短后保存。')
                    identifiers(p['image_ids'])
                    pid = identifier(p['id'])
                    values = {k: v for k, v in p.items() if k != 'id'}
                    if not values['display_name'].strip():
                        original = self.core.get(pid)
                        if original is None:
                            raise ValueError('这篇图文已不存在，请刷新列表。')
                        values['display_name'] = original.display_name
                    data = self.dto(self.core.save(pid, values))
                elif op == 'image_posts.delete':
                    self.fields(p, {'ids'})
                    ids = identifiers(p['ids'])
                    self.core.delete(ids)
                    data = {'deleted': len(ids)}
                elif op in {'image_posts.import.confirm', 'image_posts.metadata.confirm'}:
                    self.fields(p, {'token'})
                    token = identifier(p['token'])
                    def receipt_writer(db, confirmed):
                        receipt_response = self.success(rid, confirmed)
                        db.add(Setting(key=key, value_json=json.dumps({'digest': digest, 'response': receipt_response}, ensure_ascii=False)))
                    confirm = self.files.confirm_import if op == 'image_posts.import.confirm' else self.files.confirm_metadata
                    data = confirm(token, receipt_writer=receipt_writer)
                    # The business change and this bridge response were committed
                    # together; never write a second, independent receipt.
                    return self.success(rid, data)
                elif op == 'image_posts.stage.cancel':
                    self.fields(p, {'token'})
                    self.files.cancel_stage(identifier(p['token']))
                    data = {'cancelled': True}
                else:
                    return self.error(rid, '不支持此图文操作。', 'unknown_operation')
                response = self.success(rid, data)
                if op in writes:
                    with session_scope(self.engine) as db:
                        db.add(Setting(key=key, value_json=json.dumps({'digest': digest, 'response': response}, ensure_ascii=False)))
                return response
        except (ValueError, KeyError) as error:
            return self.error(rid, str(error) if isinstance(error, ValueError) else '所选图文已不存在，请刷新。')
        except Exception:
            return self.error(rid, '图文操作未完成，请检查图片文件和磁盘权限。', 'image_post_failed')

    def native(self, request, picker, drops, media):
        rid, op, p = request['request_id'], request['operation'], request['params']
        try:
            with self.lock:
                if op == 'image_posts.import':
                    self.fields(p, {'batch'})
                    if type(p['batch']) is not bool:
                        raise ValueError('请选择单篇或批量导入。')
                    data = self.files.stage_import(picker(op, p) or [], batch=p['batch'])
                elif op == 'image_posts.drop':
                    self.fields(p, {'token', 'batch'})
                    if type(p['batch']) is not bool or type(p['token']) is not str or len(p['token']) > 128:
                        raise ValueError('拖入选择无效，请重新拖入。')
                    data = self.files.stage_import(drops.consume(p['token']), batch=p['batch'])
                elif op in {'image_posts.add_images', 'image_posts.add_images.drop'}:
                    self.fields(p, {'id', 'token'} if op.endswith('.drop') else {'id'})
                    pid = identifier(p['id'])
                    if self.core.get(pid) is None:
                        raise ValueError('这篇图文已不存在。')
                    if op.endswith('.drop'):
                        if type(p['token']) is not str or len(p['token']) > 128:
                            raise ValueError('拖入选择无效，请重新拖入。')
                        selected = drops.consume(p['token'])
                    else:
                        selected = picker(op, p) or []
                    data = self.dto(self.core.add_images(pid, selected)) if selected else {'cancelled': True}
                elif op == 'image_posts.metadata.import':
                    self.fields(p, set())
                    data = self.files.stage_metadata(picker(op, p) or [])
                elif op == 'image_posts.template.export':
                    self.fields(p, set())
                    data = self.files.export_template(picker(op, p) or [])
                elif op == 'image_posts.export':
                    self.fields(p, {'ids'})
                    ids = identifiers(p['ids'])
                    data = self.files.export(ids, picker(op, p) or [])
                elif op == 'image_posts.preview':
                    self.fields(p, {'id', 'image_id'})
                    record = self.core.get(identifier(p['id']))
                    iid = identifier(p['image_id'])
                    image = next((i for i in record.images if i.id == iid), None) if record else None
                    if image is None or not image.path.resolve().is_relative_to(self.core.root.resolve()) or image.path.is_symlink():
                        raise ValueError('图片预览不可用，请重新选择。')
                    def verify(fd):
                        import os
                        value = hashlib.sha256()
                        while block := os.read(fd, 1024 * 1024):
                            value.update(block)
                        if value.hexdigest() != image.content_hash:
                            raise ValueError('图片副本已改变，请重新导入。')
                    data = media.grant_read(image.path, name=image.original_name, kind='image',
                                            owner='image_post_preview', verify_fd=verify)
                else:
                    return self.error(rid, '不支持此图文文件操作。', 'unknown_operation')
                return self.success(rid, data)
        except (ValueError, KeyError) as error:
            return self.error(rid, str(error) if isinstance(error, ValueError) else '所选图文已不存在，请刷新。')
        except Exception:
            return self.error(rid, '图文文件操作未完成，请检查文件和目录权限。', 'image_post_failed')
