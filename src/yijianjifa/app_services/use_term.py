"""Read-only compatibility status for the free desktop product.

Legacy annual receipts remain untouched. Neither the wall clock nor receipt
contents authorize local use; optional cloud voice keeps its own account and
metering checks.
"""


class UseTerm:
    KEY = 'product:annual-use:v1'

    def __init__(self, engine):
        self.engine = engine

    def allowed(self):
        return True

    def status(self, *, now=None):
        # Keep the old call shape for hosts which still request product.use_term.
        # There is no start date, expiry, receipt creation or clock heartbeat.
        return dict(mode='free', free=True, started_at=None, expires_at=None,
                    expired=False, valid=True)
