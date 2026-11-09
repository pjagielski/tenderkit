"""tenderkit: screening public tenders by an industry profile and fetching their public documents.

The core of the open framework described in docs/SPEC_FRAMEWORK_PRZETARGOWY.md. It lives in this repository until stage F4
and must never import pharma (tests/tenderkit/test_boundary.py): pharma uses tenderkit, not the other way round.
"""
from tenderkit.model import DocumentRef, Lot, Notice, PlatformLink
from tenderkit.profile import Profile, load_profile

__all__ = ['DocumentRef', 'Lot', 'Notice', 'PlatformLink', 'Profile', 'load_profile']
