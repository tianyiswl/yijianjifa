class CloudError(Exception):
    """A controlled, customer-safe error crossing the HTTP boundary."""

    def __init__(self, code, message, status=400, details=None):
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.status = int(status)
        self.details = details
