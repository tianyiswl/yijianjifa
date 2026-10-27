"""发布读取诊断仅保存固定枚举和计数，不保存平台正文或异常原文。"""
from __future__ import annotations

import re


_ERROR_KINDS = frozenset({
    'http_error', 'fetch_error', 'json_error', 'body_read_error', 'timeout',
    'page_context_unavailable', 'browser_unavailable', 'javascript_error', 'unknown',
})
_REQUESTS = {'pc_user_info_before': 1, 'profile_user_info': 2, 'pc_user_info_after': 3}
_REQUIRED = frozenset({'error_kind', 'endpoint', 'request_index',
                       'read_operation', 'request_elapsed_ms'})
_ALLOWED = _REQUIRED | {'http_status', 'content_kind'}
_CODE_NAME = re.compile(r'[a-zA-Z_]{1,64}')


def identity_failure_details(value):
    """一次读取的诊断必须整体可信，不能只截取恶意或不完整对象的一部分。"""
    if type(value) is not dict or not all(type(key) is str for key in value):
        return None
    if not _REQUIRED <= value.keys() or not value.keys() <= _ALLOWED:
        return None
    if type(value['error_kind']) is not str or value['error_kind'] not in _ERROR_KINDS:
        return None
    endpoint = value['endpoint']
    if type(endpoint) is not str or endpoint not in _REQUESTS:
        return None
    if type(value['request_index']) is not int or value['request_index'] != _REQUESTS[endpoint]:
        return None
    if type(value['read_operation']) is not str or value['read_operation'] not in {'fetch', 'json'}:
        return None
    if type(value['request_elapsed_ms']) is not int or not 0 <= value['request_elapsed_ms'] <= 30000:
        return None
    if 'http_status' in value and (type(value['http_status']) is not int or not 100 <= value['http_status'] <= 599):
        return None
    if 'content_kind' in value and (type(value['content_kind']) is not str or value['content_kind'] not in {'json', 'html', 'other', 'missing'}):
        return None
    return dict(value)


def _validated_details(details):
    safe = identity_failure_details(details)
    if safe is None:
        raise ValueError('Invalid identity read diagnostic')
    return safe


class IdentityReadFailure(RuntimeError):
    """异常正文固定，详细证据另存为经过严格验证的独立副本。"""

    def __init__(self, details):
        self._details = _validated_details(details)
        super().__init__('Platform identity read failed')

    @property
    def details(self):
        return dict(self._details)


class IdentityReadTimeout(TimeoutError):
    """读取超时保留原有超时语义，不把其他接口错误改标为超时。"""

    def __init__(self, details):
        safe = _validated_details(details)
        if safe['error_kind'] != 'timeout':
            raise ValueError('Invalid identity timeout diagnostic')
        self._details = safe
        super().__init__('Platform identity read timed out')

    @property
    def details(self):
        return dict(self._details)


def _error_kind(error):
    if isinstance(error, TimeoutError):
        return 'timeout'
    kind = type(error)
    playwright_error = kind.__module__.startswith('playwright.')
    if playwright_error and kind.__name__ == 'TimeoutError':
        return 'timeout'
    # 原文只用于匹配固定特征，绝不作为返回值、格式化文本或异常参数保存。
    message = error.args[0] if isinstance(error, BaseException) and len(error.args) == 1 and type(error.args[0]) is str else ''
    if playwright_error:
        if any(marker in message for marker in (
            'Execution context was destroyed', 'Cannot find context with specified id',
            'Target page, context or browser has been closed',
        )):
            return 'page_context_unavailable'
        if any(marker in message for marker in (
            'Browser disconnected', 'Browser has been closed', 'Browser closed',
            'browser has disconnected', 'Browser connection closed',
        )):
            return 'browser_unavailable'
        if any(marker in message for marker in (
            'TypeError: Failed to fetch', 'TypeError: NetworkError when attempting to fetch resource',
            'TypeError: Load failed',
        )):
            return 'fetch_error'
        if any(marker in message for marker in ('SyntaxError:', 'ReferenceError:', 'TypeError:')):
            return 'javascript_error'
    if isinstance(error, TypeError) and message.startswith((
        'Failed to fetch', 'NetworkError when attempting to fetch resource', 'Load failed',
    )):
        return 'fetch_error'
    return 'unknown'


def _source_location(error):
    """只取最近的应用源码位置，不读取文件路径、参数、局部变量和第三方栈。"""
    location = {}
    traceback = getattr(error, '__traceback__', None)
    while traceback is not None:
        frame = traceback.tb_frame
        module = frame.f_globals.get('__name__')
        function = frame.f_code.co_name
        if type(module) is str and module.startswith('yijianjifa.'):
            name = module.rsplit('.', 1)[-1]
            if (_CODE_NAME.fullmatch(name) and _CODE_NAME.fullmatch(function)
                    and type(traceback.tb_lineno) is int and 1 <= traceback.tb_lineno <= 10000000):
                location = {'source_module': name, 'source_function': function,
                            'source_line': traceback.tb_lineno}
        traceback = traceback.tb_next
    return location


def error_details(error):
    """输出可进入现有执行事件白名单的诊断；诊断本身不能掩盖业务异常。"""
    try:
        if isinstance(error, (IdentityReadFailure, IdentityReadTimeout)):
            safe = identity_failure_details(error.details)
            details = safe if safe is not None else {'error_kind': 'unknown'}
        else:
            details = {'error_kind': _error_kind(error)}
        return {**details, **_source_location(error)}
    except Exception:
        return {'error_kind': 'unknown'}
