"""Finite style options shared by the service and ASS writer."""
import math
import re
import sys


def validate_style(style):
    if type(style) is not dict or set(style) - {'font', 'size', 'color', 'position', 'chars', 'y_percent'}:
        raise ValueError('字幕样式无效')
    for key, low, high in (('size', 16, 200), ('chars', 4, 30)):
        if key in style and (type(style[key]) is not int or not low <= style[key] <= high):
            raise ValueError('字幕字号或每行字数无效')
    if 'y_percent' in style and (type(style['y_percent']) not in (int,float) or not math.isfinite(style['y_percent']) or not 0 <= style['y_percent'] <= 100):
        raise ValueError('字幕高度必须为0到100')
    if 'font' in style and style['font'] not in ('sans', 'serif'):
        raise ValueError('字幕字体无效')
    if 'position' in style and style['position'] not in ('top', 'middle', 'bottom'):
        raise ValueError('字幕位置无效')
    if 'color' in style and (type(style['color']) is not str or not re.fullmatch(r'#[0-9a-fA-F]{6}', style['color'])):
        raise ValueError('字幕颜色无效')


def ass_style(name, style):
    validate_style(style)
    fonts = {'sans': 'PingFang SC', 'serif': 'Songti SC'} if sys.platform == 'darwin' else {'sans': 'Microsoft YaHei', 'serif': 'SimSun'}
    font = fonts[style['font']] if 'font' in style else 'Arial'
    color = style.get('color', '#ffffff')[1:]
    color = '&H00' + (color[4:6] + color[2:4] + color[:2]).upper()
    alignment = {'top': 8, 'middle': 5, 'bottom': 2}[style.get('position', 'bottom' if name == 'Spoken' else 'top')]
    return f'Style: {name},{font},{style.get("size", 100 if name == "Full" else 75)},{color},&H00FFFFFF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,2,0,{alignment},60,60,150,1'


def ass_text(text, style):
    # Escape before adding our own ASS line separators. User overrides stay literal.
    text = str(text).replace('\\', '＼').replace('{', '｛').replace('}', '｝').replace('\r\n', '\n').replace('\r', '\n')
    lines = text.split('\n')
    width = style.get('chars')
    if width:
        lines = [part for line in lines for part in ([line[i:i+width] for i in range(0, len(line), width)] or [''])]
    return r'\N'.join(lines)


def ass_position(style, width, height):
    if 'y_percent' not in style:
        return ''
    return r'{\an5\pos(' + f"{width / 2:g},{height * style['y_percent'] / 100:g}" + ')}'
