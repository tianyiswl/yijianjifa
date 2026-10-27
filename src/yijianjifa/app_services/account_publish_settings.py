"""Identity-bound reusable account choices; never stores query/session grants."""
import copy
import hashlib
import json

from yijianjifa.features.accounts.service import AccountService
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import Setting


PREFIX = 'account-publish-settings:v1:'
FIELD_KEYS = {
    'douyin': set(),
    'channels': {'channels_short_title', 'content_declaration_choice', 'channels_original'},
    'kuaishou': {'content_declaration_choice'},
    'bilibili': {'category_choice', 'category_id', 'category_name', 'category_mode',
        'creation_statement_choice', 'creation_statement_id', 'creation_statement_name',
        'content_authorization', 'reprint_source', 'copyright', 'source'},
}


def _identity(account):
    return hashlib.sha256(json.dumps([account.id, account.platform, account.platform_user_id],
        ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def validate_fields(platform, value, *, semantic=False):
    from .multi_publish import fields
    if type(value) is not dict or platform not in FIELD_KEYS or set(value) - FIELD_KEYS[platform]:
        raise ValueError('unsupported account publish defaults')
    result = fields(value)
    if 'content_declaration_choice' in result:
        if platform == 'channels':
            from .channels_declaration_catalog import valid_choice
        else:
            from .kuaishou_declaration_catalog import valid_choice
        if not valid_choice(result['content_declaration_choice']):
            raise ValueError('unsupported account declaration')
    if semantic:
        if platform=='channels' and 'channels_short_title' in result:
            from yijianjifa.webui.channels_publish_form import valid_short_title
            if not valid_short_title(result['channels_short_title']):
                raise ValueError('视频号短标题请填写6至16个支持的字符，或留空。')
        if platform=='bilibili':
            from .bilibili_static_catalog import resolve_fields
            concrete=resolve_fields(result,None,local=True)
            statement=concrete.get('creation_statement_id')
            if 'creation_statement_id' in result and statement not in {-1,-2} and statement<=0:
                raise ValueError('请选择有效的B站创作声明。')
            source=result.get('reprint_source','');authorized=result.get('content_authorization',False)
            if len(source.encode('utf-16-le'))//2>200:raise ValueError('B站转载来源不能超过200字。')
            if statement==-2 and (authorized or not source.strip()):
                raise ValueError('转载须填写来源，不能勾选自制内容授权。')
            if statement!=-2 and source:raise ValueError('非转载声明不应填写转载来源。')
            if result.get('copyright') in {'repost',2} and (authorized or not result.get('source','').strip()):
                raise ValueError('转载须填写来源，不能勾选自制内容授权。')
    return result


class AccountPublishSettings:
    PREFIX = PREFIX

    def __init__(self, engine, *, catalog=None, declarations=None, locations=None):
        from .commerce_declarations import CommerceDeclarations
        from .douyin_declaration_catalog import catalog as declaration_catalog
        from .publish_locations import PublishLocations
        self.engine, self.catalog = engine, catalog
        self.declarations = declarations or CommerceDeclarations(engine, capability='ordinary', catalog=declaration_catalog())
        self.locations = locations or PublishLocations(engine)

    def for_engine(self, engine):
        # Frozen default resolution needs fresh query references, not the UI's
        # existing tokens. Bind cache reads/writes to the caller's transaction
        # and retain the current provider's builtin-versus-observed contract.
        from .commerce_declarations import CommerceDeclarations
        from .publish_locations import PublishLocations
        return AccountPublishSettings(engine,catalog=self.catalog,
            declarations=CommerceDeclarations(engine,capability='ordinary',catalog=copy.deepcopy(self.declarations._catalog)),
            locations=PublishLocations(engine))

    def _account(self, account_id):
        if type(account_id) is not str or not 1 <= len(account_id) <= 64:
            raise ValueError('invalid account')
        account = AccountService(self.engine).get(account_id)
        if account.platform not in FIELD_KEYS:
            raise ValueError('unsupported account platform')
        return account

    def _schema(self, platform):
        if self.catalog is not None:
            try:
                schema = self.catalog.transport_for(platform).public_field_schema
                return {key: copy.deepcopy(value) for key, value in schema.items() if key in FIELD_KEYS[platform]}
            except (ValueError, AttributeError):
                pass
        return {}

    def read(self, account_id):
        account = self._account(account_id)
        saved = None
        with session_scope(self.engine) as db:
            row = db.get(Setting, PREFIX + account.id)
            if row:
                value = json.loads(row.value_json)
                if (type(value) is dict and value.get('schema_version') == 1
                        and value.get('identity') == _identity(account)):
                    saved = value
            legacy = db.get(Setting, 'multi_publish.settings.v2')
            inherited = json.loads(legacy.value_json).get('fields_by_platform', {}).get(account.platform, {}) if legacy else {}
        if saved is not None:
            fields = validate_fields(account.platform, saved['fields'])
            douyin = copy.deepcopy(saved.get('douyin', {'declaration_id': None, 'location': None}))
            source = 'account'
        else:
            fields = validate_fields(account.platform, {key: value for key, value in inherited.items() if key in FIELD_KEYS[account.platform]})
            douyin = {'declaration_id': None, 'location': None}
            source = 'platform' if fields else 'default'
        return {'account_id': account.id, 'platform': account.platform, 'fields': fields, 'douyin': douyin,
            'has_saved_settings': saved is not None, 'source': source, 'schema': self._schema(account.platform)}

    def _declaration(self, account, selected, video_ids, *, cache=False):
        if selected is None:
            return None
        if type(selected) is not str or not 1 <= len(selected) <= 128:
            raise ValueError('invalid saved declaration choice')
        options = self.declarations.query({'account_id': account.id, 'cache_only': True})
        if not options.get('available') or not options.get('token'):
            raise ValueError('saved account declaration needs verification')
        result = self.declarations.resolve(account.id, options['token'], video_ids, selected, {})
        if cache:
            # Cache the complete current provider list, not only this account's
            # selection: native ordinary metadata is shared by platform.
            from .publish_metadata_cache import PublishMetadataCache
            PublishMetadataCache(self.engine).write(account, 'declarations-ordinary', '',
                {'required': options['required'], 'items': options['items']})
        return result['items'][video_ids[0]]

    def save(self, account_id, fields, *, douyin=None):
        account = self._account(account_id)
        fields = validate_fields(account.platform, fields,semantic=True)
        value = self.read(account.id)['douyin']
        if douyin is not None:
            if (account.platform != 'douyin' or type(douyin) is not dict
                    or set(douyin) - {'declaration_id', 'location'}):
                raise ValueError('unsupported account metadata defaults')
            if 'declaration_id' in douyin:
                self._declaration(account, douyin['declaration_id'], ['account-default'])
                value['declaration_id'] = douyin['declaration_id']
            if 'location' in douyin:
                value['location'] = self.locations.durable_choice(account.id, douyin['location'])
        record = {'schema_version': 1, 'identity': _identity(account), 'fields': fields, 'douyin': value}
        with session_scope(self.engine) as db:
            current = db.get(Setting, PREFIX + account.id)
            raw = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
            if current: current.value_json = raw
            else: db.add(Setting(key=PREFIX + account.id, value_json=raw))
        return {**self.read(account.id), 'saved': True}

    def forget(self, account_id):
        with session_scope(self.engine) as db:
            row = db.get(Setting, PREFIX + account_id)
            if row: db.delete(row)

    def _location_ref(self, account, selected):
        if selected is None:
            return None
        if (type(selected) is not dict or set(selected) != {'id', 'name', 'address', 'query', 'scope'}
                or selected['scope'] not in {'local', 'national'}):
            raise ValueError('invalid saved account location')
        current = self.locations.search({'account_id': account.id, 'query': selected['query'],
            'scope': selected['scope'], 'cache_only': True})
        expected = {key: selected[key] for key in ('id', 'name', 'address')}
        if (not current.get('available') or not current.get('token')
                or sum(row == expected for row in current.get('items', [])) != 1):
            raise ValueError('saved account location needs verification')
        return {'token': current['token'], 'id': selected['id']}

    def resolve_fields(self, account_id, *, video_ids=None):
        account = self._account(account_id)
        saved = self.read(account.id)
        result = copy.deepcopy(saved['fields'])
        validate_fields(account.platform,result,semantic=True)
        if account.platform != 'douyin':
            return result
        video_ids = video_ids or ['account-default']
        declaration = self._declaration(account, saved['douyin']['declaration_id'], video_ids, cache=True)
        if declaration is not None:
            result['declaration'] = declaration
        location = self._location_ref(account, saved['douyin']['location'])
        if location is not None:
            result['location'] = self.locations.resolve(account.id, location)
        return result

    def ordinary_params(self, params):
        result = copy.deepcopy(params)
        if type(result.get('declarations',{})) is not dict or type(result.get('locations',{})) is not dict:
            raise ValueError('invalid account metadata overrides')
        for account_id in params['account_ids']:
            account = self._account(account_id)
            saved = self.read(account.id)['douyin']
            policy=result.get('declarations',{}).get(account_id)
            if account_id in result.get('declarations',{}):
                if type(policy) is not dict:raise ValueError('invalid declaration settings')
                if 'batch' not in policy:
                    # Keep the original token for per-video choices. Querying
                    # again would invalidate that provider's scoped snapshot.
                    policy['batch']=saved['declaration_id']
                    if policy['batch'] is not None:
                        self.declarations.resolve(account.id,policy.get('token'),params['video_ids'],
                            policy['batch'],policy.get('items',{}))
            elif saved['declaration_id'] is not None:
                options = self.declarations.query({'account_id': account.id, 'cache_only': True})
                self.declarations.resolve(account.id, options.get('token'), params['video_ids'], saved['declaration_id'], {})
                result.setdefault('declarations', {})[account.id] = {'token': options['token'],
                    'batch': saved['declaration_id'], 'items': {}}
            location=result.get('locations',{}).get(account_id)
            if account_id in result.get('locations',{}):
                if type(location) is not dict:raise ValueError('invalid location settings')
                if 'batch' not in location:location['batch']=self._location_ref(account,saved['location'])
            elif saved['location'] is not None:
                result.setdefault('locations', {})[account.id] = {'batch': self._location_ref(account, saved['location']), 'items': {}}
        return result
