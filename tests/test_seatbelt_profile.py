"""The macOS profile may read exactly one sysctl, and must (CI found Python 3.10-3.12 aborting without it)."""

import re

from pydeno import _sandbox


def test_the_only_sysctl_allowance_is_the_page_size() -> None:
    allows = re.findall(r"\(allow sysctl[^\n]*", _sandbox._SEATBELT_PROFILE)  # noqa: SLF001
    assert allows == ['(allow sysctl-read (sysctl-name "hw.pagesize_compat"))']
