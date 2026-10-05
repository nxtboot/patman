#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2023 Google LLC
#

"""u_boot_pylib test runner"""

import os
import sys

# Allow 'from u_boot_pylib import xxx' to work
our_path = os.path.dirname(os.path.realpath(__file__))
sys.path.append(os.path.join(our_path, '..'))

import argparse

from u_boot_pylib import cros_subprocess
from u_boot_pylib import test_util


def run_tests():
    parser = argparse.ArgumentParser(description='u_boot_pylib test runner')
    parser.add_argument('cmd', choices=['test'], help='Command to run')
    parser.add_argument('testname', nargs='?', default=None,
                        help='Specific test to run')
    parser.add_argument('-v', '--verbose', action='store_true',
                        help='Verbose output')
    args = parser.parse_args()

    from u_boot_pylib import test_claude
    from u_boot_pylib import test_command

    to_run = args.testname if args.testname not in [None, 'test'] else None
    result = test_util.run_test_suites(
        'u_boot_pylib', False, args.verbose, False,
        False, None, to_run, None,
        ['u_boot_pylib.terminal', 'u_boot_pylib.gitutil',
         cros_subprocess.TestSubprocess, test_claude.TestClaude,
         test_command.TestRunInteractive])

    sys.exit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    run_tests()
