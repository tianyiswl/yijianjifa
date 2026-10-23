"""Transient profile contention, before any browser or final action."""

class AccountBusy(ValueError):
    def __init__(self):super().__init__('account busy')


def wait_for_account(lock,cancel,on_wait):
    if lock.acquire(blocking=False):return True
    on_wait()
    while not cancel.wait(.1):
        if lock.acquire(blocking=False):return True
    return False
