import random


def test_mixing_progress_does_not_advance_finished_metadata_progress(progress):
    progress.mark_used("mixing", "W001")
    assert progress.is_used("mixing", "W001")
    assert not progress.is_used("finished_metadata", "W001")


def test_sequential_next_copy_continues_until_cycle_exhausted(progress):
    ids = ["W001", "W002", "W003"]
    assert progress.next_copy("mixing", ids, "sequential") == "W001"
    progress.mark_used("mixing", "W001")
    assert progress.next_copy("mixing", ids, "sequential") == "W002"
    progress.mark_used("mixing", "W002")
    progress.mark_used("mixing", "W003")
    assert progress.next_copy("mixing", ids, "sequential") == "W001"


def test_random_mode_chooses_unused_before_looping(progress):
    ids = ["W001", "W002", "W003"]
    progress.mark_used("mixing", "W001")
    chosen = progress.next_copy("mixing", ids, "random", rng=random.Random(1))
    assert chosen in {"W002", "W003"}


def test_reset_cycle_makes_previously_used_copy_available(progress):
    progress.mark_used("finished_metadata", "W001")
    assert progress.is_used("finished_metadata", "W001")
    progress.reset_cycle("finished_metadata")
    assert not progress.is_used("finished_metadata", "W001")
