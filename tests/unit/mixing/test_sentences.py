import pytest


@pytest.mark.parametrize(
    "text, expected",
    [
        ("体积小，携带方便。放进背包就能出门！", ["体积小，携带方便。", "放进背包就能出门！"]),
        ("真的吗？可以。", ["真的吗？", "可以。"]),
        ("第一句,还有停顿。Second sentence! Tail", ["第一句,还有停顿。", "Second sentence!", "Tail"]),
        ("换行\n不应切句，直到这里。下一句？", ["换行\n不应切句，直到这里。", "下一句？"]),
    ],
)
def test_sentence_boundaries(text, expected):
    from yijianjifa.features.mixing.sentences import split_spoken_sentences

    assert split_spoken_sentences(text) == expected
