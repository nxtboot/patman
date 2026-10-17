# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2025 Simon Glass <sjg@chromium.org>
#
"""Functional tests for checking that patman behaves correctly"""

import asyncio
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

import pygit2

from u_boot_pylib import gitutil
from u_boot_pylib import terminal
from u_boot_pylib import tools
from u_boot_pylib import tout

from patman import cmdline
from patman import control
from patman import cseries


def find_source_dir():
    """Find patman's source tree, which holds checkpatch.pl and its config

    This is the directory above the patman package, or the current directory,
    e.g. when a packager runs the tests from the sources after installing
    patman.

    Returns:
        str or None: Directory holding scripts/checkpatch.pl and
            .checkpatch.conf, or None if the sources are not there
    """
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for path in (here, os.getcwd()):
        if (os.path.isfile(os.path.join(path, 'scripts', 'checkpatch.pl')) and
                os.path.isfile(os.path.join(path, '.checkpatch.conf'))):
            return path
    return None


class TestCommon:
    """Contains common test functions"""
    leb = (b'Lord Edmund Blackadd\xc3\xabr <weasel@blackadder.org>'.
           decode('utf-8'))

    # Fake patchwork project ID for U-Boot
    PROJ_ID = 6
    PROJ_LINK_NAME = 'uboot'
    SERIES_ID_FIRST_V3 = 31
    SERIES_ID_SECOND_V1 = 456
    SERIES_ID_SECOND_V2 = 457
    TITLE_SECOND = 'Series for my board'

    verbosity = False
    preserve_outdirs = False

    @classmethod
    def setup_test_args(cls, preserve_indir=False, preserve_outdirs=False,
                        toolpath=None, verbosity=None, no_capture=False):
        """Accept arguments controlling test execution

        Args:
            preserve_indir (bool): not used by patman
            preserve_outdirs (bool): Preserve the output directories used by
                tests. Each test has its own, so this is normally only useful
                when running a single test.
            toolpath (str): not used by patman
            verbosity (int): verbosity to use (0 means tout.INIT, 1 means means
                tout.DEBUG)
            no_capture (bool): True to output all captured text after capturing
                completes
        """
        del preserve_indir
        cls.preserve_outdirs = preserve_outdirs
        cls.toolpath = toolpath
        cls.verbosity = verbosity
        cls.no_capture = no_capture

    def __init__(self):
        super().__init__()
        self.repo = None
        self.tmpdir = None
        self.gitdir = None

    def setUp(self):
        """Set up the test temporary dir and git dir"""
        self.tmpdir = tempfile.mkdtemp(prefix='patman.')
        self.gitdir = os.path.join(self.tmpdir, '.git')

        # Make sure the tests never touch the user's own database
        self._env = mock.patch.dict(os.environ)
        self._env.start()
        os.environ.pop('PATMAN_DB_DIR', None)
        tout.init(tout.DEBUG if self.verbosity else tout.INFO,
                  allow_colour=False)
        tout.stdout_is_tty = False

        # Run from the source tree, if it is here, so that checkpatch.pl is
        # the one kept with the sources and reads their .checkpatch.conf,
        # whichever directory the tests are started from
        self._old_cwd = os.getcwd()
        source = find_source_dir()
        if source:
            os.chdir(source)

    def tearDown(self):
        """Delete the temporary dir"""
        os.chdir(self._old_cwd)
        self._env.stop()
        if self.preserve_outdirs:
            print(f'Output dir: {self.tmpdir}')
        else:
            shutil.rmtree(self.tmpdir)
        terminal.set_print_test_mode(False)

    def make_commit_with_file(self, subject, body, fname, text):
        """Create a file and add it to the git repo with a new commit

        Args:
            subject (str): Subject for the commit
            body (str): Body text of the commit
            fname (str): Filename of file to create
            text (str): Text to put into the file
        """
        path = os.path.join(self.tmpdir, fname)
        tools.write_file(path, text, binary=False)
        index = self.repo.index
        index.add(fname)
        # pylint doesn't seem to find this
        # pylint: disable=E1101
        author = pygit2.Signature('Test user', 'test@email.com')
        committer = author
        tree = index.write_tree()
        message = subject + '\n' + body
        self.repo.create_commit('HEAD', author, committer, message, tree,
                                [self.repo.head.target])

    def make_git_tree(self):
        """Make a simple git tree suitable for testing

        It has four branches:
            'base' has two commits: PCI, main
            'first' has base as upstream and two more commits: I2C, SPI
            'second' has base as upstream and three more: video, serial, bootm
            'third4' has second as upstream and four more: usb, main, test, lib

        Returns:
            pygit2.Repository: repository
        """
        os.environ['GIT_CONFIG_GLOBAL'] = '/dev/null'
        os.environ['GIT_CONFIG_SYSTEM'] = '/dev/null'

        repo = pygit2.init_repository(self.gitdir)
        self.repo = repo
        new_tree = repo.TreeBuilder().write()

        common = ['git', f'--git-dir={self.gitdir}', 'config']
        tools.run(*(common + ['user.name', 'Dummy']), cwd=self.gitdir)
        tools.run(*(common + ['user.email', 'dumdum@dummy.com']),
                  cwd=self.gitdir)

        # pylint doesn't seem to find this
        # pylint: disable=E1101
        author = pygit2.Signature('Test user', 'test@email.com')
        committer = author
        _ = repo.create_commit('HEAD', author, committer, 'Created master',
                               new_tree, [])

        self.make_commit_with_file('Initial commit', '''
Add a README

''', 'README', '''This is the README file
describing this project
in very little detail''')

        self.make_commit_with_file('pci: PCI implementation', '''
Here is a basic PCI implementation

''', 'pci.c', '''This is a file
it has some contents
and some more things''')
        self.make_commit_with_file('main: Main program', '''
Hello here is the second commit.
''', 'main.c', '''This is the main file
there is very little here
but we can always add more later
if we want to

Series-to: u-boot
Series-cc: Barry Crump <bcrump@whataroa.nz>
''')
        base_target = repo.revparse_single('HEAD')
        self.make_commit_with_file('i2c: I2C things', '''
This has some stuff to do with I2C
''', 'i2c.c', '''And this is the file contents
with some I2C-related things in it''')
        self.make_commit_with_file('spi: SPI fixes', f'''
SPI needs some fixes
and here they are

Signed-off-by: {self.leb}

Series-to: u-boot
Commit-notes:
title of the series
This is the cover letter for the series
with various details
END
''', 'spi.c', '''Some fixes for SPI in this
file to make SPI work
better than before''')
        first_target = repo.revparse_single('HEAD')

        target = repo.revparse_single('HEAD~2')
        # pylint doesn't seem to find this
        # pylint: disable=E1101
        repo.reset(target.id, pygit2.enums.ResetMode.HARD)
        self.make_commit_with_file('video: Some video improvements', '''
Fix up the video so that
it looks more purple. Purple is
a very nice colour.
''', 'video.c', '''More purple here
Purple and purple
Even more purple
Could not be any more purple''')
        self.make_commit_with_file('serial: Add a serial driver', f'''
Here is the serial driver
for my chip.

Cover-letter:
{self.TITLE_SECOND}
This series implements support
for my glorious board.
END
Series-to: u-boot
Series-links: {self.SERIES_ID_SECOND_V1}
''', 'serial.c', '''The code for the
serial driver is here''')
        self.make_commit_with_file('bootm: Make it boot', '''
This makes my board boot
with a fix to the bootm
command
''', 'bootm.c', '''Fix up the bootm
command to make the code as
complicated as possible''')
        second_target = repo.revparse_single('HEAD')

        self.make_commit_with_file('usb: Try out the new DMA feature', '''
This is just a fix that
ensures that DMA is enabled
''', 'usb-uclass.c', '''Here is the USB
implementation and as you can see it
it very nice''')
        self.make_commit_with_file('main: Change to the main program', '''
Here we adjust the main
program just a little bit
''', 'main.c', '''This is the text of the main program''')
        self.make_commit_with_file('test: Check that everything works', '''
This checks that all the
various things we've been
adding actually work.
''', 'test.c', '''Here is the test code and it seems OK''')
        self.make_commit_with_file('lib: Sort out the extra library', '''
The extra library is currently
broken. Fix it so that we can
use it in various place.
''', 'lib.c', '''Some library code is here
and a little more''')
        third_target = repo.revparse_single('HEAD')

        repo.branches.local.create('first', first_target)
        repo.config.set_multivar('branch.first.remote', '', '.')
        repo.config.set_multivar('branch.first.merge', '', 'refs/heads/base')

        repo.branches.local.create('second', second_target)
        repo.config.set_multivar('branch.second.remote', '', '.')
        repo.config.set_multivar('branch.second.merge', '', 'refs/heads/base')

        repo.branches.local.create('base', base_target)

        repo.branches.local.create('third4', third_target)
        repo.config.set_multivar('branch.third4.remote', '', '.')
        repo.config.set_multivar('branch.third4.merge', '',
                                 'refs/heads/second')

        target = repo.lookup_reference('refs/heads/first')
        repo.checkout(target, strategy=pygit2.enums.CheckoutStrategy.FORCE)
        target = repo.revparse_single('HEAD')
        repo.reset(target.id, pygit2.enums.ResetMode.HARD)

        self.assertFalse(gitutil.check_dirty(self.gitdir, self.tmpdir))
        return repo


class Namespace:
    """Simple namespace for use instead of argparse in tests"""
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class CseriesTester(unittest.TestCase, TestCommon):
    """Fixtures for tests which use a git tree and a series database"""
    def setUp(self):
        TestCommon.setUp(self)
        self.autolink_extra = None
        self.loop = asyncio.get_event_loop()
        self.cser = None

    def tearDown(self):
        TestCommon.tearDown(self)

    def get_database(self):
        """Open the database and silence the warning output

        Return:
            Cseries: Resulting Cseries object
        """
        cser = cseries.Cseries(self.tmpdir, terminal.COLOR_NEVER)
        with terminal.capture() as _:
            cser.open_database()
        self.cser = cser
        return cser

    def get_cser(self):
        """Set up a git tree and database

        Return:
            Cseries: object
        """
        self.make_git_tree()
        return self.get_database()

    def db_close(self):
        """Close the database if open"""
        if self.cser and self.cser.db.cur:
            self.cser.close_database()
            return True
        return False

    def db_open(self):
        """Open the database if closed"""
        if self.cser and not self.cser.db.cur:
            self.cser.open_database()

    def run_args(self, *argv, expect_ret=0, pwork=None, cser=None):
        """Run patman with the given arguments

        Args:
            argv (list of str): List of arguments, excluding 'patman'
            expect_ret (int): Expected return code, used to check errors
            pwork (Patchwork): Patchwork object to use when executing the
                command, or None to create one
            cser (Cseries): Cseries object to use when executing the command,
                or None to create one
        """
        was_open = self.db_close()
        args = cmdline.parse_args(['-D'] + list(argv), config_fname=False)
        exit_code = control.do_patman(args, self.tmpdir, pwork, cser)
        if exit_code != expect_ret:
            # Show what patman said, which is usually captured, so that a
            # failure in another environment can be understood
            out = ''.join(getattr(stream, 'getvalue', str)()
                          for stream in (sys.stdout, sys.stderr))
            self.fail(f"'patman {' '.join(argv)}' gave exit code "
                      f'{exit_code} rather than {expect_ret}; its output '
                      f'ended:\n{out[-3000:]}')
        if was_open:
            self.db_open()
