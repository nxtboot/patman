# SPDX-License-Identifier: GPL-2.0+

"""Patman patch manager

patman bundles a copy of U-Boot's u_boot_pylib library in _vendor/. Make it
importable by its usual name, ahead of any other copies, e.g. in a U-Boot tree.
It is kept out of the top level of site-packages, where it would clash with
U-Boot's own copy and with other packages which provide it.
"""

import os
import sys

_VENDOR_DIR = os.path.join(os.path.dirname(os.path.realpath(__file__)),
                           '_vendor')
if _VENDOR_DIR not in sys.path:
    sys.path.insert(0, _VENDOR_DIR)

__all__ = [
    'checkpatch', 'cmdline', 'commit', 'control', 'cser_helper', 'cseries',
    'database', 'func_test', 'get_maintainer', '__main__', 'patchstream',
    'patchwork', 'project', 'send', 'series', 'settings', 'status',
    'test_checkpatch', 'test_common', 'test_cseries', 'test_settings'
]
