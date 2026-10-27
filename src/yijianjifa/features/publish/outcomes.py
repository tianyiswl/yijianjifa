"""Shared completion of a local delivery, separate from public visibility.

These states retain their exact platform meaning. In particular, acceptance,
processing and review never become a claim that the work is already public.
Receipt validation and permission to submit belong to the delivery boundary.
"""

COMPLETED_DELIVERY_STATES = frozenset({
    'published', 'platform_scheduled', 'platform_accepted', 'processing', 'reviewing',
})
