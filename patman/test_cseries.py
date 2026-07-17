# SPDX-License-Identifier: GPL-2.0+

# Copyright 2025 Simon Glass <sjg@chromium.org>
#
"""Functional tests for checking that patman behaves correctly"""

import asyncio
import contextlib
from datetime import datetime
import os
import re
import types
import unittest
from unittest import mock

import pygit2

from u_boot_pylib import command
from u_boot_pylib import cros_subprocess
from u_boot_pylib import gitutil
from u_boot_pylib import terminal
from u_boot_pylib import tools
from patman import cmdline
from patman import control
from patman import cser_helper
from patman import review
from patman import cseries
from patman.database import Pcommit
from patman import database
from patman import patchstream
from patman.patchwork import Patchwork
from patman.test_common import TestCommon
from patman import workflow as wf

HASH_RE = r'[0-9a-f]+'
#pylint: disable=protected-access

class Namespace:
    """Simple namespace for use instead of argparse in tests"""
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class TestCseries(unittest.TestCase, TestCommon):
    """Test cases for the Cseries class

    In some cases there are tests for both direct Cseries calls and for
    accessing the feature via the cmdline. It is possible to do this with mocks
    but it is a bit painful to catch all cases that way. The approach here is
    to create a check_...() function which yields back to the test routines to
    make the call or run the command. The check_...() function typically yields
    a Cseries while it is working and False when it is done, allowing the test
    to check that everything is finished.

    Some subcommands don't have command tests, if it would be duplicative. Some
    tests avoid using the check_...() function and just write the test out
    twice, if it would be too confusing to use a coroutine.

    Note the -N flag which sort-of disables capturing of output, although in
    fact it is still captured, just output at the end. When debugging the code
    you may need to temporarily comment out the 'with terminal.capture()'
    parts.
    """
    def setUp(self):
        TestCommon.setUp(self)
        self.autolink_extra = None
        self.loop = asyncio.get_event_loop()
        self.cser = None

    def tearDown(self):
        TestCommon.tearDown(self)

    class _Stage:
        def __init__(self, name):
            self.name = name

        def __enter__(self):
            if not terminal.USE_CAPTURE:
                print(f"--- starting '{self.name}'")

        def __exit__(self, exc_type, exc_val, exc_tb):
            if not terminal.USE_CAPTURE:
                print(f"--- finished '{self.name}'\n")

    def stage(self, name):
        """Context manager to count requests across a range of patchwork calls

        Args:
            name (str): Stage name

        Return:
            _Stage: contect object

        Usage:
            with self.stage('name'):
                ...do things

            Note that the output only appears if the -N flag is used
        """
        return self._Stage(name)

    def assert_finished(self, itr):
        """Assert that an iterator is finished

        Args:
            itr (iter): Iterator to check
        """
        self.assertFalse(list(itr))

    def test_database_setup(self):
        """Check setting up of the series database"""
        cser = cseries.Cseries(self.tmpdir)
        with terminal.capture() as (_, err):
            cser.open_database()
        self.assertEqual(f'Creating new database {self.tmpdir}/.patman.db',
                         err.getvalue().strip())
        res = cser.db.execute("SELECT name FROM series")
        self.assertTrue(res)
        cser.close_database()

    def test_series_get_max_version_no_versions(self):
        """A series with no versions reports max version 0, not None"""
        cser = self.get_database()
        idnum = cser.db.series_add('video', 'Some series')
        # No ser_ver rows yet, so MAX(version) is SQL NULL. It must come
        # back as 0 so callers (e.g. review --scan) can compare it
        max_ver = cser.db.series_get_max_version(idnum)
        self.assertEqual(0, max_ver)
        cser.close_database()

    def test_scan_child_env_has_patman_on_path(self):
        """A scan child gets patman on PYTHONPATH, keeping any existing"""
        pkg_parent = os.path.dirname(
            os.path.dirname(os.path.abspath(review.__file__)))
        with mock.patch.dict(os.environ, {'PYTHONPATH': '/existing/path'}):
            env = review._child_env()
        parts = env['PYTHONPATH'].split(os.pathsep)
        self.assertIn(pkg_parent, parts)
        self.assertIn('/existing/path', parts)
        # 'python -m patman' from that directory imports the same package
        self.assertTrue(
            os.path.isdir(os.path.join(pkg_parent, 'patman')))

    def test_agent_options_applies_model(self):
        """_agent_options injects the chosen model, else leaves it unset"""
        captured = {}

        def fake_opts(**kwargs):
            captured.clear()
            captured.update(kwargs)
            return kwargs

        with mock.patch.object(review, 'ClaudeAgentOptions',
                               side_effect=fake_opts):
            # No model chosen: nothing is added, SDK default is used
            with mock.patch.object(review, '_AGENT_MODEL', None):
                review._agent_options(allowed_tools=[])
                self.assertNotIn('model', captured)

            # A chosen model is passed through
            with mock.patch.object(review, '_AGENT_MODEL', 'sonnet'):
                review._agent_options(allowed_tools=[])
                self.assertEqual('sonnet', captured.get('model'))

                # An explicit model wins over the run-wide default
                review._agent_options(model='opus')
                self.assertEqual('opus', captured.get('model'))

    def test_review_list_models(self):
        """--list-models prints the accepted aliases and exits cleanly"""
        args = Namespace(list_models=True, model=None)
        with terminal.capture() as (out, _):
            ret = review.do_review(args, None, None)
        self.assertEqual(0, ret)
        text = out.getvalue()
        for alias in ('opus', 'sonnet', 'haiku'):
            self.assertIn(alias, text)
        self.assertIn('--model', text)

    def test_scan_review_stats(self):
        """_review_stats counts patches, comments and approvals per series"""
        from patman import database
        cser = self.get_database()
        sid = cser.db.series_add('vid', 'My series')
        cser.db.series_set_source(sid, 'review')
        svid = cser.db.ser_ver_add(sid, 1, link='999001')
        pcs = [database.Pcommit(idnum=None, seq=i, subject=f'p{i}',
                                svid=svid, change_id=None, state=None,
                                patch_id=None, num_comments=0)
               for i in range(3)]
        cser.db.pcommit_add_list(svid, pcs)
        # cover approved (seq 0, ignored), patch 1 approved, patch 2
        # commented, patch 3 left unreviewed
        cser.db.review_add(svid, 0, 'cover', True, 't')
        cser.db.review_add(svid, 1, 'lgtm', True, 't')
        cser.db.review_add(svid, 2, 'please fix', False, 't')
        cser.commit()

        # 3 patches, 1 with comments, 1 approved
        self.assertEqual((3, 1, 1), review._review_stats(cser, '999001'))
        # Passing an int link works too (find casts to str)
        self.assertEqual((3, 1, 1), review._review_stats(cser, 999001))
        # Unknown link -> None
        self.assertIsNone(review._review_stats(cser, '404'))
        cser.close_database()

    def test_scan_child_passes_model(self):
        """--model is forwarded to each scan child review"""
        args = Namespace(
            project=None, patchwork_url=None, verbose=False, debug=False,
            upstream=None, reviewer=None, base_branch=None,
            gmail_account=None, signoff=None, spelling=None, context=None,
            model='sonnet', create_drafts=False)
        cmd = review._build_review_command(args, 12345)
        self.assertIn('--model', cmd)
        self.assertEqual('sonnet', cmd[cmd.index('--model') + 1])

        args.model = None
        self.assertNotIn('--model', review._build_review_command(args, 12345))

    def test_register_series_atomic_on_error(self):
        """A failed registration leaves no orphan series (no ser_ver row)"""
        cser = self.get_database()
        before = len(cser.db.series_get_dict(include_reviews=True))

        # Fail after the series row is added but before its version, the
        # exact window that used to leave an orphan behind
        with mock.patch.object(cser.db, 'ser_ver_add',
                               side_effect=ValueError('boom')):
            with self.assertRaises(ValueError):
                review._register_series(cser, 'Some series', 1, '12345',
                                        {'patches': []})

        # A later unrelated commit must not flush a half-created series row
        cser.commit()
        after = cser.db.series_get_dict(include_reviews=True)
        self.assertEqual(before, len(after))
        orphans = [s for s in after.values()
                   if not cser.db.series_get_max_version(s.idnum)]
        self.assertEqual([], orphans)
        cser.close_database()

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
        self.assertEqual(expect_ret, exit_code)
        if was_open:
            self.db_open()

    def test_series_add(self):
        """Test adding a new cseries"""
        cser = self.get_cser()
        self.assertFalse(cser.db.series_get_dict())

        with terminal.capture() as (out, _):
            cser.add('first', 'my description', allow_unmarked=True)
        lines = out.getvalue().strip().splitlines()
        self.assertEqual(
            "Adding series 'first' v1: mark False allow_unmarked True",
            lines[0])
        self.assertEqual("Added series 'first' v1 (2 commits)", lines[1])
        self.assertEqual(2, len(lines))

        slist = cser.db.series_get_dict()
        self.assertEqual(1, len(slist))
        self.assertEqual('first', slist['first'].name)
        self.assertEqual('my description', slist['first'].desc)

        svlist = cser.get_ser_ver_list()
        self.assertEqual(1, len(svlist))
        self.assertEqual(1, svlist[0].idnum)
        self.assertEqual(1, svlist[0].series_id)
        self.assertEqual(1, svlist[0].version)

        pclist = cser.get_pcommit_dict()
        self.assertEqual(2, len(pclist))
        self.assertIn(1, pclist)
        self.assertEqual(
            Pcommit(1, 0, 'i2c: I2C things', 1, None, None, None, None),
            pclist[1])
        self.assertEqual(
            Pcommit(2, 1, 'spi: SPI fixes', 1, None, None, None, None),
            pclist[2])

    def test_series_add_not_checked_out(self):
        """Test adding a new cseries when a different one is checked out"""
        cser = self.get_cser()
        self.assertFalse(cser.db.series_get_dict())

        with terminal.capture() as (out, _):
            cser.add('second', allow_unmarked=True)
        lines = out.getvalue().strip().splitlines()
        self.assertEqual(
            "Adding series 'second' v1: mark False allow_unmarked True",
            lines[0])
        self.assertEqual("Added series 'second' v1 (3 commits)", lines[1])
        self.assertEqual(2, len(lines))

    def test_series_add_manual(self):
        """Test adding a new cseries with a version number"""
        cser = self.get_cser()
        self.assertFalse(cser.db.series_get_dict())

        repo = pygit2.init_repository(self.gitdir)
        first_target = repo.revparse_single('first')
        repo.branches.local.create('first2', first_target)
        repo.config.set_multivar('branch.first2.remote', '', '.')
        repo.config.set_multivar('branch.first2.merge', '', 'refs/heads/base')

        with terminal.capture() as (out, _):
            cser.add('first2', 'description', allow_unmarked=True)
        lines = out.getvalue().splitlines()
        self.assertEqual(
            "Adding series 'first' v2: mark False allow_unmarked True",
            lines[0])
        self.assertEqual("Added series 'first' v2 (2 commits)", lines[1])
        self.assertEqual(2, len(lines))

        slist = cser.db.series_get_dict()
        self.assertEqual(1, len(slist))
        self.assertEqual('first', slist['first'].name)

        # We should have just one entry, with version 2
        svlist = cser.get_ser_ver_list()
        self.assertEqual(1, len(svlist))
        self.assertEqual(1, svlist[0].idnum)
        self.assertEqual(1, svlist[0].series_id)
        self.assertEqual(2, svlist[0].version)

    def add_first2(self, checkout):
        """Add a new first2 branch, a copy of first"""
        repo = pygit2.init_repository(self.gitdir)
        first_target = repo.revparse_single('first')
        repo.branches.local.create('first2', first_target)
        repo.config.set_multivar('branch.first2.remote', '', '.')
        repo.config.set_multivar('branch.first2.merge', '', 'refs/heads/base')

        if checkout:
            target = repo.lookup_reference('refs/heads/first2')
            repo.checkout(target, strategy=pygit2.enums.CheckoutStrategy.FORCE)

    def test_series_add_different(self):
        """Test adding a different version of a series from that checked out"""
        cser = self.get_cser()

        self.add_first2(True)

        # Add first2 initially
        with terminal.capture() as (out, _):
            cser.add(None, 'description', allow_unmarked=True)
        lines = out.getvalue().splitlines()
        self.assertEqual(
            "Adding series 'first' v2: mark False allow_unmarked True",
            lines[0])
        self.assertEqual("Added series 'first' v2 (2 commits)", lines[1])
        self.assertEqual(2, len(lines))

        # Now add first: it should be added as a new version
        with terminal.capture() as (out, _):
            cser.add('first', 'description', allow_unmarked=True)
        lines = out.getvalue().splitlines()
        self.assertEqual(
            "Adding series 'first' v1: mark False allow_unmarked True",
            lines[0])
        self.assertEqual(
            "Added v1 to existing series 'first' (2 commits)", lines[1])
        self.assertEqual(2, len(lines))

        slist = cser.db.series_get_dict()
        self.assertEqual(1, len(slist))
        self.assertEqual('first', slist['first'].name)

        # We should have two entries, one of each version
        svlist = cser.get_ser_ver_list()
        self.assertEqual(2, len(svlist))
        self.assertEqual(1, svlist[0].idnum)
        self.assertEqual(1, svlist[0].series_id)
        self.assertEqual(2, svlist[0].version)

        self.assertEqual(2, svlist[1].idnum)
        self.assertEqual(1, svlist[1].series_id)
        self.assertEqual(1, svlist[1].version)

    def test_series_add_dup(self):
        """Test adding a series twice"""
        cser = self.get_cser()
        with terminal.capture() as (out, _):
            cser.add(None, 'description', allow_unmarked=True)

        with terminal.capture() as (out, _):
            cser.add(None, 'description', allow_unmarked=True)
        self.assertIn("Series 'first' v1 already exists",
                      out.getvalue().strip())

        self.add_first2(False)

        with terminal.capture() as (out, _):
            cser.add('first2', 'description', allow_unmarked=True)
        lines = out.getvalue().splitlines()
        self.assertEqual(
            "Added v2 to existing series 'first' (2 commits)", lines[1])

    def test_series_add_dup_reverse(self):
        """Test adding a series twice, v2 then v1"""
        cser = self.get_cser()
        self.add_first2(True)
        with terminal.capture() as (out, _):
            cser.add(None, 'description', allow_unmarked=True)
        self.assertIn("Added series 'first' v2", out.getvalue().strip())

        with terminal.capture() as (out, _):
            cser.add('first', 'description', allow_unmarked=True)
        self.assertIn("Added v1 to existing series 'first'",
                      out.getvalue().strip())

    def test_series_add_dup_reverse_cmdline(self):
        """Test adding a series twice, v2 then v1"""
        cser = self.get_cser()
        self.add_first2(True)
        with terminal.capture() as (out, _):
            self.run_args('series', 'add', '-M', '-D', 'description',
                          pwork=True)
        self.assertIn("Added series 'first' v2 (2 commits)",
                      out.getvalue().strip())

        with terminal.capture() as (out, _):
            self.run_args('series', '-s', 'first', 'add', '-M',
                          '-D', 'description', pwork=True)
            cser.add('first', 'description', allow_unmarked=True)
        self.assertIn("Added v1 to existing series 'first'",
                      out.getvalue().strip())

    def test_series_add_skip_version(self):
        """Test adding a series which is v4 but has no earlier version"""
        cser = self.get_cser()
        with terminal.capture() as (out, _):
            cser.add('third4', 'The glorious third series', mark=False,
                     allow_unmarked=True)
        lines = out.getvalue().splitlines()
        self.assertEqual(
            "Adding series 'third' v4: mark False allow_unmarked True",
            lines[0])
        self.assertEqual("Added series 'third' v4 (4 commits)", lines[1])
        self.assertEqual(2, len(lines))

        sdict = cser.db.series_get_dict()
        self.assertIn('third', sdict)
        chk = sdict['third']
        self.assertEqual('third', chk['name'])
        self.assertEqual('The glorious third series', chk['desc'])

        svid = cser.get_series_svid(chk['idnum'], 4)
        self.assertEqual(4, len(cser.get_pcommit_dict(svid)))

        # Remove the series and add it again with just two commits
        with terminal.capture():
            cser.remove('third4')

        with terminal.capture() as (out, _):
            cser.add('third4', 'The glorious third series', mark=False,
                     allow_unmarked=True, end='third4~2')
        lines = out.getvalue().splitlines()
        self.assertEqual(
            "Adding series 'third' v4: mark False allow_unmarked True",
            lines[0])
        self.assertRegex(
            lines[1],
            'Ending before .* main: Change to the main program')
        self.assertEqual("Added series 'third' v4 (2 commits)", lines[2])

        sdict = cser.db.series_get_dict()
        self.assertIn('third', sdict)
        chk = sdict['third']
        self.assertEqual('third', chk['name'])
        self.assertEqual('The glorious third series', chk['desc'])

        svid = cser.get_series_svid(chk['idnum'], 4)
        self.assertEqual(2, len(cser.get_pcommit_dict(svid)))

    def test_series_add_wrong_version(self):
        """Test adding a series with an incorrect branch name or version

        This updates branch 'first' to have version 2, then tries to add it.
        """
        cser = self.get_cser()
        self.assertFalse(cser.db.series_get_dict())

        with terminal.capture():
            _, ser, max_vers, _ = cser.prep_series('first')
            cser.update_series('first', ser, max_vers, None, False,
                                add_vers=2)

        with self.assertRaises(ValueError) as exc:
            with terminal.capture():
                cser.add('first', 'my description', allow_unmarked=True)
        self.assertEqual(
            "Series name 'first' suggests version 1 but Series-version tag "
            'indicates 2 (see --force-version)', str(exc.exception))

        # Now try again with --force-version which should force version 1
        with terminal.capture() as (out, _):
            cser.add('first', 'my description', allow_unmarked=True,
                     force_version=True)
        itr = iter(out.getvalue().splitlines())
        self.assertEqual(
            "Adding series 'first' v1: mark False allow_unmarked True",
            next(itr))
        self.assertRegex(
            next(itr), 'Checking out upstream commit refs/heads/base: .*')
        self.assertEqual(
            "Processing 2 commits from branch 'first'", next(itr))
        self.assertRegex(next(itr),
                         f'-        {HASH_RE} as {HASH_RE} i2c: I2C things')
        self.assertRegex(next(itr),
                         f'- rm v1: {HASH_RE} as {HASH_RE} spi: SPI fixes')
        self.assertRegex(next(itr),
                         f'Updating branch first from {HASH_RE} to {HASH_RE}')
        self.assertEqual("Added series 'first' v1 (2 commits)", next(itr))
        try:
            self.assertEqual('extra line', next(itr))
        except StopIteration:
            pass

        # Since this is v1 the Series-version tag should have been removed
        series = patchstream.get_metadata('first', 0, 2, git_dir=self.gitdir)
        self.assertNotIn('version', series)

    def test_series_add_no_desc(self):
        """Test adding a cseries with no cover letter"""
        cser = self.get_cser()
        self.assertFalse(cser.db.series_get_dict())

        with self.assertRaises(ValueError) as exc:
            with terminal.capture() as (out, _):
                cser.add('first', allow_unmarked=True)
        self.assertEqual(
            "Branch 'first' has no cover letter - please provide description",
            str(exc.exception))

        with terminal.capture() as (out, _):
            self.run_args('series', '-s', 'first', 'add', '--use-first-commit',
                        '--allow-unmarked', pwork=True)
        lines = out.getvalue().splitlines()
        self.assertEqual(
            "Adding series 'first' v1: mark False allow_unmarked True",
            lines[0])
        self.assertEqual(
            "Using description from first commit: 'i2c: I2C things'",
            lines[1])
        self.assertEqual("Added series 'first' v1 (2 commits)", lines[2])
        self.assertEqual(3, len(lines))

        sdict = cser.db.series_get_dict()
        self.assertEqual(1, len(sdict))
        ser = sdict.get('first')
        self.assertTrue(ser)
        self.assertEqual('first', ser.name)
        self.assertEqual('i2c: I2C things', ser.desc)

    def _fake_patchwork_cser(self, subpath):
        """Fake Patchwork server for the function below

        This handles accessing various things used by the tests below. It has
        hard-coded data, about from self.autolink_extra which can be adjusted
        by the test.

        Args:
            subpath (str): URL subpath to use
        """
        # Get a list of projects; return them one per page to check that
        # pagination works
        re_proj = re.match(r'projects/\?page=(\d+)&per_page=\d+$', subpath)
        if re_proj:
            page = int(re_proj.group(1))
            projects = [
                {'id': self.PROJ_ID, 'name': 'U-Boot',
                 'link_name': self.PROJ_LINK_NAME},
                {'id': 9, 'name': 'other', 'link_name': 'other'}
            ]
            return projects[page - 1:page]

        # Search for series by their cover-letter name
        re_search = re.match(r'series/\?project=(\d+)&q=.*$', subpath)
        if re_search:
            result = [
                {'id': 56, 'name': 'contains first name', 'version': 1},
                {'id': 43, 'name': 'has first in it', 'version': 1},
                {'id': 1234, 'name': 'first series', 'version': 1},
                {'id': self.SERIES_ID_SECOND_V1, 'name': self.TITLE_SECOND,
                 'version': 1},
                {'id': self.SERIES_ID_SECOND_V2, 'name': self.TITLE_SECOND,
                 'version': 2},
                {'id': 12345, 'name': 'i2c: I2C things', 'version': 1},
            ]
            if self.autolink_extra:
                result += [self.autolink_extra]
            return result

        # Read information about a series, given its link (patchwork series ID)
        m_series = re.match(r'series/(\d+)/$', subpath)
        series_id = int(m_series.group(1)) if m_series else ''
        if series_id:
            if series_id == self.SERIES_ID_SECOND_V1:
                # series 'second'
                return {
                    'patches': [
                        {'id': '10',
                         'name': '[PATCH,1/3] video: Some video improvements',
                         'content': ''},
                        {'id': '11',
                         'name': '[PATCH,2/3] serial: Add a serial driver',
                         'content': ''},
                        {'id': '12', 'name': '[PATCH,3/3] bootm: Make it boot',
                         'content': ''},
                    ],
                    'cover_letter': {
                        'id': 39,
                        'name': 'The name of the cover letter',
                    }
                }
            if series_id == self.SERIES_ID_SECOND_V2:
                # series 'second2'
                return {
                    'patches': [
                        {'id': '110',
                         'name':
                             '[PATCH,v2,1/3] video: Some video improvements',
                         'content': ''},
                        {'id': '111',
                         'name': '[PATCH,v2,2/3] serial: Add a serial driver',
                         'content': ''},
                        {'id': '112',
                         'name': '[PATCH,v2,3/3] bootm: Make it boot',
                         'content': ''},
                    ],
                    'cover_letter': {
                        'id': 139,
                        'name': 'The name of the cover letter',
                    }
                }
            if series_id == self.SERIES_ID_FIRST_V3:
                # series 'first3'
                return {
                    'patches': [
                        {'id': 20, 'name': '[PATCH,v3,1/2] i2c: I2C things',
                         'content': ''},
                        {'id': 21, 'name': '[PATCH,v3,2/2] spi: SPI fixes',
                         'content': ''},
                    ],
                    'cover_letter': {
                        'id': 29,
                        'name': 'Cover letter for first',
                    }
                }
            if series_id == 123:
                return {
                    'patches': [
                        {'id': 20, 'name': '[PATCH,1/2] i2c: I2C things',
                         'content': ''},
                        {'id': 21, 'name': '[PATCH,2/2] spi: SPI fixes',
                         'content': ''},
                    ],
                }
            if series_id == 1234:
                return {
                    'patches': [
                        {'id': 20, 'name': '[PATCH,v2,1/2] i2c: I2C things',
                         'content': ''},
                        {'id': 21, 'name': '[PATCH,v2,2/2] spi: SPI fixes',
                         'content': ''},
                    ],
                }
            raise ValueError(f'Fake Patchwork unknown series_id: {series_id}')

        # Read patch status
        m_pat = re.search(r'patches/(\d*)/$', subpath)
        patch_id = int(m_pat.group(1)) if m_pat else ''
        if patch_id:
            if patch_id in [10, 110]:
                return {'state': 'accepted',
                        'content':
                            'Reviewed-by: Fred Bloggs <fred@bloggs.com>'}
            if patch_id in [11, 111]:
                return {'state': 'changes-requested', 'content': ''}
            if patch_id in [12, 112]:
                return {'state': 'rejected',
                        'content': "I don't like this at all, sorry"}
            if patch_id == 20:
                return {'state': 'awaiting-upstream', 'content': ''}
            if patch_id == 21:
                return {'state': 'not-applicable', 'content': ''}
            raise ValueError(f'Fake Patchwork unknown patch_id: {patch_id}')

        # Read comments a from patch
        m_comm = re.search(r'patches/(\d*)/comments/', subpath)
        patch_id = int(m_comm.group(1)) if m_comm else ''
        if patch_id:
            if patch_id in [10, 110]:
                return [
                    {'id': 1, 'content': ''},
                    {'id': 2,
                     'content':
                         '''On some date Mary Smith <msmith@wibble.com> wrote:
> This was my original patch
> which is being quoted

I like the approach here and I would love to see more of it.

Reviewed-by: Fred Bloggs <fred@bloggs.com>
''',
                     'submitter': {
                         'name': 'Fred Bloggs',
                         'email': 'fred@bloggs.com',
                         }
                     },
                ]
            if patch_id in [11, 111]:
                return []
            if patch_id in [12, 112]:
                return [
                    {'id': 4, 'content': ''},
                    {'id': 5, 'content': ''},
                    {'id': 6, 'content': ''},
                ]
            if patch_id == 20:
                return [
                    {'id': 7, 'content':
                     '''On some date Alex Miller <alex@country.org> wrote:

> Sometimes we need to create a patch.
> This is one of those times

Tested-by: Mary Smith <msmith@wibble.com>   # yak
'''},
                    {'id': 8, 'content': ''},
                ]
            if patch_id == 21:
                return []
            raise ValueError(
                f'Fake Patchwork does not understand patch_id {patch_id}: '
                f'{subpath}')

        # Read comments from a cover letter
        m_cover_id = re.search(r'covers/(\d*)/comments/', subpath)
        cover_id = int(m_cover_id.group(1)) if m_cover_id else ''
        if cover_id:
            if cover_id in [39, 139]:
                return [
                    {'content': 'some comment',
                        'submitter': {
                            'name': 'A user',
                            'email': 'user@user.com',
                          },
                        'date': 'Sun 13 Apr 14:06:02 MDT 2025',
                     },
                    {'content': 'another comment',
                        'submitter': {
                            'name': 'Ghenkis Khan',
                            'email': 'gk@eurasia.gov',
                        },
                     'date': 'Sun 13 Apr 13:06:02 MDT 2025',
                     },
                ]
            if cover_id == 29:
                return []

            raise ValueError(f'Fake Patchwork unknown cover_id: {cover_id}')

        raise ValueError(f'Fake Patchwork does not understand: {subpath}')

    def setup_second(self, do_sync=True):
        """Set up the 'second' series synced with the fake patchwork

        Args:
            do_sync (bool): True to sync the series

        Return: tuple:
            Cseries: New Cseries object
            pwork: Patchwork object
        """
        with self.stage('setup second'):
            cser = self.get_cser()
            pwork = Patchwork.for_testing(self._fake_patchwork_cser)
            pwork.project_set(self.PROJ_ID, self.PROJ_LINK_NAME)

            with terminal.capture() as (out, _):
                cser.add('first', '', allow_unmarked=True)
                cser.add('second', allow_unmarked=True)

            series = patchstream.get_metadata_for_list('second', self.gitdir,
                                                       3)
            self.assertEqual('456', series.links)

            with terminal.capture() as (out, _):
                cser.increment('second')

            series = patchstream.get_metadata_for_list('second', self.gitdir,
                                                       3)
            self.assertEqual('456', series.links)

            series = patchstream.get_metadata_for_list('second2', self.gitdir,
                                                       3)
            self.assertEqual('1:456', series.links)

            if do_sync:
                with terminal.capture() as (out, _):
                    cser.link_auto(pwork, 'second', 2, True)
                with terminal.capture() as (out, _):
                    cser.gather(pwork, 'second', 2, False, True, False)
                lines = out.getvalue().splitlines()
                self.assertEqual(
                    "Updating series 'second' version 2 from link '457'",
                    lines[0])
                self.assertEqual(
                    '3 patches and cover letter updated (8 requests)',
                    lines[1])
                self.assertEqual(2, len(lines))

        return cser, pwork

    def test_series_add_no_cover(self):
        """Test patchwork when adding a series which has no cover letter"""
        cser = self.get_cser()
        pwork = Patchwork.for_testing(self._fake_patchwork_cser)
        pwork.project_set(self.PROJ_ID, self.PROJ_LINK_NAME)

        with terminal.capture() as (out, _):
            cser.add('first', 'my name for this', mark=False,
                     allow_unmarked=True)
        self.assertIn("Added series 'first' v1 (2 commits)", out.getvalue())

        with terminal.capture() as (out, _):
            cser.link_auto(pwork, 'first', 1, True)
        self.assertIn("Setting link for series 'first' v1 to 12345",
                      out.getvalue())

    def test_series_list(self):
        """Test listing cseries"""
        self.setup_second()

        self.db_close()
        args = Namespace(subcmd='ls', include_archived=False)
        with terminal.capture() as (out, _):
            control.do_series(args, test_db=self.tmpdir, pwork=True)
        lines = out.getvalue().splitlines()
        self.assertEqual(5, len(lines))
        self.assertEqual(
            'Name             Description                               '
            'Accepted  Us  Versions', lines[0])
        self.assertTrue(lines[1].startswith('--'))
        self.assertEqual(
            'first                                                      '
            '     -/2      1', lines[2])
        self.assertEqual(
            'second           Series for my board                       '
            '     1/3      1 2', lines[3])
        self.assertTrue(lines[4].startswith('--'))

    def test_series_list_archived(self):
        """Archive a series and test listing it"""
        self.setup_second()
        with terminal.capture():
            self.cser.archive('first')
        with terminal.capture() as (out, _):
            self.run_args('series', 'ls', pwork=True)
        lines = out.getvalue().splitlines()
        self.assertEqual(4, len(lines))
        self.assertEqual(
            'second           Series for my board                       '
            '     1/3      1 2', lines[2])

        # Now list including archived series
        with terminal.capture() as (out, _):
            self.run_args('series', 'ls', '--include-archived', pwork=True)
        lines = out.getvalue().splitlines()
        self.assertEqual(5, len(lines))
        self.assertEqual(
            'first                                                      '
            '     -/2      1', lines[2])
        self.assertEqual(
            'second           Series for my board                       '
            '     1/3      1 2', lines[3])

    def test_do_series_add(self):
        """Add a new cseries"""
        self.make_git_tree()
        args = Namespace(subcmd='add', desc='my-description', series='first',
                         mark=False, allow_unmarked=True, upstream=None,
                         set_upstream=None,
                         use_first_commit=False, dry_run=False)
        with terminal.capture() as (out, _):
            control.do_series(args, test_db=self.tmpdir, pwork=True)

        cser = self.get_database()
        slist = cser.db.series_get_dict()
        self.assertEqual(1, len(slist))
        ser = slist.get('first')
        self.assertTrue(ser)
        self.assertEqual('first', ser.name)
        self.assertEqual('my-description', ser.desc)

        self.db_close()
        args.subcmd = 'ls'
        args.include_archived = False
        with terminal.capture() as (out, _):
            control.do_series(args, test_db=self.tmpdir, pwork=True)
        lines = out.getvalue().splitlines()
        self.assertEqual(4, len(lines))
        self.assertTrue(lines[1].startswith('--'))
        self.assertEqual(
            'first            my-description                                 '
            '-/2      1', lines[2])

    def test_do_series_add_upstream(self):
        """Test that series add can set the upstream"""
        self.make_git_tree()
        args = Namespace(subcmd='add', desc='my-description', series='first',
                         mark=False, allow_unmarked=True, upstream=None,
                         set_upstream='origin',
                         use_first_commit=False, dry_run=False)
        with terminal.capture():
            control.do_series(args, test_db=self.tmpdir, pwork=True)

        cser = self.get_database()
        slist = cser.db.series_get_dict()
        ser = slist.get('first')
        self.assertEqual('origin', ser.upstream)

    def test_do_series_add_cmdline(self):
        """Add a new cseries using the cmdline"""
        self.make_git_tree()
        with terminal.capture():
            self.run_args('series', '-s', 'first', 'add', '-M',
                          '-D', 'my-description', pwork=True)

        cser = self.get_database()
        slist = cser.db.series_get_dict()
        self.assertEqual(1, len(slist))
        ser = slist.get('first')
        self.assertTrue(ser)
        self.assertEqual('first', ser.name)
        self.assertEqual('my-description', ser.desc)

    def test_do_series_add_auto(self):
        """Add a new cseries without any arguments"""
        self.make_git_tree()

        # Use the 'second' branch, which has a cover letter
        gitutil.checkout('second', self.gitdir, work_tree=self.tmpdir,
                         force=True)
        args = Namespace(subcmd='add', series=None, mark=False,
                         allow_unmarked=True, upstream=None, dry_run=False,
                         set_upstream=None,
                         desc=None, use_first_commit=False)
        with terminal.capture():
            control.do_series(args, test_db=self.tmpdir, pwork=True)

        cser = self.get_database()
        slist = cser.db.series_get_dict()
        self.assertEqual(1, len(slist))
        ser = slist.get('second')
        self.assertTrue(ser)
        self.assertEqual('second', ser.name)
        self.assertEqual('Series for my board', ser.desc)
        cser.close_database()

    def _check_inc(self, out):
        """Check output from an 'increment' operation

        Args:
            out (StringIO): Text to check
        """
        itr = iter(out.getvalue().splitlines())

        self.assertEqual("Increment 'first' v1: 2 patches", next(itr))
        self.assertRegex(next(itr), 'Checking out upstream commit .*')
        self.assertEqual("Processing 2 commits from branch 'first2'",
                         next(itr))
        self.assertRegex(next(itr),
                         f'-         {HASH_RE} as {HASH_RE} i2c: I2C things')
        self.assertRegex(next(itr),
                         f'- add v2: {HASH_RE} as {HASH_RE} spi: SPI fixes')
        self.assertRegex(
            next(itr), f'Updating branch first2 from {HASH_RE} to {HASH_RE}')
        self.assertEqual("Incremented series 'first' to v2", next(itr))
        return itr

    def test_series_link(self):
        """Test adding a patchwork link to a cseries"""
        cser = self.get_cser()

        repo = pygit2.init_repository(self.gitdir)
        first = repo.lookup_branch('first').peel(
            pygit2.enums.ObjectType.COMMIT).id
        base = repo.lookup_branch('base').peel(
            pygit2.enums.ObjectType.COMMIT).id

        gitutil.checkout('first', self.gitdir, work_tree=self.tmpdir,
                         force=True)

        with terminal.capture() as (out, _):
            cser.add('first', '', allow_unmarked=True)

        with self.assertRaises(ValueError) as exc:
            cser.link_set('first', 2, '1234', True)
        self.assertEqual("Series 'first' does not have a version 2",
                         str(exc.exception))

        self.assertEqual('first', gitutil.get_branch(self.gitdir))
        with terminal.capture() as (out, _):
            cser.increment('first')
        self.assertTrue(repo.lookup_branch('first2'))

        with terminal.capture() as (out, _):
            cser.link_set('first', 2, '2345', True)

        lines = out.getvalue().splitlines()
        self.assertEqual(6, len(lines))
        self.assertRegex(
            lines[0], 'Checking out upstream commit refs/heads/base: .*')
        self.assertEqual("Processing 2 commits from branch 'first2'",
                         lines[1])
        self.assertRegex(
            lines[2],
            f'-                        {HASH_RE} as {HASH_RE} i2c: I2C things')
        self.assertRegex(
            lines[3],
            f"- add v2 links '2:2345': {HASH_RE} as {HASH_RE} spi: SPI fixes")
        self.assertRegex(
            lines[4], f'Updating branch first2 from {HASH_RE} to {HASH_RE}')
        self.assertEqual("Setting link for series 'first' v2 to 2345",
                         lines[5])

        self.assertEqual('2345', cser.link_get('first', 2))

        series = patchstream.get_metadata_for_list('first2', self.gitdir, 2)
        self.assertEqual('2:2345', series.links)

        self.assertEqual('first2', gitutil.get_branch(self.gitdir))

        # Check the original series was left alone
        self.assertEqual(
            first, repo.lookup_branch('first').peel(
                pygit2.enums.ObjectType.COMMIT).id)
        count = 2
        series1 = patchstream.get_metadata_for_list('first', self.gitdir,
                                                    count)
        self.assertFalse('links' in series1)
        self.assertFalse('version' in series1)

        # Check that base is left alone
        self.assertEqual(
            base, repo.lookup_branch('base').peel(
                pygit2.enums.ObjectType.COMMIT).id)
        series1 = patchstream.get_metadata_for_list('base', self.gitdir, count)
        self.assertFalse('links' in series1)
        self.assertFalse('version' in series1)

        # Check out second and try to update first
        gitutil.checkout('second', self.gitdir, work_tree=self.tmpdir,
                         force=True)
        with terminal.capture():
            cser.link_set('first', 1, '16', True)

        # Overwrite the link
        with terminal.capture():
            cser.link_set('first', 1, '17', True)

        series2 = patchstream.get_metadata_for_list('first', self.gitdir,
                                                    count)
        self.assertEqual('1:17', series2.links)

    def test_series_link_cmdline(self):
        """Test adding a patchwork link to a cseries using the cmdline"""
        cser = self.get_cser()

        gitutil.checkout('first', self.gitdir, work_tree=self.tmpdir,
                         force=True)

        with terminal.capture() as (out, _):
            cser.add('first', '', allow_unmarked=True)

        with terminal.capture() as (out, _):
            self.run_args('series', '-s', 'first', '-V', '4', 'set-link', '-u',
                          '1234', expect_ret=1, pwork=True)
        self.assertIn("Series 'first' does not have a version 4",
                      out.getvalue())

        with self.assertRaises(ValueError) as exc:
            cser.link_get('first', 4)
        self.assertEqual("Series 'first' does not have a version 4",
                         str(exc.exception))

        with terminal.capture() as (out, _):
            cser.increment('first')

        with self.assertRaises(ValueError) as exc:
            cser.link_get('first', 4)
        self.assertEqual("Series 'first' does not have a version 4",
                         str(exc.exception))

        with terminal.capture() as (out, _):
            cser.increment('first')
            cser.increment('first')

        with terminal.capture() as (out, _):
            self.run_args('series', '-s', 'first', '-V', '4', 'set-link', '-u',
                          '1234', pwork=True)
        lines = out.getvalue().splitlines()
        self.assertRegex(
            lines[-3],
            f"- add v4 links '4:1234': {HASH_RE} as {HASH_RE} spi: SPI fixes")
        self.assertEqual("Setting link for series 'first' v4 to 1234",
                         lines[-1])

        with terminal.capture() as (out, _):
            self.run_args('series', '-s', 'first', '-V', '4', 'get-link',
                          pwork=True)
        self.assertIn('1234', out.getvalue())

        series = patchstream.get_metadata_for_list('first4', self.gitdir, 1)
        self.assertEqual('4:1234', series.links)

        with terminal.capture() as (out, _):
            self.run_args('series', '-s', 'first', '-V', '5', 'get-link',
                          expect_ret=1, pwork=True)

        self.assertIn("Series 'first' does not have a version 5",
                      out.getvalue())

        # Checkout 'first' and try to get the link from 'first4'
        gitutil.checkout('first', self.gitdir, work_tree=self.tmpdir,
                         force=True)

        with terminal.capture() as (out, _):
            self.run_args('series', '-s', 'first4', 'get-link', pwork=True)
        self.assertIn('1234', out.getvalue())

        # This should get the link for 'first'
        with terminal.capture() as (out, _):
            self.run_args('series', 'get-link', pwork=True)
        self.assertIn('None', out.getvalue())

        # Checkout 'first4' again; this should get the link for 'first4'
        gitutil.checkout('first4', self.gitdir, work_tree=self.tmpdir,
                         force=True)

        with terminal.capture() as (out, _):
            self.run_args('series', 'get-link', pwork=True)
        self.assertIn('1234', out.getvalue())

    def test_series_link_auto_version(self):
        """Test finding the patchwork link for a cseries automatically"""
        cser = self.get_cser()

        with terminal.capture() as (out, _):
            cser.add('second', allow_unmarked=True)

        # Make sure that the link is there
        count = 3
        series = patchstream.get_metadata('second', 0, count,
                                          git_dir=self.gitdir)
        self.assertEqual(f'{self.SERIES_ID_SECOND_V1}', series.links)

        # Set link with detected version
        with terminal.capture() as (out, _):
            cser.link_set('second', None, f'{self.SERIES_ID_SECOND_V1}', True)
        self.assertEqual(
            "Setting link for series 'second' v1 to 456",
            out.getvalue().splitlines()[-1])

        # Make sure that the link was set
        series = patchstream.get_metadata('second', 0, count,
                                          git_dir=self.gitdir)
        self.assertEqual(f'1:{self.SERIES_ID_SECOND_V1}', series.links)

        with terminal.capture():
            cser.increment('second')

        # Make sure that the new series gets the same link
        series = patchstream.get_metadata('second2', 0, 3,
                                          git_dir=self.gitdir)

        pwork = Patchwork.for_testing(self._fake_patchwork_cser)
        pwork.project_set(self.PROJ_ID, self.PROJ_LINK_NAME)
        self.assertFalse(cser.project_get())
        cser.project_set(pwork, 'U-Boot', quiet=True)

        with terminal.capture():
            self.assertEqual(
                (self.SERIES_ID_SECOND_V1, None, 'second', 1,
                 'Series for my board'),
                cser.link_search(pwork, 'second', 1))

        with terminal.capture():
            cser.increment('second')

        with terminal.capture():
            self.assertEqual(
                (457, None, 'second', 2, 'Series for my board'),
                cser.link_search(pwork, 'second', 2))

    def test_series_link_auto_renamed(self):
        """autolink uses the live cover title, not a stale stored desc

        When a later version renames the series (changes the cover-letter
        title) after the version was created, the per-version description
        stored at creation time goes stale. link_search must use the
        current cover title from the branch so it searches patchwork for
        the right name.
        """
        cser = self.get_cser()
        with terminal.capture():
            cser.add('second', allow_unmarked=True)
            cser.link_set('second', None, f'{self.SERIES_ID_SECOND_V1}', True)
            cser.increment('second')

        pwork = Patchwork.for_testing(self._fake_patchwork_cser)
        pwork.project_set(self.PROJ_ID, self.PROJ_LINK_NAME)
        cser.project_set(pwork, 'U-Boot', quiet=True)

        # Simulate the rename: the stored v2 description no longer matches
        # the cover-letter title on the branch
        ser = cser.get_series_by_name('second')
        svid = cser.get_series_svid(ser.idnum, 2)
        cser.db.ser_ver_set_desc(svid, 'Stale old title')
        cser.commit()

        with terminal.capture():
            _, _, _, _, desc = cser.link_search(pwork, 'second', 2)
        self.assertEqual(self.TITLE_SECOND, desc)

    def test_series_link_auto_name(self):
        """Test finding the patchwork link for a cseries with auto name"""
        cser = self.get_cser()

        with terminal.capture() as (out, _):
            cser.add('first', '', allow_unmarked=True)

        # Set link with detected name
        with self.assertRaises(ValueError) as exc:
            cser.link_set(None, 2, '2345', True)
        self.assertEqual(
            "Series 'first' does not have a version 2", str(exc.exception))

        with terminal.capture():
            cser.increment('first')

        with terminal.capture() as (out, _):
            cser.link_set(None, 2, '2345', True)
        self.assertEqual(
                "Setting link for series 'first' v2 to 2345",
                out.getvalue().splitlines()[-1])

        svlist = cser.get_ser_ver_list()
        self.assertEqual(2, len(svlist))
        self.assertEqual(1, svlist[0].idnum)
        self.assertEqual(1, svlist[0].series_id)
        self.assertEqual(1, svlist[0].version)
        self.assertIsNone(svlist[0].link)

        self.assertEqual(2, svlist[1].idnum)
        self.assertEqual(1, svlist[1].series_id)
        self.assertEqual(2, svlist[1].version)
        self.assertEqual('2345', svlist[1].link)

    def test_series_link_auto_name_version(self):
        """Find patchwork link for a cseries with auto name + version"""
        cser = self.get_cser()

        with terminal.capture() as (out, _):
            cser.add('first', '', allow_unmarked=True)

        # Set link with detected name and version
        with terminal.capture() as (out, _):
            cser.link_set(None, None, '1234', True)
        self.assertEqual(
                "Setting link for series 'first' v1 to 1234",
                out.getvalue().splitlines()[-1])

        with terminal.capture():
            cser.increment('first')

        with terminal.capture() as (out, _):
            cser.link_set(None, None, '2345', True)
        self.assertEqual(
                "Setting link for series 'first' v2 to 2345",
                out.getvalue().splitlines()[-1])

        svlist = cser.get_ser_ver_list()
        self.assertEqual(2, len(svlist))
        self.assertEqual(1, svlist[0].idnum)
        self.assertEqual(1, svlist[0].series_id)
        self.assertEqual(1, svlist[0].version)
        self.assertEqual('1234', svlist[0].link)

        self.assertEqual(2, svlist[1].idnum)
        self.assertEqual(1, svlist[1].series_id)
        self.assertEqual(2, svlist[1].version)
        self.assertEqual('2345', svlist[1].link)

    def test_series_link_missing(self):
        """Test finding patchwork link for a cseries but it is missing"""
        cser = self.get_cser()

        with terminal.capture():
            cser.add('second', allow_unmarked=True)

        with terminal.capture():
            cser.increment('second')
            cser.increment('second')

        pwork = Patchwork.for_testing(self._fake_patchwork_cser)
        pwork.project_set(self.PROJ_ID, self.PROJ_LINK_NAME)
        self.assertFalse(cser.project_get())
        cser.project_set(pwork, 'U-Boot', quiet=True)

        with terminal.capture():
            self.assertEqual(
                (self.SERIES_ID_SECOND_V1, None, 'second', 1,
                 'Series for my board'),
                cser.link_search(pwork, 'second', 1))
            self.assertEqual(
                (457, None, 'second', 2, 'Series for my board'),
                cser.link_search(pwork, 'second', 2))
            res = cser.link_search(pwork, 'second', 3)
        self.assertEqual(
            (None,
             [{'id': self.SERIES_ID_SECOND_V1, 'name': 'Series for my board',
               'version': 1},
              {'id': 457, 'name': 'Series for my board', 'version': 2}],
             'second', 3, 'Series for my board'),
            res)

    def check_series_autolink(self):
        """Common code for autolink tests"""
        cser = self.get_cser()

        with self.stage('setup'):
            pwork = Patchwork.for_testing(self._fake_patchwork_cser)
            pwork.project_set(self.PROJ_ID, self.PROJ_LINK_NAME)
            self.assertFalse(cser.project_get())
            cser.project_set(pwork, 'U-Boot', quiet=True)

            with terminal.capture():
                cser.add('first', '', allow_unmarked=True)
                cser.add('second', allow_unmarked=True)

        with self.stage('autolink unset'):
            with terminal.capture() as (out, _):
                yield cser, pwork
            self.assertEqual(
                "Setting link for series 'second' v1 to "
                f'{self.SERIES_ID_SECOND_V1}',
                out.getvalue().splitlines()[-1])

        svlist = cser.get_ser_ver_list()
        self.assertEqual(2, len(svlist))
        self.assertEqual(1, svlist[0].idnum)
        self.assertEqual(1, svlist[0].series_id)
        self.assertEqual(1, svlist[0].version)
        self.assertEqual(2, svlist[1].idnum)
        self.assertEqual(2, svlist[1].series_id)
        self.assertEqual(1, svlist[1].version)
        self.assertEqual(str(self.SERIES_ID_SECOND_V1), svlist[1].link)
        yield None

    def test_series_autolink(self):
        """Test linking a cseries to its patchwork series by description"""
        cor = self.check_series_autolink()
        cser, pwork = next(cor)

        with self.assertRaises(ValueError) as exc:
            cser.link_auto(pwork, 'first', None, True)
        self.assertIn("Series 'first' has an empty description",
                      str(exc.exception))

        # autolink unset
        cser.link_auto(pwork, 'second', None, True)

        self.assertFalse(next(cor))
        cor.close()

    def test_series_autolink_cmdline(self):
        """Test linking to patchwork series by description on cmdline"""
        cor = self.check_series_autolink()
        _, pwork = next(cor)

        with terminal.capture() as (out, _):
            self.run_args('series', '-s', 'first', 'autolink', expect_ret=1,
                          pwork=pwork)
        self.assertEqual(
            "patman: ValueError: Series 'first' has an empty description",
            out.getvalue().strip())

        # autolink unset
        self.run_args('series', '-s', 'second', 'autolink', '-u', pwork=pwork)

        self.assertFalse(next(cor))
        cor.close()

    def _autolink_setup(self):
        """Set things up for autolink tests

        Return: tuple:
            Cseries object
            Patchwork object
        """
        cser = self.get_cser()

        pwork = Patchwork.for_testing(self._fake_patchwork_cser)
        pwork.project_set(self.PROJ_ID, self.PROJ_LINK_NAME)
        self.assertFalse(cser.project_get())
        cser.project_set(pwork, 'U-Boot', quiet=True)

        with terminal.capture():
            cser.add('first', 'first series', allow_unmarked=True)
            cser.add('second', allow_unmarked=True)
            cser.increment('first')
        return cser, pwork

    def test_series_link_auto_all(self):
        """Test linking all cseries to their patchwork series by description"""
        cser, pwork = self._autolink_setup()
        with terminal.capture() as (out, _):
            summary = cser.link_auto_all(pwork, update_commit=True,
                                         link_all_versions=True,
                                         replace_existing=False, dry_run=True,
                                         show_summary=False)
        self.assertEqual(3, len(summary))
        items = iter(summary.values())
        linked = next(items)
        self.assertEqual(
            ('first', 1, None, 'first series', 'linked:1234'), linked)
        self.assertEqual(
            ('first', 2, None, 'first series', 'not found'), next(items))
        self.assertEqual(
            ('second', 1, f'{self.SERIES_ID_SECOND_V1}', 'Series for my board',
             f'already:{self.SERIES_ID_SECOND_V1}'),
            next(items))
        self.assertEqual('Dry run completed', out.getvalue().splitlines()[-1])

        # A second dry run should do exactly the same thing
        with terminal.capture() as (out2, _):
            summary2 = cser.link_auto_all(pwork, update_commit=True,
                                          link_all_versions=True,
                                          replace_existing=False, dry_run=True,
                                          show_summary=False)
        self.assertEqual(out.getvalue(), out2.getvalue())
        self.assertEqual(summary, summary2)

        # Now do it for real
        with terminal.capture():
            summary = cser.link_auto_all(pwork, update_commit=True,
                                         link_all_versions=True,
                                         replace_existing=False, dry_run=False,
                                         show_summary=False)

        # Check the link was updated
        pdict = cser.get_ser_ver_dict()
        svid = list(summary)[0]
        self.assertEqual('1234', pdict[svid].link)

        series = patchstream.get_metadata_for_list('first', self.gitdir, 2)
        self.assertEqual('1:1234', series.links)

    def test_series_autolink_latest(self):
        """Test linking the lastest versions"""
        cser, pwork = self._autolink_setup()
        with terminal.capture():
            summary = cser.link_auto_all(pwork, update_commit=True,
                                         link_all_versions=False,
                                         replace_existing=False, dry_run=False,
                                         show_summary=False)
        self.assertEqual(2, len(summary))
        items = iter(summary.values())
        self.assertEqual(
            ('first', 2, None, 'first series', 'not found'), next(items))
        self.assertEqual(
            ('second', 1, f'{self.SERIES_ID_SECOND_V1}', 'Series for my board',
             f'already:{self.SERIES_ID_SECOND_V1}'),
            next(items))

    def test_series_autolink_no_update(self):
        """Test linking the lastest versions without updating commits"""
        cser, pwork = self._autolink_setup()
        with terminal.capture():
            cser.link_auto_all(pwork, update_commit=False,
                               link_all_versions=True, replace_existing=False,
                               dry_run=False,
                               show_summary=False)

        series = patchstream.get_metadata_for_list('first', self.gitdir, 2)
        self.assertNotIn('links', series)

    def test_series_autolink_replace(self):
        """Test linking the lastest versions without updating commits"""
        cser, pwork = self._autolink_setup()
        with terminal.capture():
            summary = cser.link_auto_all(pwork, update_commit=True,
                                         link_all_versions=True,
                                         replace_existing=True, dry_run=False,
                                         show_summary=False)
        self.assertEqual(3, len(summary))
        items = iter(summary.values())
        linked = next(items)
        self.assertEqual(
            ('first', 1, None, 'first series', 'linked:1234'), linked)
        self.assertEqual(
            ('first', 2, None, 'first series', 'not found'), next(items))
        self.assertEqual(
            ('second', 1, f'{self.SERIES_ID_SECOND_V1}', 'Series for my board',
             f'linked:{self.SERIES_ID_SECOND_V1}'),
            next(items))

    def test_series_autolink_extra(self):
        """Test command-line operation

        This just uses mocks for now since we can rely on the direct tests for
        the actual operation.
        """
        _, pwork = self._autolink_setup()
        with (mock.patch.object(cseries.Cseries, 'link_auto_all',
                                return_value=None) as method):
            self.run_args('series', 'autolink-all', pwork=True)
        method.assert_called_once_with(True, update_commit=True,
                                       link_all_versions=False,
                                       replace_existing=False, dry_run=False,
                                       show_summary=True)

        with (mock.patch.object(cseries.Cseries, 'link_auto_all',
                                return_value=None) as method):
            self.run_args('series', 'autolink-all', '-a', pwork=True)
        method.assert_called_once_with(True, update_commit=True,
                                       link_all_versions=True,
                                       replace_existing=False, dry_run=False,
                                       show_summary=True)

        with (mock.patch.object(cseries.Cseries, 'link_auto_all',
                                return_value=None) as method):
            self.run_args('series', 'autolink-all', '-a', '-r', pwork=True)
        method.assert_called_once_with(True, update_commit=True,
                                       link_all_versions=True,
                                       replace_existing=True, dry_run=False,
                                       show_summary=True)

        with (mock.patch.object(cseries.Cseries, 'link_auto_all',
                                return_value=None) as method):
            self.run_args('series', '-n', 'autolink-all', '-r', pwork=True)
        method.assert_called_once_with(True, update_commit=True,
                                       link_all_versions=False,
                                       replace_existing=True, dry_run=True,
                                       show_summary=True)

        with (mock.patch.object(cseries.Cseries, 'link_auto_all',
                                return_value=None) as method):
            self.run_args('series', 'autolink-all', '--no-update', pwork=True)
        method.assert_called_once_with(True, update_commit=False,
                                       link_all_versions=False,
                                       replace_existing=False, dry_run=False,
                                       show_summary=True)

        # Now do a real one to check the patchwork handling and output
        with terminal.capture() as (out, _):
            self.run_args('series', 'autolink-all', '-a', '--no-update',
                          pwork=pwork)
        lines = [ln for ln in out.getvalue().splitlines()
                 if not ln.startswith('Searching for ')]
        itr = iter(lines)
        self.assertEqual(
            '1 series linked, 1 already linked, 1 not found (3 requests)',
            next(itr))
        self.assertEqual('', next(itr))
        self.assertEqual(
            'Name             Version  Description                            '
            '   Result', next(itr))
        self.assertTrue(next(itr).startswith('--'))
        self.assertEqual(
            'first                  1  first series                           '
            '   linked:1234', next(itr))
        self.assertEqual(
            'first                  2  first series                           '
            '   not found', next(itr))
        self.assertEqual(
            'second                 1  Series for my board                   '
            f'    already:{self.SERIES_ID_SECOND_V1}',
            next(itr))
        self.assertTrue(next(itr).startswith('--'))
        self.assert_finished(itr)

    def check_series_archive(self):
        """Coroutine to run the archive test"""
        cser = self.get_cser()
        with self.stage('setup'):
            with terminal.capture():
                cser.add('first', '', allow_unmarked=True)

            # Check the series is visible in the list
            slist = cser.db.series_get_dict()
            self.assertEqual(1, len(slist))
            self.assertEqual('first', slist['first'].name)

            # Add a second branch
            with terminal.capture():
                cser.increment('first')

        cser.fake_now = datetime(24, 9, 14)
        repo = pygit2.init_repository(self.gitdir)
        with self.stage('archive'):
            expected_commit1 = repo.revparse_single('first')
            expected_commit2 = repo.revparse_single('first2')
            expected_tag1 = 'first-14sep24'
            expected_tag2 = 'first2-14sep24'

            # Archive it and make sure it is invisible
            yield cser
            slist = cser.db.series_get_dict()
            self.assertFalse(slist)

            # ...unless we include archived items
            slist = cser.db.series_get_dict(include_archived=True)
            self.assertEqual(1, len(slist))
            first = slist['first']
            self.assertEqual('first', first.name)

            # Make sure the branches have been tagged
            svlist = cser.db.ser_ver_get_for_series(first.idnum)
            self.assertEqual(expected_tag1, svlist[0].archive_tag)
            self.assertEqual(expected_tag2, svlist[1].archive_tag)

            # Check that the tags were created and point to old branch commits
            target1 = repo.revparse_single(expected_tag1)
            self.assertEqual(expected_commit1, target1.get_object())
            target2 = repo.revparse_single(expected_tag2)
            self.assertEqual(expected_commit2, target2.get_object())

            # The branches should be deleted
            self.assertFalse('first' in repo.branches)
            self.assertFalse('first2' in repo.branches)

        with self.stage('unarchive'):
            # or we unarchive it
            yield cser
            slist = cser.db.series_get_dict()
            self.assertEqual(1, len(slist))

            # Make sure the branches have been restored
            branch1 = repo.branches['first']
            branch2 = repo.branches['first2']
            self.assertEqual(expected_commit1.id, branch1.target)
            self.assertEqual(expected_commit2.id, branch2.target)

            # Make sure the tags were deleted
            try:
                target1 = repo.revparse_single(expected_tag1)
                self.fail('target1 is still present')
            except KeyError:
                pass
            try:
                target1 = repo.revparse_single(expected_tag2)
                self.fail('target2 is still present')
            except KeyError:
                pass

            # Make sure the tag information has been removed
            svlist = cser.db.ser_ver_get_for_series(first.idnum)
            self.assertFalse(svlist[0].archive_tag)
            self.assertFalse(svlist[1].archive_tag)

        yield False

    def test_series_archive(self):
        """Test marking a series as archived"""
        cor = self.check_series_archive()
        cser = next(cor)

        # Archive it and make sure it is invisible
        with terminal.capture():
            cser.archive('first')
        cser = next(cor)
        with terminal.capture():
            cser.unarchive('first')
        self.assertFalse(next(cor))
        cor.close()

    def test_series_archive_cmdline(self):
        """Test marking a series as archived with cmdline"""
        cor = self.check_series_archive()
        cser = next(cor)

        # Archive it and make sure it is invisible
        with terminal.capture():
            self.run_args('series', '-s', 'first', 'archive', pwork=True,
                          cser=cser)
        next(cor)
        with terminal.capture():
            self.run_args('series', '-s', 'first', 'unarchive', pwork=True,
                          cser=cser)
        self.assertFalse(next(cor))
        cor.close()

    def check_series_inc(self):
        """Coroutine to run the increment test"""
        cser = self.get_cser()

        with self.stage('setup'):
            gitutil.checkout('first', self.gitdir, work_tree=self.tmpdir,
                             force=True)
            with terminal.capture() as (out, _):
                cser.add('first', '', allow_unmarked=True)

        with self.stage('increment'):
            with terminal.capture() as (out, _):
                yield cser
            self._check_inc(out)

            slist = cser.db.series_get_dict()
            self.assertEqual(1, len(slist))

            svlist = cser.get_ser_ver_list()
            self.assertEqual(2, len(svlist))
            self.assertEqual(1, svlist[0].idnum)
            self.assertEqual(1, svlist[0].series_id)
            self.assertEqual(1, svlist[0].version)

            self.assertEqual(2, svlist[1].idnum)
            self.assertEqual(1, svlist[1].series_id)
            self.assertEqual(2, svlist[1].version)

            series = patchstream.get_metadata_for_list('first2', self.gitdir,
                                                       1)
            self.assertEqual('2', series.version)

            series = patchstream.get_metadata_for_list('first', self.gitdir, 1)
            self.assertNotIn('version', series)

            self.assertEqual('first2', gitutil.get_branch(self.gitdir))
        yield None

    def test_series_inc(self):
        """Test incrementing the version"""
        cor = self.check_series_inc()
        cser = next(cor)

        cser.increment('first')
        self.assertFalse(next(cor))

        cor.close()

    def test_series_inc_cmdline(self):
        """Test incrementing the version with cmdline"""
        cor = self.check_series_inc()
        next(cor)

        self.run_args('series', '-s', 'first', 'inc', pwork=True)
        self.assertFalse(next(cor))
        cor.close()

    def test_series_inc_no_upstream(self):
        """Increment a series which has no upstream branch"""
        cser = self.get_cser()

        gitutil.checkout('first', self.gitdir, work_tree=self.tmpdir,
                         force=True)
        with terminal.capture():
            cser.add('first', '', allow_unmarked=True)

        repo = pygit2.init_repository(self.gitdir)
        upstream = repo.lookup_branch('base')
        upstream.delete()
        with terminal.capture():
            cser.increment('first')

        slist = cser.db.series_get_dict()
        self.assertEqual(1, len(slist))

    def test_series_inc_dryrun(self):
        """Test incrementing the version with cmdline"""
        cser = self.get_cser()

        gitutil.checkout('first', self.gitdir, work_tree=self.tmpdir,
                         force=True)
        with terminal.capture() as (out, _):
            cser.add('first', '', allow_unmarked=True)

        with terminal.capture() as (out, _):
            cser.increment('first', dry_run=True)
        itr = self._check_inc(out)
        self.assertEqual('Dry run completed', next(itr))

        # Make sure that nothing was added
        svlist = cser.get_ser_ver_list()
        self.assertEqual(1, len(svlist))
        self.assertEqual(1, svlist[0].idnum)
        self.assertEqual(1, svlist[0].series_id)
        self.assertEqual(1, svlist[0].version)

        # We should still be on the same branch
        self.assertEqual('first', gitutil.get_branch(self.gitdir))

    def test_series_dec(self):
        """Test decrementing the version"""
        cser = self.get_cser()

        gitutil.checkout('first', self.gitdir, work_tree=self.tmpdir,
                         force=True)
        with terminal.capture() as (out, _):
            cser.add('first', '', allow_unmarked=True)

        pclist = cser.get_pcommit_dict()
        self.assertEqual(2, len(pclist))

        # Try decrementing when there is only one version
        with self.assertRaises(ValueError) as exc:
            cser.decrement('first')
        self.assertEqual("Series 'first' only has one version",
                         str(exc.exception))

        # Add a version; now there should be two
        with terminal.capture() as (out, _):
            cser.increment('first')
        svdict = cser.get_ser_ver_dict()
        self.assertEqual(2, len(svdict))

        pclist = cser.get_pcommit_dict()
        self.assertEqual(4, len(pclist))

        # Remove version two, using dry run (i.e. no effect)
        with terminal.capture() as (out, _):
            cser.decrement('first', dry_run=True)
        svdict = cser.get_ser_ver_dict()
        self.assertEqual(2, len(svdict))

        repo = pygit2.init_repository(self.gitdir)
        branch = repo.lookup_branch('first2')
        self.assertTrue(branch)
        branch_oid = branch.peel(pygit2.enums.ObjectType.COMMIT).id

        pclist = cser.get_pcommit_dict()
        self.assertEqual(4, len(pclist))

        # Now remove version two for real
        with terminal.capture() as (out, _):
            cser.decrement('first')
        lines = out.getvalue().splitlines()
        self.assertEqual(3, len(lines))
        self.assertEqual("Removing series 'first' v2", lines[0])
        self.assertEqual(
            f"Deleted branch 'first2' {str(branch_oid)[:10]}", lines[1])
        self.assertEqual("Decremented series 'first' to v1", lines[2])

        svdict = cser.get_ser_ver_dict()
        self.assertEqual(1, len(svdict))

        pclist = cser.get_pcommit_dict()
        self.assertEqual(2, len(pclist))

        branch = repo.lookup_branch('first2')
        self.assertFalse(branch)

        # Removing the only version should not be allowed
        with self.assertRaises(ValueError) as exc:
            cser.decrement('first', dry_run=True)
        self.assertEqual("Series 'first' only has one version",
                         str(exc.exception))

    def test_upstream_add(self):
        """Test adding an upsream"""
        cser = self.get_cser()

        with terminal.capture():
            cser.upstream_add('us', 'https://one')
        ulist = cser.get_upstream_dict()
        self.assertEqual(1, len(ulist))
        self.assertEqual(('https://one', None, None, None, None, 0, 0), ulist['us'])

        with terminal.capture():
            cser.upstream_add('ci', 'git@two')
        ulist = cser.get_upstream_dict()
        self.assertEqual(2, len(ulist))
        self.assertEqual(('https://one', None, None, None, None, 0, 0), ulist['us'])
        self.assertEqual(('git@two', None, None, None, None, 0, 0), ulist['ci'])

        # Try to add a duplicate
        with self.assertRaises(ValueError) as exc:
            cser.upstream_add('ci', 'git@three')
        self.assertEqual("Upstream 'ci' already exists", str(exc.exception))

        with terminal.capture() as (out, _):
            cser.upstream_list()
        lines = out.getvalue().splitlines()
        self.assertEqual(4, len(lines))
        self.assertIn('Name', lines[0])
        self.assertIn('https://one', lines[2])
        self.assertIn('git@two', lines[3])

    def test_upstream_add_patchwork_url(self):
        """Test adding an upstream with a patchwork URL"""
        cser = self.get_cser()

        with terminal.capture():
            cser.upstream_add('us', 'https://one',
                              patchwork_url='https://pw.example.com')
        ulist = cser.get_upstream_dict()
        self.assertEqual(1, len(ulist))
        self.assertEqual(
            ('https://one', None, 'https://pw.example.com', None, None, 0, 0),
            ulist['us'])

        # Check that the patchwork URL shows in the list
        with terminal.capture() as (out, _):
            cser.upstream_list()
        lines = out.getvalue().splitlines()
        self.assertEqual(3, len(lines))
        self.assertIn('pw:https://pw.example.com', lines[2])

        # Check database lookup
        pw_url = cser.db.upstream_get_patchwork_url('us')
        self.assertEqual('https://pw.example.com', pw_url)

        # Non-existent upstream returns None
        pw_url = cser.db.upstream_get_patchwork_url('nonexistent')
        self.assertIsNone(pw_url)

    def test_upstream_add_cmdline(self):
        """Test adding an upsream with cmdline"""
        with terminal.capture():
            self.run_args('upstream', 'add', 'us', 'https://one')

        with terminal.capture() as (out, _):
            self.run_args('upstream', 'list')
        lines = out.getvalue().splitlines()
        self.assertEqual(3, len(lines))
        self.assertIn('us', lines[2])
        self.assertIn('https://one', lines[2])

    def test_upstream_set(self):
        """Test updating settings on an existing upstream"""
        cser = self.get_cser()

        with terminal.capture():
            cser.upstream_add('us', 'https://one')

        # Set identity and series_to
        with terminal.capture():
            cser.upstream_set('us', identity='chromium', series_to='concept')
        settings = cser.db.upstream_get_send_settings('us')
        self.assertEqual('chromium', settings[0])
        self.assertEqual('concept', settings[1])

        # Set boolean flags
        with terminal.capture():
            cser.upstream_set('us', no_maintainers=True, no_tags=True)
        settings = cser.db.upstream_get_send_settings('us')
        self.assertTrue(settings[2])
        self.assertTrue(settings[3])

        # Clear boolean flags
        with terminal.capture():
            cser.upstream_set('us', no_maintainers=False, no_tags=False)
        settings = cser.db.upstream_get_send_settings('us')
        self.assertFalse(settings[2])
        self.assertFalse(settings[3])

        # Non-existent upstream
        with self.assertRaises(ValueError) as exc:
            cser.upstream_set('nonexistent', identity='x')
        self.assertIn('nonexistent', str(exc.exception))

    def test_upstream_set_cmdline(self):
        """Test upstream set via the command line"""
        with terminal.capture():
            self.run_args('upstream', 'add', 'us', 'https://one')

        with terminal.capture():
            self.run_args('upstream', 'set', 'us', '-I', 'chromium',
                          '-t', 'concept', '-m', '--no-tags')

        with terminal.capture() as (out, _):
            self.run_args('upstream', 'list')
        line = out.getvalue().strip()
        self.assertIn('id:chromium', line)
        self.assertIn('to:concept', line)
        self.assertIn('no-maintainers', line)
        self.assertIn('no-tags', line)

    def test_upstream_default(self):
        """Operation of the default upstream"""
        cser = self.get_cser()

        with self.assertRaises(ValueError) as exc:
            cser.upstream_set_default('us')
        self.assertEqual("No such upstream 'us'", str(exc.exception))

        with terminal.capture():
            cser.upstream_add('us', 'https://one')
            cser.upstream_add('ci', 'git@two')

        self.assertIsNone(cser.upstream_get_default())

        with terminal.capture():
            cser.upstream_set_default('us')
        self.assertEqual('us', cser.upstream_get_default())

        with terminal.capture():
            cser.upstream_set_default('us')

        with terminal.capture():
            cser.upstream_set_default('ci')
        self.assertEqual('ci', cser.upstream_get_default())

        with terminal.capture() as (out, _):
            cser.upstream_list()
        lines = out.getvalue().splitlines()
        self.assertEqual(4, len(lines))
        self.assertNotIn('*', lines[2])
        self.assertIn('*', lines[3])

        cser.upstream_set_default(None)
        self.assertIsNone(cser.upstream_get_default())

    def test_upstream_default_cmdline(self):
        """Operation of the default upstream on cmdline"""
        with terminal.capture() as (out, _):
            self.run_args('upstream', 'default', 'us', expect_ret=1)
        self.assertEqual("patman: ValueError: No such upstream 'us'",
                         out.getvalue().strip().splitlines()[-1])

        with terminal.capture():
            self.run_args('upstream', 'add', 'us', 'https://one')
            self.run_args('upstream', 'add', 'ci', 'git@two')

        with terminal.capture() as (out, _):
            self.run_args('upstream', 'default')
        self.assertEqual('unset', out.getvalue().strip())

        with terminal.capture():
            self.run_args('upstream', 'default', 'us')
        with terminal.capture() as (out, _):
            self.run_args('upstream', 'default')
        self.assertEqual('us', out.getvalue().strip())

        with terminal.capture():
            self.run_args('upstream', 'default', 'ci')
        with terminal.capture() as (out, _):
            self.run_args('upstream', 'default')
        self.assertEqual('ci', out.getvalue().strip())

        with terminal.capture() as (out, _):
            self.run_args('upstream', 'default', '--unset')
        self.assertFalse(out.getvalue().strip())

        with terminal.capture() as (out, _):
            self.run_args('upstream', 'default')
        self.assertEqual('unset', out.getvalue().strip())

    def test_upstream_delete(self):
        """Test operation of the default upstream"""
        cser = self.get_cser()

        with self.assertRaises(ValueError) as exc:
            cser.upstream_delete('us')
        self.assertEqual("No such upstream 'us'", str(exc.exception))

        with terminal.capture():
            cser.upstream_add('us', 'https://one')
            cser.upstream_add('ci', 'git@two')

        with terminal.capture():
            cser.upstream_set_default('us')
            cser.upstream_delete('us')
        self.assertIsNone(cser.upstream_get_default())

        with terminal.capture():
            cser.upstream_delete('ci')
        ulist = cser.get_upstream_dict()
        self.assertFalse(ulist)

    def test_upstream_delete_cmdline(self):
        """Test deleting an upstream"""
        with terminal.capture() as (out, _):
            self.run_args('upstream', 'delete', 'us', expect_ret=1)
        self.assertEqual("patman: ValueError: No such upstream 'us'",
                         out.getvalue().strip().splitlines()[-1])

        with terminal.capture():
            self.run_args('us', 'add', 'us', 'https://one')
            self.run_args('us', 'add', 'ci', 'git@two')

        with terminal.capture():
            self.run_args('upstream', 'default', 'us')
            self.run_args('upstream', 'delete', 'us')
        with terminal.capture() as (out, _):
            self.run_args('upstream', 'default', 'us', expect_ret=1)
        self.assertEqual("patman: ValueError: No such upstream 'us'",
                         out.getvalue().strip())

        with terminal.capture():
            self.run_args('upstream', 'delete', 'ci')
        with terminal.capture() as (out, _):
            self.run_args('upstream', 'list')
        lines = out.getvalue().splitlines()
        self.assertEqual(2, len(lines))

    def test_series_upstream(self):
        """Test upstream field in the series table"""
        cser = self.get_cser()

        # Add a series without upstream
        cser.db.series_add('first', 'my desc')
        cser.db.commit()
        slist = cser.db.series_get_dict()
        self.assertIsNone(slist['first'].upstream)

        # Add a series with upstream
        cser.db.series_add('second', 'desc2', ups='us')
        cser.db.commit()
        slist = cser.db.series_get_dict()
        self.assertIsNone(slist['first'].upstream)
        self.assertEqual('us', slist['second'].upstream)

        # Update upstream on existing series
        idnum = cser.db.series_find_by_name('first')
        cser.db.series_set_upstream(idnum, 'ci')
        cser.db.commit()
        slist = cser.db.series_get_dict()
        self.assertEqual('ci', slist['first'].upstream)

        # Clear upstream
        cser.db.series_set_upstream(idnum, None)
        cser.db.commit()
        slist = cser.db.series_get_dict()
        self.assertIsNone(slist['first'].upstream)

    def test_series_set_upstream(self):
        """Test setting upstream via the set-upstream command"""
        cser = self.get_cser()
        with terminal.capture():
            cser.add('first', '', allow_unmarked=True)

        self.db_close()
        with terminal.capture() as (out, _):
            self.run_args('series', '-s', 'first', 'set-upstream',
                          'origin')
        self.assertIn("Set upstream for series 'first' to 'origin'",
                      out.getvalue())

        self.db_open()
        slist = cser.db.series_get_dict()
        self.assertEqual('origin', slist['first'].upstream)

    def test_series_add_mark(self):
        """Test marking a cseries with Change-Id fields"""
        cser = self.get_cser()

        with terminal.capture():
            cser.add('first', '', mark=True)

        pcdict = cser.get_pcommit_dict()

        series = patchstream.get_metadata('first', 0, 2, git_dir=self.gitdir)
        self.assertEqual(2, len(series.commits))
        self.assertIn(1, pcdict)
        self.assertEqual(1, pcdict[1].idnum)
        self.assertEqual('i2c: I2C things', pcdict[1].subject)
        self.assertEqual(1, pcdict[1].svid)
        self.assertEqual(series.commits[0].change_id, pcdict[1].change_id)

        self.assertIn(2, pcdict)
        self.assertEqual(2, pcdict[2].idnum)
        self.assertEqual('spi: SPI fixes', pcdict[2].subject)
        self.assertEqual(1, pcdict[2].svid)
        self.assertEqual(series.commits[1].change_id, pcdict[2].change_id)

    def test_series_add_mark_fail(self):
        """Test marking a cseries when the tree is dirty"""
        cser = self.get_cser()

        tools.write_file(os.path.join(self.tmpdir, 'fname'), b'123')
        with terminal.capture():
            cser.add('first', '', mark=True)

        tools.write_file(os.path.join(self.tmpdir, 'i2c.c'), b'123')
        with self.assertRaises(ValueError) as exc:
            with terminal.capture():
                cser.add('first', '', mark=True)
        self.assertEqual(
            "Modified files exist: use 'git status' to check: [' M i2c.c']",
            str(exc.exception))

    def test_series_add_mark_dry_run(self):
        """Test marking a cseries with Change-Id fields"""
        cser = self.get_cser()

        with terminal.capture() as (out, _):
            cser.add('first', '', mark=True, dry_run=True)
        itr = iter(out.getvalue().splitlines())
        self.assertEqual(
            "Adding series 'first' v1: mark True allow_unmarked False",
            next(itr))
        self.assertRegex(
            next(itr), 'Checking out upstream commit refs/heads/base: .*')
        self.assertEqual("Processing 2 commits from branch 'first'",
                         next(itr))
        self.assertRegex(
            next(itr), f'- marked: {HASH_RE} as {HASH_RE} i2c: I2C things')
        self.assertRegex(
            next(itr), f'- marked: {HASH_RE} as {HASH_RE} spi: SPI fixes')
        self.assertRegex(
            next(itr), f'Updating branch first from {HASH_RE} to {HASH_RE}')
        self.assertEqual("Added series 'first' v1 (2 commits)",
                         next(itr))
        self.assertEqual('Dry run completed', next(itr))

        # Doing another dry run should produce the same result
        with terminal.capture() as (out2, _):
            cser.add('first', '', mark=True, dry_run=True)
        self.assertEqual(out.getvalue(), out2.getvalue())

        tools.write_file(os.path.join(self.tmpdir, 'i2c.c'), b'123')
        with terminal.capture() as (out, _):
            with self.assertRaises(ValueError) as exc:
                cser.add('first', '', mark=True, dry_run=True)
        self.assertEqual(
            "Modified files exist: use 'git status' to check: [' M i2c.c']",
            str(exc.exception))

        pcdict = cser.get_pcommit_dict()
        self.assertFalse(pcdict)

    def test_series_add_mark_cmdline(self):
        """Test marking a cseries with Change-Id fields using the cmdline"""
        cser = self.get_cser()

        with terminal.capture():
            self.run_args('series', '-s', 'first', 'add', '-m',
                          '-D', 'my-description', pwork=True)

        pcdict = cser.get_pcommit_dict()
        self.assertTrue(pcdict[1].change_id)
        self.assertTrue(pcdict[2].change_id)

    def test_series_add_unmarked_cmdline(self):
        """Test adding an unmarked cseries using the command line"""
        cser = self.get_cser()

        with terminal.capture():
            self.run_args('series', '-s', 'first', 'add', '-M',
                          '-D', 'my-description', pwork=True)

        pcdict = cser.get_pcommit_dict()
        self.assertFalse(pcdict[1].change_id)
        self.assertFalse(pcdict[2].change_id)

    def test_series_add_unmarked_bad_cmdline(self):
        """Test failure to add an unmarked cseries using a bad command line"""
        self.get_cser()

        with terminal.capture() as (out, _):
            self.run_args('series', '-s', 'first', 'add',
                          '-D', 'my-description', expect_ret=1, pwork=True)
        last_line = out.getvalue().splitlines()[-2]
        self.assertEqual(
            'patman: ValueError: 2 commit(s) are unmarked; '
            'please use -m or -M', last_line)

    def check_series_unmark(self):
        """Checker for unmarking tests"""
        cser = self.get_cser()
        with self.stage('unmarked commits'):
            yield cser

        with self.stage('mark commits'):
            with terminal.capture() as (out, _):
                yield cser

        with self.stage('unmark: dry run'):
            with terminal.capture() as (out, _):
                yield cser

        itr = iter(out.getvalue().splitlines())
        self.assertEqual(
            "Unmarking series 'first': allow_unmarked False",
            next(itr))
        self.assertRegex(
            next(itr), 'Checking out upstream commit refs/heads/base: .*')
        self.assertEqual("Processing 2 commits from branch 'first'",
                         next(itr))
        self.assertRegex(
            next(itr),
            f'- unmarked: {HASH_RE} as {HASH_RE} i2c: I2C things')
        self.assertRegex(
            next(itr),
            f'- unmarked: {HASH_RE} as {HASH_RE} spi: SPI fixes')
        self.assertRegex(
            next(itr), f'Updating branch first from {HASH_RE} to {HASH_RE}')
        self.assertEqual("Unmarked 2 commits in series 'first'", next(itr))
        self.assertEqual('Dry run completed', next(itr))

        with self.stage('unmark'):
            with terminal.capture() as (out, _):
                yield cser
            self.assertIn('- unmarked', out.getvalue())

        with self.stage('unmark: allow unmarked'):
            with terminal.capture() as (out, _):
                yield cser
            self.assertIn('- no mark', out.getvalue())

        yield None

    def test_series_unmark(self):
        """Test unmarking a cseries, i.e. removing Change-Id fields"""
        cor = self.check_series_unmark()
        cser = next(cor)

        # check the allow_unmarked flag
        with terminal.capture():
            with self.assertRaises(ValueError) as exc:
                cser.unmark('first', dry_run=True)
        self.assertEqual('Unmarked commits 2/2', str(exc.exception))

        # mark commits
        cser = next(cor)
        cser.add('first', '', mark=True)

        # unmark: dry run
        cser = next(cor)
        cser.unmark('first', dry_run=True)

        # unmark
        cser = next(cor)
        cser.unmark('first')

        # unmark: allow unmarked
        cser = next(cor)
        cser.unmark('first', allow_unmarked=True)

        self.assertFalse(next(cor))

    def test_series_unmark_cmdline(self):
        """Test the unmark command"""
        cor = self.check_series_unmark()
        next(cor)

        # check the allow_unmarked flag
        with terminal.capture() as (out, _):
            self.run_args('series', 'unmark', expect_ret=1, pwork=True)
        self.assertIn('Unmarked commits 2/2', out.getvalue())

        # mark commits
        next(cor)
        self.run_args('series', '-s', 'first', 'add',  '-D', '', '--mark',
                      pwork=True)

        # unmark: dry run
        next(cor)
        self.run_args('series', '-s', 'first', '-n', 'unmark', pwork=True)

        # unmark
        next(cor)
        self.run_args('series', '-s', 'first', 'unmark', pwork=True)

        # unmark: allow unmarked
        next(cor)
        self.run_args('series', '-s', 'first', 'unmark', '--allow-unmarked',
                      pwork=True)

        self.assertFalse(next(cor))

    def test_series_unmark_middle(self):
        """Test unmarking with Change-Id fields not last in the commit"""
        cser = self.get_cser()
        with terminal.capture():
            cser.add('first', '', allow_unmarked=True)

        # Add some change IDs in the middle of the commit message
        with terminal.capture():
            name, ser, _, _ = cser.prep_series('first')
            old_msgs = []
            for vals in cser.process_series(name, ser):
                old_msgs.append(vals.msg)
                lines = vals.msg.splitlines()
                change_id = cser.make_change_id(vals.commit)
                extra = [f'{cser_helper.CHANGE_ID_TAG}: {change_id}']
                vals.msg = '\n'.join(lines[:2] + extra + lines[2:]) + '\n'

        with terminal.capture():
            cser.unmark('first')

        # We should get back the original commit message
        series = patchstream.get_metadata('first', 0, 2, git_dir=self.gitdir)
        self.assertEqual(old_msgs[0], series.commits[0].msg)
        self.assertEqual(old_msgs[1], series.commits[1].msg)

    def check_series_mark(self):
        """Checker for marking tests"""
        cser = self.get_cser()
        yield cser

        # Start with a dry run, which should do nothing
        with self.stage('dry run'):
            with terminal.capture():
                yield cser

            series = patchstream.get_metadata_for_list('first', self.gitdir, 2)
            self.assertEqual(2, len(series.commits))
            self.assertFalse(series.commits[0].change_id)
            self.assertFalse(series.commits[1].change_id)

        # Now do a real run
        with self.stage('real run'):
            with terminal.capture():
                yield cser

            series = patchstream.get_metadata_for_list('first', self.gitdir, 2)
            self.assertEqual(2, len(series.commits))
            self.assertTrue(series.commits[0].change_id)
            self.assertTrue(series.commits[1].change_id)

        # Try to mark again, which should fail
        with self.stage('mark twice'):
            with terminal.capture():
                with self.assertRaises(ValueError) as exc:
                    cser.mark('first', dry_run=False)
            self.assertEqual('Marked commits 2/2', str(exc.exception))

        # Use the --marked flag to make it succeed
        with self.stage('mark twice with --marked'):
            with terminal.capture():
                yield cser
            self.assertEqual('Marked commits 2/2', str(exc.exception))

            series2 = patchstream.get_metadata_for_list('first', self.gitdir,
                                                        2)
            self.assertEqual(2, len(series2.commits))
            self.assertEqual(series.commits[0].change_id,
                             series2.commits[0].change_id)
            self.assertEqual(series.commits[1].change_id,
                             series2.commits[1].change_id)

        yield None

    def test_series_mark(self):
        """Test marking a cseries, i.e. adding Change-Id fields"""
        cor = self.check_series_mark()
        cser = next(cor)

        # Start with a dry run, which should do nothing
        cser = next(cor)
        cser.mark('first', dry_run=True)

        # Now do a real run
        cser = next(cor)
        cser.mark('first', dry_run=False)

        # Try to mark again, which should fail
        with terminal.capture():
            with self.assertRaises(ValueError) as exc:
                cser.mark('first', dry_run=False)
        self.assertEqual('Marked commits 2/2', str(exc.exception))

        # Use the --allow-marked flag to make it succeed
        cser = next(cor)
        cser.mark('first', allow_marked=True, dry_run=False)

        self.assertFalse(next(cor))

    def test_series_mark_cmdline(self):
        """Test marking a cseries, i.e. adding Change-Id fields"""
        cor = self.check_series_mark()
        next(cor)

        # Start with a dry run, which should do nothing
        next(cor)
        self.run_args('series', '-n', '-s', 'first', 'mark', pwork=True)

        # Now do a real run
        next(cor)
        self.run_args('series', '-s', 'first', 'mark', pwork=True)

        # Try to mark again, which should fail
        with terminal.capture() as (out, _):
            self.run_args('series', '-s', 'first', 'mark', expect_ret=1,
                          pwork=True)
        self.assertIn('Marked commits 2/2', out.getvalue())

        # Use the --allow-marked flag to make it succeed
        next(cor)
        self.run_args('series', '-s', 'first', 'mark', '--allow-marked',
                      pwork=True)
        self.assertFalse(next(cor))

    def test_series_remove(self):
        """Test removing a series"""
        cser = self.get_cser()

        with self.stage('remove non-existent series'):
            with self.assertRaises(ValueError) as exc:
                cser.remove('first')
            self.assertEqual(
                "Series 'first' not found in database; use "
                "'patman series add' first",
                str(exc.exception))

        with self.stage('add'):
            with terminal.capture() as (out, _):
                cser.add('first', '', mark=True)
            self.assertTrue(cser.db.series_get_dict())
            pclist = cser.get_pcommit_dict()
            self.assertEqual(2, len(pclist))

        with self.stage('remove'):
            with terminal.capture() as (out, _):
                cser.remove('first')
            self.assertEqual("Removed series 'first'", out.getvalue().strip())
            self.assertFalse(cser.db.series_get_dict())

            pclist = cser.get_pcommit_dict()
            self.assertFalse(len(pclist))

    def test_series_remove_cmdline(self):
        """Test removing a series using the command line"""
        cser = self.get_cser()

        with self.stage('remove non-existent series'):
            with terminal.capture() as (out, _):
                self.run_args('series', '-s', 'first', 'rm', expect_ret=1,
                              pwork=True)
            self.assertEqual(
                "patman: ValueError: Series 'first' not found in database;"
                " use 'patman series add' first",
                out.getvalue().strip())

        with self.stage('add'):
            with terminal.capture() as (out, _):
                cser.add('first', '', mark=True)
            self.assertTrue(cser.db.series_get_dict())

        with self.stage('remove'):
            with terminal.capture() as (out, _):
                cser.remove('first')
            self.assertEqual("Removed series 'first'", out.getvalue().strip())
            self.assertFalse(cser.db.series_get_dict())

    def check_series_remove_multiple(self):
        """Check for removing a series with more than one version"""
        cser = self.get_cser()

        with self.stage('setup'):
            self.add_first2(True)

            with terminal.capture() as (out, _):
                cser.add(None, '', mark=True)
                cser.add('first', '', mark=True)
            self.assertTrue(cser.db.series_get_dict())
            pclist = cser.get_pcommit_dict()
            self.assertEqual(4, len(pclist))

        # Do a dry-run removal
        with self.stage('dry run'):
            with terminal.capture() as (out, _):
                yield cser
            self.assertEqual("Removed version 1 from series 'first'\n"
                             'Dry run completed', out.getvalue().strip())
            self.assertEqual({'first'}, cser.db.series_get_dict().keys())

            svlist = cser.get_ser_ver_list()
            self.assertEqual(2, len(svlist))
            self.assertEqual(1, svlist[0].idnum)
            self.assertEqual(1, svlist[0].series_id)
            self.assertEqual(2, svlist[0].version)

            self.assertEqual(2, svlist[1].idnum)
            self.assertEqual(1, svlist[1].series_id)
            self.assertEqual(1, svlist[1].version)

        # Now remove for real
        with self.stage('real'):
            with terminal.capture() as (out, _):
                yield cser
            self.assertEqual("Removed version 1 from series 'first'",
                             out.getvalue().strip())
            self.assertEqual({'first'}, cser.db.series_get_dict().keys())
            plist = cser.get_ser_ver_list()
            self.assertEqual(1, len(plist))
            pclist = cser.get_pcommit_dict()
            self.assertEqual(2, len(pclist))

        with self.stage('remove only version'):
            yield cser
            self.assertEqual({'first'}, cser.db.series_get_dict().keys())

            svlist = cser.get_ser_ver_list()
            self.assertEqual(1, len(svlist))
            self.assertEqual(1, svlist[0].idnum)
            self.assertEqual(1, svlist[0].series_id)
            self.assertEqual(2, svlist[0].version)

        with self.stage('remove series (dry run'):
            with terminal.capture() as (out, _):
                yield cser
            self.assertEqual("Removed series 'first'\nDry run completed",
                             out.getvalue().strip())
            self.assertTrue(cser.db.series_get_dict())
            self.assertTrue(cser.get_ser_ver_list())

        with self.stage('remove series'):
            with terminal.capture() as (out, _):
                yield cser
            self.assertEqual("Removed series 'first'", out.getvalue().strip())
            self.assertFalse(cser.db.series_get_dict())
            self.assertFalse(cser.get_ser_ver_list())

        yield False

    def test_series_remove_multiple(self):
        """Test removing a series with more than one version"""
        cor = self.check_series_remove_multiple()
        cser = next(cor)

        # Do a dry-run removal
        cser.version_remove('first', 1, dry_run=True)
        cser = next(cor)

        # Now remove for real
        cser.version_remove('first', 1)
        cser = next(cor)

        # Remove only version
        with self.assertRaises(ValueError) as exc:
            cser.version_remove('first', 2, dry_run=True)
        self.assertEqual(
            "Series 'first' only has one version: remove the series",
            str(exc.exception))
        cser = next(cor)

        # Remove series (dry run)
        cser.remove('first', dry_run=True)
        cser = next(cor)

        # Remove series (real)
        cser.remove('first')

        self.assertFalse(next(cor))
        cor.close()

    def test_series_remove_multiple_cmdline(self):
        """Test removing a series with more than one version on cmdline"""
        cor = self.check_series_remove_multiple()
        next(cor)

        # Do a dry-run removal
        self.run_args('series', '-n', '-s', 'first', '-V', '1', 'rm-version',
                      pwork=True)
        next(cor)

        # Now remove for real
        self.run_args('series', '-s', 'first', '-V', '1', 'rm-version',
                      pwork=True)
        next(cor)

        # Remove only version
        with terminal.capture() as (out, _):
            self.run_args('series', '-n', '-s', 'first', '-V', '2',
                          'rm-version', expect_ret=1, pwork=True)
        self.assertIn(
            "Series 'first' only has one version: remove the series",
            out.getvalue().strip())
        next(cor)

        # Remove series (dry run)
        self.run_args('series', '-n', '-s', 'first', 'rm', pwork=True)
        next(cor)

        # Remove series (real)
        self.run_args('series', '-s', 'first', 'rm', pwork=True)

        self.assertFalse(next(cor))
        cor.close()

    def test_patchwork_set_project(self):
        """Test setting the project ID"""
        cser = self.get_cser()
        pwork = Patchwork.for_testing(self._fake_patchwork_cser)
        with terminal.capture() as (out, _):
            cser.project_set(pwork, 'U-Boot')
        self.assertEqual(
            f"Project 'U-Boot' patchwork-ID {self.PROJ_ID} link-name 'uboot'",
            out.getvalue().strip())

    def test_patchwork_project_get(self):
        """Test setting the project ID"""
        cser = self.get_cser()
        pwork = Patchwork.for_testing(self._fake_patchwork_cser)
        self.assertFalse(cser.project_get())
        with terminal.capture() as (out, _):
            cser.project_set(pwork, 'U-Boot')
        self.assertEqual(
            f"Project 'U-Boot' patchwork-ID {self.PROJ_ID} link-name 'uboot'",
            out.getvalue().strip())

        name, pwid, link_name = cser.project_get()
        self.assertEqual('U-Boot', name)
        self.assertEqual(self.PROJ_ID, pwid)
        self.assertEqual('uboot', link_name)

    def test_patchwork_project_get_cmdline(self):
        """Test setting the project ID"""
        cser = self.get_cser()

        self.assertFalse(cser.project_get())

        cser.db.upstream_add('us', 'https://us.example.com')
        cser.db.commit()

        pwork = Patchwork.for_testing(self._fake_patchwork_cser)
        with terminal.capture() as (out, _):
            self.run_args('-P', 'https://url', 'patchwork', 'set-project',
                          'U-Boot', 'us', pwork=pwork)
        self.assertEqual(
            f"Project 'U-Boot' patchwork-ID {self.PROJ_ID} "
            f"link-name 'uboot' remote 'us'",
            out.getvalue().strip())

        name, pwid, link_name = cser.project_get('us')
        self.assertEqual('U-Boot', name)
        self.assertEqual(6, pwid)
        self.assertEqual('uboot', link_name)

        with terminal.capture() as (out, _):
            self.run_args('-P', 'https://url', 'patchwork', 'get-project',
                          'us')
        self.assertEqual(
            f"Project 'U-Boot' patchwork-ID {self.PROJ_ID} "
            f"link-name 'uboot' remote 'us'",
            out.getvalue().strip())

    def test_patchwork_list(self):
        """Test listing patchwork project configurations"""
        cser = self.get_cser()

        # No projects configured
        with terminal.capture() as (out, _):
            cser.project_list()
        self.assertEqual('No patchwork projects configured',
                         out.getvalue().strip())

        # Add two remotes for U-Boot and one for Linux
        cser.db.upstream_add('us', 'https://us.example.com')
        cser.db.upstream_add('ci', 'https://ci.example.com')
        cser.db.upstream_add('linus', 'https://linus.example.com')
        cser.db.patchwork_update('U-Boot', 6, 'uboot', 'us')
        cser.db.patchwork_update('U-Boot', 6, 'uboot', 'ci')
        cser.db.patchwork_update('Linux', 10, 'linux', 'linus')
        cser.db.commit()

        with terminal.capture() as (out, _):
            cser.project_list()
        lines = out.getvalue().splitlines()
        self.assertEqual(5, len(lines))
        self.assertIn('Linux', lines[2])
        self.assertIn('linus', lines[2])
        self.assertIn('U-Boot', lines[3])
        self.assertIn('ci', lines[3])
        self.assertIn('us', lines[3])

    def test_patchwork_upstream(self):
        """Test patchwork project with upstream association"""
        cser = self.get_cser()

        # Add two upstreams
        cser.db.upstream_add('us', 'https://us.example.com')
        cser.db.upstream_add('ci', 'https://ci.example.com')
        cser.db.commit()

        # Set project for a specific upstream
        cser.db.patchwork_update('U-Boot', 6, 'uboot', 'us')
        cser.db.commit()

        # Look up by upstream
        info = cser.db.patchwork_get('us')
        self.assertEqual(('U-Boot', 6, 'uboot'), info)

        # Different upstream has no project
        self.assertIsNone(cser.db.patchwork_get('ci'))

        # No upstream arg returns any match
        info = cser.db.patchwork_get()
        self.assertEqual(('U-Boot', 6, 'uboot'), info)

        # Set a different project for ci
        cser.db.patchwork_update('Linux', 10, 'linux', 'ci')
        cser.db.commit()

        self.assertEqual(('Linux', 10, 'linux'), cser.db.patchwork_get('ci'))
        self.assertEqual(('U-Boot', 6, 'uboot'), cser.db.patchwork_get('us'))

    def test_patchwork_rm(self):
        """Test deleting a patchwork project configuration"""
        cser = self.get_cser()

        cser.db.upstream_add('us', 'https://us.example.com')
        cser.db.upstream_add('ci', 'https://ci.example.com')
        cser.db.patchwork_update('U-Boot', 6, 'uboot', 'us')
        cser.db.patchwork_update('Linux', 10, 'linux', 'ci')
        cser.db.commit()

        # Delete by upstream name
        cser.db.patchwork_delete('us')
        cser.db.commit()
        self.assertIsNone(cser.db.patchwork_get('us'))
        self.assertEqual(('Linux', 10, 'linux'), cser.db.patchwork_get('ci'))

        # Delete non-existent raises ValueError
        with self.assertRaises(ValueError):
            cser.db.patchwork_delete('us')

    def test_patchwork_rm_default(self):
        """Test deleting the default (no upstream) patchwork project"""
        cser = self.get_cser()

        cser.db.patchwork_update('U-Boot', 6, 'uboot')
        cser.db.commit()
        self.assertIsNotNone(cser.db.patchwork_get())

        cser.db.patchwork_delete(None)
        cser.db.commit()
        self.assertIsNone(cser.db.patchwork_get())

    def test_migrate_patchwork_upstream(self):
        """Test that migrating to v5 renames settings to patchwork"""
        db = database.Database(f'{self.tmpdir}/.patman3.db')
        with terminal.capture():
            db.open_it()

        # Create a v4 database with an upstream and a patchwork row
        with terminal.capture():
            db.migrate_to(4)
        db.execute(
            "INSERT INTO upstream (name, url, is_default) "
            "VALUES ('us', 'https://us.example.com', 1)")
        db.execute(
            "INSERT INTO settings (name, proj_id, link_name) "
            "VALUES ('U-Boot', 6, 'uboot')")
        db.commit()

        # Migrate to v5
        with terminal.capture():
            db.migrate_to(5)

        # The existing row should now be in 'patchwork' with the default upstream
        res = db.execute(
            'SELECT name, proj_id, link_name, upstream FROM patchwork')
        recs = res.fetchall()
        self.assertEqual(1, len(recs))
        self.assertEqual(('U-Boot', 6, 'uboot', 'us'), recs[0])
        db.close()

    def check_series_list_patches(self):
        """Test listing the patches for a series"""
        cser = self.get_cser()

        with self.stage('setup'):
            with terminal.capture() as (out, _):
                cser.add(None, '', allow_unmarked=True)
                cser.add('second', allow_unmarked=True)
                target = self.repo.lookup_reference('refs/heads/second')
                self.repo.checkout(
                    target, strategy=pygit2.enums.CheckoutStrategy.FORCE)
                cser.increment('second')

        with self.stage('list first'):
            with terminal.capture() as (out, _):
                yield cser
            itr = iter(out.getvalue().splitlines())
            self.assertEqual("Branch 'first' (total 2): 2:unknown", next(itr))
            self.assertIn('PatchId', next(itr))
            self.assertRegex(next(itr), r'  0 .* i2c: I2C things')
            self.assertRegex(next(itr), r'  1 .* spi: SPI fixes')

        with self.stage('list second2'):
            with terminal.capture() as (out, _):
                yield cser
            itr = iter(out.getvalue().splitlines())
            self.assertEqual(
                "Branch 'second2' (total 3): 3:unknown", next(itr))
            self.assertIn('PatchId', next(itr))
            self.assertRegex(
                next(itr), '  0 .* video: Some video improvements')
            self.assertRegex(next(itr), '  1 .* serial: Add a serial driver')
            self.assertRegex(next(itr), '  2 .* bootm: Make it boot')

        yield None

    def test_series_list_patches(self):
        """Test listing the patches for a series"""
        cor = self.check_series_list_patches()
        cser = next(cor)

        # list first
        cser.list_patches('first', 1)
        cser = next(cor)

        # list second2
        cser.list_patches('second2', 2)
        self.assertFalse(next(cor))
        cor.close()

    def test_series_list_patches_cmdline(self):
        """Test listing the patches for a series using the cmdline"""
        cor = self.check_series_list_patches()
        next(cor)

        # list first
        self.run_args('series',  '-s', 'first', 'patches', pwork=True)
        next(cor)

        # list second2
        self.run_args('series',  '-s', 'second', '-V', '2', 'patches',
                      pwork=True)
        self.assertFalse(next(cor))
        cor.close()

    def test_series_list_patches_detail(self):
        """Test listing the patches for a series"""
        cser = self.get_cser()
        with terminal.capture():
            cser.add(None, '', allow_unmarked=True)
            cser.add('second', allow_unmarked=True)
            target = self.repo.lookup_reference('refs/heads/second')
            self.repo.checkout(
                target, strategy=pygit2.enums.CheckoutStrategy.FORCE)
            cser.increment('second')

        with terminal.capture() as (out, _):
            cser.list_patches('first', 1, show_commit=True)
        expect = r'''Branch 'first' (total 2): 2:unknown
Seq State      Com PatchId Commit     Subject
  0 unknown      -         .* i2c: I2C things

commit .*
Author: Test user <test@email.com>
Date:   .*

    i2c: I2C things

    This has some stuff to do with I2C

 i2c.c | 2 ++
 1 file changed, 2 insertions(+)


  1 unknown      -         .* spi: SPI fixes

commit .*
Author: Test user <test@email.com>
Date:   .*

    spi: SPI fixes

    SPI needs some fixes
    and here they are

    Signed-off-by: Lord Edmund Blackaddër <weasel@blackadder.org>

    Series-to: u-boot
    Commit-notes:
    title of the series
    This is the cover letter for the series
    with various details
    END

 spi.c | 3 +++
 1 file changed, 3 insertions(+)
'''
        itr = iter(out.getvalue().splitlines())
        for seq, eline in enumerate(expect.splitlines()):
            line = next(itr).rstrip()
            if '*' in eline:
                self.assertRegex(line, eline, f'line {seq + 1}')
            else:
                self.assertEqual(eline, line, f'line {seq + 1}')

        # Show just the patch; this should exclude the commit message
        with terminal.capture() as (out, _):
            cser.list_patches('first', 1, show_patch=True)
        chk = out.getvalue()
        self.assertIn('SPI fixes', chk)                 # subject
        self.assertNotIn('SPI needs some fixes', chk)   # commit body
        self.assertIn('make SPI work', chk)             # patch body

        # Show both
        with terminal.capture() as (out, _):
            cser.list_patches('first', 1, show_commit=True, show_patch=True)
        chk = out.getvalue()
        self.assertIn('SPI fixes', chk)                 # subject
        self.assertIn('SPI needs some fixes', chk)   # commit body
        self.assertIn('make SPI work', chk)             # patch body

    def check_series_gather(self):
        """Checker for gathering tags for a series"""
        cser = self.get_cser()
        with self.stage('setup'):
            pwork = Patchwork.for_testing(self._fake_patchwork_cser)
            self.assertFalse(cser.project_get())
            cser.project_set(pwork, 'U-Boot', quiet=True)

            with terminal.capture() as (out, _):
                cser.add('second', 'description', allow_unmarked=True)

            ser = cser.get_series_by_name('second')
            pwid = cser.get_series_svid(ser.idnum, 1)

        # First do a dry run
        with self.stage('gather: dry run'):
            with terminal.capture() as (out, _):
                yield cser, pwork
            lines = out.getvalue().splitlines()
            self.assertEqual(
                f"Updating series 'second' version 1 from link "
                f"'{self.SERIES_ID_SECOND_V1}'",
                lines[0])
            self.assertEqual('3 patches updated (7 requests)', lines[1])
            self.assertEqual('Dry run completed', lines[2])
            self.assertEqual(3, len(lines))

            pwc = cser.get_pcommit_dict(pwid)
            self.assertIsNone(pwc[0].state)
            self.assertIsNone(pwc[1].state)
            self.assertIsNone(pwc[2].state)

        # Now try it again, gathering tags
        with self.stage('gather: dry run'):
            with terminal.capture() as (out, _):
                yield cser, pwork
            lines = out.getvalue().splitlines()
            itr = iter(lines)
            self.assertEqual(
                f"Updating series 'second' version 1 from link "
                f"'{self.SERIES_ID_SECOND_V1}'",
                next(itr))
            self.assertEqual('  1 video: Some video improvements', next(itr))
            self.assertEqual('  + Reviewed-by: Fred Bloggs <fred@bloggs.com>',
                             next(itr))
            self.assertEqual('  2 serial: Add a serial driver', next(itr))
            self.assertEqual('  3 bootm: Make it boot', next(itr))

            self.assertRegex(
                next(itr), 'Checking out upstream commit refs/heads/base: .*')
            self.assertEqual("Processing 3 commits from branch 'second'",
                             next(itr))
            self.assertRegex(
                next(itr),
                f'- added 1 tag:       {HASH_RE} as {HASH_RE} '
                'video: Some video improvements')
            self.assertRegex(
                next(itr),
                f"- upd links '1:456': {HASH_RE} as {HASH_RE} "
                'serial: Add a serial driver')
            self.assertRegex(
                next(itr),
                f'-                    {HASH_RE} as {HASH_RE} '
                'bootm: Make it boot')
            self.assertRegex(
                next(itr),
                f'Updating branch second from {HASH_RE} to {HASH_RE}')
            self.assertEqual('3 patches updated (7 requests)', next(itr))
            self.assertEqual('Dry run completed', next(itr))
            self.assert_finished(itr)

            # Make sure that no tags were added to the branch
            series = patchstream.get_metadata_for_list('second', self.gitdir,
                                                       3)
            for cmt in series.commits:
                self.assertFalse(cmt.rtags,
                                 'Commit {cmt.subject} rtags {cmt.rtags}')

        # Now do it for real
        with self.stage('gather: real'):
            with terminal.capture() as (out, _):
                yield cser, pwork
            lines2 = out.getvalue().splitlines()
            self.assertEqual(lines2, lines[:-1])

            # Make sure that the tags were added to the branch
            series = patchstream.get_metadata_for_list('second', self.gitdir,
                                                       3)
            self.assertEqual(
                {'Reviewed-by': {'Fred Bloggs <fred@bloggs.com>'}},
                series.commits[0].rtags)
            self.assertFalse(series.commits[1].rtags)
            self.assertFalse(series.commits[2].rtags)

            # Make sure the status was updated
            pwc = cser.get_pcommit_dict(pwid)
            self.assertEqual('accepted', pwc[0].state)
            self.assertEqual('changes-requested', pwc[1].state)
            self.assertEqual('rejected', pwc[2].state)

        yield None

    def test_series_gather(self):
        """Test gathering tags for a series"""
        cor = self.check_series_gather()
        cser, pwork = next(cor)

        # sync (dry_run)
        cser.gather(pwork, 'second', None, False, False, False, dry_run=True)
        cser, pwork = next(cor)

        # gather (dry_run)
        cser.gather(pwork, 'second', None, False, False, True, dry_run=True)
        cser, pwork = next(cor)

        # gather (real)
        cser.gather(pwork, 'second', None, False, False, True)

        self.assertFalse(next(cor))

    def test_series_gather_cmdline(self):
        """Test gathering tags for a series with cmdline"""
        cor = self.check_series_gather()
        _, pwork = next(cor)

        # sync (dry_run)
        self.run_args(
            'series', '-n', '-s', 'second', 'gather', '-G', pwork=pwork)

        # gather (dry_run)
        _, pwork = next(cor)
        self.run_args('series', '-n', '-s', 'second', 'gather', pwork=pwork)

        # gather (real)
        _, pwork = next(cor)
        self.run_args('series', '-s', 'second', 'gather', pwork=pwork)

        self.assertFalse(next(cor))

    def check_series_gather_all(self):
        """Gather all series at once"""
        with self.stage('setup'):
            cser, pwork = self.setup_second(False)

            with terminal.capture():
                cser.add('first', 'description', allow_unmarked=True)
                cser.increment('first')
                cser.increment('first')
                cser.link_set('first', 1, '123', True)
                cser.link_set('first', 2, '1234', True)
                cser.link_set('first', 3, f'{self.SERIES_ID_FIRST_V3}', True)
                cser.link_auto(pwork, 'second', 2, True)

        with self.stage('no options'):
            with terminal.capture() as (out, _):
                yield cser, pwork
            self.assertEqual(
                "Syncing 'first' v3\n"
                "Syncing 'second' v2\n"
                '\n'
                '5 patches and 2 cover letters updated, 0 missing links '
                '(14 requests)\n'
                'Dry run completed',
                out.getvalue().strip())

        with self.stage('gather'):
            with terminal.capture() as (out, _):
                yield cser, pwork
            lines = out.getvalue().splitlines()
            itr = iter(lines)
            self.assertEqual("Syncing 'first' v3", next(itr))
            self.assertEqual('  1 i2c: I2C things', next(itr))
            self.assertEqual(
                '  + Tested-by: Mary Smith <msmith@wibble.com>   # yak',
                next(itr))
            self.assertEqual('  2 spi: SPI fixes', next(itr))
            self.assertRegex(
                next(itr), 'Checking out upstream commit refs/heads/base: .*')
            self.assertEqual(
                "Processing 2 commits from branch 'first3'", next(itr))
            self.assertRegex(
                next(itr),
                f'- added 1 tag:      {HASH_RE} as {HASH_RE} i2c: I2C things')
            self.assertRegex(
                next(itr),
                f"- upd links '3:31': {HASH_RE} as {HASH_RE} spi: SPI fixes")
            self.assertRegex(
                next(itr),
                f'Updating branch first3 from {HASH_RE} to {HASH_RE}')
            self.assertEqual('', next(itr))

            self.assertEqual("Syncing 'second' v2", next(itr))
            self.assertEqual('  1 video: Some video improvements', next(itr))
            self.assertEqual(
                '  + Reviewed-by: Fred Bloggs <fred@bloggs.com>', next(itr))
            self.assertEqual('  2 serial: Add a serial driver', next(itr))
            self.assertEqual('  3 bootm: Make it boot', next(itr))
            self.assertRegex(
                next(itr), 'Checking out upstream commit refs/heads/base: .*')
            self.assertEqual(
                "Processing 3 commits from branch 'second2'", next(itr))
            self.assertRegex(
                next(itr),
                f'- added 1 tag:             {HASH_RE} as {HASH_RE} '
                'video: Some video improvements')
            self.assertRegex(
                next(itr),
                f"- upd links '2:457 1:456': {HASH_RE} as {HASH_RE} "
                'serial: Add a serial driver')
            self.assertRegex(
                next(itr),
                f'-                          {HASH_RE} as {HASH_RE} '
                'bootm: Make it boot')
            self.assertRegex(
                next(itr),
                f'Updating branch second2 from {HASH_RE} to {HASH_RE}')
            self.assertEqual('', next(itr))
            self.assertEqual(
                '5 patches and 2 cover letters updated, 0 missing links '
                '(14 requests)',
                next(itr))
            self.assertEqual('Dry run completed', next(itr))
            self.assert_finished(itr)

        with self.stage('gather, patch comments,!dry_run'):
            with terminal.capture() as (out, _):
                yield cser, pwork
            lines = out.getvalue().splitlines()
            itr = iter(lines)
            self.assertEqual("Syncing 'first' v1", next(itr))
            self.assertEqual('  1 i2c: I2C things', next(itr))
            self.assertEqual(
                '  + Tested-by: Mary Smith <msmith@wibble.com>   # yak',
                next(itr))
            self.assertEqual('  2 spi: SPI fixes', next(itr))
            self.assertRegex(
                next(itr), 'Checking out upstream commit refs/heads/base: .*')
            self.assertEqual(
                "Processing 2 commits from branch 'first'", next(itr))
            self.assertRegex(
                next(itr),
                f'- added 1 tag:       {HASH_RE} as {HASH_RE} i2c: I2C things')
            self.assertRegex(
                next(itr),
                f"- upd links '1:123': {HASH_RE} as {HASH_RE} spi: SPI fixes")
            self.assertRegex(
                next(itr),
                f'Updating branch first from {HASH_RE} to {HASH_RE}')
            self.assertEqual('', next(itr))

            self.assertEqual("Syncing 'first' v2", next(itr))
            self.assertEqual('  1 i2c: I2C things', next(itr))
            self.assertEqual(
                '  + Tested-by: Mary Smith <msmith@wibble.com>   # yak',
                next(itr))
            self.assertEqual('  2 spi: SPI fixes', next(itr))
            self.assertRegex(
                next(itr), 'Checking out upstream commit refs/heads/base: .*')
            self.assertEqual(
                "Processing 2 commits from branch 'first2'", next(itr))
            self.assertRegex(
                next(itr),
                f'- added 1 tag:        {HASH_RE} as {HASH_RE} '
                'i2c: I2C things')
            self.assertRegex(
                next(itr),
                f"- upd links '2:1234': {HASH_RE} as {HASH_RE} spi: SPI fixes")
            self.assertRegex(
                next(itr),
                f'Updating branch first2 from {HASH_RE} to {HASH_RE}')
            self.assertEqual('', next(itr))
            self.assertEqual("Syncing 'first' v3", next(itr))
            self.assertEqual('  1 i2c: I2C things', next(itr))
            self.assertEqual(
                '  + Tested-by: Mary Smith <msmith@wibble.com>   # yak',
                next(itr))
            self.assertEqual('  2 spi: SPI fixes', next(itr))
            self.assertRegex(
                next(itr), 'Checking out upstream commit refs/heads/base: .*')
            self.assertEqual(
                "Processing 2 commits from branch 'first3'", next(itr))
            self.assertRegex(
                next(itr),
                f'- added 1 tag:      {HASH_RE} as {HASH_RE} i2c: I2C things')
            self.assertRegex(
                next(itr),
                f"- upd links '3:31': {HASH_RE} as {HASH_RE} spi: SPI fixes")
            self.assertRegex(
                next(itr),
                f'Updating branch first3 from {HASH_RE} to {HASH_RE}')
            self.assertEqual('', next(itr))

            self.assertEqual("Syncing 'second' v1", next(itr))
            self.assertEqual('  1 video: Some video improvements', next(itr))
            self.assertEqual(
                '  + Reviewed-by: Fred Bloggs <fred@bloggs.com>', next(itr))
            self.assertEqual(
                'Review: Fred Bloggs <fred@bloggs.com>', next(itr))
            self.assertEqual('    > This was my original patch', next(itr))
            self.assertEqual('    > which is being quoted', next(itr))
            self.assertEqual(
                '    I like the approach here and I would love to see more '
                'of it.', next(itr))
            self.assertEqual('', next(itr))
            self.assertEqual('  2 serial: Add a serial driver', next(itr))
            self.assertEqual('  3 bootm: Make it boot', next(itr))
            self.assertRegex(
                next(itr), 'Checking out upstream commit refs/heads/base: .*')
            self.assertEqual(
                "Processing 3 commits from branch 'second'", next(itr))
            self.assertRegex(
                next(itr),
                f'- added 1 tag:       {HASH_RE} as {HASH_RE} '
                'video: Some video improvements')
            self.assertRegex(
                next(itr),
                f"- upd links '1:456': {HASH_RE} as {HASH_RE} "
                'serial: Add a serial driver')
            self.assertRegex(
                next(itr),
                f'-                    {HASH_RE} as {HASH_RE} '
                'bootm: Make it boot')
            self.assertRegex(
                next(itr),
                f'Updating branch second from {HASH_RE} to {HASH_RE}')
            self.assertEqual('', next(itr))

            self.assertEqual("Syncing 'second' v2", next(itr))
            self.assertEqual('  1 video: Some video improvements', next(itr))
            self.assertEqual(
                '  + Reviewed-by: Fred Bloggs <fred@bloggs.com>', next(itr))
            self.assertEqual(
                'Review: Fred Bloggs <fred@bloggs.com>', next(itr))
            self.assertEqual('    > This was my original patch', next(itr))
            self.assertEqual('    > which is being quoted', next(itr))
            self.assertEqual(
                '    I like the approach here and I would love to see more '
                'of it.', next(itr))
            self.assertEqual('', next(itr))
            self.assertEqual('  2 serial: Add a serial driver', next(itr))
            self.assertEqual('  3 bootm: Make it boot', next(itr))
            self.assertRegex(
                next(itr), 'Checking out upstream commit refs/heads/base: .*')
            self.assertEqual(
                "Processing 3 commits from branch 'second2'", next(itr))
            self.assertRegex(
                next(itr),
                f'- added 1 tag:             {HASH_RE} as {HASH_RE} '
                'video: Some video improvements')
            self.assertRegex(
                next(itr),
                f"- upd links '2:457 1:456': {HASH_RE} as {HASH_RE} "
                'serial: Add a serial driver')
            self.assertRegex(
                next(itr),
                f'-                          {HASH_RE} as {HASH_RE} '
                'bootm: Make it boot')
            self.assertRegex(
                next(itr),
                f'Updating branch second2 from {HASH_RE} to {HASH_RE}')
            self.assertEqual('', next(itr))
            self.assertEqual(
                '12 patches and 3 cover letters updated, 0 missing links '
                '(32 requests)', next(itr))
            self.assert_finished(itr)

        yield None

    def test_series_gather_all(self):
        """Gather all series at once"""
        cor = self.check_series_gather_all()
        cser, pwork = next(cor)

        # no options
        cser.gather_all(pwork, False, True, False, False, dry_run=True)
        cser, pwork = next(cor)

        # gather
        cser.gather_all(pwork, False, False, False, True, dry_run=True)
        cser, pwork = next(cor)

        # gather, patch comments, !dry_run
        cser.gather_all(pwork, True, False, True, True)

        self.assertFalse(next(cor))

    def test_series_gather_all_cmdline(self):
        """Sync all series at once using cmdline"""
        cor = self.check_series_gather_all()
        _, pwork = next(cor)

        # no options
        self.run_args('series', '-n', '-s', 'second', 'gather-all', '-G',
                      pwork=pwork)
        _, pwork = next(cor)

        # gather
        self.run_args('series', '-n', '-s', 'second', 'gather-all',
                      pwork=pwork)
        _, pwork = next(cor)

        # gather, patch comments, !dry_run
        self.run_args('series',  '-s', 'second', 'gather-all', '-a', '-c',
                      pwork=pwork)

        self.assertFalse(next(cor))

    def _check_second(self, itr, show_all):
        """Check output from a 'progress' command

        Args:
            itr (Iterator): Contains the output lines to check
            show_all (bool): True if all versions are being shown, not just
                latest
        """
        self.assertEqual('second: Series for my board (versions: 1 2)',
                         next(itr))
        if show_all:
            self.assertEqual("Branch 'second' (total 3): 3:unknown",
                             next(itr))
            self.assertIn('PatchId', next(itr))
            self.assertRegex(
                next(itr),
                '  0 unknown      -         .* video: Some video improvements')
            self.assertRegex(
                next(itr),
                '  1 unknown      -         .* serial: Add a serial driver')
            self.assertRegex(
                next(itr),
                '  2 unknown      -         .* bootm: Make it boot')
            self.assertEqual('', next(itr))
        self.assertEqual(
            "Branch 'second2' (total 3): 1:accepted 1:changes 1:rejected",
            next(itr))
        self.assertIn('PatchId', next(itr))
        self.assertEqual(
            'Cov              2     139            '
            'The name of the cover letter', next(itr))
        self.assertRegex(
            next(itr),
            '  0 accepted     2     110 .* video: Some video improvements')
        self.assertRegex(
            next(itr),
            '  1 changes            111 .* serial: Add a serial driver')
        self.assertRegex(
            next(itr),
            '  2 rejected     3     112 .* bootm: Make it boot')

    def test_series_progress(self):
        """Test showing progress for a cseries"""
        self.setup_second()
        self.db_close()

        with self.stage('latest versions'):
            args = Namespace(subcmd='progress', series='second',
                             show_all_versions=False, list_patches=True,
                             include_archived=False)
            with terminal.capture() as (out, _):
                control.do_series(args, test_db=self.tmpdir, pwork=True)
            lines = iter(out.getvalue().splitlines())
            self._check_second(lines, False)

        with self.stage('all versions'):
            args.show_all_versions = True
            with terminal.capture() as (out, _):
                control.do_series(args, test_db=self.tmpdir, pwork=True)
            lines = iter(out.getvalue().splitlines())
            self._check_second(lines, True)

    def _check_first(self, itr):
        """Check output from the progress command

        Args:
            itr (Iterator): Contains the output lines to check
        """
        self.assertEqual('first:  (versions: 1)', next(itr))
        self.assertEqual("Branch 'first' (total 2): 2:unknown", next(itr))
        self.assertIn('PatchId', next(itr))
        self.assertRegex(
            next(itr),
            '  0 unknown      -        .* i2c: I2C things')
        self.assertRegex(
            next(itr),
            '  1 unknown      -        .* spi: SPI fixes')
        self.assertEqual('', next(itr))

    def test_series_progress_all(self):
        """Test showing progress for all cseries"""
        self.setup_second()
        self.db_close()

        with self.stage('progress with patches'):
            args = Namespace(subcmd='progress', series=None,
                             show_all_versions=False, list_patches=True,
                             include_archived=False)
            with terminal.capture() as (out, _):
                control.do_series(args, test_db=self.tmpdir, pwork=True)
            lines = iter(out.getvalue().splitlines())
            self._check_first(lines)
            self._check_second(lines, False)

        with self.stage('all versions'):
            args.show_all_versions = True
            with terminal.capture() as (out, _):
                control.do_series(args, test_db=self.tmpdir, pwork=True)
            lines = iter(out.getvalue().splitlines())
            self._check_first(lines)
            self._check_second(lines, True)

    def test_series_progress_all_archived(self):
        """Test showing progress for all cseries including archived ones"""
        self.setup_second()
        with terminal.capture():
            self.cser.archive('first')

        with self.stage('progress without archived'):
            with terminal.capture() as (out, _):
                self.run_args('series', 'progress', pwork=True)
            itr = iter(out.getvalue().splitlines())
            self.assertEqual(
                'Name             Description                               Count  Status',
                next(itr))
            self.assertTrue(next(itr).startswith('--'))
            self.assertEqual(
                'second2          The name of the cover letter              '
                '    3  1:accepted 1:changes 1:rejected', next(itr))

        with self.stage('progress with archived'):
            with terminal.capture() as (out, _):
                self.run_args('series', 'progress', '--include-archived',
                              pwork=True)
            lines = out.getvalue().splitlines()
            self.assertEqual(
                'first                                                      '
                '    2  2:unknown', lines[2])
            self.assertEqual(
                'second2          The name of the cover letter              '
                '    3  1:accepted 1:changes 1:rejected', lines[3])

    def test_series_progress_no_patches(self):
        """Test showing progress for all cseries without patches"""
        self.setup_second()

        with terminal.capture() as (out, _):
            self.run_args('series', 'progress', pwork=True)
        itr = iter(out.getvalue().splitlines())
        self.assertEqual(
            'Name             Description                               '
            'Count  Status', next(itr))
        self.assertTrue(next(itr).startswith('--'))
        self.assertEqual(
            'first                                                      '
            '    2  2:unknown', next(itr))
        self.assertEqual(
            'second2          The name of the cover letter              '
            '    3  1:accepted 1:changes 1:rejected', next(itr))
        self.assertTrue(next(itr).startswith('--'))
        self.assertEqual(
            ['2', 'series', '5', '2:unknown', '1:accepted', '1:changes',
             '1:rejected'],
            next(itr).split())
        self.assert_finished(itr)

    def test_series_progress_all_no_patches(self):
        """Test showing progress for all cseries versions without patches"""
        self.setup_second()

        with terminal.capture() as (out, _):
            self.run_args('series', 'progress', '--show-all-versions',
                          pwork=True)
        itr = iter(out.getvalue().splitlines())
        self.assertEqual(
            'Name             Description                               '
            'Count  Status', next(itr))
        self.assertTrue(next(itr).startswith('--'))
        self.assertEqual(
            'first                                                      '
            '    2  2:unknown', next(itr))
        self.assertEqual(
            'second           Series for my board                       '
            '    3  3:unknown', next(itr))
        self.assertEqual(
            'second2          The name of the cover letter              '
            '    3  1:accepted 1:changes 1:rejected', next(itr))
        self.assertTrue(next(itr).startswith('--'))
        self.assertEqual(
            ['3', 'series', '8', '5:unknown', '1:accepted', '1:changes',
             '1:rejected'],
            next(itr).split())
        self.assert_finished(itr)

    def test_series_summary(self):
        """Test showing a summary of series status"""
        self.setup_second()

        self.db_close()
        args = Namespace(subcmd='summary', series=None)
        with terminal.capture() as (out, _):
            control.do_series(args, test_db=self.tmpdir, pwork=True)
        lines = out.getvalue().splitlines()
        self.assertEqual(
            'Name               Status  Description',
            lines[0])
        self.assertEqual(
            '-----------------  ------  ------------------------------',
            lines[1])
        self.assertEqual('first          -/2  ', lines[2])
        self.assertEqual('second         1/3  Series for my board', lines[3])

    def test_series_open(self):
        """Test opening a series in a web browser"""
        cser = self.get_cser()
        pwork = Patchwork.for_testing(self._fake_patchwork_cser)
        self.assertFalse(cser.project_get())
        pwork.project_set(self.PROJ_ID, self.PROJ_LINK_NAME)

        with terminal.capture():
            cser.add('second', allow_unmarked=True)
            cser.increment('second')
            cser.link_auto(pwork, 'second', 2, True)
            cser.gather(pwork, 'second', 2, False, False, False)

        with mock.patch.object(cros_subprocess.Popen, '__init__',
                               return_value=None) as method:
            with terminal.capture() as (out, _):
                cser.open(pwork, 'second2', 2)

        url = ('https://my-fake-url/project/uboot/list/?series=457'
               '&state=*&archive=both')
        method.assert_called_once_with(['xdg-open', url])
        self.assertEqual(f'Opening {url}', out.getvalue().strip())

    def test_name_version(self):
        """Test handling of series names and versions"""
        cser = self.get_cser()
        repo = self.repo

        self.assertEqual(('fred', None),
                         patchstream.split_name_version('fred'))
        self.assertEqual(('mary', 2), patchstream.split_name_version('mary2'))

        # Only the trailing digits are the version, so a name may have
        # digits of its own in the middle
        self.assertEqual(('rv1106e', None),
                         patchstream.split_name_version('rv1106e'))
        self.assertEqual(('rv1106e', 2),
                         patchstream.split_name_version('rv1106e2'))
        self.assertEqual(('rv1106e', 10),
                         patchstream.split_name_version('rv1106e10'))

        ser, version = cser._parse_series_and_version(None, None)
        self.assertEqual('first', ser.name)
        self.assertEqual(1, version)

        ser, version = cser._parse_series_and_version('first', None)
        self.assertEqual('first', ser.name)
        self.assertEqual(1, version)

        ser, version = cser._parse_series_and_version('first', 2)
        self.assertEqual('first', ser.name)
        self.assertEqual(2, version)

        with self.assertRaises(ValueError) as exc:
            cser._parse_series_and_version('123', 2)
        self.assertEqual(
            "Series name '123' cannot be a number, use '<name><version>'",
            str(exc.exception))

        with self.assertRaises(ValueError) as exc:
            cser._parse_series_and_version('first', 100)
        self.assertEqual("Version 100 exceeds 99", str(exc.exception))

        with terminal.capture() as (_, err):
            cser._parse_series_and_version('mary3', 4)
        self.assertIn('Version mismatch: -V has 4 but branch name indicates 3',
                      err.getvalue())

        ser, version = cser._parse_series_and_version('mary', 4)
        self.assertEqual('mary', ser.name)
        self.assertEqual(4, version)

        # Move off the branch and check for a sensible error
        commit = repo.revparse_single('first~')
        repo.checkout_tree(commit)
        repo.set_head(commit.id)

        with self.assertRaises(ValueError) as exc:
            cser._parse_series_and_version(None, None)
        self.assertEqual('No branch detected: please use -s <series>',
                         str(exc.exception))

        name, version = patchstream.split_name_version('x86a')
        self.assertEqual('x86a', name)
        self.assertEqual(None, version)

    def test_name_version_extra(self):
        """More tests for some corner cases"""
        cser, _ = self.setup_second()
        target = self.repo.lookup_reference('refs/heads/second2')
        self.repo.checkout(
            target, strategy=pygit2.enums.CheckoutStrategy.FORCE)

        ser, version = cser._parse_series_and_version(None, None)
        self.assertEqual('second', ser.name)
        self.assertEqual(2, version)

        ser, version = cser._parse_series_and_version('second2', None)
        self.assertEqual('second', ser.name)
        self.assertEqual(2, version)

    def test_migrate(self):
        """Test migration to later schema versions"""
        db = database.Database(f'{self.tmpdir}/.patman.db')
        with terminal.capture() as (out, err):
            db.open_it()
        self.assertEqual(
            f'Creating new database {self.tmpdir}/.patman.db',
            err.getvalue().strip())

        self.assertEqual(0, db.get_schema_version())

        for version in range(1, database.LATEST + 1):
            with terminal.capture() as (out, _):
                db.migrate_to(version)
            self.assertTrue(os.path.exists(
                f'{self.tmpdir}/.patman.dbold.v{version - 1}'))
            self.assertEqual(f'Update database to v{version}',
                             out.getvalue().strip())
            self.assertEqual(version, db.get_schema_version())
        self.assertEqual(10, database.LATEST)

    def test_migrate_future_version(self):
        """Test that a database newer than patman is rejected"""
        db = database.Database(f'{self.tmpdir}/.patman.db')
        with terminal.capture():
            db.start()

        # Set the schema version beyond what patman supports
        db.cur.execute(
            f'UPDATE schema_version SET version = {database.LATEST + 1}')
        db.commit()
        db.close()

        with self.assertRaises(SystemExit):
            with terminal.capture() as (_, err):
                db.start()
        self.assertIn('is too new', err.getvalue())

    def test_migrate_upstream_warning(self):
        """Test that migrating to v5 warns about series without upstream"""
        self.make_git_tree()

        # Set 'first' branch to track a remote-style upstream so that
        # auto-detection can find it
        self.repo.config.set_multivar('branch.first.remote', '', 'origin')
        self.repo.config.set_multivar('branch.first.merge', '',
                                      'refs/heads/main')

        db = database.Database(f'{self.tmpdir}/.patman2.db')
        with terminal.capture():
            db.open_it()

        # Create a v4 database with some series; 'first' has a matching
        # branch with a detectable remote, 'second' and 'third' do not
        with terminal.capture() as (out, _):
            db.migrate_to(4)
        self.assertEqual(
            'Update database to v1\nUpdate database to v2\n'
            'Update database to v3\nUpdate database to v4',
            out.getvalue().strip())
        db.execute(
            "INSERT INTO series (name, desc, archived) "
            "VALUES ('first', 'desc1', 0)")
        db.execute(
            "INSERT INTO series (name, desc, archived) "
            "VALUES ('second', 'desc2', 0)")
        db.execute(
            "INSERT INTO series (name, desc, archived) "
            "VALUES ('third', 'desc3', 0)")
        db.commit()
        db.close()

        cser = cseries.Cseries(self.tmpdir, terminal.COLOR_NEVER)
        cser.topdir = self.tmpdir
        cser.gitdir = self.gitdir

        # Point at our v4 database
        database.Database.instances = {}
        cser.db, _ = database.Database.get_instance(
            f'{self.tmpdir}/.patman2.db')
        with terminal.capture() as (_, err):
            old_version = cser.db.start()
        self.assertEqual(4, old_version)

        # 'first' should be auto-detected, 'second' and 'third' have no
        # matching branch with a remote upstream
        with terminal.capture() as (out, err):
            cser._check_null_upstreams()
        self.assertIn("Set upstream for series 'first' to 'origin'",
                      out.getvalue())
        lines = err.getvalue().strip().splitlines()
        self.assertEqual('2 series without an upstream:', lines[0])
        self.assertEqual('  second', lines[1])
        self.assertEqual('  third', lines[2])

        # Check that 'first' was actually updated in the database
        slist = cser.db.series_get_dict()
        self.assertEqual('origin', slist['first'].upstream)
        self.assertIsNone(slist['second'].upstream)

        cser.db.close()

    def test_series_scan(self):
        """Test scanning a series for updates"""
        cser, _ = self.setup_second()
        target = self.repo.lookup_reference('refs/heads/second2')
        self.repo.checkout(
            target, strategy=pygit2.enums.CheckoutStrategy.FORCE)

        # Add a new commit
        self.repo = pygit2.init_repository(self.gitdir)
        self.make_commit_with_file(
            'wip: Try out a new thing', 'Just checking', 'wibble.c',
            '''changes to wibble''')
        target = self.repo.revparse_single('HEAD')
        self.repo.reset(target.id, pygit2.enums.ResetMode.HARD)

        # name = gitutil.get_branch(self.gitdir)
        # upstream_name = gitutil.get_upstream(self.gitdir, name)
        name, ser, version, _ = cser.prep_series(None)

        # We now have 4 commits numbered 0 (second~3) to 3 (the one we just
        # added). Drop commit 1 (the 'serial' one) from the branch
        cser._filter_commits(name, ser, 1)
        svid = cser.get_ser_ver(ser.idnum, version).idnum
        old_pcdict = cser.get_pcommit_dict(svid).values()

        expect = '''Syncing series 'second2' v2: mark False allow_unmarked True
    0 video: Some video improvements
-   1 serial: Add a serial driver
    1 bootm: Make it boot
+   2 Just checking
'''
        with terminal.capture() as (out, _):
            self.run_args('series', '-n', 'scan', '-M', pwork=True)
        self.assertEqual(expect + 'Dry run completed\n', out.getvalue())

        new_pcdict = cser.get_pcommit_dict(svid).values()
        self.assertEqual(list(old_pcdict), list(new_pcdict))

        with terminal.capture() as (out, _):
            self.run_args('series', 'scan', '-M', pwork=True)
        self.assertEqual(
            expect + 'Scanned 3 commits (1 added, 1 removed)\n',
            out.getvalue())

        new_pcdict = cser.get_pcommit_dict(svid).values()
        self.assertEqual(len(old_pcdict), len(new_pcdict))
        chk = list(new_pcdict)
        self.assertNotEqual(list(old_pcdict), list(new_pcdict))
        self.assertEqual('video: Some video improvements', chk[0].subject)
        self.assertEqual('bootm: Make it boot', chk[1].subject)
        self.assertEqual('Just checking', chk[2].subject)

    def test_series_send(self):
        """Test sending a series"""
        cser, pwork = self.setup_second()

        # Create a third version
        with terminal.capture():
            cser.increment('second')
        series = patchstream.get_metadata_for_list('second3', self.gitdir, 3)
        self.assertEqual('2:457 1:456', series.links)
        self.assertEqual('3', series.version)

        # Use a hermetic get_maintainer stub backed by a fixture
        # MAINTAINERS file, so the maintainer Cc does not depend on
        # running inside a real U-Boot tree
        get_maint = os.path.join(
            os.path.dirname(os.path.realpath(__file__)), 'test',
            'get_maintainer')
        with terminal.capture() as (out, err):
            self.run_args('series', '-n', '-s', 'second3', 'send',
                          '--no-autolink', '--get-maintainer-script',
                          get_maint, pwork=pwork)
        self.assertIn('Send a total of 3 patches with a cover letter',
                      out.getvalue())
        self.assertIn(
            'video.c:1: warning: Missing or malformed SPDX-License-Identifier '
            'tag in line 1', err.getvalue())
        self.assertIn(
            '<patch>:19: warning: added, moved or deleted file(s), does '
            'MAINTAINERS need updating?', err.getvalue())
        self.assertIn('bootm.c:1: check: Avoid CamelCase: <Fix>',
                      err.getvalue())
        self.assertIn(
            'Cc:  Anatolij Gustschin <ag.dev.uboot@gmail.com>', out.getvalue())

        self.assertTrue(os.path.exists(os.path.join(
            self.tmpdir, '0001-video-Some-video-improvements.patch')))
        self.assertTrue(os.path.exists(os.path.join(
            self.tmpdir, '0002-serial-Add-a-serial-driver.patch')))
        self.assertTrue(os.path.exists(os.path.join(
            self.tmpdir, '0003-bootm-Make-it-boot.patch')))

    def test_series_send_and_link(self):
        """Test sending a series and then adding its link to the database"""
        def h_sleep(time_s):
            if cser.get_time() > 25:
                self.autolink_extra = {'id': 500,
                                       'name': 'Series for my board',
                                       'version': 3}
            cser.inc_fake_time(time_s)

        cser, pwork = self.setup_second()

        # Create a third version
        with terminal.capture():
            cser.increment('second')
        series = patchstream.get_metadata_for_list('second3', self.gitdir, 3)
        self.assertEqual('2:457 1:456', series.links)
        self.assertEqual('3', series.version)

        with terminal.capture():
            self.run_args('series', '-n', 'send', pwork=pwork)

        cser.set_fake_time(h_sleep)
        with terminal.capture() as (out, _):
            cser.link_auto(pwork, 'second3', 3, True, 50)
        lines = [ln for ln in out.getvalue().splitlines()
                 if not ln.startswith('Searching for ')]
        itr = iter(lines)

        # Matches shown only once (they don't change between retries)
        self.assertEqual(
            "Possible matches for 'second' v3 desc 'Series for my board':",
            next(itr))
        self.assertEqual('  Link  Version  Description', next(itr))
        self.assertEqual('   456        1  Series for my board', next(itr))
        self.assertEqual('   457        2  Series for my board', next(itr))

        # Progress messages with backoff (5, 10, 15, 20s sleeps)
        self.assertEqual(
            'Waiting for series on patchwork (0s)...', next(itr))
        self.assertEqual(
            'Waiting for series on patchwork (5s)...', next(itr))
        self.assertEqual(
            'Waiting for series on patchwork (15s)...', next(itr))
        self.assertEqual(
            'Waiting for series on patchwork (30s)...', next(itr))
        self.assertEqual('Link completed after 50 seconds', next(itr))
        self.assertRegex(
            next(itr), 'Checking out upstream commit refs/heads/base: .*')
        self.assertEqual(
            "Processing 3 commits from branch 'second3'", next(itr))
        self.assertRegex(
            next(itr),
            f'-                                {HASH_RE} as {HASH_RE} '
            'video: Some video improvements')
        self.assertRegex(
            next(itr),
            f"- add links '3:500 2:457 1:456': {HASH_RE} as {HASH_RE} "
            'serial: Add a serial driver')
        self.assertRegex(
            next(itr),
            f'- add v3:                        {HASH_RE} as {HASH_RE} '
            'bootm: Make it boot')
        self.assertRegex(
            next(itr),
            f'Updating branch second3 from {HASH_RE} to {HASH_RE}')
        self.assertEqual(
            "Setting link for series 'second' v3 to 500", next(itr))

    def _check_status(self, out, has_comments, has_cover_comments):
        """Check output from the status command

        Args:
            itr (Iterator): Contains the output lines to check
        """
        itr = iter(out.getvalue().splitlines())
        if has_cover_comments:
            self.assertEqual('Cov The name of the cover letter', next(itr))
            self.assertEqual(
                'From: A user <user@user.com>: Sun 13 Apr 14:06:02 MDT 2025',
                next(itr))
            self.assertEqual('some comment', next(itr))
            self.assertEqual('', next(itr))

            self.assertEqual(
                'From: Ghenkis Khan <gk@eurasia.gov>: Sun 13 Apr 13:06:02 '
                'MDT 2025',
                next(itr))
            self.assertEqual('another comment', next(itr))
            self.assertEqual('', next(itr))

        self.assertEqual('  1 video: Some video improvements', next(itr))
        self.assertEqual('  + Reviewed-by: Fred Bloggs <fred@bloggs.com>',
                         next(itr))
        if has_comments:
            self.assertEqual(
                'Review: Fred Bloggs <fred@bloggs.com>', next(itr))
            self.assertEqual('    > This was my original patch', next(itr))
            self.assertEqual('    > which is being quoted', next(itr))
            self.assertEqual(
                '    I like the approach here and I would love to see more '
                'of it.', next(itr))
            self.assertEqual('', next(itr))

        self.assertEqual('  2 serial: Add a serial driver', next(itr))
        self.assertEqual('  3 bootm: Make it boot', next(itr))
        self.assertEqual(
            '1 new response available in patchwork (use -d to write them to '
            'a new branch)', next(itr))

    def test_series_status(self):
        """Test getting the status of a series, including comments"""
        cser, pwork = self.setup_second()

        # Use single threading for easy debugging, but the multithreaded
        # version should produce the same output
        with self.stage('status second2: single-threaded'):
            with terminal.capture() as (out, _):
                cser.status(pwork, 'second', 2, False)
            self._check_status(out, False, False)
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)

        with self.stage('status second2 (normal)'):
            with terminal.capture() as (out2, _):
                cser.status(pwork, 'second', 2, False)
            self.assertEqual(out.getvalue(), out2.getvalue())
            self._check_status(out, False, False)

        with self.stage('with comments'):
            with terminal.capture() as (out, _):
                cser.status(pwork, 'second', 2, show_comments=True)
            self._check_status(out, True, False)

        with self.stage('with comments and cover comments'):
            with terminal.capture() as (out, _):
                cser.status(pwork, 'second', 2, show_comments=True,
                            show_cover_comments=True)
            self._check_status(out, True, True)

    def test_series_status_gather_archived(self):
        """status and gather should still work on an archived series"""
        cser, pwork = self.setup_second()
        with terminal.capture():
            cser.archive('second')

        # status reads the patches from the archive tag, since archiving
        # deletes the branch
        with terminal.capture() as (out, _):
            cser.status(pwork, 'second', 2, False)
        self._check_status(out, False, False)

        # gather works from patchwork and the database, with no branch
        with terminal.capture() as (out, _):
            cser.gather(pwork, 'second', 2, False, False, True, dry_run=True)
        self.assertIn('updated', out.getvalue())

    def test_series_status_cmdline(self):
        """Test getting the status of a series, including comments"""
        cser, pwork = self.setup_second()

        with self.stage('status second2'):
            with terminal.capture() as (out, _):
                self.run_args('series', '-s', 'second', '-V', '2', 'status',
                              pwork=pwork)
            self._check_status(out, False, False)

        with self.stage('status second2 (normal)'):
            with terminal.capture() as (out, _):
                cser.status(pwork, 'second', 2, show_comments=True)
            self._check_status(out, True, False)

        with self.stage('with comments and cover comments'):
            with terminal.capture() as (out, _):
                cser.status(pwork, 'second', 2, show_comments=True,
                                   show_cover_comments=True)
            self._check_status(out, True, True)

    def test_series_no_subcmd(self):
        """Test handling of things without a subcommand"""
        parsers = cmdline.setup_parser()
        parsers['series'].catch_error = True
        with terminal.capture() as (out, _):
            cmdline.parse_args(['series'], parsers=parsers)
        self.assertIn('usage: patman series', out.getvalue())

        parsers['patchwork'].catch_error = True
        with terminal.capture() as (out, _):
            cmdline.parse_args(['patchwork'], parsers=parsers)
        self.assertIn('usage: patman patchwork', out.getvalue())

        parsers['upstream'].catch_error = True
        with terminal.capture() as (out, _):
            cmdline.parse_args(['upstream'], parsers=parsers)
        self.assertIn('usage: patman upstream', out.getvalue())

    def check_series_rename(self):
        """Check renaming a series"""
        cser = self.get_cser()
        with self.stage('setup'):
            with terminal.capture() as (out, _):
                cser.add('first', 'my name', allow_unmarked=True)

            # Remember the old series
            old = cser.get_series_by_name('first')

            self.assertEqual('first', gitutil.get_branch(self.gitdir))
            with terminal.capture() as (out, _):
                cser.increment('first')
            self.assertEqual('first2', gitutil.get_branch(self.gitdir))

            with terminal.capture() as (out, _):
                cser.increment('first')
            self.assertEqual('first3', gitutil.get_branch(self.gitdir))

        # Do the dry run
        with self.stage('rename - dry run'):
            with terminal.capture() as (out, _):
                yield cser
            lines = out.getvalue().splitlines()
            itr = iter(lines)
            self.assertEqual("Renaming branch 'first' to 'newname'", next(itr))
            self.assertEqual(
                "Renaming branch 'first2' to 'newname2'", next(itr))
            self.assertEqual(
                "Renaming branch 'first3' to 'newname3'", next(itr))
            self.assertEqual("Renamed series 'first' to 'newname'", next(itr))
            self.assertEqual("Dry run completed", next(itr))
            self.assert_finished(itr)

            # Check nothing changed
            self.assertEqual('first3', gitutil.get_branch(self.gitdir))
            sdict = cser.db.series_get_dict()
            self.assertIn('first', sdict)

        # Now do it for real
        with self.stage('rename - real'):
            with terminal.capture() as (out2, _):
                yield cser
            lines2 = out2.getvalue().splitlines()
            self.assertEqual(lines[:-1], lines2)

            self.assertEqual('newname3', gitutil.get_branch(self.gitdir))

            # Check the series ID did not change
            ser = cser.get_series_by_name('newname')
            self.assertEqual(old.idnum, ser.idnum)

        yield None

    def test_series_rename(self):
        """Test renaming of a series"""
        cor = self.check_series_rename()
        cser = next(cor)

        # Rename (dry run)
        cser.rename('first', 'newname', dry_run=True)
        cser = next(cor)

        # Rename (real)
        cser.rename('first', 'newname')
        self.assertFalse(next(cor))

    def test_series_rename_cmdline(self):
        """Test renaming of a series with the cmdline"""
        cor = self.check_series_rename()
        next(cor)

        # Rename (dry run)
        self.run_args('series', '-n', '-s', 'first', 'rename', '-N', 'newname',
                      pwork=True)
        next(cor)

        # Rename (real)
        self.run_args('series', '-s', 'first', 'rename', '-N', 'newname',
                      pwork=True)

        self.assertFalse(next(cor))

    def test_series_rename_bad(self):
        """Test renaming when it is not allowed"""
        cser = self.get_cser()
        with terminal.capture():
            cser.add('first', 'my name', allow_unmarked=True)
            cser.increment('first')
            cser.increment('first')

        with self.assertRaises(ValueError) as exc:
            cser.rename('first', 'first')
        self.assertEqual("Cannot rename series 'first' to itself",
                         str(exc.exception))

        with self.assertRaises(ValueError) as exc:
            cser.rename('first2', 'newname')
        self.assertEqual(
            "Invalid series name 'first2': did you use the branch name?",
            str(exc.exception))

        with self.assertRaises(ValueError) as exc:
            cser.rename('first', 'newname2')
        self.assertEqual(
            "Invalid series name 'newname2': did you use the branch name?",
            str(exc.exception))

        with self.assertRaises(ValueError) as exc:
            cser.rename('first', 'second')
        self.assertEqual("Cannot rename: branches exist: second",
                         str(exc.exception))

        with terminal.capture():
            cser.add('second', 'another name', allow_unmarked=True)
            cser.increment('second')

        with self.assertRaises(ValueError) as exc:
            cser.rename('first', 'second')
        self.assertEqual("Cannot rename: series 'second' already exists",
                         str(exc.exception))

        # Rename second2 so that it gets in the way of the rename
        gitutil.rename_branch('second2', 'newname2', self.gitdir)
        with self.assertRaises(ValueError) as exc:
            cser.rename('first', 'newname')
        self.assertEqual("Cannot rename: branches exist: newname2",
                         str(exc.exception))

        # Rename first3 and make sure it stops the rename
        gitutil.rename_branch('first3', 'tempbranch', self.gitdir)
        with self.assertRaises(ValueError) as exc:
            cser.rename('first', 'newname')
        self.assertEqual(
            "Cannot rename: branches missing: first3: branches exist: "
            'newname2', str(exc.exception))

    def test_version_change(self):
        """Test changing a version of a series to a different version number"""
        cser = self.get_cser()

        with self.stage('setup'):
            with terminal.capture():
                cser.add('first', 'my description', allow_unmarked=True)

        with self.stage('non-existent version'):
            # Check changing a non-existent version
            with self.assertRaises(ValueError) as exc:
                cser.version_change('first', 2, 3, dry_run=True)
            self.assertEqual("Series 'first' does not have a version 2",
                             str(exc.exception))

        with self.stage('new version missing'):
            with self.assertRaises(ValueError) as exc:
                cser.version_change('first', None, None, dry_run=True)
            self.assertEqual("Please provide a new version number",
                             str(exc.exception))

        # Change v1 to v2 (dry run)
        with self.stage('v1 -> 2 dry run'):
            with terminal.capture():
                self.assertTrue(gitutil.check_branch('first', self.gitdir))
                cser.version_change('first', 1, 3, dry_run=True)
                self.assertTrue(gitutil.check_branch('first', self.gitdir))
                self.assertFalse(gitutil.check_branch('first3', self.gitdir))

                # Check that nothing actually happened
                series = patchstream.get_metadata('first', 0, 2,
                                                  git_dir=self.gitdir)
                self.assertNotIn('version', series)

                svlist = cser.get_ser_ver_list()
                self.assertEqual(1, len(svlist))
                item = svlist[0]
                self.assertEqual(1, item.version)

        with self.stage('increment twice'):
            # Increment so that we get first3
            with terminal.capture():
                cser.increment('first')
                cser.increment('first')

        with self.stage('existing version'):
            # Check changing to an existing version
            with self.assertRaises(ValueError) as exc:
                cser.version_change('first', 1, 3, dry_run=True)
            self.assertEqual("Series 'first' already has a v3: 1 2 3",
                             str(exc.exception))

        # Change v1 to v4 (for real)
        with self.stage('v1 -> 4'):
            with terminal.capture():
                self.assertTrue(gitutil.check_branch('first', self.gitdir))
                cser.version_change('first', 1, 4)
                self.assertTrue(gitutil.check_branch('first', self.gitdir))
                self.assertTrue(gitutil.check_branch('first4', self.gitdir))

                series = patchstream.get_metadata('first4', 0, 2,
                                                  git_dir=self.gitdir)
                self.assertIn('version', series)
                self.assertEqual('4', series.version)

                svdict = cser.get_ser_ver_dict()
                self.assertEqual(3, len(svdict))
                item = svdict[item.idnum]
                self.assertEqual(4, item.version)

        with self.stage('increment'):
            # Now try to increment first again
            with terminal.capture():
                cser.increment('first')

                ser = cser.get_series_by_name('first')
                self.assertIn(5, cser._get_version_list(ser.idnum))

    def test_version_change_cmdline(self):
        """Check changing a version on the cmdline"""
        self.get_cser()
        with (mock.patch.object(cseries.Cseries, 'version_change',
                                return_value=None) as method):
            self.run_args('series', '-s', 'first', 'version-change',
                          pwork=True)
        method.assert_called_once_with('first', None, None, dry_run=False)

        with (mock.patch.object(cseries.Cseries, 'version_change',
                                return_value=None) as method):
            self.run_args('series', '-s', 'first', 'version-change',
                          '--new-version', '3', pwork=True)
        method.assert_called_once_with('first', None, 3, dry_run=False)

    def test_workflow_db_methods(self):
        """Test workflow database methods"""
        cser = self.get_cser()
        with terminal.capture():
            cser.add('first', 'my description', allow_unmarked=True)

        ser = cser.get_series_by_name('first')

        # Initially there is no workflow entry
        self.assertIsNone(cser.db.workflow_get('todo', ser.idnum))

        # Add a todo entry
        cser.db.workflow_add('todo', ser.idnum, '2025-03-15 10:00:00')
        cser.commit()

        # Should be able to read it back
        ts = cser.db.workflow_get('todo', ser.idnum)
        self.assertEqual('2025-03-15 10:00:00', ts)

        # Get by type should return it
        entries = cser.db.workflow_get_by_type('todo')
        self.assertEqual(1, len(entries))
        entry = entries[0]
        self.assertEqual(ser.idnum, entry[0])
        self.assertEqual('first', entry[1])
        self.assertEqual('my description', entry[2])
        self.assertEqual('2025-03-15 10:00:00', entry[3])

        # Get by type with before filter
        entries = cser.db.workflow_get_by_type(
            'todo', before='2025-03-14 00:00:00')
        self.assertEqual(0, len(entries))
        entries = cser.db.workflow_get_by_type(
            'todo', before='2025-03-16 00:00:00')
        self.assertEqual(1, len(entries))

        # Archive it - should no longer be active, but still in the table
        cser.db.workflow_archive('todo', ser.idnum)
        cser.commit()
        self.assertIsNone(cser.db.workflow_get('todo', ser.idnum))
        res = cser.db.execute(
            'SELECT archived FROM workflow WHERE series_id = ?',
            (ser.idnum,))
        self.assertEqual(1, res.fetchone()[0])

    def test_workflow_todo(self):
        """Test setting and clearing a todo"""
        cser = self.get_cser()
        with terminal.capture():
            cser.add('first', 'my description', allow_unmarked=True)

        cser.fake_now = datetime(2025, 3, 1, 12, 0, 0)
        ser = cser.get_series_by_name('first')

        # Set a todo for 7 days
        with terminal.capture() as (out, _):
            wf.todo(cser,'first', 7)
        self.assertIn('2025-03-08 12:00:00', out.getvalue())

        # Check the DB entry
        ts = cser.db.workflow_get('todo', ser.idnum)
        self.assertEqual('2025-03-08 12:00:00', ts)

        # Replacing the todo should work
        with terminal.capture() as (out, _):
            wf.todo(cser,'first', 14)
        self.assertIn('2025-03-15 12:00:00', out.getvalue())
        ts = cser.db.workflow_get('todo', ser.idnum)
        self.assertEqual('2025-03-15 12:00:00', ts)

        # Clear it
        with terminal.capture() as (out, _):
            wf.todo_clear(cser,'first')
        self.assertIn('Todo cleared', out.getvalue())
        self.assertIsNone(cser.db.workflow_get('todo', ser.idnum))

    def test_workflow_todo_list(self):
        """Test listing todos"""
        cser = self.get_cser()
        with terminal.capture():
            cser.add('first', 'my description', allow_unmarked=True)
            cser.add('second', 'board stuff', allow_unmarked=True)

        cser.fake_now = datetime(2025, 3, 10, 12, 0, 0)

        # Set todos: first is due, second is in the future
        with terminal.capture():
            wf.todo(cser,'first', 0)
            wf.todo(cser,'second', 7)

        # Default list shows only due entries
        with terminal.capture() as (out, _):
            wf.todo_list(cser,show_all=False)
        lines = out.getvalue().splitlines()
        self.assertEqual(3, len(lines))
        self.assertIn('first', lines[2])
        self.assertIn('today', lines[2])

        # --all shows all entries
        with terminal.capture() as (out, _):
            wf.todo_list(cser,show_all=True)
        lines = out.getvalue().splitlines()
        self.assertEqual(4, len(lines))
        self.assertIn('first', lines[2])
        self.assertIn('today', lines[2])
        self.assertIn('second', lines[3])
        self.assertIn('in 7d', lines[3])

        # No todos
        with terminal.capture():
            wf.todo_clear(cser,'first')
            wf.todo_clear(cser,'second')
        with terminal.capture() as (out, _):
            wf.todo_list(cser,show_all=False)
        self.assertIn('No todos due', out.getvalue())

    def test_workflow_summary_marker(self):
        """Test that [todo] shows in series summary"""
        cser = self.get_cser()
        with terminal.capture():
            cser.add('first', 'my description', allow_unmarked=True)

        cser.fake_now = datetime(2025, 3, 10, 12, 0, 0)

        # Set a todo that is already due
        with terminal.capture():
            wf.todo(cser,'first', 0)

        # Summary should show [todo]
        with terminal.capture() as (out, _):
            cser.summary(None)
        self.assertIn('[todo]', out.getvalue())

        # Set a todo in the future
        with terminal.capture():
            wf.todo(cser,'first', 14)

        # Summary should NOT show [todo]
        with terminal.capture() as (out, _):
            cser.summary(None)
        self.assertNotIn('[todo]', out.getvalue())

    def test_workflow_todo_cmdline(self):
        """Test todo via the command line"""
        cser = self.get_cser()
        with terminal.capture():
            cser.add('first', 'my description', allow_unmarked=True)

        # Test via command line
        self.db_close()
        with terminal.capture() as (out, _):
            self.run_args('workflow', 'todo', '-s', 'first', '7')
        self.assertIn('marked for todo', out.getvalue())

        with terminal.capture() as (out, _):
            self.run_args('workflow', 'todo', '-s', 'first', '--clear')
        self.assertIn('Todo cleared', out.getvalue())

        with terminal.capture() as (out, _):
            self.run_args('wf', 'todo-list')
        self.assertIn('No todos due', out.getvalue())

    def test_workflow_sent(self):
        """Test that sending a series creates SENT and TODO entries"""
        cser = self.get_cser()
        with terminal.capture():
            cser.add('first', 'my description', allow_unmarked=True)

        cser.fake_now = datetime(2025, 3, 1, 12, 0, 0)
        ser = cser.get_series_by_name('first')

        svid = cser.get_series_svid(ser.idnum, 1)

        # Record a send with ser_ver_id
        wf.sent(cser, ser.idnum, ser_ver_id=svid)

        # Should have a SENT entry with current time
        ts = cser.db.workflow_get('sent', ser.idnum)
        self.assertEqual('2025-03-01 12:00:00', ts)

        # The SENT entry should have the ser_ver_id
        res = cser.db.execute(
            'SELECT ser_ver_id FROM workflow '
            'WHERE type = ? AND series_id = ? AND archived = 0',
            ('sent', ser.idnum))
        self.assertEqual(svid, res.fetchone()[0])

        # Should have a TODO entry 7 days out (no ser_ver_id)
        ts = cser.db.workflow_get('todo', ser.idnum)
        self.assertEqual('2025-03-08 12:00:00', ts)

        # Sending again should archive old entries and create new ones
        cser.fake_now = datetime(2025, 3, 5, 12, 0, 0)
        wf.sent(cser, ser.idnum, ser_ver_id=svid)

        ts = cser.db.workflow_get('sent', ser.idnum)
        self.assertEqual('2025-03-05 12:00:00', ts)

        ts = cser.db.workflow_get('todo', ser.idnum)
        self.assertEqual('2025-03-12 12:00:00', ts)

    def test_workflow_list(self):
        """Test listing all workflow entries"""
        cser = self.get_cser()
        with terminal.capture():
            cser.add('first', 'my description', allow_unmarked=True)

        cser.fake_now = datetime(2025, 3, 1, 12, 0, 0)

        # Record a send (creates SENT + TODO)
        ser = cser.get_series_by_name('first')
        wf.sent(cser, ser.idnum)

        # Default list shows only active entries
        with terminal.capture() as (out, _):
            wf.list_entries(cser, show_all=False)
        lines = out.getvalue().splitlines()
        self.assertEqual(4, len(lines))
        self.assertIn('sent', lines[2])
        self.assertIn('todo', lines[3])

        # Archive the todo
        with terminal.capture():
            wf.todo_clear(cser, 'first')

        # Without --all, only SENT is active; no 'A' column
        with terminal.capture() as (out, _):
            wf.list_entries(cser, show_all=False)
        lines = out.getvalue().splitlines()
        self.assertEqual(3, len(lines))
        self.assertIn('sent', lines[2])
        self.assertNotIn('A', lines[0])

        # With --all, archived entries appear with '*' marker
        with terminal.capture() as (out, _):
            wf.list_entries(cser, show_all=True)
        lines = out.getvalue().splitlines()
        self.assertGreater(len(lines), 3)
        self.assertIn('  A  ', lines[0])
        has_archived = any('*' in line for line in lines[2:])
        self.assertTrue(has_archived)

    def test_friendly_time(self):
        """Test friendly timestamp formatting"""
        now = datetime(2025, 3, 10, 15, 0, 0)  # Monday

        # Same day
        when = datetime(2025, 3, 10, 9, 30, 0)
        self.assertEqual('09:30', wf.friendly_time(now, when))

        # Earlier this week (3 days ago = Friday)
        when = datetime(2025, 3, 7, 14, 20, 0)
        self.assertEqual('Fri 14:20', wf.friendly_time(now, when))

        # 10 days ago
        when = datetime(2025, 2, 28, 10, 0, 0)
        self.assertEqual('10d ago', wf.friendly_time(now, when))

        # 3 weeks ago
        when = datetime(2025, 2, 17, 10, 0, 0)
        self.assertEqual('3w ago', wf.friendly_time(now, when))

        # Future within a week (3 days from now = Thursday)
        when = datetime(2025, 3, 13, 16, 0, 0)
        self.assertEqual('Thu 16:00', wf.friendly_time(now, when))

        # Future 10 days
        when = datetime(2025, 3, 20, 10, 0, 0)
        self.assertEqual('in 10d', wf.friendly_time(now, when))

        # Future 3 weeks
        when = datetime(2025, 3, 31, 10, 0, 0)
        self.assertEqual('in 3w', wf.friendly_time(now, when))

    def test_series_info(self):
        """Test the series info command"""
        cser = self.get_database()

        # Create a series with upstream and two versions
        cser.db.upstream_add('us', 'https://us.example.com')
        series_id = cser.db.series_add('test-info', 'My test series', ups='us')
        svid1 = cser.db.ser_ver_add(series_id, 1, link='12345',
                                     desc='First version desc')
        svid2 = cser.db.ser_ver_add(series_id, 2, desc='Second version desc')

        # Add patches to v1
        cser.db.pcommit_add_list(svid1, [
            Pcommit(idnum=None, seq=0, subject='Fix the widget',
                    svid=svid1, change_id=None, state=None,
                    patch_id=None, num_comments=0),
            Pcommit(idnum=None, seq=1, subject='Add widget tests',
                    svid=svid1, change_id=None, state=None,
                    patch_id=None, num_comments=0)])

        # Add notes to v2
        cser.db.ser_ver_set_notes(svid2, 'Fixed review feedback')
        cser.commit()

        with terminal.capture() as (out, _):
            cser.show_info('test-info')

        output = out.getvalue()
        self.assertIn('Series: test-info', output)
        self.assertIn('Description: My test series', output)
        self.assertIn('Upstream: us', output)
        self.assertIn('Version 1:', output)
        self.assertIn('Link: 12345', output)
        self.assertIn('First version desc', output)
        self.assertIn('Patches: 2', output)
        self.assertIn('Fix the widget', output)
        self.assertIn('Add widget tests', output)
        self.assertIn('Version 2:', output)
        self.assertIn('Second version desc', output)
        self.assertIn('Notes: Fixed review feedback', output)

    def test_series_find(self):
        """Test the series find command"""
        cser = self.get_database()

        # Create two series: one with patches matching 'widget', one with
        # matching cover description, one with neither
        alpha_id = cser.db.series_add('alpha', 'Widget subsystem refresh')
        alpha_svid = cser.db.ser_ver_add(alpha_id, 1)
        cser.db.pcommit_add_list(alpha_svid, [
            Pcommit(idnum=None, seq=0, subject='Fix the widget',
                    svid=alpha_svid, change_id=None, state=None,
                    patch_id=None, num_comments=0)])

        beta_id = cser.db.series_add('beta', 'Unrelated cleanup')
        beta_svid = cser.db.ser_ver_add(beta_id, 1,
                                         desc='Touch up the widget driver')
        cser.db.pcommit_add_list(beta_svid, [
            Pcommit(idnum=None, seq=0, subject='cleanup',
                    svid=beta_svid, change_id=None, state=None,
                    patch_id=None, num_comments=0)])

        gamma_id = cser.db.series_add('gamma', 'Something different')
        gamma_svid = cser.db.ser_ver_add(gamma_id, 1)
        cser.db.pcommit_add_list(gamma_svid, [
            Pcommit(idnum=None, seq=0, subject='other work',
                    svid=gamma_svid, change_id=None, state=None,
                    patch_id=None, num_comments=0)])
        cser.commit()

        # Match on cover-letter description and per-version description
        with terminal.capture() as (out, _):
            cser.series_find('widget')
        output = out.getvalue()
        self.assertIn('2 match(es)', output)
        self.assertIn('alpha', output)
        self.assertIn('beta', output)
        self.assertNotIn('gamma', output)

        # Match only on patch subject
        with terminal.capture() as (out, _):
            cser.series_find('Fix the')
        output = out.getvalue()
        self.assertIn('1 match(es)', output)
        self.assertIn('alpha', output)

        # No matches
        with terminal.capture() as (out, _):
            cser.series_find('nonexistent')
        output = out.getvalue()
        self.assertIn("No series match 'nonexistent'", output)

    # Series link used by the review tests
    REVIEW_LINK = 497923
    REVIEW_LINK_V2 = 497924
    REVIEW_NAME = 'boot/bootm: Disable interrupts after loading the image'

    def _fake_patchwork_review(self, subpath):
        """Fake Patchwork server for review tests

        Args:
            subpath (str): URL subpath to use
        """
        if re.match(r'projects/\?page=(\d+)&per_page=\d+$', subpath):
            if 'page=1&' not in subpath:
                return []
            return [
                {'id': self.PROJ_ID, 'name': 'U-Boot',
                 'link_name': self.PROJ_LINK_NAME},
            ]

        re_search = re.match(r'series/\?project=(\d+)&q=(.*)$', subpath)
        if re_search:
            return [
                {'id': self.REVIEW_LINK, 'name': self.REVIEW_NAME,
                 'version': 1, 'date': '2026-03-29T15:17:33'},
                {'id': self.REVIEW_LINK_V2, 'name': self.REVIEW_NAME,
                 'version': 2, 'date': '2026-04-01T10:00:00'},
            ]

        m_series = re.match(r'series/(\d+)/$', subpath)
        if m_series:
            series_id = int(m_series.group(1))
            if series_id == self.REVIEW_LINK:
                return {
                    'name': f'[PATCH] {self.REVIEW_NAME}',
                    'version': 1,
                    'received_total': 1,
                    'mbox': f'https://my-fake-url/series/{series_id}/mbox/',
                    'submitter': {'name': 'Test Author',
                                  'email': 'author@example.com'},
                    'project': {'list_email': 'u-boot@lists.denx.de'},
                    'cover_letter': None,
                    'patches': [
                        {'id': 900,
                         'name': f'[PATCH] {self.REVIEW_NAME}',
                         'msgid': '<20260329-bootm-v1-1-abc@posteo.net>'},
                    ],
                }
            if series_id == self.REVIEW_LINK_V2:
                return {
                    'name': f'[PATCH v2] {self.REVIEW_NAME}',
                    'version': 2,
                    'received_total': 1,
                    'mbox': f'https://my-fake-url/series/{series_id}/mbox/',
                    'submitter': {'name': 'Test Author',
                                  'email': 'author@example.com'},
                    'project': {'list_email': 'u-boot@lists.denx.de'},
                    'cover_letter': None,
                    'patches': [
                        {'id': 901,
                         'name': f'[PATCH,v2] {self.REVIEW_NAME}',
                         'msgid': '<20260401-bootm-v2-1-def@posteo.net>'},
                    ],
                }
            raise ValueError(
                f'Fake Patchwork unknown series_id: {series_id}')

        m_pstate = re.match(r'patches/\?series=(\d+)$', subpath)
        if m_pstate:
            sid = int(m_pstate.group(1))
            states = getattr(self, 'review_states', {}).get(sid, ['new'])
            return [{'id': i, 'state': st} for i, st in enumerate(states)]

        m_patch = re.match(r'patches/(\d+)/$', subpath)
        if m_patch:
            return {
                'headers': {
                    'Reply-To': 'author@posteo.net',
                    'To': 'u-boot@lists.denx.de',
                    'Cc': 'Tom Rini <trini@konsulko.com>',
                },
            }

        m_pcomm = re.match(r'patches/(\d+)/comments/$', subpath)
        if m_pcomm:
            return []

        m_ccomm = re.match(r'covers/(\d+)/comments/$', subpath)
        if m_ccomm:
            return []

        raise ValueError(f'Fake Patchwork unhandled URL: {subpath}')

    REVIEWER = 'Test Reviewer <test@test.com>'

    def run_review(self, *argv, **kwargs):
        """Run a review command with the test reviewer identity"""
        return self.run_args('review', '--reviewer', self.REVIEWER,
                             *argv, **kwargs)

    def _mock_review(self):
        """Context manager to mock apply, upstream, git and AI review"""
        fake_review = {1: f'Reviewed-by: {self.REVIEWER}'}
        return (mock.patch('patman.review._apply_and_check',
                           return_value=True),
                mock.patch('patman.review._get_upstream_branch',
                           return_value='origin/master'),
                mock.patch('patman.review.review_patches_sync',
                           return_value=fake_review),
                mock.patch('patman.review.gitutil.get_top_level',
                           return_value=self.tmpdir),
                mock.patch('patman.review.gitutil.ensure_worktree',
                           return_value=self.tmpdir))

    def test_review_new_series(self):
        """Test reviewing a new series creates database records"""
        cser = self.get_cser()
        pwork = Patchwork.for_testing(self._fake_patchwork_review)
        pwork.project_set(self.PROJ_ID, self.PROJ_LINK_NAME)

        mocks = self._mock_review()
        with contextlib.ExitStack() as stack:
            for m in mocks:
                stack.enter_context(m)
            with terminal.capture() as _:
                self.run_review('-s', str(self.REVIEW_LINK), pwork=pwork)

        # Check the series was created with source='review'
        self.db_open()
        result = cser.db.series_find_by_link(str(self.REVIEW_LINK))
        self.assertIsNotNone(result)
        series_id, name, version, svid = result
        self.assertEqual(f'pw-{self.REVIEW_LINK}-review', name)
        self.assertEqual(1, version)

        # Check source is 'review'
        res = cser.db.execute(
            'SELECT source FROM series WHERE id = ?', (series_id,))
        self.assertEqual('review', res.fetchone()[0])

        # Check pcommit was created
        pclist = cser.db.pcommit_get_list(svid)
        self.assertEqual(1, len(pclist))
        self.assertEqual(900, pclist[0].patch_id)

    def test_review_already_reviewed(self):
        """Test that reviewing the same link again is detected"""
        cser = self.get_cser()
        pwork = Patchwork.for_testing(self._fake_patchwork_review)
        pwork.project_set(self.PROJ_ID, self.PROJ_LINK_NAME)

        mocks = self._mock_review()
        with contextlib.ExitStack() as stack:
            for m in mocks:
                stack.enter_context(m)
            with terminal.capture() as _:
                self.run_review('-s', str(self.REVIEW_LINK), pwork=pwork)

        # Review the same link again
        mocks = self._mock_review()
        with contextlib.ExitStack() as stack:
            for m in mocks:
                stack.enter_context(m)
            with terminal.capture() as (out, err):
                self.run_review('-s', str(self.REVIEW_LINK), pwork=pwork)
        self.assertIn('Already reviewed', out.getvalue())

    def test_review_new_version(self):
        """Test that reviewing v2 detects v1 as previously reviewed"""
        cser = self.get_cser()
        pwork = Patchwork.for_testing(self._fake_patchwork_review)
        pwork.project_set(self.PROJ_ID, self.PROJ_LINK_NAME)

        # Review v1 first
        mocks = self._mock_review()
        with contextlib.ExitStack() as stack:
            for m in mocks:
                stack.enter_context(m)
            with terminal.capture() as _:
                self.run_review('-s', str(self.REVIEW_LINK), pwork=pwork)

        # Now review v2 - should detect the previous review
        mocks = self._mock_review()
        with contextlib.ExitStack() as stack:
            for m in mocks:
                stack.enter_context(m)
            with terminal.capture() as (out, _):
                self.run_review('-s', str(self.REVIEW_LINK_V2), pwork=pwork)
        self.assertIn('Previously reviewed', out.getvalue())

        # Check both versions are under the same series
        self.db_open()
        v1 = cser.db.series_find_by_link(str(self.REVIEW_LINK))
        v2 = cser.db.series_find_by_link(str(self.REVIEW_LINK_V2))
        self.assertEqual(v1[0], v2[0])  # same series_id
        self.assertEqual(1, v1[2])  # version 1
        self.assertEqual(2, v2[2])  # version 2

    def test_review_title_search(self):
        """Test searching for a series by title"""
        cser = self.get_cser()
        pwork = Patchwork.for_testing(self._fake_patchwork_review)
        pwork.project_set(self.PROJ_ID, self.PROJ_LINK_NAME)

        mocks = self._mock_review()
        with contextlib.ExitStack() as stack:
            for m in mocks:
                stack.enter_context(m)
            with terminal.capture() as (out, _):
                self.run_review('-S', 'Disable interrupts', pwork=pwork)
        # Should pick the most recent (v2)
        self.assertIn('Using most recent', out.getvalue())

        self.db_open()
        result = cser.db.series_find_by_link(str(self.REVIEW_LINK_V2))
        self.assertIsNotNone(result)

    def test_review_search_series_version(self):
        """Test -V selects a specific version when searching by title"""
        from patman import review as review_mod
        pwork = Patchwork.for_testing(self._fake_patchwork_review)
        pwork.project_set(self.PROJ_ID, self.PROJ_LINK_NAME)

        with terminal.capture():
            # Default picks the most recent (v2)
            self.assertEqual(
                str(self.REVIEW_LINK_V2),
                review_mod.search_series(pwork, self.REVIEW_NAME))
            # -V selects the requested version
            self.assertEqual(
                str(self.REVIEW_LINK),
                review_mod.search_series(pwork, self.REVIEW_NAME, 1))
            self.assertEqual(
                str(self.REVIEW_LINK_V2),
                review_mod.search_series(pwork, self.REVIEW_NAME, 2))

        # An unavailable version errors, listing what is available
        with terminal.capture():
            with self.assertRaises(ValueError) as cm:
                review_mod.search_series(pwork, self.REVIEW_NAME, 5)
        self.assertIn('available', str(cm.exception))
        self.assertIn('v1', str(cm.exception))

    def test_review_patch_title_whole_series(self):
        """Test -P restricts to the found patch unless -w reviews it all"""
        def make_args(whole):
            return Namespace(
                learn_voice=False, sync=False, relink=False, scan=False,
                pw_link=None, title=None, patch=None,
                patch_title='some subject', patches=None, whole_series=whole)

        # search_patch locates the series (link) and the patch's position
        with mock.patch.object(review, 'search_patch',
                               return_value=('link-xyz', 3)), \
                mock.patch.object(review, '_review_link') as rev_link:
            # Default: review just the located patch (index 3)
            args = make_args(False)
            review.do_review(args, None, None)
            self.assertEqual('3', args.patches)
            rev_link.assert_called_once_with(args, None, None, 'link-xyz')

            # -w: locate via the patch but review the whole series
            args = make_args(True)
            review.do_review(args, None, None)
            self.assertIsNone(args.patches)
            rev_link.assert_called_with(args, None, None, 'link-xyz')

    def test_review_no_link_or_title(self):
        """Test that missing -l and -t gives a proper error"""
        self.get_cser()
        pwork = Patchwork.for_testing(self._fake_patchwork_review)
        pwork.project_set(self.PROJ_ID, self.PROJ_LINK_NAME)

        with terminal.capture() as _:
            self.run_review( pwork=pwork, expect_ret=1)

    def test_review_apply_failure(self):
        """Test that apply failure is reported"""
        self.get_cser()
        pwork = Patchwork.for_testing(self._fake_patchwork_review)
        pwork.project_set(self.PROJ_ID, self.PROJ_LINK_NAME)

        with mock.patch('patman.review._apply_and_check',
                        return_value=False), \
             mock.patch('patman.review._get_upstream_branch',
                        return_value='origin/master'), \
             mock.patch('patman.review.gitutil.get_top_level',
                        return_value=self.tmpdir), \
             mock.patch('patman.review.gitutil.ensure_worktree',
                        return_value=self.tmpdir):
            with terminal.capture() as _:
                self.run_review('-s', str(self.REVIEW_LINK),
                                pwork=pwork, expect_ret=1)

    def test_review_create_drafts_dry_run(self):
        """Test dry-run draft creation shows what would be created"""
        self.get_cser()
        pwork = Patchwork.for_testing(self._fake_patchwork_review)
        pwork.project_set(self.PROJ_ID, self.PROJ_LINK_NAME)

        mocks = self._mock_review()
        with contextlib.ExitStack() as stack:
            for m in mocks:
                stack.enter_context(m)
            with terminal.capture() as (out, _):
                self.run_review('-s', str(self.REVIEW_LINK), '--create-drafts',
                                '-n', pwork=pwork)
        output = out.getvalue()
        self.assertIn('Would create draft', output)
        self.assertIn('author@posteo.net', output)
        self.assertIn('trini@konsulko.com', output)

    def test_review_create_drafts(self):
        """Test actual draft creation calls Gmail API"""
        self.get_cser()
        pwork = Patchwork.for_testing(self._fake_patchwork_review)
        pwork.project_set(self.PROJ_ID, self.PROJ_LINK_NAME)

        mocks = self._mock_review()
        with contextlib.ExitStack() as stack:
            for m in mocks:
                stack.enter_context(m)
            with mock.patch('patman.gmail.check_available',
                            return_value=True):
                with mock.patch('patman.gmail.get_service') as mock_svc:
                    mock_svc.return_value.users.return_value \
                        .drafts.return_value \
                        .create.return_value \
                        .execute.return_value = {'id': 'draft123'}
                    mock_svc.return_value.users.return_value \
                        .messages.return_value \
                        .list.return_value \
                        .execute.return_value = {'messages': []}
                    with terminal.capture() as (out, _):
                        self.run_review('-s', str(self.REVIEW_LINK),
                                        '--create-drafts', pwork=pwork)
        output = out.getvalue()
        self.assertIn('Created 1 Gmail draft', output)

    def test_review_redraft(self):
        """Test --redraft recreates drafts for an already-reviewed series"""
        self.get_cser()
        pwork = Patchwork.for_testing(self._fake_patchwork_review)
        pwork.project_set(self.PROJ_ID, self.PROJ_LINK_NAME)

        def run(*extra):
            mocks = self._mock_review()
            with contextlib.ExitStack() as stack:
                for m in mocks:
                    stack.enter_context(m)
                with mock.patch('patman.gmail.check_available',
                                return_value=True):
                    with mock.patch('patman.gmail.get_service') as mock_svc:
                        mock_svc.return_value.users.return_value \
                            .drafts.return_value \
                            .create.return_value \
                            .execute.return_value = {'id': 'draft123'}
                        mock_svc.return_value.users.return_value \
                            .messages.return_value \
                            .list.return_value \
                            .execute.return_value = {'messages': []}
                        with terminal.capture() as (out, _):
                            self.run_review('-s', str(self.REVIEW_LINK),
                                            *extra, pwork=pwork)
            return out.getvalue()

        # The first review stores the reviews and creates the drafts
        self.assertIn('Created 1 Gmail draft', run('--create-drafts'))

        # Re-running with --create-drafts leaves the existing drafts alone
        output = run('--create-drafts')
        self.assertIn('Already reviewed', output)
        self.assertIn('All reviews already have Gmail drafts', output)

        # --redraft deletes the old drafts and recreates them
        with mock.patch('patman.gmail.delete_draft') as mock_del:
            output = run('--redraft')
        self.assertIn('Already reviewed', output)
        self.assertIn('Deleted 1 old Gmail draft', output)
        self.assertIn('Created 1 Gmail draft', output)
        mock_del.assert_called_once()

    def test_delete_gmail_drafts(self):
        """Test only reviews that have a recorded draft are deleted"""
        def rev(idnum, draft_id):
            return database.Review(
                idnum=idnum, svid=1, seq=idnum, body='b', approved=1,
                timestamp='t', draft_id=draft_id, status='draft',
                gmail_msg_id=None, gmail_thread_id=None)

        reviews = [rev(1, 'd1'), rev(2, None), rev(3, 'd3')]
        args = types.SimpleNamespace(gmail_account=None)
        with mock.patch('patman.gmail.check_available', return_value=True), \
                mock.patch('patman.gmail.get_service'), \
                mock.patch('patman.gmail.delete_draft') as mock_del:
            with terminal.capture() as (out, _):
                review._delete_gmail_drafts(args, reviews)
        self.assertEqual(2, mock_del.call_count)
        self.assertIn('Deleted 2 old Gmail draft', out.getvalue())

    def test_review_force_deletes_drafts(self):
        """Test -f re-review removes the old Gmail drafts before recreating"""
        self.get_cser()
        pwork = Patchwork.for_testing(self._fake_patchwork_review)
        pwork.project_set(self.PROJ_ID, self.PROJ_LINK_NAME)

        def run(*extra):
            mocks = self._mock_review()
            with contextlib.ExitStack() as stack:
                for m in mocks:
                    stack.enter_context(m)
                with mock.patch('patman.gmail.check_available',
                                return_value=True), \
                        mock.patch('patman.gmail.get_service') as mock_svc:
                    mock_svc.return_value.users.return_value \
                        .drafts.return_value.create.return_value \
                        .execute.return_value = {'id': 'draft123'}
                    mock_svc.return_value.users.return_value \
                        .messages.return_value.list.return_value \
                        .execute.return_value = {'messages': []}
                    with terminal.capture() as (out, _):
                        self.run_review('-s', str(self.REVIEW_LINK), *extra,
                                        pwork=pwork)
            return out.getvalue()

        # First review creates a draft
        self.assertIn('Created 1 Gmail draft', run('--create-drafts'))

        # Forced re-review with -d deletes the old draft first
        with mock.patch('patman.gmail.delete_draft') as mock_del:
            output = run('-f', '--create-drafts')
        self.assertIn('Re-reviewing (forced)', output)
        self.assertIn('Deleted 1 old Gmail draft', output)
        mock_del.assert_called_once()

    def _fake_patchwork_review_incomplete(self, subpath):
        """Fake Patchwork where v2 has not fully appeared yet"""
        data = self._fake_patchwork_review(subpath)
        m_series = re.match(r'series/(\d+)/$', subpath)
        if m_series and int(m_series.group(1)) == self.REVIEW_LINK_V2:
            data['total'] = 2
            data['received_total'] = 1
        return data

    def _review_v1(self, pwork, link=None):
        """Review the given series in-process, to seed the database"""
        mocks = self._mock_review()
        with contextlib.ExitStack() as stack:
            for m in mocks:
                stack.enter_context(m)
            with terminal.capture() as _:
                self.run_review('-s', str(link or self.REVIEW_LINK),
                                pwork=pwork)

    def test_review_scan(self):
        """Test --scan launches a review of a new version"""
        self.get_cser()
        pwork = Patchwork.for_testing(self._fake_patchwork_review)
        pwork.project_set(self.PROJ_ID, self.PROJ_LINK_NAME)

        # Review v1 first to record the series
        self._review_v1(pwork)

        # Scanning should launch a review of v2 in a child process
        launched = []

        def fake_sub(args, desc, version, link):
            launched.append((version, link))
            return review.ScanResult(desc, version, link, 0, 'reviewed v2')

        with mock.patch('patman.review._review_one_subprocess',
                        side_effect=fake_sub):
            with terminal.capture() as (out, _):
                self.run_review('--scan', pwork=pwork)
        output = out.getvalue()
        self.assertIn('New version v2', output)
        self.assertIn('Launching 1 review(s), 1 at a time', output)
        self.assertIn('[1/1]', output)
        self.assertIn('reviewed v2', output)
        self.assertIn('Scanned: 1 new, 1 reviewed, 0 waiting, 0 skipped, 0 failed',
                      output)
        self.assertEqual([(2, self.REVIEW_LINK_V2)], launched)

    def test_review_scan_dry_run(self):
        """Test --scan -n reports what it would do without reviewing"""
        self.get_cser()
        pwork = Patchwork.for_testing(self._fake_patchwork_review)
        pwork.project_set(self.PROJ_ID, self.PROJ_LINK_NAME)

        # Review v1 first so v2 shows up as a new version
        self._review_v1(pwork)

        with mock.patch('patman.review._review_one_subprocess') as mock_sub:
            with terminal.capture() as (out, _):
                self.run_review('--scan', '-n', pwork=pwork)
        output = out.getvalue()
        self.assertIn('Would review v2', output)
        self.assertIn('Dry run: 1 new, 1 to review, 0 waiting, 0 skipped',
                      output)
        mock_sub.assert_not_called()

    def test_review_scan_no_new(self):
        """Test --scan reports nothing when there is no newer version"""
        self.get_cser()
        pwork = Patchwork.for_testing(self._fake_patchwork_review)
        pwork.project_set(self.PROJ_ID, self.PROJ_LINK_NAME)

        # Review the latest version (v2)
        self._review_v1(pwork, self.REVIEW_LINK_V2)

        with mock.patch('patman.review._review_one_subprocess') as mock_sub:
            with terminal.capture() as (out, _):
                self.run_review('--scan', pwork=pwork)
        self.assertIn('No new versions found', out.getvalue())
        mock_sub.assert_not_called()

    def test_review_scan_incomplete(self):
        """Test --scan waits when the newest version is incomplete"""
        self.get_cser()
        pwork = Patchwork.for_testing(self._fake_patchwork_review_incomplete)
        pwork.project_set(self.PROJ_ID, self.PROJ_LINK_NAME)

        # Review v1 (complete)
        self._review_v1(pwork)

        # v2 is only partly on patchwork; scan should wait, not review it
        with mock.patch('patman.review._review_one_subprocess') as mock_sub:
            with terminal.capture() as (out, _):
                self.run_review('--scan', pwork=pwork)
        output = out.getvalue()
        self.assertIn('Waiting for v2', output)
        self.assertIn('Scanned: 1 new, 0 reviewed, 1 waiting, 0 skipped, 0 failed',
                      output)
        mock_sub.assert_not_called()

    def test_review_inactive_refused(self):
        """Test reviewing a non-active series is refused with the flag named"""
        cser = self.get_cser()
        pwork = Patchwork.for_testing(self._fake_patchwork_review)
        pwork.project_set(self.PROJ_ID, self.PROJ_LINK_NAME)
        self.review_states = {self.REVIEW_LINK: ['superseded']}

        mocks = self._mock_review()
        with contextlib.ExitStack() as stack:
            for m in mocks:
                stack.enter_context(m)
            with terminal.capture() as (out, _):
                self.run_review('-s', str(self.REVIEW_LINK), pwork=pwork,
                                expect_ret=1)
        output = out.getvalue()
        self.assertIn('not active', output)
        self.assertIn('--any-state', output)

        # Nothing was recorded
        self.db_open()
        self.assertIsNone(cser.db.series_find_by_link(str(self.REVIEW_LINK)))

    def test_review_inactive_any_state(self):
        """Test --any-state reviews a non-active series anyway"""
        cser = self.get_cser()
        pwork = Patchwork.for_testing(self._fake_patchwork_review)
        pwork.project_set(self.PROJ_ID, self.PROJ_LINK_NAME)
        self.review_states = {self.REVIEW_LINK: ['superseded']}

        mocks = self._mock_review()
        with contextlib.ExitStack() as stack:
            for m in mocks:
                stack.enter_context(m)
            with terminal.capture() as _:
                self.run_review('-s', str(self.REVIEW_LINK), '--any-state',
                                pwork=pwork)
        self.db_open()
        self.assertIsNotNone(cser.db.series_find_by_link(str(self.REVIEW_LINK)))

    def test_review_scan_skips_inactive(self):
        """Test --scan skips a new version that is not active"""
        self.get_cser()
        pwork = Patchwork.for_testing(self._fake_patchwork_review)
        pwork.project_set(self.PROJ_ID, self.PROJ_LINK_NAME)

        # Review v1 first (active)
        self._review_v1(pwork)

        # v2 has appeared but is not in an active state
        self.review_states = {self.REVIEW_LINK_V2: ['superseded']}
        with mock.patch('patman.review._review_one_subprocess') as mock_sub:
            with terminal.capture() as (out, _):
                self.run_review('--scan', pwork=pwork)
        output = out.getvalue()
        self.assertIn('Skipping v2', output)
        self.assertIn('Scanned: 1 new, 0 reviewed, 0 waiting, 1 skipped, '
                      '0 failed', output)
        mock_sub.assert_not_called()

    def test_review_scan_command(self):
        """Test the child review command passes through the right options"""
        args = types.SimpleNamespace(
            project='myproj', patchwork_url='https://pw.example',
            verbose=False, debug=False, upstream='us', reviewer=None,
            base_branch=None, gmail_account=None, signoff='',
            spelling='British', context=None, create_drafts=True)
        cmd = review._build_review_command(args, self.REVIEW_LINK_V2)

        self.assertEqual(['-m', 'patman'], cmd[1:3])
        self.assertIn('review', cmd)
        # Global options come before the subcommand
        self.assertLess(cmd.index('-P'), cmd.index('review'))
        self.assertEqual('https://pw.example', cmd[cmd.index('-P') + 1])
        self.assertEqual('myproj', cmd[cmd.index('-p') + 1])
        # Series and review options come after
        self.assertEqual(str(self.REVIEW_LINK_V2), cmd[cmd.index('-s') + 1])
        self.assertEqual('us', cmd[cmd.index('-U') + 1])
        self.assertIn('--create-drafts', cmd)
        # The scan has already checked the state, so the child skips it
        self.assertIn('--any-state', cmd)

    def test_register_series_cleans_desc(self):
        """Test a series is stored under its cleaned title, not the cover name"""
        cser = self.get_cser()
        series_data = {
            'patches': [{'id': 1, 'name': '[v2,1/1] foo bar'}],
            'cover_letter': {'id': 9, 'name': '[v2,0/1] foo bar'},
        }
        # Two versions of the same series must land under one record
        review._register_series(cser, 'foo bar', 1, '500', series_data)
        review._register_series(cser, 'foo bar', 2, '501', series_data)
        cser.commit()

        self.db_open()
        v1 = cser.db.series_find_by_link('500')
        v2 = cser.db.series_find_by_link('501')
        self.assertIsNotNone(v1)
        self.assertIsNotNone(v2)
        self.assertEqual(v1[0], v2[0])  # linked under the same series

    def test_review_get_previous_skips_gap(self):
        """Test prior-review lookup skips versions that have no reviews"""
        cser = self.get_cser()
        db = cser.db
        sid = db.series_add('pw-x-review', 'foo bar')
        db.series_set_source(sid, 'review')
        sv1 = db.ser_ver_add(sid, 1, link='10')
        db.review_add(sv1, 1, 'v1 body', 1, '2026-01-01')
        db.ser_ver_add(sid, 2, link='11')  # v2 has no reviews
        cser.commit()

        # Reviewing v3 should fall back to v1, not the empty v2
        prev = db.review_get_previous(sid, 3)
        self.assertEqual(1, len(prev))
        self.assertEqual('v1 body', prev[0].body)

    def test_review_relink(self):
        """Test --relink merges version records split by the old bug"""
        cser = self.get_cser()
        db = cser.db
        # Simulate the old bug: two unlinked records, raw prefixed descs
        s1 = db.series_add('pw-1-review', '[v1,0/2] foo bar')
        db.series_set_source(s1, 'review')
        sv1 = db.ser_ver_add(s1, 1, link='1')
        db.review_add(sv1, 1, 'v1 body', 1, '2026-01-01')
        s2 = db.series_add('pw-2-review', '[v2,0/3] foo bar')
        db.series_set_source(s2, 'review')
        sv2 = db.ser_ver_add(s2, 2, link='2')
        db.review_add(sv2, 1, 'v2 body', 1, '2026-01-02')
        cser.commit()

        # Before: separate series, v2 has no prior context
        self.assertNotEqual(s1, s2)
        self.assertEqual([], db.review_get_previous(s2, 2))

        with terminal.capture() as (out, _):
            self.run_review('--relink')
        self.assertIn('Relinked 1', out.getvalue())

        # After: one series with both versions; v2 now sees v1's review
        self.db_open()
        v1 = cser.db.series_find_by_link('1')
        v2 = cser.db.series_find_by_link('2')
        self.assertEqual(v1[0], v2[0])
        prev = cser.db.review_get_previous(v1[0], 2)
        self.assertEqual(1, len(prev))
        self.assertEqual('v1 body', prev[0].body)

    def test_review_lock_in_progress(self):
        """Test a review is refused when one is already running for it"""
        cser = self.get_cser()
        pwork = Patchwork.for_testing(self._fake_patchwork_review)
        pwork.project_set(self.PROJ_ID, self.PROJ_LINK_NAME)

        # Hold the lock for this series, as if a review were in progress
        ups = pwork.upstream if pwork else None
        branch = review._make_review_name(str(self.REVIEW_LINK), ups)
        lock_fd = review._acquire_review_lock(self.tmpdir, branch)
        try:
            mocks = self._mock_review()
            with contextlib.ExitStack() as stack:
                for m in mocks:
                    stack.enter_context(m)
                with terminal.capture() as (out, err):
                    self.run_review('-s', str(self.REVIEW_LINK), pwork=pwork)
            self.assertIn('already in progress', err.getvalue())
        finally:
            review._release_review_lock(lock_fd)

        # The refused review recorded nothing
        self.db_open()
        self.assertIsNone(cser.db.series_find_by_link(str(self.REVIEW_LINK)))

        # With the lock released, the review now runs
        mocks = self._mock_review()
        with contextlib.ExitStack() as stack:
            for m in mocks:
                stack.enter_context(m)
            with terminal.capture() as _:
                self.run_review('-s', str(self.REVIEW_LINK), pwork=pwork)
        self.db_open()
        self.assertIsNotNone(cser.db.series_find_by_link(str(self.REVIEW_LINK)))

    def _make_review_ctx(self, reviewer_name='Test', reviewer_email='test@test.com',
                         author_name='', author_email='', date='', signoff=None,
                         diffstat=None):
        """Build a ReviewContext for testing format_review_email()"""
        from patman.review import ReviewContext

        ctx = ReviewContext(None, None,
            {'submitter': {'name': author_name, 'email': author_email},
             'date': date})
        ctx.reviewer_name = reviewer_name
        ctx.reviewer_email = reviewer_email
        ctx.signoff = signoff
        ctx.diffstat = diffstat
        return ctx

    def test_review_parse_approved(self):
        """Test parsing an approved review"""
        from patman.review import parse_review_output, format_review_email

        text = """GREETING: Marek
VERDICT: approved"""
        greeting, verdict, comments = parse_review_output(text)
        self.assertEqual('Marek', greeting)
        self.assertEqual('approved', verdict)
        self.assertEqual([], comments)

        ctx = self._make_review_ctx(author_name='Marek Vasut',
            author_email='marex@denx.de', date='2026-03-21',
            diffstat=' drivers/pci.c | 2 +-\n 1 file changed')
        email = format_review_email(ctx, greeting, verdict, comments,
            commit_message='pci: Fix the return type\n\nThe return is wrong.')
        self.assertNotIn('Hi Marek,', email)
        self.assertIn('On 2026-03-21, Marek Vasut', email)
        self.assertIn('> pci: Fix the return type', email)
        self.assertIn('> The return is wrong.', email)
        self.assertIn('> drivers/pci.c', email)
        self.assertIn('Reviewed-by: Test <test@test.com>', email)

    def test_review_parse_skip(self):
        """Test parsing a skipped review (e.g. cover letter with no issues)"""
        from patman.review import parse_review_output

        text = """GREETING: Michal
VERDICT: skip"""
        greeting, verdict, comments = parse_review_output(text)
        self.assertEqual('Michal', greeting)
        self.assertEqual('skip', verdict)
        self.assertEqual([], comments)

    def test_review_empty_dropped(self):
        """Test a non-approval with no comments yields no review"""
        from patman import review as review_mod

        ctx = self._make_review_ctx(author_name='Quentin',
            author_email='qstrydom0@gmail.com', date='2026-06-19')
        ctx.repo_path = self.tmpdir
        ctx.previous_reviews = {}
        cmt = types.SimpleNamespace(hash='abc1234', subject='spl: pad',
                                    msg='spl: pad\n\nbody text', rtags={})

        async def mock_agent(prompt, options):
            # A greeting but no COMMENT block and no VERDICT line
            return True, 'GREETING: Quentin\n'

        loop = asyncio.new_event_loop()
        with mock.patch.object(review_mod.claude_mod, 'run_agent_collect',
                               side_effect=mock_agent), \
             mock.patch.object(review_mod, 'ClaudeAgentOptions',
                               mock.MagicMock()), \
             mock.patch.object(review_mod, '_build_review_prompt',
                               return_value='prompt'), \
             mock.patch.object(review_mod.gitutil, 'diff_stat',
                               return_value=''), \
             terminal.capture():
            result = loop.run_until_complete(
                review_mod._review_single_patch(
                    ctx, cmt, 1, [(1, 'abc1234', 'spl: pad')]))
        loop.close()
        self.assertIsNone(result)

    def test_review_is_code_comment(self):
        """Test code comments are told apart from commit-message comments"""
        from patman.review import _is_code_comment

        self.assertTrue(_is_code_comment(
            '> diff --git a/foo.c b/foo.c\n> @@ -1 +1 @@\n> +code'))
        self.assertTrue(_is_code_comment('> @@ -10,2 +10,3 @@ func()'))
        self.assertFalse(_is_code_comment(
            '> When CONFIG_SPL_SEPARATE_BSS is enabled'))
        self.assertFalse(_is_code_comment(''))

    def test_review_commit_msg_comment_first(self):
        """Test commit-message comments come before code comments"""
        from patman.review import format_review_email

        ctx = self._make_review_ctx(author_name='Quentin',
            author_email='q@gmail.com', date='2026-06-19')
        # Agent emitted the code comment first, commit-message comment second
        comments = [
            ('> diff --git a/foo.c b/foo.c\n> @@ -1 +1 @@\n> +code line',
             'This code comment.'),
            ('> When CONFIG_SPL_SEPARATE_BSS is enabled',
             'This commit-message comment.'),
        ]
        body = format_review_email(ctx, 'Quentin', 'changes_needed', comments)
        self.assertLess(body.index('This commit-message comment.'),
                        body.index('This code comment.'))

    def test_coverity_find_new_defects(self):
        """Test only defects absent from the base are reported as new"""
        from patman import coverity
        base = [{'mergeKey': 'a'}, {'mergeKey': 'b'}]
        patched = [{'mergeKey': 'a'}, {'mergeKey': 'c'}]
        new = coverity.find_new_defects(base, patched)
        self.assertEqual([{'mergeKey': 'c'}], new)

    def test_coverity_format_defect(self):
        """Test a defect is summarised with checker, location and text"""
        from patman import coverity
        defect = {
            'checkerName': 'RESOURCE_LEAK',
            'mainEventFilePathname': 'drivers/foo.c',
            'mainEventLineNumber': 42,
            'functionDisplayName': 'foo_probe',
            'subcategoryLongDescription': 'Handle leaked',
        }
        summary = coverity.format_defect(defect)
        self.assertEqual(
            'RESOURCE_LEAK: drivers/foo.c:42 (foo_probe): Handle leaked',
            summary)

    def test_coverity_check_available(self):
        """Test availability depends on all three cov tools being present"""
        from patman import coverity
        with mock.patch('patman.coverity.shutil.which',
                        return_value='/usr/bin/x'):
            self.assertTrue(coverity.check_available())
        with mock.patch('patman.coverity.shutil.which', return_value=None):
            self.assertFalse(coverity.check_available())

    def test_coverity_analyze(self):
        """Test analyze() configures, builds under cov-build and parses"""
        import json
        from patman import coverity
        emit = os.path.join(self.tmpdir, 'emit')
        os.makedirs(emit, exist_ok=True)
        cmds = []

        def fake_run(cmd, cwd):
            cmds.append(cmd)
            if cmd[0] == 'cov-format-errors':
                out = cmd[cmd.index('--json-output-v7') + 1]
                with open(out, 'w', encoding='utf-8') as fd:
                    json.dump({'issues': [{'mergeKey': 'k1'}]}, fd)

        with mock.patch('patman.coverity._run', side_effect=fake_run):
            issues = coverity.analyze(self.tmpdir, 'sandbox_defconfig', emit)
        self.assertEqual([{'mergeKey': 'k1'}], issues)
        self.assertEqual(['make', 'sandbox_defconfig'], cmds[0])
        self.assertEqual('cov-build', cmds[1][0])
        self.assertEqual('cov-analyze', cmds[2][0])

    def test_run_coverity_unavailable(self):
        """Test --coverity is skipped cleanly when the tools are missing"""
        from patman import review as review_mod
        ctx = self._make_review_ctx()
        args = types.SimpleNamespace(coverity_defconfig=None)
        with mock.patch('patman.coverity.check_available',
                        return_value=False):
            with terminal.capture() as (out, err):
                result = review_mod._run_coverity(ctx, args)
        self.assertIsNone(result)
        self.assertIn('skipping --coverity', err.getvalue())

    def test_run_coverity_reports_new(self):
        """Test _run_coverity returns a summary of the new defects only"""
        from patman import review as review_mod
        ctx = self._make_review_ctx()
        ctx.main_repo = self.tmpdir
        ctx.repo_path = self.tmpdir
        ctx.upstream_branch = 'us/master'
        base = [{'mergeKey': 'a'}]
        patched = [
            {'mergeKey': 'a'},
            {'mergeKey': 'b', 'checkerName': 'RESOURCE_LEAK',
             'mainEventFilePathname': 'drivers/foo.c',
             'mainEventLineNumber': 42,
             'subcategoryLongDescription': 'Handle leaked'},
        ]
        args = types.SimpleNamespace(coverity_defconfig=None)
        with mock.patch('patman.coverity.check_available',
                        return_value=True), \
                mock.patch('patman.coverity.analyze',
                           side_effect=[base, patched]), \
                mock.patch('patman.review.subprocess.run'), \
                mock.patch('patman.review.gitutil.remove_worktree'):
            with terminal.capture():
                text = review_mod._run_coverity(ctx, args)
        self.assertIn('RESOURCE_LEAK', text)
        self.assertIn('drivers/foo.c:42', text)
        self.assertNotIn('mergeKey', text)

    def test_review_prompt_coverity(self):
        """Test new Coverity defects are placed in the review prompt"""
        from patman import review as review_mod
        ctx = self._make_review_ctx()
        ctx.comments_path = None
        ctx.spelling = 'British'
        ctx.coverity_text = '- RESOURCE_LEAK: drivers/foo.c:42: Handle leaked'
        with mock.patch.object(review_mod, 'get_voice', return_value=None):
            prompt = review_mod._build_review_prompt(
                ctx, 'abc1234', 1, [(1, 'abc1234', 'subj')], None)
        self.assertIn('COVERITY', prompt)
        self.assertIn('drivers/foo.c:42', prompt)

    def test_review_aligns_by_subject(self):
        """Test reviews attach to the patchwork patch by subject, not order"""
        from patman import review as review_mod
        from patman.review import ReviewContext

        ctx = ReviewContext(None, None, {'patches': [
            {'name': '[v2,1/3] alpha'},
            {'name': '[v2,2/3] beta'},
            {'name': '[v2,3/3] gamma'}]})
        ctx.branch_name = 'b'
        ctx.upstream_branch = 'u'
        ctx.main_repo = self.tmpdir
        ctx.patch_count = 3
        ctx.cover_content = None
        ctx.svid = None
        ctx.patch_selection = None
        ctx.reviewer_name = 'Test'
        ctx.reviewer_email = 't@t.com'

        # The branch has only beta and gamma applied; alpha (1/3) failed to
        # apply, so a positional mapping would misattribute the reviews
        commits = [types.SimpleNamespace(subject='beta', hash='h2', rtags={}),
                   types.SimpleNamespace(subject='gamma', hash='h3', rtags={})]
        series = types.SimpleNamespace(commits=commits)

        async def fake_single(ctx, cmt, seq, all_commits):
            return f'review-{cmt.subject}'

        loop = asyncio.new_event_loop()
        with mock.patch.object(review_mod.claude_mod, 'check_available',
                               return_value=True), \
                mock.patch.object(review_mod.patchstream,
                                  'get_metadata_for_list',
                                  return_value=series), \
                mock.patch.object(review_mod, '_review_single_patch',
                                  side_effect=fake_single), \
                terminal.capture():
            bodies = loop.run_until_complete(review_mod.review_patches(ctx))
        loop.close()
        # beta is patchwork patch 2 and gamma is 3 -- not 1 and 2
        self.assertEqual({2: 'review-beta', 3: 'review-gamma'}, bodies)

    def test_format_findings(self):
        """Test findings are formatted for storage"""
        from patman.review import _format_findings
        self.assertEqual('Looks good; no issues found.',
                         _format_findings('approved', []))
        body = _format_findings('changes_needed', [('> code line', 'Do X.')])
        self.assertIn('> code line', body)
        self.assertIn('Do X.', body)

    def test_series_review_stores(self):
        """Test 'series review' stores the findings in the database"""
        from patman import review as review_mod
        cser = self.get_cser()
        sid = cser.db.series_add('foo', 'foo')
        cser.db.ser_ver_add(sid, 1)
        cser.commit()
        svid = cser.get_ser_ver(sid, 1).idnum

        commits = [
            types.SimpleNamespace(hash='h1', subject='alpha',
                                  msg='alpha\n\nbody', rtags={}),
            types.SimpleNamespace(hash='h2', subject='beta',
                                  msg='beta\n\nbody', rtags={})]
        series = types.SimpleNamespace(
            commits=commits, cover=['Cover subject', 'cover body'])
        cover = ('', 'changes_needed', [('', 'Add a Changes-in-v2 block.')])
        results = [
            ('', 'changes_needed',
             [('> diff --git a/x b/x\n> @@ -1 +1 @@', 'Fix this.')], 'm'),
            ('', 'approved', [], 'm')]

        with mock.patch.object(review_mod.claude_mod, 'check_available',
                               return_value=True), \
                mock.patch.object(review_mod.gitutil, 'get_top_level',
                                  return_value=self.tmpdir), \
                mock.patch.object(review_mod, '_run_cover_review_sync',
                                  return_value=cover), \
                mock.patch.object(review_mod, '_run_patch_review_sync',
                                  side_effect=results), \
                terminal.capture():
            review_mod.review_series(cser, sid, svid, 1, 'foo', series)

        stored = {r.seq: r for r in cser.db.review_get_for_version(svid)}
        self.assertEqual(3, len(stored))
        self.assertIn('Changes-in-v2', stored[0].body)  # cover letter (seq 0)
        self.assertIn('Fix this.', stored[1].body)
        self.assertEqual(0, stored[1].approved)
        self.assertIn('Looks good', stored[2].body)
        self.assertEqual(1, stored[2].approved)

    def test_series_review_already(self):
        """Test 'series review' refuses to overwrite without --force"""
        cser = self.get_cser()
        sid = cser.db.series_add('foo', 'foo')
        cser.db.ser_ver_add(sid, 1)
        cser.commit()
        svid = cser.get_ser_ver(sid, 1).idnum
        cser.db.review_add(svid, 1, 'body', False, 't')
        cser.commit()

        with self.assertRaises(ValueError) as cm:
            cser.review('foo', 1)
        self.assertIn('already has', str(cm.exception))

    def _relay_submit(self, response_bytes, reflect=False):
        """Call relay.submit with urlopen mocked to return response_bytes

        Returns (result, captured_request_body).
        """
        import json
        from patman import relay

        class Resp:
            def read(self):
                return response_bytes

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        captured = {}

        def fake_open(req, timeout=None):
            captured['url'] = req.full_url
            captured['body'] = json.loads(req.data)
            return Resp()

        with mock.patch('patman.relay.urllib.request.urlopen', fake_open):
            with terminal.capture():
                result = relay.submit('https://relay.example/submit',
                                      ['m1', 'm2'], reflect=reflect)
        return result, captured

    def test_relay_submit(self):
        """Test the relay submit request and success handling"""
        n, cap = self._relay_submit(b'{"result": "success"}')
        self.assertEqual(2, n)
        self.assertEqual('receive', cap['body']['action'])
        self.assertEqual(['m1', 'm2'], cap['body']['messages'])

        # --reflect uses a different action
        _, cap = self._relay_submit(b'{"result": "success"}', reflect=True)
        self.assertEqual('reflect', cap['body']['action'])

    def test_relay_submit_error(self):
        """Test an error result from the endpoint is raised with its message"""
        with self.assertRaises(ValueError) as cm:
            self._relay_submit(b'{"result": "error", "message": "no key"}')
        self.assertIn('no key', str(cm.exception))

    def test_relay_submit_bad_json(self):
        """Test a non-JSON response is reported clearly"""
        with self.assertRaises(ValueError) as cm:
            self._relay_submit(b'<html>nope</html>')
        self.assertIn('Unexpected response', str(cm.exception))

    def test_relay_sign_message(self):
        """Test sign_message delegates to patatt"""
        from patman import relay
        fake = mock.MagicMock()
        fake.rfc2822_sign.return_value = b'signed body'
        with mock.patch.dict('sys.modules', {'patatt': fake}):
            out = relay.sign_message(b'raw body')
        self.assertEqual(b'signed body', out)
        fake.rfc2822_sign.assert_called_once_with(b'raw body')

    def test_relay_sign_no_key(self):
        """Test a missing patatt key gives a helpful message, not a traceback"""
        from patman import relay
        fake = types.ModuleType('patatt')

        class NoKeyError(Exception):
            pass

        fake.NoKeyError = NoKeyError
        fake.SigningError = type('SigningError', (Exception,), {})

        def rfc2822_sign(data):
            raise NoKeyError('patatt.signingkey is not set')

        fake.rfc2822_sign = rfc2822_sign
        with mock.patch.dict('sys.modules', {'patatt': fake}):
            with self.assertRaises(ValueError) as cm:
                relay.sign_message(b'x')
        self.assertIn('patatt.signingkey', str(cm.exception))

    def test_relay_auth_new(self):
        """Test auth_new posts the registration request"""
        import json
        from patman import relay

        class Resp:
            def read(self):
                return b'{"result": "success"}'

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        captured = {}

        def fake_open(req, timeout=None):
            captured['body'] = json.loads(req.data)
            return Resp()

        with mock.patch.object(relay, '_auth_config',
                               return_value=('Me', 'me@x', 'sel', 'PUBKEY')), \
                mock.patch('patman.relay.urllib.request.urlopen', fake_open):
            with terminal.capture():
                relay.auth_new('https://relay/x')
        body = captured['body']
        self.assertEqual('auth-new', body['action'])
        self.assertEqual('Me', body['name'])
        self.assertEqual('me@x', body['identity'])
        self.assertEqual('sel', body['selector'])
        self.assertEqual('PUBKEY', body['pubkey'])

    def test_relay_auth_verify(self):
        """Test auth_verify signs the challenge and posts it"""
        import email as email_mod
        import json
        from patman import relay

        class Resp:
            def read(self):
                return b'{"result": "success"}'

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        captured = {}

        def fake_open(req, timeout=None):
            captured['body'] = json.loads(req.data)
            return Resp()

        with mock.patch.object(relay, '_auth_config',
                               return_value=('Me', 'me@x', 'sel', 'PUB')), \
                mock.patch.object(relay, 'sign_message',
                                  side_effect=lambda data: b'SIGNED:' + data), \
                mock.patch('patman.relay.urllib.request.urlopen', fake_open):
            with terminal.capture():
                relay.auth_verify('https://relay/x', 'CHAL')
        body = captured['body']
        self.assertEqual('auth-verify', body['action'])
        self.assertTrue(body['msg'].startswith('SIGNED:'))
        # The signed message is a MIME message carrying the challenge
        inner = email_mod.message_from_string(body['msg'][len('SIGNED:'):])
        self.assertEqual('me@x', inner['From'])
        self.assertEqual('b4-send-verify', inner['Subject'])
        self.assertEqual(b'verify:CHAL\n', inner.get_payload(decode=True))

    def test_send_via_relay_threading(self):
        """Test relay threading: patches reply to the cover, root to -r"""
        from patman import send as send_mod
        from patman import relay
        import email as email_mod

        d = self.tmpdir
        # cover and patch 'a' have Message-Ids; patch 'b' has none (generated)
        specs = [('0000-cover.patch', 'cover', 'Message-Id: <cover@x>\n'),
                 ('0001-a.patch', 'a', 'Message-Id: <a@x>\n'),
                 ('0002-b.patch', 'b', '')]
        for name, subj, mid in specs:
            with open(os.path.join(d, name), 'w') as fd:
                fd.write(f'From: Me <me@example.org>\nSubject: {subj}\n'
                         f'{mid}\nbody\n')
        ccf = os.path.join(d, 'cc')
        with open(ccf, 'w') as fd:
            for name, _, _ in specs:
                fd.write(f'{name} \n')
        series = types.SimpleNamespace(get=lambda key, dflt=None: dflt)

        sent = {}

        def fake_submit(endpoint, messages, reflect=False):
            sent['messages'] = messages
            return len(messages)

        with mock.patch.object(relay, 'sign_message',
                               side_effect=lambda data: data), \
                mock.patch.object(relay, 'submit', side_effect=fake_submit):
            with terminal.capture():
                send_mod.send_via_relay(
                    series, '0000-cover.patch',
                    ['0001-a.patch', '0002-b.patch'], ccf, 'https://r/x',
                    reflect=False, dry_run=False, thread=True,
                    in_reply_to='<prev@x>', cwd=d)

        cover = email_mod.message_from_string(sent['messages'][0])
        a = email_mod.message_from_string(sent['messages'][1])
        b = email_mod.message_from_string(sent['messages'][2])
        # The root replies to the --in-reply-to message
        self.assertEqual('<prev@x>', cover['In-Reply-To'])
        # Patches reply to the cover (shallow threading)
        self.assertEqual('<cover@x>', a['In-Reply-To'])
        self.assertEqual('<cover@x>', b['In-Reply-To'])
        self.assertIn('<cover@x>', a['References'])
        self.assertIn('<prev@x>', a['References'])
        # The patch with no Message-Id got one generated
        self.assertTrue(b['Message-ID'])

    def test_send_web_auth_requires_endpoint(self):
        """Test a web-auth action without an endpoint errors clearly"""
        from patman import send as send_mod
        args = types.SimpleNamespace(send_endpoint_web=None,
                                     web_auth_new=True, web_auth_verify=None)
        with self.assertRaises(ValueError) as cm:
            send_mod.send(args)
        self.assertIn('No web endpoint', str(cm.exception))

    def test_send_endpoint_no_relay(self):
        """Test --no-relay forces git send-email over a configured relay"""
        from patman import send as send_mod
        args = types.SimpleNamespace(send_endpoint_web='https://relay/x',
                                     no_relay=False)
        self.assertEqual('https://relay/x', send_mod._send_endpoint(args))
        args.no_relay = True
        self.assertIsNone(send_mod._send_endpoint(args))
        # No relay configured -> None regardless
        args = types.SimpleNamespace(send_endpoint_web=None, no_relay=False)
        self.assertIsNone(send_mod._send_endpoint(args))

    def test_send_parse_cc_file(self):
        """Test parsing the MakeCcFile output, incl. names with spaces"""
        from patman import send as send_mod
        path = os.path.join(self.tmpdir, 'cc')
        with open(path, 'w', encoding='utf-8') as fd:
            # '<fname> <cc1>\0<cc2>' -- cc addresses may contain spaces
            fd.write('0001-a.patch a@x\0Bob B <bob@x>\n')
            fd.write('0002-b.patch \n')  # no Cc
        cc_map = send_mod._parse_cc_file(path)
        self.assertEqual(['a@x', 'Bob B <bob@x>'], cc_map['0001-a.patch'])
        self.assertEqual([], cc_map['0002-b.patch'])

    def test_send_via_relay(self):
        """Test the relay send builds signed messages with To/Cc and posts"""
        from patman import send as send_mod
        from patman import relay
        import email as email_mod

        d = self.tmpdir
        with open(os.path.join(d, '0000-cover.patch'), 'w') as fd:
            fd.write('From: Me <me@x>\nSubject: cover\n'
                     'Message-Id: <0@x>\n\ncover body\n')
        with open(os.path.join(d, '0001-first.patch'), 'w') as fd:
            fd.write('From: Me <me@x>\nSubject: first\n'
                     'Message-Id: <1@x>\n\npatch body\n')
        ccf = os.path.join(d, 'cc')
        with open(ccf, 'w') as fd:
            fd.write('0000-cover.patch a@x\0Bob B <bob@x>\n')
            fd.write('0001-first.patch b@x\n')

        series = types.SimpleNamespace(
            get=lambda key, dflt=None: ['to@list'] if key == 'to' else dflt)

        sent = {}

        def fake_submit(endpoint, messages, reflect=False):
            sent.update(endpoint=endpoint, messages=messages, reflect=reflect)
            return len(messages)

        with mock.patch.object(relay, 'sign_message',
                               side_effect=lambda data: data), \
                mock.patch.object(relay, 'submit', side_effect=fake_submit):
            with terminal.capture():
                n = send_mod.send_via_relay(
                    series, '0000-cover.patch', ['0001-first.patch'], ccf,
                    'https://relay/x', reflect=False, dry_run=False, cwd=d)

        self.assertEqual(2, n)
        self.assertEqual('https://relay/x', sent['endpoint'])
        cover = email_mod.message_from_string(sent['messages'][0])
        self.assertEqual('to@list', cover['To'])
        self.assertIn('a@x', cover['Cc'])
        self.assertIn('Bob B <bob@x>', cover['Cc'])
        patch = email_mod.message_from_string(sent['messages'][1])
        self.assertEqual('b@x', patch['Cc'])
        self.assertEqual('patman', patch['X-Mailer'])

        # Dry run posts nothing
        sent.clear()
        with mock.patch.object(relay, 'sign_message',
                               side_effect=lambda data: data), \
                mock.patch.object(relay, 'submit',
                                  side_effect=fake_submit) as mock_submit:
            with terminal.capture():
                n = send_mod.send_via_relay(
                    series, None, ['0001-first.patch'], ccf,
                    'https://relay/x', reflect=True, dry_run=True, cwd=d)
        self.assertEqual(0, n)
        mock_submit.assert_not_called()

    def test_review_guess_name(self):
        """Test guessing first name from email address"""
        from patman.review import guess_name_from_email

        self.assertEqual('Simon', guess_name_from_email('simon.glass@xxx'))
        self.assertEqual('Michal', guess_name_from_email('michal@amd.com'))
        self.assertEqual('Marek',
                         guess_name_from_email('marek-vasut@denx.de'))
        self.assertEqual('', guess_name_from_email('j@posteo.net'))
        self.assertEqual('', guess_name_from_email(''))
        self.assertEqual('', guess_name_from_email('12345@test.com'))

    def test_review_cleanup(self):
        """Test mechanical cleanup of review text"""
        from patman.review import cleanup_review_text

        # Backticks removed
        self.assertEqual('Use foo here',
                         cleanup_review_text('Use `foo` here'))

        # Quoted function references unquoted
        self.assertEqual('Call malloc() first',
                         cleanup_review_text("Call 'malloc()' first"))
        self.assertEqual('Call free() after',
                         cleanup_review_text('Call "free()" after'))

        # Normal quotes preserved
        self.assertEqual("Normal 'text' stays",
                         cleanup_review_text("Normal 'text' stays"))

        # Double-quoted short tokens in our own prose become single-quoted
        self.assertEqual("Use 'handoff' here",
                         cleanup_review_text('Use "handoff" here'))

        # Quoted lines are reproduced verbatim: the author's code, with its
        # exact quotes and any backticks, must not be restyled
        self.assertEqual('> +\tkeyfile = "some_key";',
                         cleanup_review_text('> +\tkeyfile = "some_key";'))
        self.assertEqual('> +\t`something`',
                         cleanup_review_text('> +\t`something`'))

        # The fix applies within a full email: prose is cleaned, the quoted
        # commit-message line keeps its double quotes
        email = ('> +\tkeyfile = "some_key";\n\n'
                 'Please quote "some_key" consistently')
        self.assertEqual(
            '> +\tkeyfile = "some_key";\n\n'
            "Please quote 'some_key' consistently",
            cleanup_review_text(email))

    def test_review_greeting_fallback(self):
        """Test greeting falls back to email when name is empty"""
        from patman.review import format_review_email

        # Empty greeting should be guessed from email
        ctx = self._make_review_ctx(author_name='Simon Glass',
            author_email='simon.glass@xxx.com', date='2026-04-01',
            signoff='Regards,\nTest')
        email = format_review_email(ctx, '', 'changes_needed',
            [('> +\tsome code', 'Fix this')])
        self.assertIn('Hi Simon,', email)

        # Unguessable email falls back to bare 'Hi,'
        ctx = self._make_review_ctx(author_email='x@test.com',
                                     date='2026-04-01')
        email = format_review_email(ctx, '', 'changes_needed',
            [('> +\tsome code', 'Fix this')])
        self.assertIn('Hi,', email)
        self.assertNotIn('Hi ,', email)

    def test_review_refine_skips_approved(self):
        """Test that refinement skips approved reviews without comments"""
        import asyncio
        from unittest.mock import patch, AsyncMock

        from patman.review import refine_reviews

        # An approved review with only structural lines
        approved = ('On 2026-04-01, A <a@b.com> wrote:\n'
                    '> Some commit message\n'
                    '>\n'
                    '> drivers/foo.c | 2 +-\n'
                    '\n'
                    'Reviewed-by: Test <test@test.com>\n')

        # Should be returned unchanged without calling the agent
        loop = asyncio.new_event_loop()
        with patch('patman.review.get_voice', return_value=None):
            result = loop.run_until_complete(
                refine_reviews({1: approved}))
        loop.close()
        self.assertEqual({1: approved}, result)

    def test_review_refine_processes_comments(self):
        """Test that refinement processes reviews with comments"""
        import asyncio
        import sys
        import types
        from unittest.mock import patch, MagicMock

        review_with_comments = (
            'Hi Marek,\n\n'
            'On 2026-04-01, Marek <m@d.de> wrote:\n'
            '> +\tsome code\n\n'
            'This needs fixing.\n\n'
            'Regards,\nSimon\n')

        # Mock the agent to return a slightly trimmed version
        refined = '---SEQ 1---\n' + review_with_comments.replace(
            'This needs fixing.', 'Fix this.')

        async def mock_agent(prompt, options):
            return True, refined

        from patman import review as review_mod

        loop = asyncio.new_event_loop()
        with patch.object(review_mod, 'get_voice', return_value=None), \
             patch.object(review_mod.claude_mod, 'run_agent_collect',
                          side_effect=mock_agent), \
             patch.object(review_mod, 'ClaudeAgentOptions', MagicMock()), \
             terminal.capture():
            result = loop.run_until_complete(
                review_mod.refine_reviews({1: review_with_comments}))
        loop.close()
        self.assertIn('Fix this.', result[1])

    def test_review_parse_changes(self):
        """Test parsing a review with comments"""
        from patman.review import parse_review_output, format_review_email

        text = """GREETING: J.
COMMENT:
> +	if (ret < 0)
> +		return ret;

This should use goto err instead.

COMMENT:
> +	bootm_disable_interrupts();

This call should be conditional.

VERDICT: changes_needed"""

        greeting, verdict, comments = parse_review_output(text)
        self.assertEqual('J.', greeting)
        self.assertEqual('changes_needed', verdict)
        self.assertEqual(2, len(comments))
        self.assertIn('goto err', comments[0][1])
        self.assertIn('conditional', comments[1][1])

        ctx = self._make_review_ctx(author_name='J. Neuschäfer',
            author_email='j.ne@posteo.net', date='2026-03-29',
            signoff='Regards,\nSimon')
        email = format_review_email(ctx, greeting, verdict, comments)
        self.assertIn('Hi J.,', email)
        self.assertIn('> +\tif (ret < 0)', email)
        self.assertIn('goto err', email)
        self.assertNotIn('Reviewed-by', email)
        self.assertIn('Regards,\nSimon', email)

    def test_review_commit_msg_no_duplicate(self):
        """Test that the commit-msg builder avoids duplicating the subject"""
        # Simulate cmt.subject and cmt.msg where msg starts with subject
        subject = 'Drop unused macros'
        msg_with_dup = 'Drop unused macros\n\nThese macros are never used.'
        msg_without_dup = '\nThese macros are never used.'

        # When body starts with subject, use body as-is
        body = msg_with_dup.strip()
        if body.startswith(subject):
            commit_msg = body
        else:
            commit_msg = (subject + '\n' + body).strip()
        self.assertEqual(1, commit_msg.count('Drop unused macros'))

        # When body doesn't start with subject, prepend it
        body = msg_without_dup.strip()
        if body.startswith(subject):
            commit_msg = body
        else:
            commit_msg = (subject + '\n' + body).strip()
        self.assertTrue(commit_msg.startswith('Drop unused macros'))
        self.assertIn('These macros are never used.', commit_msg)

    def test_review_commit_msg_with_body(self):
        """Test that subject + body are both quoted when body differs"""
        from patman.review import format_review_email

        ctx = self._make_review_ctx(author_name='Author',
            author_email='a@b.com', date='2026-04-01',
            diffstat=' file.c | 1 +\n 1 file changed')
        email = format_review_email(ctx, '', 'approved', [],
            commit_message='Fix the bug\n\nThe bug causes a crash.')
        self.assertIn('> Fix the bug', email)
        self.assertIn('> The bug causes a crash.', email)

    def test_gmail_subject_preserves_patch_prefix(self):
        """Test that reply subjects use the original Subject header"""
        from patman.gmail import create_review_drafts

        series_data = {
            'submitter': {'email': 'a@b.com'},
            'project': {'list_email': 'list@test.com'},
            'cover_letter': None,
            'patches': [
                {'id': 1, 'name': 'Fix the bug',
                 'msgid': '<1@test.com>'},
            ],
        }
        patch_headers = {
            1: {'Subject': '[PATCH v2 1/3] Fix the bug',
                'Message-Id': '<1@test.com>'},
        }
        review_bodies = {1: 'Reviewed-by: Test <test@test.com>'}

        with terminal.capture():
            create_review_drafts(series_data, review_bodies,
                                 patch_headers=patch_headers, dry_run=True)

    def test_gmail_subject_falls_back_to_name(self):
        """Test that reply subjects fall back to patchwork name"""
        from patman.gmail import create_review_drafts

        series_data = {
            'submitter': {'email': 'a@b.com'},
            'project': {'list_email': 'list@test.com'},
            'cover_letter': None,
            'patches': [
                {'id': 1, 'name': 'Fix the bug',
                 'msgid': '<1@test.com>'},
            ],
        }
        # No Subject in headers — should fall back to patch name
        patch_headers = {1: {'Message-Id': '<1@test.com>'}}
        review_bodies = {1: 'Reviewed-by: Test <test@test.com>'}

        with terminal.capture():
            create_review_drafts(series_data, review_bodies,
                                 patch_headers=patch_headers, dry_run=True)

    def test_gmail_from_header(self):
        """Test that create_draft sets From when sender is provided"""
        from patman.gmail import create_draft
        from email import message_from_bytes
        from unittest.mock import MagicMock, patch
        import base64

        service = MagicMock()
        service.users().drafts().create().execute.return_value = {
            'id': 'draft123'}

        draft = create_draft(
            service, 'to@test.com', 'Re: test', 'body',
            sender='Simon Glass <sjg@chromium.org>')

        # Extract the raw message that was passed to the API
        call_kwargs = (service.users().drafts().create
                       .call_args)
        raw = call_kwargs[1]['body']['message']['raw']
        msg = message_from_bytes(base64.urlsafe_b64decode(raw))
        self.assertEqual('Simon Glass <sjg@chromium.org>', msg['from'])

    def test_gmail_no_from_without_sender(self):
        """Test that create_draft omits From when sender is None"""
        from patman.gmail import create_draft
        from email import message_from_bytes
        from unittest.mock import MagicMock
        import base64

        service = MagicMock()
        service.users().drafts().create().execute.return_value = {
            'id': 'draft123'}

        draft = create_draft(
            service, 'to@test.com', 'Re: test', 'body')

        call_kwargs = (service.users().drafts().create
                       .call_args)
        raw = call_kwargs[1]['body']['message']['raw']
        msg = message_from_bytes(base64.urlsafe_b64decode(raw))
        self.assertIsNone(msg['from'])

    def test_review_delete_for_version(self):
        """Test deleting all reviews for a series version"""
        cser = self.get_database()

        series_id = cser.db.series_add('test-delete', 'Test')
        svid = cser.db.ser_ver_add(series_id, 1)

        from datetime import datetime
        ts = datetime.now().isoformat()
        cser.db.review_add(svid, 1, 'review body 1', True, ts)
        cser.db.review_add(svid, 2, 'review body 2', False, ts)
        cser.commit()

        reviews = cser.db.review_get_for_version(svid)
        self.assertEqual(2, len(reviews))

        cser.db.review_delete_for_version(svid)
        cser.commit()

        reviews = cser.db.review_get_for_version(svid)
        self.assertEqual(0, len(reviews))

    def test_series_set_source(self):
        """Test setting the source field on a series"""
        cser = self.get_database()

        series_id = cser.db.series_add('test-source', 'Test')
        cser.db.series_set_source(series_id, 'review')
        cser.commit()

        res = cser.db.execute(
            'SELECT source FROM series WHERE id = ?', (series_id,))
        self.assertEqual('review', res.fetchone()[0])

    def test_review_notes_save_and_show(self):
        """Test saving and retrieving review notes"""
        cser = self.get_database()

        # Create a series with two versions
        series_id = cser.db.series_add('test-notes', 'Test series')
        svid1 = cser.db.ser_ver_add(series_id, 1)
        svid2 = cser.db.ser_ver_add(series_id, 2)

        # No notes initially
        notes = cser.db.ser_ver_get_all_notes(series_id)
        self.assertEqual([], notes)

        # Save notes for v1
        cser.db.ser_ver_set_notes(svid1, 'Fixed the memory leak issue')
        cser.commit()

        notes = cser.db.ser_ver_get_all_notes(series_id)
        self.assertEqual(1, len(notes))
        self.assertEqual(1, notes[0][0])
        self.assertIn('memory leak', notes[0][1])

        # Save notes for v2
        cser.db.ser_ver_set_notes(svid2, 'Addressed style feedback')
        cser.commit()

        notes = cser.db.ser_ver_get_all_notes(series_id)
        self.assertEqual(2, len(notes))
        self.assertEqual(1, notes[0][0])
        self.assertEqual(2, notes[1][0])

    def test_review_notes_skips_empty(self):
        """Test that versions without notes are excluded"""
        cser = self.get_database()

        series_id = cser.db.series_add('test-empty', 'Test')
        svid1 = cser.db.ser_ver_add(series_id, 1)
        svid2 = cser.db.ser_ver_add(series_id, 2)
        svid3 = cser.db.ser_ver_add(series_id, 3)

        # Only set notes for v1 and v3
        cser.db.ser_ver_set_notes(svid1, 'v1 notes')
        cser.db.ser_ver_set_notes(svid3, 'v3 notes')
        cser.commit()

        notes = cser.db.ser_ver_get_all_notes(series_id)
        self.assertEqual(2, len(notes))
        self.assertEqual(1, notes[0][0])
        self.assertEqual(3, notes[1][0])

    def test_add_change_tag_helper(self):
        """Unit-test the _add_change_tag() message-rewriting helper"""
        # New block, with a trailer
        msg = ("Subject\n\nBody.\n\n"
               "Signed-off-by: Me <me@x>\n")
        out = cseries._add_change_tag(msg, 2, 'first bullet')
        self.assertIn(
            "Series-changes: 2\n- first bullet\n\nSigned-off-by:",
            out)
        # Result ends with a single trailing newline
        self.assertTrue(out.endswith('\n'))
        self.assertFalse(out.endswith('\n\n'))

        # Append to an existing block of the same version
        msg = ("Subject\n\nBody.\n\n"
               "Series-changes: 2\n- existing\n\n"
               "Signed-off-by: Me\n")
        out = cseries._add_change_tag(msg, 2, 'second')
        self.assertIn(
            "Series-changes: 2\n- existing\n- second\n\nSigned-off-by:",
            out)

        # New version creates a separate block alongside an existing one
        msg = ("Subject\n\nBody.\n\n"
               "Series-changes: 2\n- v2 thing\n\n"
               "Signed-off-by: Me\n")
        out = cseries._add_change_tag(msg, 3, 'v3 thing')
        self.assertIn("Series-changes: 2\n- v2 thing", out)
        self.assertIn("Series-changes: 3\n- v3 thing", out)
        # Both blocks sit before the signoff
        self.assertLess(
            out.index('Series-changes: 3'), out.index('Signed-off-by:'))

        # cover=True writes Cover-changes instead
        msg = ("Subject\n\nBody.\n\nSigned-off-by: Me\n")
        out = cseries._add_change_tag(msg, 2, 'drop X', cover=True)
        self.assertIn("Cover-changes: 2\n- drop X", out)
        self.assertNotIn("Series-changes:", out)

        # Pre-prefixed bullet is left as-is (no double dash)
        msg = ("Subject\n\nBody.\n\nSigned-off-by: Me\n")
        out = cseries._add_change_tag(msg, 1, '- already a bullet')
        self.assertIn("- already a bullet", out)
        self.assertNotIn("- - already", out)

        # Required blank line between bullets and trailers is always present
        msg = ("Subject\n\nBody.\n\nSigned-off-by: Me\n")
        out = cseries._add_change_tag(msg, 1, 'x')
        self.assertIn("- x\n\nSigned-off-by:", out)

    def test_series_changes_cmdline(self):
        """Test the 'series changes' subcommand via the cmdline"""
        cser = self.get_cser()

        # Use 'second' (v1 of series 'second') as the working branch
        gitutil.checkout('second', self.gitdir, work_tree=self.tmpdir,
                         force=True)
        with terminal.capture():
            cser.add('second', allow_unmarked=True, use_commit=True)

        with terminal.capture():
            self.run_args('series', 'changes', 'fix the offset')

        msg = command.output(
            'git', '-C', self.tmpdir, 'log', '-1', '--format=%B').strip()
        self.assertIn('Series-changes: 1', msg)
        self.assertIn('- fix the offset', msg)

        # A second invocation extends the existing block instead of
        # creating a duplicate
        with terminal.capture():
            self.run_args('series', 'changes', 'and the size')
        msg = command.output(
            'git', '-C', self.tmpdir, 'log', '-1', '--format=%B').strip()
        self.assertEqual(1, msg.count('Series-changes: 1'))
        self.assertIn('- fix the offset\n- and the size', msg)

        # -c writes a Cover-changes block instead
        with terminal.capture():
            self.run_args('series', 'changes', '-c', 'drop NAK-ed patch')
        msg = command.output(
            'git', '-C', self.tmpdir, 'log', '-1', '--format=%B').strip()
        self.assertIn('Cover-changes: 1', msg)
        self.assertIn('- drop NAK-ed patch', msg)


class TestGetUpstreamBranch(unittest.TestCase):
    """Tests for review._get_upstream_branch() base-branch selection."""

    def _cser(self, default_upstream=None):
        cser = mock.Mock()
        cser.db.upstream_get_default.return_value = default_upstream
        return cser

    def test_explicit_base_branch_wins(self):
        """An explicit -b/--base-branch overrides every other check."""
        args = Namespace(base_branch='custom/branch', upstream='us')
        with mock.patch('patman.review.gitutil') as gu:
            self.assertEqual(
                'custom/branch',
                review._get_upstream_branch(args, self._cser()))
            gu.ref_exists.assert_not_called()
            gu.count_revs.assert_not_called()

    def test_next_ahead_of_master_picks_next(self):
        """When next has commits ahead of master, next is chosen."""
        args = Namespace(base_branch=None, upstream='us')
        with mock.patch('patman.review.gitutil.ref_exists',
                        return_value=True), \
             mock.patch('patman.review.gitutil.count_revs',
                        return_value=3):
            self.assertEqual(
                'us/next',
                review._get_upstream_branch(args, self._cser()))

    def test_next_empty_falls_back_to_master(self):
        """When next exists but has no commits ahead, master is chosen."""
        args = Namespace(base_branch=None, upstream='us')
        with mock.patch('patman.review.gitutil.ref_exists',
                        return_value=True), \
             mock.patch('patman.review.gitutil.count_revs',
                        return_value=0):
            self.assertEqual(
                'us/master',
                review._get_upstream_branch(args, self._cser()))

    def test_next_missing_falls_back_to_master(self):
        """When next does not exist at all, master is chosen."""
        args = Namespace(base_branch=None, upstream='us')
        with mock.patch('patman.review.gitutil.ref_exists',
                        return_value=False):
            self.assertEqual(
                'us/master',
                review._get_upstream_branch(args, self._cser()))

    def test_default_upstream_used_when_unset(self):
        """When args.upstream is unset, the cser default is consulted."""
        args = Namespace(base_branch=None, upstream=None)
        cser = self._cser(default_upstream='us')
        with mock.patch('patman.review.gitutil.ref_exists',
                        return_value=True), \
             mock.patch('patman.review.gitutil.count_revs',
                        return_value=5):
            self.assertEqual(
                'us/next', review._get_upstream_branch(args, cser))

    def test_no_upstream_returns_origin_master(self):
        """With no upstream configured anywhere, return 'origin/master'."""
        args = Namespace(base_branch=None, upstream=None)
        self.assertEqual(
            'origin/master',
            review._get_upstream_branch(args, self._cser()))
