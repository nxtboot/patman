# SPDX-License-Identifier: GPL-2.0+
# Copyright 2026 Simon Glass <sjg@chromium.org>

"""Tests for the u_boot_pylib.command module"""

import contextlib
import os
import unittest

from u_boot_pylib import command


@contextlib.contextmanager
def _silence_fd1():
    """Redirect raw fd 1 to /dev/null so PTY echo does not leak"""
    saved = os.dup(1)
    devnull = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(devnull, 1)
        yield
    finally:
        os.dup2(saved, 1)
        os.close(saved)
        os.close(devnull)


class TestRunInteractive(unittest.TestCase):
    """Tests for command.run_interactive()"""

    def test_captures_stdout(self):
        """run_interactive() returns text written to stdout"""
        with _silence_fd1():
            out = command.run_interactive(['printf', 'hello'])
        self.assertIn('hello', out)

    def test_captures_stderr(self):
        """run_interactive() also captures stderr through the PTY"""
        with _silence_fd1():
            out = command.run_interactive(
                ['sh', '-c', 'printf out; printf err >&2'])
        self.assertIn('out', out)
        self.assertIn('err', out)

    def test_silent_failing_command(self):
        """run_interactive() returns empty for a silent failing command"""
        with _silence_fd1():
            out = command.run_interactive(['false'])
        self.assertEqual('', out)

    def test_cwd(self):
        """run_interactive() honours the cwd argument"""
        with _silence_fd1():
            out = command.run_interactive(['pwd'], cwd='/tmp')
        self.assertIn('/tmp', out)


if __name__ == '__main__':
    unittest.main()
