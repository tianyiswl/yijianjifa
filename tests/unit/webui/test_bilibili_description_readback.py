"""Quill paragraphs are source lines, not browser paragraph spacing."""
import pytest

from yijianjifa.webui import bilibili_publish_form as form
from test_bilibili_preparation_observation import offline_browser, offline_page
from test_bilibili_publish_form import payload, raw, request


@pytest.mark.parametrize('html,expected', [
    ('<p>单行简介</p>', '单行简介'),
    ('<p>第一行</p><p>第二行</p>', '第一行\n第二行'),
    ('<p>第一段</p><p><br></p><p>第二段</p>', '第一段\n\n第二段'),
    ('<p>前</p><p><br></p><p><br></p><p>后</p>', '前\n\n\n后'),
    ('<p>原文末尾换行</p><p><br></p>', '原文末尾换行\n'),
    ('<p>原文末尾空行</p><p><br></p><p><br></p>', '原文末尾空行\n\n'),
    ('<p><br></p><p>原文前有换行</p>', '\n原文前有换行'),
    ('<p><br></p>', ''),
    ('<p>保留<span>嵌套<strong>文字</strong></span>和<a href="https://example.invalid/">链接</a></p>'
     '<p><span>下一行</span></p>', '保留嵌套文字和链接\n下一行'),
    ('<p>  原文空格  </p><p>\t制表符</p>', '  原文空格  \n\t制表符'),
    ('<p>段内<br>换行</p><p>下一段</p>', '段内\n换行\n下一段'),
    ('<div>旧结构第一行</div><div>旧结构第二行</div>', '旧结构第一行\n旧结构第二行'),
    ('<span>旧结构内联文字</span>', '旧结构内联文字'),
    ('纯文本兼容', '纯文本兼容'),
])
def test_read_form_preserves_exact_description_lines(offline_page, html, expected):
    offline_page.set_content('<style>.ql-editor{white-space:pre-wrap}.ql-editor p{margin:0;padding:0}</style>'
        '<div class="desc-container"><div class="archive-info-editor">'
        '<div class="ql-editor" contenteditable="true">' + html + '</div></div></div>')
    observed = offline_page.evaluate(form.READ_FORM)
    assert observed['body_count'] == 1
    assert observed['body'] == expected


@pytest.mark.parametrize('changed', [
    '首行\n末行\n', '首行\n\n\n末行\n', '首行\n\n末行', ' 首行\n\n末行\n',
])
def test_description_whitespace_differences_still_block_final_and_receipt(payload, changed):
    payload['fields']['body'] = '首行\n\n末行\n'
    expected = form.validate_form(payload)
    observed = raw(payload)
    submitted = request(payload, observed)
    assert form.verify_form(observed, expected)
    assert form.matches_submit(submitted, expected, observed)
    observed['body'] = changed
    assert not form.verify_form(observed, expected)
    observed['body'] = '首行\n\n末行\n'
    submitted['desc'] = changed
    assert not form.matches_submit(submitted, expected, observed)
