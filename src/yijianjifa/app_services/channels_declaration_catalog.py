"""Reviewed local choices from the official 2026-10-09 source freeze.

The AI name is visible in a WeChat Coral Safety announcement image reproduced
by Beijing Daily. That image is a mobile UI, not proof of the desktop menu.
The desktop helper must expose exactly this name before a normal control click;
its current numeric type and tagKey are read and frozen only in live memory.
"""

VERSION = 'channels-content-declarations-20261009'
EVIDENCE_STATUS = 'partial'
EVIDENCE_GAP = '完整桌面视频标注菜单尚未核实；AI 名称来自微信官方公告转载中的移动端配图，桌面必须同名精确匹配。'
AI_LABEL = '内容为 AI 技术制作，虚构内容请谨慎甄别'
HELP = '可选择不添加或 AI 内容声明；发布时会核对当前账号的实际选项。原创协议需在原页面亲自确认。'


def choices():
    """No account, browser, credential or platform read is needed."""
    return [{'choice': 'none', 'name': '不添加内容声明'}, {'choice': 'ai_generated', 'name': AI_LABEL}]


def valid_choice(value):
    return type(value) is str and value in {'none', 'ai_generated'}


def name_for(value):
    normalize_choice(value)
    return '' if value == 'none' else AI_LABEL


def normalize_choice(value):
    if not valid_choice(value):
        raise ValueError('channels content declaration choice not verified')
    return value


def catalog():
    return {'version': VERSION, 'source': 'builtin', 'evidence_status': EVIDENCE_STATUS,
            'evidence_gap': EVIDENCE_GAP, 'choices': choices()}
