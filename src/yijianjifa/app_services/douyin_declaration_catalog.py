"""Built-in labels observed in the official creator form on 2026-09-28.

ui-v1 references identify client labels, not platform submission IDs. The
publishing adapter still selects and verifies the exact current form label.
"""
import hashlib

LABELS = (
    '内容由AI生成',
    '内容为个人观点或见解',
    '内容为转载信息',
    '内容含营销推广信息',
    '虚构演绎，仅供娱乐',
    '无需添加自主声明',
)


def catalog():
    return {
        'version': 'douyin-declarations-20260928',
        'verified_at': '2026-09-28T00:00:00+00:00',
        'required': False,
        'items': [{'id': 'ui-v1:' + hashlib.sha256(label.encode()).hexdigest(), 'label': label}
                  for label in LABELS],
    }
