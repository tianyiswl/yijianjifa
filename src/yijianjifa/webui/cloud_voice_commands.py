"""Native-only cloud voice commands, including private recovery file dialogs."""
from pathlib import Path


def preview_words(engine, params, mode):
    """A draft upper bound: random copy selection and cache hits are not frozen yet."""
    if (set(params) != {'voice', 'quantity', 'copy_categories', 'use_copy'}
            or type(params['voice']) is not str or len(params['voice']) > 256
            or type(params['quantity']) is not int or not 1 <= params['quantity'] <= 10000
            or type(params['use_copy']) is not bool or type(params['copy_categories']) is not list
            or len(params['copy_categories']) > 100
            or any(type(value) is not str or not 1 <= len(value) <= 64 for value in params['copy_categories'])):
        raise ValueError('配音预估设置无效。')
    source = mode if params['voice'].startswith('doubao:') and params['use_copy'] else 'system'
    if source != 'hosted':
        return {'source': source, 'words_upper_bound': 0}
    from yijianjifa.features.library.copy_service import CopyRepository
    from yijianjifa.features.mixing.sentences import split_spoken_sentences
    repository = CopyRepository(engine)
    categories = set(params['copy_categories'])
    pool = [copy for copy in repository.list_all() if not categories or repository.category_ids(copy.id) & categories]
    largest = max((sum(map(len, split_spoken_sentences(copy.voice))) for copy in pool), default=0)
    return {'source': source, 'words_upper_bound': largest * params['quantity']}


def dispatch_cloud_voice(cloud, api, operation, params, window, dialogs):
    if operation == 'voice.cloud.preview':
        return preview_words(api._engine, params, cloud.mode)
    schemas = {
        'read': set(), 'policy.seen': set(), 'register': {'username', 'password'}, 'login': {'username', 'password'},
        'logout': set(), 'redeem': {'code'}, 'usage': {'page'}, 'mode.save': {'mode'},
        'password.change': {'old', 'new'}, 'logout_all': {'password'},
        'estimate': {'id'},
    }
    name = operation.removeprefix('voice.cloud.')
    if name not in schemas or set(params) != schemas[name]:
        raise ValueError('配音请求无效。')
    if name == 'usage':
        if type(params['page']) is not int or not 0 <= params['page'] <= 10000:
            raise ValueError('用量页码无效。')
    elif any(type(value) is not str or not 1 <= len(value) <= 4096 for value in params.values()):
        raise ValueError('请填写有效的配音信息。')
    if name == 'policy.seen':
        return cloud.policy_seen()
    if name == 'read':
        return cloud.read()
    if name in {'login', 'register'}:
        return getattr(cloud, name)(params['username'], params['password'])
    if name == 'logout':
        return cloud.logout()
    if name == 'redeem':
        if len(params['code']) > 128:
            raise ValueError('兑换码格式不正确。')
        return cloud.redeem(params['code'])
    if name == 'usage':
        return cloud.usage(params['page'])
    if name == 'mode.save':
        if params['mode'] != 'hosted':
            raise ValueError('配音来源无效。')
        return cloud.set_mode(params['mode'])
    if name == 'password.change':
        return cloud.change_password(params['old'], params['new'])
    if name == 'logout_all':
        return cloud.logout_all(params['password'])
    task = api._mix.queue.get(params['id'])
    return api._mix.tts.estimate_batch(task.snapshot)
