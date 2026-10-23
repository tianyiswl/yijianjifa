"""Pure, bounded local facts for a failed creator-page step.

Never returns page contents, identities, addresses, exception messages or URLs.
The caller owns any browser observation and local persistence independently.
"""
import re


_LOCAL_FAILURES = {
    'existing creator draft must be handled first': 'existing_draft',
    'ambiguous publish mode': 'ambiguous_publish_mode',
    'final authority unavailable': 'final_authority_unavailable',
    'frozen video changed': 'frozen_video_changed',
}


def classify_exception(exc):
    """Return the bounded class name and a finite reason, never raw text."""
    cls = type(exc)
    name = cls.__name__
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,79}', name):
        name = 'Exception'
    reason = 'unknown'
    message = exc.args[0] if isinstance(exc, BaseException) and exc.args and type(exc.args[0]) is str else ''
    if cls is ValueError:
        reason = _LOCAL_FAILURES.get(message, 'unknown')
    elif isinstance(exc, TimeoutError):
        reason = 'timeout'
    else:
        # Avoid importing/starting an optional browser runtime. Playwright's
        # actual Error and TimeoutError carry their message as their first arg.
        playwright = [base for base in cls.__mro__
                      if base.__module__.startswith('playwright.') and base.__name__ in {'Error', 'TimeoutError'}]
        if any(base.__name__ == 'TimeoutError' for base in playwright):
            reason = 'timeout'
        elif playwright:
            lower = message.lower()
            if any(marker in lower for marker in ('execution context was destroyed', 'cannot find context with specified id')):
                reason = 'execution_context_destroyed'
            elif any(marker in lower for marker in ('navigation interrupted by another one',
                     'navigation failed because page was closed', 'frame was detached', 'net::err_aborted')):
                reason = 'navigation_interrupted'
    return {'exception_type': name, 'reason': reason}


def _text_match(actual, expected, *, compact=False, allow_empty=True):
    if type(actual) is not str or type(expected) is not str:
        return False
    if not allow_empty and not expected.strip():
        return False
    return ' '.join(actual.split()) == ' '.join(expected.split()) if compact else actual == expected


def _tags_match(actual, expected):
    return (type(actual) is list and type(expected) is list
            and all(type(tag) is str and bool(tag) for tag in actual + expected)
            and actual == expected)


def _count(value):
    return value if type(value) is int and 0 <= value <= 100 else 0


def formfacts(raw, expected):
    """Keep only match/ready flags and 0..100 counts from an existing read."""
    raw = raw if type(raw) is dict else {}
    expected = expected if type(expected) is dict else {}
    return {
        'title_match': _text_match(raw.get('caption'), expected.get('caption'), compact=True, allow_empty=False),
        'tags_match': _tags_match(raw.get('tags'), expected.get('tags')),
        'time_match': _text_match(raw.get('time_text'), expected.get('time_text')),
        'mode_match': _text_match(raw.get('mode'), expected.get('mode'), allow_empty=False),
        'upload_complete': raw.get('upload_complete') is True,
        'editor_count': _count(raw.get('editor_count')),
        'button_count': _count(raw.get('button_count')),
        'button_enabled': raw.get('button_enabled') is True,
    }
