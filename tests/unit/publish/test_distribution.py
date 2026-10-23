from yijianjifa.features.publish.distribution import distribute


def test_split_across_accounts_is_default_even_distribution():
    result = distribute(["V1", "V2", "V3", "V4", "V5"], ["A1", "A2"], mode="split_across_accounts")
    assert [(x.video_id, x.account_id) for x in result] == [
        ("V1", "A1"), ("V2", "A2"), ("V3", "A1"), ("V4", "A2"), ("V5", "A1")
    ]


def test_whole_batch_assigns_every_video_to_every_account():
    result = distribute(["V1", "V2"], ["A1", "A2"], mode="whole_batch")
    assert {(x.video_id, x.account_id) for x in result} == {
        ("V1", "A1"), ("V2", "A1"), ("V1", "A2"), ("V2", "A2")
    }
