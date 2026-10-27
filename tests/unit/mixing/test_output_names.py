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
    assert output_filename({'output_name_stamp':new_output_stamp()},0).endswith('_01.mp4')
    with pytest.raises(ValueError):output_filename({'output_name_stamp':'../bad'},0)


def test_new_compact_names_keep_retry_paths_and_sequence_without_renaming_legacy():
    snapshot = {'output_name_stamp': '1025_153026'}
    assert output_filename(snapshot, 0) == '1025_153026_01.mp4'
    assert output_filename(dict(snapshot), 0) == output_filename(snapshot, 0)
    assert output_filename(snapshot, 99) == '1025_153026_100.mp4'
    assert len(output_filename(snapshot, 0).removesuffix('.mp4')) <= 14
    assert output_filename({'output_name_stamp': '20261021_150620_205031'}, 0) == '20261021_150620_205031_001.mp4'
