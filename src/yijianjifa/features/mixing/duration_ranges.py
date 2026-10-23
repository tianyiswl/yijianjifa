"""Duration choices preserve total length and are reproducible for a frozen seed."""
import random


def validate_range(value):
    if value is None:
        return
    if type(value) is not list or len(value) != 2 or any(type(n) is not int or n < 1 for n in value) or value[0] > value[1]:
        raise ValueError('时长范围必须为正数，且最小值不能大于最大值')


def segment_durations(total_ms, fixed_ms, duration_range, *, seed=0):
    validate_range(duration_range)
    if type(total_ms) is not int or type(fixed_ms) is not int or total_ms < 1 or fixed_ms < 1:
        raise ValueError('时长必须为正整数毫秒')
    rng = random.Random(seed)
    remaining, parts = total_ms, []
    while remaining:
        chosen = rng.randint(*duration_range) if duration_range is not None else fixed_ms
        length = min(chosen, remaining)
        parts.append(length)
        remaining -= length
    return parts
