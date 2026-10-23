"""Allowed V1 music and simple-transition parameters. No paths from UI."""
import math

TRANSITIONS = ('fade', 'slideleft', 'slideright')


def music_spec(value):
    if value is None:
        return {'mode': 'none'}
    if type(value) is not dict or set(value) - {'mode', 'music_id', 'category_ids'}:
        raise ValueError('背景音乐设置无效')
    mode = value.get('mode')
    if mode not in ('none', 'fixed', 'random'):
        raise ValueError('背景音乐模式无效')
    music_id = value.get('music_id')
    categories = value.get('category_ids', [])
    if type(categories) is not list or len(categories) > 100 or any(type(x) is not str or not 1 <= len(x) <= 64 for x in categories):
        raise ValueError('音乐分类无效')
    if mode == 'fixed':
        if type(music_id) is not str or not 1 <= len(music_id) <= 64 or categories:
            raise ValueError('请选择一首音乐')
    elif music_id is not None or (mode == 'none' and categories):
        raise ValueError('背景音乐范围无效')
    return {'mode': mode, 'music_id': music_id, 'category_ids': categories}


def transition_spec(value):
    if value is None:
        return {'mode': 'none'}
    if type(value) is not dict or set(value) - {'mode', 'name', 'duration_ms'}:
        raise ValueError('转场设置无效')
    mode = value.get('mode')
    name = value.get('name')
    duration = value.get('duration_ms', 500)
    if mode not in ('none', 'fixed', 'random') or type(duration) is not int or not 0 <= duration <= 60000:
        raise ValueError('转场模式或时长无效')
    if mode == 'fixed' and name not in TRANSITIONS:
        raise ValueError('请选择支持的简单转场')
    if mode != 'fixed' and name is not None:
        raise ValueError('仅指定转场可设置效果名称')
    return {'mode': mode, 'name': name, 'duration_ms': duration}


def validate_music_gain(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 2:
        raise ValueError('音乐音量无效')
