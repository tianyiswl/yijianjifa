import pytest
from yijianjifa.features.mixing.duration_ranges import segment_durations, validate_range


def test_range_is_seeded_and_last_segment_fills_exact_total():
    parts=segment_durations(2375,1000,[400,700],seed=29)
    assert parts==segment_durations(2375,1000,[400,700],seed=29)
    assert sum(parts)==2375
    assert all(400<=d<=700 for d in parts[:-1])
    assert 0<parts[-1]<=700
    assert len(set(parts[:-1]))>1


def test_equal_range_and_short_remainder_are_valid():
    assert segment_durations(2500,999,[1000,1000],seed=1)==[1000,1000,500]
    assert segment_durations(100,999,[400,700],seed=1)==[100]
    assert segment_durations(2500,1000,None,seed=1)==[1000,1000,500]


@pytest.mark.parametrize('value',[[2000,1000],[0,100],[-1,100],[True,100],[1.5,100],[1],[1,2,3],{'min':1,'max':2}])
def test_invalid_range_rejected(value):
    with pytest.raises(ValueError):validate_range(value)
