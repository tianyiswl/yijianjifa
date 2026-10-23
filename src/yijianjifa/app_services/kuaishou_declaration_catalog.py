"""Fixed legacy author labels from the official anonymous creator bundle.

Verified 2026-10-09, index-ih4TlJ04.js SHA-256
31700ae668ae3ecb4d2204f108ac4b0bee2c4045b33a07b6947b02d422743b7a.
These semantic choices are not account-specific statementId values. The newer
required authorStatementList form is deliberately not inferred from this list.
Network and account access are never needed to return the local choices.
"""

_LEGACY = (
    ('ai_generated', '内容为AI生成', 1),
    ('fictional_performance', '演绎情节，仅供娱乐', 2),
    ('personal_opinion', '个人观点，仅供参考', 3),
)


def choices():
    return [{'choice': 'none', 'name': '不添加作者声明'},
            *({'choice': choice, 'name': name} for choice, name, _ in _LEGACY)]


def valid_choice(value):
    return type(value) is str and value in {'none', *(row[0] for row in _LEGACY)}


def selection(value='none'):
    """Freeze one supported semantic choice and its proved legacy mapping."""
    if not valid_choice(value):
        raise ValueError('kuaishou declaration choice not verified')
    if value == 'none':
        return {'choice': value, 'name': '', 'source': None}
    for choice, name, source in _LEGACY:
        if choice == value:
            return {'choice': choice, 'name': name, 'source': source}


def catalog():
    return {'version': 'kuaishou-legacy-author-declarations-20261009',
            'verified_at': '2026-10-09T00:00:00+00:00', 'source': 'builtin',
            'choices': choices(), 'supported_form': 'legacy',
            'account_statement_ids_verified': False}
