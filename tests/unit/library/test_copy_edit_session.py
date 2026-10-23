from yijianjifa.features.library.edit_session import CopyEditSession, SwitchDecision


def test_switching_dirty_copy_requires_three_way_decision(copy_repo):
    copy_repo.create(id="W001", title="A", voice="", body="B", tags="", full="")
    copy_repo.create(id="W002", title="C", voice="", body="D", tags="", full="")
    session = CopyEditSession(copy_repo)
    session.open("W001")
    session.update_field("title", "changed")

    prompt = session.request_open("W002")

    assert prompt is not None
    assert prompt.choices == {
        SwitchDecision.SAVE_AND_CONTINUE,
        SwitchDecision.DISCARD,
        SwitchDecision.KEEP_EDITING,
    }
    assert session.current.id == "W001"


def test_save_and_continue_persists_then_switches(copy_repo):
    copy_repo.create(id="W001", title="A", voice="", body="B", tags="", full="")
    copy_repo.create(id="W002", title="C", voice="", body="D", tags="", full="")
    session = CopyEditSession(copy_repo)
    session.open("W001")
    session.update_field("title", "changed")
    session.request_open("W002")

    session.resolve_switch(SwitchDecision.SAVE_AND_CONTINUE)

    assert copy_repo.get("W001").title == "changed"
    assert session.current.id == "W002"
    assert not session.dirty


def test_keep_editing_does_not_switch_or_save(copy_repo):
    copy_repo.create(id="W001", title="A", voice="", body="B", tags="", full="")
    copy_repo.create(id="W002", title="C", voice="", body="D", tags="", full="")
    session = CopyEditSession(copy_repo)
    session.open("W001")
    session.update_field("title", "changed")
    session.request_open("W002")

    session.resolve_switch(SwitchDecision.KEEP_EDITING)

    assert copy_repo.get("W001").title == "A"
    assert session.current.id == "W001"
    assert session.dirty
