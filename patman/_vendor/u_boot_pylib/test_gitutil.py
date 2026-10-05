# SPDX-License-Identifier: GPL-2.0+
# Copyright 2026 Simon Glass <sjg@chromium.org>

"""Tests for the cwd-style helpers in u_boot_pylib.gitutil"""

import os
import shutil
import subprocess
import tempfile
import unittest

from u_boot_pylib import gitutil


def _git(*args, cwd):
    """Run git silently in cwd, raising on failure."""
    subprocess.run(['git', *args], cwd=cwd, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _commit(cwd, fname, body, msg):
    """Add a tracked file and commit it."""
    with open(os.path.join(cwd, fname), 'w') as f:
        f.write(body)
    _git('add', fname, cwd=cwd)
    _git('-c', 'user.email=t@e', '-c', 'user.name=T',
         'commit', '-m', msg, cwd=cwd)


class TestGitutilHelpers(unittest.TestCase):
    """Tests for the gitutil helpers introduced for review.py."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='gitutil-test-')
        _git('init', '-q', '-b', 'main', cwd=self.tmpdir)
        _commit(self.tmpdir, 'a', 'first', 'first')

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_count_revs_empty_range(self):
        """count_revs() returns 0 when the range contains no commits."""
        self.assertEqual(0, gitutil.count_revs(self.tmpdir, 'main..main'))

    def test_count_revs_with_commits(self):
        """count_revs() returns the number of commits in the range."""
        _git('checkout', '-q', '-b', 'work', cwd=self.tmpdir)
        _commit(self.tmpdir, 'b', 'second', 'second')
        _commit(self.tmpdir, 'c', 'third', 'third')
        self.assertEqual(2, gitutil.count_revs(self.tmpdir, 'main..work'))

    def test_count_revs_invalid_range(self):
        """count_revs() returns None when one side of the range is bad."""
        self.assertIsNone(
            gitutil.count_revs(self.tmpdir, 'no-such-ref..main'))

    def test_diff_stat(self):
        """diff_stat() reports a stat summary for a valid range."""
        _git('checkout', '-q', '-b', 'work', cwd=self.tmpdir)
        _commit(self.tmpdir, 'b', 'second', 'second')
        out = gitutil.diff_stat('main..work', self.tmpdir)
        self.assertIn('b', out)
        self.assertIn('insert', out)

    def test_diff_stat_invalid(self):
        """diff_stat() returns '' when the range is invalid."""
        self.assertEqual(
            '', gitutil.diff_stat('no-such-ref..main', self.tmpdir))

    def test_ref_exists(self):
        """ref_exists() resolves valid refs and rejects missing ones."""
        self.assertTrue(gitutil.ref_exists('main', self.tmpdir))
        self.assertFalse(gitutil.ref_exists('does-not-exist', self.tmpdir))

    def test_current_branch(self):
        """current_branch() returns the active branch name."""
        self.assertEqual('main', gitutil.current_branch(self.tmpdir))
        _git('checkout', '-q', '-b', 'feature', cwd=self.tmpdir)
        self.assertEqual('feature', gitutil.current_branch(self.tmpdir))

    def test_checkout_branch(self):
        """checkout_branch() swaps the active branch."""
        _git('branch', 'other', cwd=self.tmpdir)
        gitutil.checkout_branch('other', self.tmpdir)
        self.assertEqual('other', gitutil.current_branch(self.tmpdir))

    def test_stash_save_pop(self):
        """stash_save() saves a tracked-file change; stash_pop() restores it."""
        with open(os.path.join(self.tmpdir, 'a'), 'w') as f:
            f.write('changed')
        out = gitutil.stash_save(self.tmpdir)
        self.assertNotIn('No local changes', out)
        with open(os.path.join(self.tmpdir, 'a')) as f:
            self.assertEqual('first', f.read())
        gitutil.stash_pop(self.tmpdir)
        with open(os.path.join(self.tmpdir, 'a')) as f:
            self.assertEqual('changed', f.read())

    def test_stash_save_clean_tree(self):
        """stash_save() reports no changes when nothing is modified."""
        out = gitutil.stash_save(self.tmpdir)
        self.assertIn('No local changes', out)

    def test_stash_save_include_untracked(self):
        """stash_save(include_untracked=True) covers untracked files too."""
        path = os.path.join(self.tmpdir, 'extra')
        with open(path, 'w') as f:
            f.write('untracked')

        # Default: untracked files are not stashed.
        out = gitutil.stash_save(self.tmpdir)
        self.assertIn('No local changes', out)
        self.assertTrue(os.path.exists(path))

        # With include_untracked, the file is stashed away.
        out = gitutil.stash_save(self.tmpdir, include_untracked=True)
        self.assertNotIn('No local changes', out)
        self.assertFalse(os.path.exists(path))


if __name__ == '__main__':
    unittest.main()
