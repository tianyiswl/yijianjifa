import pytest
from yijianjifa.features.mixing.desktop_audio import write_subtitles
from yijianjifa.features.mixing.tts.base import SentenceSpan


def test_independent_styles_wrap_and_escape_without_changing_sentence_times(tmp_path):
    options = {'full_enabled': True,
               'spoken_style': {'font': 'sans', 'size': 34, 'color': '#123456', 'position': 'top', 'chars': 4},
               'full_style': {'font': 'serif', 'size': 42, 'color': '#ff8800', 'position': 'middle', 'chars': 5}}
    path = write_subtitles(tmp_path/'out.ass', {'title': '不应补入', 'full': '一二三四五六七'}, options, 2340,
                           [SentenceSpan('甲乙丙丁戊己', 120, 2100)])
    ass = path.read_text(encoding='utf-8')
    spoken = next(s for s in ass.splitlines() if s.startswith('Style: Spoken')).split(',')
    full = next(s for s in ass.splitlines() if s.startswith('Style: Full')).split(',')
    assert spoken[2:4] == ['34', '&H00563412']
    assert spoken[18] == '8'
    assert full[2:4] == ['42', '&H000088FF']
    assert full[18] == '5'
    assert r'0:00:00.12,0:00:01.11,Spoken,,0,0,0,,{\q2}甲乙丙' in ass
    assert r'0:00:01.11,0:00:02.10,Spoken,,0,0,0,,{\q2}丁戊己' in ass
    assert '0:00:02.34,Full,,0,0,0,,一二三四五\\N六七' in ass


def test_style_canvas_matches_output_and_text_cannot_inject_ass(tmp_path):
    path = write_subtitles(tmp_path/'out.ass', {'title':'', 'full':r'{\pos(1,1)}'},
                           {'full_enabled':True}, 1000, [], aspect_ratio='16:9', resolution='720p')
    ass = path.read_text(encoding='utf-8')
    assert 'PlayResX: 1920\nPlayResY: 1080' in ass
    assert r'{\pos' not in ass
    assert '｛＼pos(1,1)｝' in ass


@pytest.mark.parametrize('style', [{'size':True}, {'size':201}, {'chars':0}, {'color':'red'},
                                  {'font':'Arial,1'}, {'position':'left'}, {'unknown':1}])
def test_invalid_styles_rejected_even_without_events(tmp_path, style):
    with pytest.raises(ValueError):
        write_subtitles(tmp_path/'out.ass', {'title':'', 'full':''}, {'spoken_style':style}, 1000, [])
    assert not (tmp_path/'out.ass').exists()

@pytest.mark.parametrize('size', [73, 100, 200])
def test_large_subtitle_sizes_reach_renderer(size):
    from yijianjifa.features.mixing.subtitle_styles import ass_style
    assert ass_style('Full', {'size':size}).split(',')[2] == str(size)

def test_spoken_long_sentence_is_sequential_single_lines(tmp_path):
    text='同一个地方，在不同的光线下，看起来可能很不一样。'
    path=write_subtitles(tmp_path/'single.ass',{'title':'','full':''},
        {'spoken_style':{'size':75,'chars':30}},4000,[SentenceSpan(text,100,3900)])
    events=[s for s in path.read_text(encoding='utf-8').splitlines() if s.startswith('Dialogue:')]
    assert len(events)>1
    parts=[e.split(',,',2)[-1].removeprefix('0,0,0,,') for e in events]
    assert all(r'\N' not in e for e in events)
    assert all(r'{\q2}' in e for e in events)
    assert events[0].startswith('Dialogue: 0,0:00:00.10,')
    assert ',0:00:03.90,Spoken' in events[-1]
    assert ''.join(e.split(r'{\q2}')[1] for e in events)==text.replace('，','').replace('。','')

@pytest.mark.parametrize('ratio', ['9:16','16:9','1:1'])
def test_resolution_does_not_change_subtitle_layout(tmp_path, ratio):
    texts=[]
    for resolution in ('720p','1080p'):
        path=write_subtitles(tmp_path/f'{resolution}.ass',{'title':'','full':'测试'},
            {'full_enabled':True},3000,[SentenceSpan('同一个地方，在不同的光线下，看起来可能很不一样。',0,3000)],
            aspect_ratio=ratio,resolution=resolution)
        texts.append(path.read_text(encoding='utf-8'))
    assert texts[0]==texts[1]
    assert ',100,' in next(s for s in texts[0].splitlines() if s.startswith('Style: Full'))

@pytest.mark.parametrize('value', [-1,101,True,float('nan')])
def test_invalid_manual_height_rejected(value):
    from yijianjifa.features.mixing.subtitle_styles import validate_style
    with pytest.raises(ValueError):validate_style({'y_percent':value})


def test_independent_manual_heights_in_ass(tmp_path):
    p=write_subtitles(tmp_path/'height.ass',{'title':'','full':'标题'},
        {'full_enabled':True,'spoken_style':{'y_percent':72.5},'full_style':{'y_percent':23}},1000,
        [SentenceSpan('口播',0,1000)])
    text=p.read_text(encoding='utf-8')
    assert r'{\an5\pos(540,1392)}' in text
    assert r'{\an5\pos(540,441.6)}' in text


def test_spoken_punctuation_chunks_preserve_text_and_fit():
    from yijianjifa.features.mixing.sentences import split_subtitle_chunks
    text = '一会儿；重复的画面，就适合换一个角度再观察。'
    chunks = split_subtitle_chunks(text, 12)
    assert chunks[:2] == ['一会儿；', '重复的画面，']
    assert ''.join(chunks) == text
    assert all(0 < len(c) <= 12 for c in chunks)
    assert split_subtitle_chunks('一二三四五六七八九十一二三', 12) == ['一二三四五六七', '八九十一二三']


def test_spoken_hides_punctuation_without_changing_timing_or_full_text(tmp_path):
    span = SentenceSpan('你好，世界！', 0, 1200)
    path = write_subtitles(tmp_path/'punctuation.ass', {'title':'','full':'你好，世界！'},
                           {'full_enabled':True}, 1200, [span])
    events = [s for s in path.read_text(encoding='utf-8').splitlines() if s.startswith('Dialogue:')]
    spoken = [s for s in events if ',Spoken,' in s]
    assert [s.split(r'{\q2}')[1] for s in spoken] == ['你好','世界']
    assert '0:00:00.00,0:00:00.60' in spoken[0]
    assert '0:00:00.60,0:00:01.20' in spoken[1]
    assert events[-1].endswith('你好，世界！')
    assert span.text == '你好，世界！'
    assert write_subtitles(tmp_path/'empty.ass', {'title':'','full':''}, {}, 1000,
                           [SentenceSpan('，。！？',0,1000)]) is None
