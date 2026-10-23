import pytest
from yijianjifa.webui.douyin_topics import topic_names,compose_description


def test_topics_keep_order_and_unicode_and_do_not_change_body():
    body='同一批素材，换个顺序。'
    text,names=compose_description(body,'#混剪测试 #剪辑对比 #混剪测试')
    assert names==['混剪测试','剪辑对比']
    assert text==body+'\n#混剪测试 #剪辑对比'


@pytest.mark.parametrize('tags',['#','#甲#乙','##测试'])
def test_ambiguous_topics_fail_without_silently_dropping_content(tags):
    with pytest.raises(ValueError):topic_names(tags)


def test_description_limit_counts_topics_too():
    with pytest.raises(ValueError):compose_description('字'*998,'#测试')
    assert compose_description('','#测试')==('#测试',['测试'])
    assert compose_description('正文','')==('正文',[])
