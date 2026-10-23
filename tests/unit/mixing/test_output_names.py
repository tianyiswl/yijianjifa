import pytest
from yijianjifa.features.mixing.output_names import output_filename, new_output_stamp


def test_time_names_are_stable_across_retry_and_distinct_across_batches():
    a={'output_name_stamp':'20260928_231200_123456'}
    b={'output_name_stamp':'20260928_231200_123457'}
    assert output_filename(a,0)=='20260928_231200_123456_001.mp4'
    assert output_filename(a,0)==output_filename(dict(a),0)
    assert output_filename(a,0)!=output_filename(b,0)
    assert output_filename(a,0)!=output_filename(a,1)
    assert output_filename({},0)=='001.mp4'
    assert output_filename({'output_name_stamp':new_output_stamp()},0).endswith('_001.mp4')
    with pytest.raises(ValueError):output_filename({'output_name_stamp':'../bad'},0)
