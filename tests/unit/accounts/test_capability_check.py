from yijianjifa.features.accounts.models import CapabilitySnapshot
from yijianjifa.features.accounts.service import AccountCapabilityChecker


class FakeAdapter:
    def __init__(self):
        self.upload_count = 0
        self.received = None

    def check_capabilities(self, *, allow_upload_probe=False):
        self.received = allow_upload_probe
        if allow_upload_probe:
            self.upload_count += 1
        return CapabilitySnapshot(ordinary="available", commerce="pending", location_scope="pending")


def test_capability_check_does_not_upload_without_explicit_probe_permission():
    adapter = FakeAdapter()
    result = AccountCapabilityChecker().check(adapter)
    assert adapter.received is False
    assert adapter.upload_count == 0
    assert result.commerce == "pending"


def test_explicit_probe_permission_is_forwarded_but_never_invented():
    adapter = FakeAdapter()
    AccountCapabilityChecker().check(adapter, allow_upload_probe=True)
    assert adapter.received is True
    assert adapter.upload_count == 1
