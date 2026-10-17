# SPDX-License-Identifier: GPL-2.0+

# Copyright 2025 Simon Glass <sjg@chromium.org>
#
"""Tests for patman review and the review modules"""

import asyncio
import base64
import contextlib
from datetime import datetime, timedelta
import email.utils
import os
import re
import subprocess
import types
from unittest import mock

from u_boot_pylib import terminal
from u_boot_pylib import tools
from patman import cmdline
from patman import control
from patman import review
from patman import cseries
from patman import gmail
from patman.database import Pcommit
from patman import database
from patman.patchwork import Patchwork
from patman.test_common import CseriesTester, Namespace

#pylint: disable=protected-access


class TestReview(CseriesTester):
    """Tests for reviewing patches with 'patman review'"""
    # Series link used by the review tests
    REVIEW_LINK = 497923
    REVIEW_LINK_V2 = 497924
    REVIEW_NAME = 'boot/bootm: Disable interrupts after loading the image'
    REVIEWER = 'Test Reviewer <test@test.com>'

    def test_scan_child_env_has_patman_on_path(self):
        """A scan child gets patman on PYTHONPATH, keeping any existing"""
        pkg_parent = os.path.dirname(
            os.path.dirname(os.path.abspath(cseries.__file__)))
        with mock.patch.dict(os.environ, {'PYTHONPATH': '/existing/path'}):
            env = review.scan.child_env()
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

        with mock.patch.object(review.common, 'ClaudeAgentOptions',
                               side_effect=fake_opts):
            # No model chosen: nothing is added, SDK default is used
            with mock.patch.object(review.common, '_AGENT_MODEL', None):
                review.common.agent_options(allowed_tools=[])
                self.assertNotIn('model', captured)

            # A chosen model is passed through
            with mock.patch.object(review.common, '_AGENT_MODEL', 'sonnet'):
                review.common.agent_options(allowed_tools=[])
                self.assertEqual('sonnet', captured.get('model'))

                # An explicit model wins over the run-wide default
                review.common.agent_options(model='opus')
                self.assertEqual('opus', captured.get('model'))

    def test_agent_options_cli_path(self):
        """_agent_options uses the installed claude, if any"""
        with mock.patch.object(review.common, 'ClaudeAgentOptions',
                               side_effect=lambda **kw: kw):
            with mock.patch('shutil.which', return_value='/bin/claude'):
                self.assertEqual('/bin/claude',
                                 review.common.agent_options()['cli_path'])
                self.assertEqual('/x/claude', review.common.agent_options(
                    cli_path='/x/claude')['cli_path'])

            # With no installed claude, the SDK picks its bundled one
            with mock.patch('shutil.which', return_value=None):
                self.assertNotIn('cli_path', review.common.agent_options())

    def test_review_list_models(self):
        """--list-models prints the accepted aliases and exits cleanly"""
        args = Namespace(list_models=True, model=None)
        with terminal.capture() as (out, _):
            ret = review.core.do_review(args, None, None)
        self.assertEqual(0, ret)
        text = out.getvalue()
        for alias in ('opus', 'sonnet', 'haiku'):
            self.assertIn(alias, text)
        self.assertIn('--model', text)

    def test_scan_draft_undrafted(self):
        """--scan -d drafts reviewed series that have no Gmail draft yet"""
        cser = self.get_database()
        # Series A: reviewed, no draft yet
        a = cser.db.series_add('a', 'Series A')
        cser.db.series_set_source(a, 'review')
        sva = cser.db.ser_ver_add(a, 1, link='100')
        cser.db.review_add(sva, 1, 'comment', False, 't')
        # Series B: reviewed and already drafted -- must be left alone
        b = cser.db.series_add('b', 'Series B')
        cser.db.series_set_source(b, 'review')
        svb = cser.db.ser_ver_add(b, 1, link='200')
        rid = cser.db.review_add(svb, 1, 'comment', False, 't')
        cser.db.review_set_draft_id(rid, 'draft-xyz')
        cser.commit()

        args = Namespace(redraft=False, dry_run=False, create_drafts=True)
        drafted = []

        def fake_draft(args, reviews, series_data, pwork, cser_):
            drafted.append(series_data['id'])

        with mock.patch.object(review.lookup, 'fetch_series',
                               side_effect=lambda pw, link: ({'id': link},)), \
                mock.patch.object(review.drafts, 'draft_stored_reviews',
                                  side_effect=fake_draft), \
                terminal.capture():
            n = review.scan._draft_undrafted(args, None, cser)
        # Only the undrafted series A is drafted
        self.assertEqual(1, n)
        self.assertEqual(['100'], drafted)

        # Dry run counts it but drafts nothing
        with mock.patch.object(review.drafts, 'draft_stored_reviews') as md, \
                mock.patch.object(review.lookup, 'fetch_series') as mf, \
                terminal.capture():
            n = review.scan._draft_undrafted(args, None, cser, dry_run=True)
        self.assertEqual(1, n)
        md.assert_not_called()
        mf.assert_not_called()
        cser.close_database()

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
        self.assertEqual((3, 1, 1), review.scan._review_stats(cser, '999001'))
        # Passing an int link works too (find casts to str)
        self.assertEqual((3, 1, 1), review.scan._review_stats(cser, 999001))
        # Unknown link -> None
        self.assertIsNone(review.scan._review_stats(cser, '404'))
        cser.close_database()

    def test_scan_child_passes_model(self):
        """--model is forwarded to each scan child review"""
        args = Namespace(
            project=None, patchwork_url=None, verbose=False, debug=False,
            upstream=None, reviewer=None, base_branch=None,
            gmail_account=None, signoff=None, spelling=None, context=None,
            model='sonnet', create_drafts=False)
        cmd = review.scan._build_review_command(args, 12345)
        self.assertIn('--model', cmd)
        self.assertEqual('sonnet', cmd[cmd.index('--model') + 1])

        args.model = None
        self.assertNotIn(
            '--model', review.scan._build_review_command(args, 12345))

    def test_register_series_atomic_on_error(self):
        """A failed registration leaves no orphan series (no ser_ver row)"""
        cser = self.get_database()
        before = len(cser.db.series_get_dict(include_reviews=True))

        # Fail after the series row is added but before its version, the
        # exact window that used to leave an orphan behind
        with mock.patch.object(cser.db, 'ser_ver_add',
                               side_effect=ValueError('boom')):
            with self.assertRaises(ValueError):
                review.core._register_series(cser, 'Some series', 1, '12345',
                                             {'patches': []})

        # A later unrelated commit must not flush a half-created series row
        cser.commit()
        after = cser.db.series_get_dict(include_reviews=True)
        self.assertEqual(before, len(after))
        orphans = [s for s in after.values()
                   if not cser.db.series_get_max_version(s.idnum)]
        self.assertEqual([], orphans)
        cser.close_database()

    def test_review_db_dir(self):
        """'review --review-db-dir' keeps reviews in a separate database"""
        cser = self.get_cser()
        with terminal.capture():
            cser.upstream_add('us', 'https://us-repo')
            cser.upstream_set_default('us')
        cser.close_database()
        rdir = os.path.join(self.tmpdir, 'reviews')
        os.mkdir(rdir)
        args = cmdline.parse_args(['review', '--sync', '--review-db-dir', rdir],
                                  config_fname=False)
        seen = {}

        def fake_review(args, pwork, rcser):
            seen['path'] = rcser.db.db_path
            seen['topdir'] = rcser.topdir
            seen['ups'] = args.upstream
            return 0

        with mock.patch.object(review, 'do_review', side_effect=fake_review):
            self.assertEqual(0, control.do_review(args, self.tmpdir))
        self.assertEqual(os.path.join(rdir, '.patman.db'), seen['path'])
        self.assertEqual(self.tmpdir, seen['topdir'])

        # The default upstream is only in the main database
        self.assertEqual('us', seen['ups'])

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

    def _review_pwork(self):
        """Get a fake patchwork with the series used by the review tests

        Returns:
            Patchwork: Patchwork instance, with the project set
        """
        pwork = Patchwork.for_testing(self._fake_patchwork_review)
        pwork.project_set(self.PROJ_ID, self.PROJ_LINK_NAME)
        return pwork

    def run_review(self, *argv, **kwargs):
        """Run a review command with the test reviewer identity"""
        return self.run_args('review', '--reviewer', self.REVIEWER,
                             *argv, **kwargs)

    @contextlib.contextmanager
    def _mock_review(self):
        """Mock applying the series, the upstream, git and the AI review"""
        fake_review = {1: f'Reviewed-by: {self.REVIEWER}'}
        with mock.patch('patman.review.apply.apply_and_check',
                        return_value=True), \
                mock.patch('patman.review.apply.get_upstream_branch',
                           return_value='origin/master'), \
                mock.patch('patman.review.core.review_patches_sync',
                           return_value=fake_review), \
                mock.patch('patman.review.core.gitutil.get_top_level',
                           return_value=self.tmpdir), \
                mock.patch('patman.review.core.gitutil.ensure_worktree',
                           return_value=self.tmpdir):
            yield

    @contextlib.contextmanager
    def _mock_gmail(self):
        """Mock Gmail, so that creating a draft gives the ID 'draft123'

        Yields:
            Mock: The mocked Gmail service
        """
        with mock.patch('patman.gmail.check_available', return_value=True), \
                mock.patch('patman.gmail.get_service') as mock_svc:
            users = mock_svc.return_value.users.return_value
            users.drafts.return_value.create.return_value \
                .execute.return_value = {'id': 'draft123'}
            users.messages.return_value.list.return_value \
                .execute.return_value = {'messages': []}
            yield mock_svc.return_value

    def test_review_new_series(self):
        """Test reviewing a new series creates database records"""
        cser = self.get_cser()
        pwork = self._review_pwork()

        with self._mock_review():
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
        pwork = self._review_pwork()

        with self._mock_review():
            with terminal.capture() as _:
                self.run_review('-s', str(self.REVIEW_LINK), pwork=pwork)

        # Review the same link again
        with self._mock_review():
            with terminal.capture() as (out, err):
                self.run_review('-s', str(self.REVIEW_LINK), pwork=pwork)
        self.assertIn('Already reviewed', out.getvalue())

    def test_review_new_version(self):
        """Test that reviewing v2 detects v1 as previously reviewed"""
        cser = self.get_cser()
        pwork = self._review_pwork()

        # Review v1 first
        with self._mock_review():
            with terminal.capture() as _:
                self.run_review('-s', str(self.REVIEW_LINK), pwork=pwork)

        # Now review v2 - should detect the previous review
        with self._mock_review():
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
        pwork = self._review_pwork()

        with self._mock_review():
            with terminal.capture() as (out, _):
                self.run_review('-S', 'Disable interrupts', pwork=pwork)
        # Should pick the most recent (v2)
        self.assertIn('Using most recent', out.getvalue())

        self.db_open()
        result = cser.db.series_find_by_link(str(self.REVIEW_LINK_V2))
        self.assertIsNotNone(result)

    def test_review_apply_all_upstream(self):
        """Applying 0 patches (all already upstream) is a clean skip"""
        ctx = mock.Mock()
        ctx.branch_name = 'pw-526073-review'
        ctx.upstream_branch = 'us/next'
        ctx.repo_path = '/tmp/x'
        ctx.series_id = 5
        ctx.version = 1
        with mock.patch('patman.review.apply.apply_series_sync',
                        return_value=(True, None)), \
                mock.patch('patman.review.core.gitutil.count_revs',
                           return_value=0), \
                terminal.capture():
            result = review.apply.apply_and_check(ctx, '526073')
        # None means 'nothing to review', not a failure
        self.assertIsNone(result)
        ctx.cser.db.ser_ver_remove.assert_called_once_with(5, 1)

    def test_review_apply_check_failure(self):
        """A genuine apply failure still returns False"""
        ctx = mock.Mock()
        ctx.series_id = 5
        ctx.version = 1
        with mock.patch('patman.review.apply.apply_series_sync',
                        return_value=(False, None)), \
                terminal.capture():
            result = review.apply.apply_and_check(ctx, '526073')
        self.assertFalse(result)
        ctx.cser.db.ser_ver_remove.assert_called_once_with(5, 1)

    def test_review_drafts_fetch_cover_headers(self):
        """Drafts fetch the cover's headers so its reply is a reply-all"""
        from unittest.mock import AsyncMock

        pwork = mock.Mock()
        pwork.get_cover = AsyncMock(
            return_value={'headers': {'To': 'list@x', 'Cc': 'maint@x'}})
        pwork.get_patch = AsyncMock(
            return_value={'headers': {'Message-Id': '<1@x>'}})
        ctx = types.SimpleNamespace(
            pwork=pwork, cser=mock.Mock(), branch_name='b', series_id=1,
            reviewer_email=None,
            series_data={'cover_letter': {'id': 99},
                         'patches': [{'id': 1}]})
        args = Namespace(dry_run=True, gmail_account=None)

        captured = {}

        def fake_crd(series_data, bodies, patch_headers=None, **kw):
            captured['ph'] = patch_headers
            return {}

        with mock.patch.object(gmail, 'create_review_drafts',
                               side_effect=fake_crd), \
                terminal.capture():
            review.drafts.create_drafts(ctx, args, {0: 'cov', 1: 'pat'},
                                        {0: 10, 1: 11})

        pwork.get_cover.assert_awaited_once_with(mock.ANY, '99')
        # The cover letter's headers land at index 0, carrying To and Cc
        self.assertEqual({'To': 'list@x', 'Cc': 'maint@x'},
                         captured['ph'][0])

    def test_review_search_series_version(self):
        """Test -V selects a specific version when searching by title"""
        from patman import review as review_mod
        pwork = self._review_pwork()

        with terminal.capture():
            # Default picks the most recent (v2)
            self.assertEqual(
                str(self.REVIEW_LINK_V2),
                review_mod.lookup.search_series(pwork, self.REVIEW_NAME))
            # -V selects the requested version
            self.assertEqual(
                str(self.REVIEW_LINK),
                review_mod.lookup.search_series(pwork, self.REVIEW_NAME, 1))
            self.assertEqual(
                str(self.REVIEW_LINK_V2),
                review_mod.lookup.search_series(pwork, self.REVIEW_NAME, 2))

        # An unavailable version errors, listing what is available
        with terminal.capture():
            with self.assertRaises(ValueError) as cm:
                review_mod.lookup.search_series(pwork, self.REVIEW_NAME, 5)
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
        with mock.patch.object(review.lookup, 'search_patch',
                               return_value=('link-xyz', 3)), \
                mock.patch.object(review.core, '_review_link') as rev_link:
            # Default: review just the located patch (index 3)
            args = make_args(False)
            review.core.do_review(args, None, None)
            self.assertEqual('3', args.patches)
            rev_link.assert_called_once_with(args, None, None, 'link-xyz')

            # -w: locate via the patch but review the whole series
            args = make_args(True)
            review.core.do_review(args, None, None)
            self.assertIsNone(args.patches)
            rev_link.assert_called_with(args, None, None, 'link-xyz')

    def test_review_no_link_or_title(self):
        """Test that missing -l and -t gives a proper error"""
        self.get_cser()
        pwork = self._review_pwork()

        with terminal.capture() as _:
            self.run_review( pwork=pwork, expect_ret=1)

    def test_review_apply_failure(self):
        """Test that apply failure is reported"""
        self.get_cser()
        pwork = self._review_pwork()

        with mock.patch('patman.review.apply.apply_and_check',
                        return_value=False), \
             mock.patch('patman.review.apply.get_upstream_branch',
                        return_value='origin/master'), \
             mock.patch('patman.review.core.gitutil.get_top_level',
                        return_value=self.tmpdir), \
             mock.patch('patman.review.core.gitutil.ensure_worktree',
                        return_value=self.tmpdir):
            with terminal.capture() as _:
                self.run_review('-s', str(self.REVIEW_LINK),
                                pwork=pwork, expect_ret=1)

    def test_review_create_drafts_dry_run(self):
        """Test dry-run draft creation shows what would be created"""
        self.get_cser()
        pwork = self._review_pwork()

        with self._mock_review():
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
        pwork = self._review_pwork()

        with self._mock_review(), self._mock_gmail():
            with terminal.capture() as (out, _):
                self.run_review('-s', str(self.REVIEW_LINK),
                                '--create-drafts', pwork=pwork)
        output = out.getvalue()
        self.assertIn('Created 1 Gmail draft', output)

    def test_review_draft_headers(self):
        """Test decoding of mangled patchwork headers for Gmail drafts"""
        # Raw 8-bit characters in a header end up as 'unknown-8bit' encoded
        # words, which hide the addresses from Gmail
        hdrs = {
            'To': 'Peter Robinson <pbrobinson@gmail.com>,\n'
                  ' Tom Rini <trini@konsulko.com>',
            'Cc': '=?unknown-8bit?q?Tom_Rini_=3Ctrini=40konsulko=2Ecom=3E=2C_?='
                  '\n\t=?unknown-8bit?q?=22Filip_Kokosi=C5=84ski=22_=3Cfilip'
                  '=40example=2Ecom=3E=2C_u-boot=40lists=2Edenx=2Ede?=',
        }
        self.assertEqual(
            'Peter Robinson <pbrobinson@gmail.com>, '
            'Tom Rini <trini@konsulko.com>, '
            '=?utf-8?q?Filip_Kokosi=C5=84ski?= <filip@example.com>, '
            'u-boot@lists.denx.de',
            gmail._build_cc(hdrs, 'u-boot@lists.denx.de'))

        self.assertEqual('[PATCH] Kokosiński: fix',
                         gmail._decode_hdr('=?utf-8?q?=5BPATCH=5D_Kokosi=C5=84'
                                           'ski=3A_fix?='))
        self.assertEqual('a@b.org, c@d.org',
                         gmail._format_addrs([['a@b.org', 'c@d.org']]))

    @staticmethod
    def gmail_msg(msg_id, hdrs, body, sent=False):
        """Make a message as returned by the Gmail API

        Args:
            msg_id (str): Gmail message ID
            hdrs (dict): Headers
            body (str): Text of the message
            sent (bool): True if we sent it

        Return:
            dict: Message
        """
        return {
            'id': msg_id,
            'labelIds': ['SENT'] if sent else ['INBOX'],
            'payload': {
                'mimeType': 'text/plain',
                'headers': [{'name': key, 'value': val}
                            for key, val in hdrs.items()],
                'body': {'data': base64.urlsafe_b64encode(
                    body.encode()).decode()},
            },
        }

    def fake_gmail(self):
        """Set up a fake Gmail service with a thread containing a review

        The thread has the patch, our review, a reply to the review, a reply
        to someone else and a response which we sent.

        Return:
            MagicMock: Gmail service
        """
        msgs = [
            self.gmail_msg('m0', {'Message-ID': '<p0@x>'}, 'The patch'),
            self.gmail_msg('m1', {'Message-ID': '<r1@me>'}, 'My review',
                           sent=True),
            self.gmail_msg('m2', {
                'From': 'Fred Bloggs <fred@x.com>',
                'To': 'Simon Glass <sjg@chromium.org>',
                'Cc': 'u-boot@lists.denx.de, Tom Rini <trini@k.com>, '
                      'fred@x.com',
                'Subject': 'Re: [PATCH 2/5] video: Add a driver',
                'Date': email.utils.format_datetime(
                    datetime.now().astimezone()),
                'Message-ID': '<a2@x>', 'In-Reply-To': '<r1@me>',
                'References': '<p0@x> <r1@me>'}, 'Why do you say that?'),
            self.gmail_msg('m3', {'From': 'Tom <trini@k.com>',
                                  'References': '<p0@x> <o@y>',
                                  'Message-ID': '<t3@k>'}, 'Not to you'),
            self.gmail_msg('m4', {'References': '<p0@x> <r1@me>',
                                  'Message-ID': '<r4@me>'}, 'More from me',
                           sent=True),
            self.gmail_msg('m5', {
                'From': 'Tom Rini <trini@k.com>', 'Subject': 'Re: x',
                'Message-ID': '<t5@k>', 'References': '<p0@x> <r1@me>'},
                'Looks fine to me'),
            self.gmail_msg('m6', {'References': '<r1@me> <t5@k>',
                                  'Message-ID': '<r6@me>'}, 'Thanks Tom',
                           sent=True),
        ]
        service = mock.MagicMock()
        service.users.return_value.threads.return_value.get.return_value \
            .execute.return_value = {'messages': msgs}
        return service

    def test_review_replies(self):
        """Replies to a sent review are recorded and can be responded to"""
        cser = self.get_database()
        db = cser.db
        sid = db.series_add('rev', 'Fred series', None)
        db.series_set_source(sid, 'review')
        svid = db.ser_ver_add(sid, 1, link='123')
        rid = db.review_add(svid, 2, 'Please fix this', False,
                            datetime.now().isoformat())
        db.review_set_sent(rid, 'Please fix this', 'm1', 't1')
        old = db.review_add(svid, 3, 'Old review', False,
                            '2020-01-01T00:00:00')
        db.review_set_sent(old, 'Old review', 'm1', 't1')
        db.commit()

        service = self.fake_gmail()
        replies = gmail.fetch_thread_replies(service, 't1', 'm1')
        self.assertEqual([('m2', None), ('m5', 'Thanks Tom')],
                         [(rep['msg_id'], rep['answer']) for rep in replies])
        self.assertEqual(('<a2@x>', '<p0@x> <r1@me>', 'Why do you say that?'),
                         (replies[0]['message_id'], replies[0]['references'],
                          replies[0]['body']))

        ctx = review.common.ReviewContext(None, cser, {})
        ctx.reviewer_name = 'Simon Glass'
        ctx.reviewer_email = 'sjg@chromium.org'
        with mock.patch.object(review.replies, 'handle_reply_sync',
                               return_value=('suggested', 'Because')) as ai, \
                terminal.capture() as (out, _):
            self.assertEqual(1, review.replies._handle_replies(service, ctx))

            # A second sync records nothing new
            self.assertEqual(0, review.replies._handle_replies(service, ctx))
        ai.assert_called_once_with(ctx, 'Please fix this',
                                   'Fred Bloggs <fred@x.com>',
                                   'Why do you say that?')
        self.assertIn('response suggested', out.getvalue())
        self.assertEqual('replied', db.review_get(rid).status)
        self.assertEqual('sent', db.review_get(old).status)

        reply, answered = db.reply_get_for_review(rid)
        self.assertEqual(('suggested', 'Because', 'Fred Bloggs <fred@x.com>'),
                         (reply.status, reply.response, reply.sender))
        self.assertEqual(('sent', 'Thanks Tom'),
                         (answered.status, answered.response))
        self.assertEqual([reply.idnum],
                         [rep.idnum for rep in db.reply_get_by_status(
                             database.REPLY_WAITING)])

        with terminal.capture() as (out, _):
            review.replies._list_replies(cser)
        self.assertIn('Fred Bloggs <fred@x.com>: Re: [PATCH 2/5] video: Add a '
                      'driver (response suggested)', out.getvalue())

        # The response is copied to everyone but ourselves and the sender
        self.assertEqual('u-boot@lists.denx.de, Tom Rini <trini@k.com>',
                         review.replies._reply_cc(reply, {'sjg@chromium.org'}))

        draft_svc = mock.MagicMock()
        with mock.patch.object(gmail, 'create_draft',
                               return_value={'id': 'd1'}) as create, \
                terminal.capture():
            review.replies._draft_response(draft_svc, ctx, reply.idnum)
        create.assert_called_once_with(
            draft_svc, 'Fred Bloggs <fred@x.com>',
            'Re: [PATCH 2/5] video: Add a driver', 'Because',
            in_reply_to='<a2@x>', references='<p0@x> <r1@me> <a2@x>',
            cc='u-boot@lists.denx.de, Tom Rini <trini@k.com>',
            sender='Simon Glass <sjg@chromium.org>')
        reply = db.reply_get(reply.idnum)
        self.assertEqual(('draft', 'd1'), (reply.status, reply.draft_id))

        # Drafting again replaces the old draft
        with mock.patch.object(gmail, 'create_draft',
                               return_value={'id': 'd2'}), \
                mock.patch.object(gmail, 'delete_draft') as delete, \
                terminal.capture():
            review.replies._draft_response(draft_svc, ctx, reply.idnum)
        delete.assert_called_once_with(draft_svc, 'd1')

        # Once the draft is sent, the response is recorded as sent
        with mock.patch.object(gmail, 'sync_drafts',
                               return_value=({reply.idnum: ('Sent text', 'm9',
                                                            't1')}, [], {})), \
                terminal.capture():
            review.replies._sync_reply_drafts(draft_svc, cser)
        reply = db.reply_get(reply.idnum)
        self.assertEqual(('sent', 'Sent text'),
                         (reply.status, reply.response))
        with self.assertRaises(ValueError) as exc:
            review.replies._draft_response(draft_svc, ctx, reply.idnum)
        self.assertIn('has been sent', str(exc.exception))

        # The agent may decide that no response is needed
        db.reply_set_response(reply.idnum, None, database.REPLY_NEW)
        with mock.patch.object(review.replies, 'handle_reply_sync',
                               return_value=('skip', None)), \
                terminal.capture() as (out, _):
            review.replies._suggest_response(ctx, reply.idnum)
        self.assertIn('no response needed', out.getvalue())
        self.assertEqual('skip', db.reply_get(reply.idnum).status)
        with self.assertRaises(ValueError) as exc:
            review.replies._draft_response(draft_svc, ctx, reply.idnum)
        self.assertIn('There is no response', str(exc.exception))

        # Sending a response with no draft creates one first, and records
        # the text which was sent, which may have been changed in Gmail
        db.reply_set_response(reply.idnum, 'My answer',
                              database.REPLY_SUGGESTED)
        with mock.patch.object(gmail, 'create_draft',
                               return_value={'id': 'd3'}) as create, \
                mock.patch.object(gmail, 'delete_draft'), \
                mock.patch.object(gmail, 'send_draft',
                                  return_value=('Edited', 'm3', 't3')) as send, \
                terminal.capture() as (out, _):
            review.replies._send_response(draft_svc, ctx, reply.idnum)
        self.assertEqual('My answer', create.call_args[0][3])
        send.assert_called_once_with(draft_svc, 'd3')
        self.assertIn('sent the response', out.getvalue())
        reply = db.reply_get(reply.idnum)
        self.assertEqual(('sent', 'Edited'), (reply.status, reply.response))
        with self.assertRaises(ValueError) as exc:
            review.replies._send_response(draft_svc, ctx, reply.idnum)
        self.assertIn('has been sent', str(exc.exception))

        # An existing draft is sent as it is; if it has been deleted in
        # Gmail, nothing is sent
        db.reply_set_response(reply.idnum, 'Again', database.REPLY_SUGGESTED)
        db.reply_set_status(reply.idnum, database.REPLY_DRAFT, 'd4')
        with mock.patch.object(gmail, 'create_draft') as create, \
                mock.patch.object(gmail, 'send_draft',
                                  return_value=None) as send:
            with self.assertRaises(ValueError) as exc:
                review.replies._send_response(draft_svc, ctx, reply.idnum)
        self.assertIn('has been deleted', str(exc.exception))
        create.assert_not_called()
        send.assert_called_once_with(draft_svc, 'd4')
        self.assertEqual('suggested', db.reply_get(reply.idnum).status)

        # Deleting the review deletes its replies
        db.review_delete_for_version(svid)
        self.assertEqual([], db.reply_get_by_status(['skip']))

    def test_gmail_sync_drafts(self):
        """Our messages in response to drafts are found reliably"""
        review_text = ('Hi Fred,\n\nOn Mon, Fred wrote:\n> code\n\n'
                       'Please use a helper for the colour mode checks here.\n'
                       '\nRegards,\nSimon')
        self.assertEqual('please use a helper for the colour mode',
                         gmail.search_phrase(review_text))
        self.assertIsNone(gmail.search_phrase(
            'Reviewed-by: Simon Glass <sjg@chromium.org>'))
        self.assertEqual(('video add it', 2, '2/5'), gmail.subject_key(
            'Re: [PATCH v2 02/5] video: Add it'))
        self.assertEqual(('video add it', 1, None),
                         gmail.subject_key('Re: [PATCH] video: Add it'))
        self.assertTrue(gmail.same_subject('[RFC,v2,3/5] video: Add it',
                                           'Re: [PATCH RFC v2 03/5] Video: '
                                           'add it'))
        self.assertFalse(gmail.same_subject('[v2,3/5] video: Add it',
                                            'Re: [PATCH v3 3/5] video: Add it'))
        self.assertFalse(gmail.same_subject('[v2,3/5] video: Add it',
                                            'Re: [PATCH v2 4/5] video: Add it'))

        def msg(msg_id, subject, body, when, sent=True, thread='t1'):
            out = self.gmail_msg(msg_id, {'Subject': subject}, body,
                                 sent=sent)
            out['threadId'] = thread
            out['internalDate'] = str(when * 1000)
            return out

        thread = [
            msg('p1', '[PATCH 1/2] video: Add it', 'patch', 100, sent=False),
            msg('old', 'Re: [PATCH 1/2] video: Add it', 'Earlier', 150),
            msg('s1', 'Re: [PATCH 2/2] video: Other', 'Something', 300),
            msg('s2', 'Re: [PATCH 1/2] video: Add it', 'My own words', 400),
            msg('s3', 'Re: [PATCH 1/2] video: Add it', 'Later', 500),
            msg('v2', 'Re: [PATCH v2 4/4] video: Fourth', 'Next version', 600),
            msg('s4', 'Re: [PATCH 3/4] video: Third', 'Much later',
                200 + 15 * 86400),
        ]
        service = mock.MagicMock()
        users = service.users.return_value

        def get_draft(userId, id, format):  # pylint: disable=W0622,W0613
            req = mock.MagicMock()
            if id.startswith('pending'):
                req.execute.return_value = {'message': {'threadId': 't1'}}
            else:
                req.execute.side_effect = ValueError('not found')
            return req

        users.drafts.return_value.get.side_effect = get_draft
        users.threads.return_value.get.return_value.execute.return_value = {
            'messages': thread}
        found = msg('f1', 'Re: [PATCH] net: Fix it',
                    'Edited.\n\n' + review_text, 400, thread='t2')
        users.messages.return_value.list.return_value.execute.return_value = {
            'messages': [{'id': 'f1'}]}
        users.messages.return_value.get.return_value.execute.return_value = \
            found

        def item(idnum, draft_id, thread_id, subject, check_sent=True,
                 body='Draft text'):
            return types.SimpleNamespace(
                idnum=idnum, draft_id=draft_id, body=body,
                thread_id=thread_id, subject=subject, after='2026/10/01',
                since=200, check_sent=check_sent)

        sent, deleted, threads = gmail.sync_drafts(service, [
            # Draft still there, but we wrote our own reply in its thread
            item(1, 'pending1', None, '[1/2] video: Add it'),
            # Draft still there, and not checked for a reply
            item(2, 'pending2', 't1', '[1/2] video: Add it', False),
            # Draft gone; we sent a rewritten reply
            item(3, 'gone3', 't1', '[1/2] video: Add it'),
            # Draft gone and thread unknown, so search the sent mail
            item(4, 'gone4', None, 'net: Fix it', body=review_text),
            # Draft gone, with no reply on this patch
            item(5, 'gone5', 't1', '[3/3] video: Not sent'),
            # Draft gone; our only reply was to a later version
            item(6, 'gone6', 't1', '[4/4] video: Fourth'),
            # Draft gone; our only reply on the patch came much later
            item(7, 'gone7', 't1', '[3/4] video: Third'),
        ])
        self.assertEqual({1: 't1'}, threads)
        self.assertEqual({1: ('My own words', 's2', 't1'),
                          3: ('My own words', 's2', 't1'),
                          4: ('Edited.\n\n' + review_text, 'f1', 't2')},
                         sent)
        self.assertEqual([5, 6, 7], deleted)
        queries = [call.kwargs['q'] for call in
                   users.messages.return_value.list.call_args_list]
        self.assertEqual(
            ['in:sent after:2026/10/01 '
             '"please use a helper for the colour mode"',
             'in:sent after:2026/10/01 "net fix it"'], queries)

    def test_review_sync_drafts(self):
        """Review drafts are synced, with limited voice refinement"""
        cser = self.get_database()
        db = cser.db
        sid = db.series_add('rev', 'Fred series', None)
        db.series_set_source(sid, 'review')
        svid = db.ser_ver_add(sid, 1, link='123')
        db.pcommit_add_list(svid, [Pcommit(None, seq, f'[{seq + 1}/6] subj',
                                           None, None, None, None, None)
                                   for seq in range(6)])
        now = datetime.now().isoformat()
        rids = []
        for seq in range(1, 7):
            rid = db.review_add(svid, seq, f'review {seq}', False,
                                now if seq < 6 else '2020-01-01T00:00:00')
            db.review_set_draft_id(rid, f'd{seq}')
            rids.append(rid)
        db.commit()

        def fake_sync(_service, items):
            self.assertEqual('[1/6] subj', items[0].subject)
            sent = {item.idnum: (f'edited {item.idnum}', f'm{item.idnum}',
                                 't1') for item in items[1:]}
            return sent, [], {items[0].idnum: 't5'}

        with mock.patch.object(gmail, 'sync_drafts', side_effect=fake_sync), \
                mock.patch.object(
                    review.voice, 'refine_voice_sync') as refine, \
                terminal.capture() as (out, _):
            review.drafts.sync_drafts(None, cser)
        self.assertIn('Synced 5 sent, 0 deleted', out.getvalue())
        self.assertIn('1 draft(s) still pending', out.getvalue())
        self.assertEqual(3, refine.call_count)
        first = db.review_get(rids[0])
        self.assertEqual(('draft', 't5'), (first.status, first.gmail_thread_id))
        self.assertEqual(('sent', 'edited %d' % rids[5]),
                         (db.review_get(rids[5]).status,
                          db.review_get(rids[5]).body))

    def test_review_revise(self):
        """The agent changes unsent reviews following a reviewer's note"""
        cser = self.get_database()
        db = cser.db
        sid = db.series_add('rev', 'Fred series', None)
        db.series_set_source(sid, 'review')
        svid = db.ser_ver_add(sid, 1, link='123')
        cover = db.review_add(svid, 0, 'Hi Fred,\n\nNice series', False,
                              '2026-10-01T00:00:00')
        first = db.review_add(svid, 1, 'Hi Fred,\n\n> code\n\nNit', False,
                              '2026-10-01T00:00:00')
        db.review_set_draft_id(first, 'd1')
        sent = db.review_add(svid, 2, 'Sent already', False,
                             '2026-10-01T00:00:00')
        db.review_set_sent(sent, 'Sent already', 'm1', 't1')
        db.commit()

        new_body = ('Hi Fred,\n\nReviewed-by: Simon Glass '
                    '<sjg@chromium.org>')
        seen = {}

        async def mock_agent(prompt, _options):
            seen['prompt'] = prompt
            fname = re.search(r'see file: (\S+)\)', prompt).group(1)
            seen['reviews'] = tools.read_file(fname, binary=False)
            # The cover-letter review comes back unchanged
            return True, ('---SEQ 0---\nHi Fred,\n\nNice series\n'
                          f'---SEQ 1---\n{new_body}\n')

        args = cmdline.parse_args(
            ['review', '-s', '123', '--revise', 'Drop the nit, approve it',
             '-d', '--reviewer', 'Simon Glass <sjg@chromium.org>'],
            config_fname=False)
        with mock.patch.object(review.core.claude_mod, 'run_agent_collect',
                               side_effect=mock_agent), \
                mock.patch.object(review.core.claude_mod, 'check_available',
                                  return_value=True), \
                mock.patch.object(review.common, 'ClaudeAgentOptions',
                                  mock.MagicMock()), \
                mock.patch.object(review.stored, '_review_commits',
                                  return_value=['abc1 dm: Fix it']), \
                mock.patch.object(review.lookup, 'fetch_series',
                                  return_value=({'id': 123}, 'x', 1, 2)), \
                mock.patch.object(
                    review.drafts, 'delete_gmail_drafts') as delete, \
                mock.patch.object(review.drafts, 'create_drafts') as create, \
                terminal.capture() as (out, _):
            self.assertEqual(
                0, review.stored._do_revise(args, None, cser, '123'))
        self.assertIn('Drop the nit, approve it', seen['prompt'])
        self.assertIn('Patch 1: abc1 dm: Fix it', seen['prompt'])
        self.assertIn('Reviewed-by: Simon Glass <sjg@chromium.org>',
                      seen['prompt'])
        self.assertIn('---SEQ 1---', seen['reviews'])
        self.assertNotIn('Sent already', seen['reviews'])

        # The review which was sent is given as context, not to be changed
        self.assertIn('OTHER REVIEWS OF THE SERIES', seen['prompt'])
        self.assertIn('---SEQ 2---\nSent already', seen['prompt'])
        self.assertIn('Revised the review of patch 1', out.getvalue())
        self.assertNotIn('cover letter', out.getvalue())

        rev = db.review_get(first)
        self.assertEqual((new_body, 1), (rev.body, rev.approved))
        self.assertEqual('Hi Fred,\n\nNice series', db.review_get(cover).body)
        self.assertEqual(['d1'], [r.draft_id for r in delete.call_args.args[1]])
        self.assertEqual(({1: new_body}, {1: first}),
                         create.call_args.args[2:])

        # Nothing to do if the selected reviews have all been sent
        args = cmdline.parse_args(
            ['review', '-s', '123', '-i', '2', '--revise', 'x'],
            config_fname=False)
        with self.assertRaises(ValueError) as exc:
            review.stored._do_revise(args, None, cser, '123')
        self.assertIn('No unsent reviews', str(exc.exception))

    def test_review_revise_context(self):
        """A revision is given the rest of the series as context"""
        self.assertEqual('', review.prompts.revise_context(None, {}, None))
        text = review.prompts.revise_context(
            'Cover text', {2: 'Second review'}, '/tmp/comments.txt')
        self.assertIn('COVER LETTER:\nCover text', text)
        self.assertIn('---SEQ 2---\nSecond review', text)
        self.assertIn('(see file: /tmp/comments.txt)', text)

        # The cover letter and comments come from patchwork
        ctx = review.common.ReviewContext(None, None, {})
        series = {'id': 123, 'cover_letter': {'id': 9}}
        with mock.patch.object(review.lookup, 'fetch_series',
                               return_value=(series, 'x', 1, 2)), \
                mock.patch.object(review.lookup, 'fetch_cover_content',
                                  return_value='Cover text'), \
                mock.patch.object(review.lookup, 'write_comments_file',
                                  return_value='/tmp/c.txt'):
            review.stored._read_series_context(ctx, mock.Mock(), '123')
        self.assertEqual((series, 'Cover text', '/tmp/c.txt'),
                         (ctx.series_data, ctx.cover_content,
                          ctx.comments_path))

        # A problem reading patchwork does not stop the revision
        ctx = review.common.ReviewContext(None, None, {})
        with mock.patch.object(review.lookup, 'fetch_series',
                               side_effect=ValueError('no server')), \
                terminal.capture() as (_, err):
            review.stored._read_series_context(ctx, mock.Mock(), '123')
        self.assertIsNone(ctx.cover_content)
        self.assertIn('Could not read the series from patchwork: no server',
                      err.getvalue())

        # The writing voice is used too
        with mock.patch.object(review.voice, 'get_voice',
                               return_value='Be terse'):
            self.assertIn('Be terse', review.prompts.voice_section())

    def test_review_session(self):
        """Each review records its agent session, which revise resumes"""
        cser = self.get_database()
        db = cser.db
        sid = db.series_add('rev', 'Fred series', None)
        db.series_set_source(sid, 'review')
        svid = db.ser_ver_add(sid, 1, link='123')
        rid = db.review_add(svid, 1, 'Hi Fred,\n\n> code\n\nNit', False,
                            '2026-10-01T00:00:00')
        other = db.review_add(svid, 2, 'Hi Fred,\n\nLooks fine', False,
                              '2026-10-01T00:00:00')
        self.assertIsNone(db.review_get_session(rid))
        db.review_set_session(rid, 'sess-1')
        db.commit()
        self.assertEqual('sess-1', db.review_get_session(rid))

        # A new session is recorded for each patch which is reviewed
        ctx = review.common.ReviewContext(None, cser, {})
        with mock.patch.object(review.common, 'ClaudeAgentOptions') as opts:
            review.common.session_options(ctx, 3, cwd='/tmp')
        self.assertEqual(36, len(ctx.session_ids[3]))
        self.assertEqual(ctx.session_ids[3],
                         opts.call_args.kwargs['session_id'])

        new_body = 'Hi Fred,\n\nReviewed-by: Simon Glass <sjg@chromium.org>'
        results = []

        async def mock_agent(_prompt, _options):
            return results.pop(0)

        def run(*argv):
            args = cmdline.parse_args(
                ['review', '-s', '123', '--revise', 'Approve it',
                 '--reviewer', 'Simon Glass <sjg@chromium.org>', *argv],
                config_fname=False)
            with mock.patch.object(review.core.claude_mod,
                                   'run_agent_collect',
                                   side_effect=mock_agent), \
                    mock.patch.object(review.core.claude_mod,
                                      'check_available', return_value=True), \
                    mock.patch.object(review.common,
                                      'ClaudeAgentOptions') as opts, \
                    mock.patch.object(review.stored, '_review_commits',
                                      return_value=['abc1 dm: Fix it']), \
                    terminal.capture() as (out, _):
                self.assertEqual(
                    0, review.stored._do_revise(args, None, cser, '123'))
            return [call.kwargs for call in opts.call_args_list], out.getvalue()

        # Revising one review carries on from a copy of its session
        results[:] = [(True, f'---SEQ 1---\n{new_body}\n')]
        calls, out = run('-i', '1')
        self.assertEqual(1, len(calls))
        self.assertEqual(('sess-1', True),
                         (calls[0]['resume'], calls[0]['fork_session']))
        self.assertIn('carrying on from the review', out)
        self.assertEqual(new_body, db.review_get(rid).body)

        # If the session has gone, a new one is used
        db.review_set_body(rid, 'Hi Fred,\n\nNit', False)
        results[:] = [(False, ''), (True, f'---SEQ 1---\n{new_body}\n')]
        calls, out = run('-i', '1')
        self.assertEqual(['sess-1', None],
                         [call.get('resume') for call in calls])
        self.assertIn('Could not resume the review session', out)
        self.assertEqual(new_body, db.review_get(rid).body)

        # Revising more than one review starts a new session
        db.review_set_body(rid, 'Hi Fred,\n\nNit', False)
        results[:] = [(True, f'---SEQ 1---\n{new_body}\n')]
        calls, _ = run()
        self.assertEqual([None], [call.get('resume') for call in calls])
        self.assertEqual('Hi Fred,\n\nLooks fine', db.review_get(other).body)

    def test_review_send(self):
        """The Gmail drafts of a series' reviews can be sent"""
        cser = self.get_database()
        db = cser.db
        sid = db.series_add('rev', 'Fred series', None)
        db.series_set_source(sid, 'review')
        svid = db.ser_ver_add(sid, 1, link='123')
        ids = {}
        for seq in (2, 0, 1, 3):
            ids[seq] = db.review_add(svid, seq, f'Review {seq}', False,
                                     '2026-10-01T00:00:00')
        for seq in (0, 1, 2):
            db.review_set_draft_id(ids[seq], f'd{seq}')
        db.commit()

        def send(_service, draft_id):
            if draft_id == 'd1':
                return None
            return f'Edited {draft_id}', f'm-{draft_id}', 't1'

        def run(*argv):
            args = cmdline.parse_args(['review', '-s', '123', '--send',
                                       *argv], config_fname=False)
            with mock.patch.object(gmail, 'check_available',
                                   return_value=True), \
                    mock.patch.object(gmail, 'get_service'), \
                    mock.patch.object(gmail, 'send_draft',
                                      side_effect=send) as sender, \
                    terminal.capture() as (out, err):
                ret = review.stored._do_send(args, cser, '123')
            return ret, [call.args[1] for call in sender.call_args_list], \
                out.getvalue() + err.getvalue()

        # A dry run sends nothing
        ret, sent, out = run('-n', '-i', '0,2')
        self.assertEqual((0, []), (ret, sent))
        self.assertIn('would send the review of the cover letter', out)
        self.assertIn('would send the review of patch 2', out)

        # The cover letter goes first; a draft which has gone is reported
        ret, sent, out = run()
        self.assertEqual((1, ['d0', 'd1', 'd2']), (ret, sent))
        self.assertIn('The draft of the review of patch 1 has gone', out)
        self.assertIn('Sent 2 of 3 review(s)', out)
        rev = db.review_get(ids[2])
        self.assertEqual(('sent', 'Edited d2', 'm-d2', 't1'),
                         (rev.status, rev.body, rev.gmail_msg_id,
                          rev.gmail_thread_id))
        self.assertEqual('draft', db.review_get(ids[1]).status)
        self.assertEqual('new', db.review_get(ids[3]).status)

        # Only patch 1's draft is left
        ret, sent, _ = run()
        self.assertEqual(['d1'], sent)
        with self.assertRaises(ValueError) as exc:
            run('-i', '2')
        self.assertIn('No Gmail drafts to send', str(exc.exception))

    def test_gmail_send_draft(self):
        """A draft is read, then sent"""
        service = mock.MagicMock()
        drafts = service.users.return_value.drafts.return_value
        body = base64.urlsafe_b64encode(b'Hi Fred').decode()
        drafts.get.return_value.execute.return_value = {
            'message': {'payload': {'mimeType': 'text/plain',
                                    'body': {'data': body}}}}
        drafts.send.return_value.execute.return_value = {
            'id': 'm1', 'threadId': 't1'}
        self.assertEqual(('Hi Fred', 'm1', 't1'),
                         gmail.send_draft(service, 'd1'))
        drafts.send.assert_called_once_with(userId='me', body={'id': 'd1'})

    def test_review_find_base(self):
        """The base commit or branch is found in a cover letter"""
        find = review.apply.find_series_base
        self.assertEqual(('0123456789ab', None),
                         find('Some text\n\nbase-commit: 0123456789ab\n'))
        self.assertEqual((None, 'main'),
                         find('This series is based on u-boot/master.'))
        self.assertEqual((None, 'main'), find('Applies on top of main'))
        self.assertEqual((None, 'next'), find("It is rebased onto 'next'"))
        self.assertEqual((None, None), find('This is based on a true story'))
        self.assertEqual((None, None), find(None))

    def test_review_series_base(self):
        """A series is applied to the branch holding its base commit"""
        def git(cwd, *args):
            return subprocess.run(
                ['git', '-c', 'user.name=T', '-c', 'user.email=t@x', *args],
                cwd=cwd, check=True, capture_output=True,
                text=True).stdout.strip()

        def commit(cwd, msg):
            git(cwd, 'commit', '-q', '--allow-empty', '-m', msg)
            return git(cwd, 'rev-parse', 'HEAD')

        work = os.path.join(self.tmpdir, 'work')
        os.mkdir(work)
        git(work, 'init', '-q', '-b', 'master')
        first = commit(work, 'first')
        bare = os.path.join(self.tmpdir, 'up.git')
        git(self.tmpdir, 'clone', '-q', '--bare', work, bare)

        # Upstream moves on, with master and next each gaining a commit
        other = os.path.join(self.tmpdir, 'other')
        git(self.tmpdir, 'clone', '-q', bare, other)
        git(other, 'checkout', '-q', '-b', 'next')
        commit(other, 'on next')
        git(other, 'checkout', '-q', 'master')
        on_master = commit(other, 'on master')
        git(other, 'push', '-q', 'origin', 'master', 'next')
        git(work, 'remote', 'add', 'us', bare)
        git(work, 'checkout', '-q', '-b', 'local')
        local = commit(work, 'local only')

        ctx = review.common.ReviewContext(None, None, {})
        ctx.main_repo = work

        def base(text):
            with mock.patch.object(
                    review.apply, '_base_text', return_value=text):
                return review.apply.series_base(ctx, 'us', 'us/next')

        # The commit is only upstream, so it is fetched, and it is on master
        with terminal.capture() as (out, _):
            self.assertEqual('us/master', base(f'base-commit: {on_master}'))
        self.assertIn("Fetching 'us'", out.getvalue())
        self.assertIn('which is in us/master', out.getvalue())

        # A commit in both branches uses the default
        with terminal.capture():
            self.assertEqual('us/next', base(f'base-commit: {first}'))

            # A commit in neither is used directly
            self.assertEqual(local, base(f'base-commit: {local}'))

            # Without the commit, the cover letter's words are used
            with terminal.capture() as (_, err):
                self.assertEqual('us/master', base(
                    'base-commit: 0123456789abcdef\nBased on master'))
        self.assertIn('Cannot find base-commit 0123456789abcdef',
                      err.getvalue())
        self.assertIsNone(base('Nothing to see'))

        # Once the upstream has a main branch, it is used instead of master
        git(other, 'checkout', '-q', '-b', 'main')
        on_main = commit(other, 'on main')
        git(other, 'push', '-q', 'origin', 'main')
        with terminal.capture():
            self.assertEqual('us/main', base(f'base-commit: {on_main}'))
            self.assertEqual('us/main', base('Based on master'))

    def test_review_reply_recent(self):
        """Only recent replies get a suggested response"""
        now = datetime.now().astimezone()
        self.assertTrue(review.replies._is_recent(
            email.utils.format_datetime(now - timedelta(days=13)), 14))
        self.assertFalse(review.replies._is_recent(
            email.utils.format_datetime(now - timedelta(days=15)), 14))
        self.assertFalse(review.replies._is_recent('garbage', 14))
        self.assertFalse(review.replies._is_recent(None, 14))

    def test_review_needs_draft(self):
        """Sent reviews are never drafted again; -i selects the patches"""
        def rev(seq, draft_id, status):
            return database.Review(seq, 1, seq, f'body {seq}', False, None,
                                   draft_id, status, None, None)

        new = rev(1, None, database.REVIEW_NEW)
        draft = rev(2, 'd2', database.REVIEW_DRAFT)
        deleted = rev(3, None, database.REVIEW_DELETED)
        sent = rev(4, None, database.REVIEW_SENT)
        replied = rev(5, None, database.REVIEW_REPLIED)
        reviews = [new, draft, deleted, sent, replied]
        self.assertEqual([new], [r for r in reviews
                                 if review.drafts.needs_draft(r, False)])
        self.assertEqual([new, draft, deleted],
                         [r for r in reviews
                          if review.drafts.needs_draft(r, True)])

        args = cmdline.parse_args(['review', '--redraft', '-i', '2-4'],
                                  config_fname=False)
        with mock.patch.object(review.drafts, 'create_drafts') as create, \
                mock.patch.object(review.drafts, 'delete_gmail_drafts'):
            review.drafts.draft_stored_reviews(args, reviews, {}, None, None)
        self.assertEqual({2: 'body 2', 3: 'body 3'}, create.call_args.args[2])

    def test_review_redraft(self):
        """Test --redraft recreates drafts for an already-reviewed series"""
        self.get_cser()
        pwork = self._review_pwork()

        def run(*extra):
            with self._mock_review(), self._mock_gmail():
                with terminal.capture() as (out, _):
                    self.run_review('-s', str(self.REVIEW_LINK), *extra,
                                    pwork=pwork)
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
                review.drafts.delete_gmail_drafts(args, reviews)
        self.assertEqual(2, mock_del.call_count)
        self.assertIn('Deleted 2 old Gmail draft', out.getvalue())

    def test_review_force_deletes_drafts(self):
        """Test -f re-review removes the old Gmail drafts before recreating"""
        self.get_cser()
        pwork = self._review_pwork()

        def run(*extra):
            with self._mock_review(), self._mock_gmail():
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
        with self._mock_review():
            with terminal.capture() as _:
                self.run_review('-s', str(link or self.REVIEW_LINK),
                                pwork=pwork)

    def test_review_scan(self):
        """Test --scan launches a review of a new version"""
        self.get_cser()
        pwork = self._review_pwork()

        # Review v1 first to record the series
        self._review_v1(pwork)

        # Scanning should launch a review of v2 in a child process
        launched = []

        def fake_sub(args, desc, version, link):
            launched.append((version, link))
            return review.scan.ScanResult(
                desc, version, link, 0, 'reviewed v2')

        with mock.patch('patman.review.scan._review_one_subprocess',
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
        pwork = self._review_pwork()

        # Review v1 first so v2 shows up as a new version
        self._review_v1(pwork)

        with mock.patch(
                'patman.review.scan._review_one_subprocess') as mock_sub:
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
        pwork = self._review_pwork()

        # Review the latest version (v2)
        self._review_v1(pwork, self.REVIEW_LINK_V2)

        with mock.patch(
                'patman.review.scan._review_one_subprocess') as mock_sub:
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
        with mock.patch(
                'patman.review.scan._review_one_subprocess') as mock_sub:
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
        pwork = self._review_pwork()
        self.review_states = {self.REVIEW_LINK: ['superseded']}

        with self._mock_review():
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
        pwork = self._review_pwork()
        self.review_states = {self.REVIEW_LINK: ['superseded']}

        with self._mock_review():
            with terminal.capture() as _:
                self.run_review('-s', str(self.REVIEW_LINK), '--any-state',
                                pwork=pwork)
        self.db_open()
        self.assertIsNotNone(cser.db.series_find_by_link(str(self.REVIEW_LINK)))

    def test_review_scan_skips_inactive(self):
        """Test --scan skips a new version that is not active"""
        self.get_cser()
        pwork = self._review_pwork()

        # Review v1 first (active)
        self._review_v1(pwork)

        # v2 has appeared but is not in an active state
        self.review_states = {self.REVIEW_LINK_V2: ['superseded']}
        with mock.patch(
                'patman.review.scan._review_one_subprocess') as mock_sub:
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
        cmd = review.scan._build_review_command(args, self.REVIEW_LINK_V2)

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
        review.core._register_series(cser, 'foo bar', 1, '500', series_data)
        review.core._register_series(cser, 'foo bar', 2, '501', series_data)
        cser.commit()

        self.db_open()
        v1 = cser.db.series_find_by_link('500')
        v2 = cser.db.series_find_by_link('501')
        self.assertIsNotNone(v1)
        self.assertIsNotNone(v2)
        self.assertEqual(v1[0], v2[0])  # linked under the same series

        # The cover letter is recorded, so that it can be shown later
        svr = cser.db.ser_ver_get_by_id(v2[3])
        self.assertEqual((9, '[v2,0/1] foo bar'), (svr.cover_id, svr.name))

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
        pwork = self._review_pwork()

        # Hold the lock for this series, as if a review were in progress
        ups = pwork.upstream if pwork else None
        branch = review.lookup.make_review_name(str(self.REVIEW_LINK), ups)
        lock_fd = review.core._acquire_review_lock(self.tmpdir, branch)
        try:
            with self._mock_review():
                with terminal.capture() as (out, err):
                    self.run_review('-s', str(self.REVIEW_LINK), pwork=pwork)
            self.assertIn('already in progress', err.getvalue())
        finally:
            review.core._release_review_lock(lock_fd)

        # The refused review recorded nothing
        self.db_open()
        self.assertIsNone(cser.db.series_find_by_link(str(self.REVIEW_LINK)))

        # With the lock released, the review now runs
        with self._mock_review():
            with terminal.capture() as _:
                self.run_review('-s', str(self.REVIEW_LINK), pwork=pwork)
        self.db_open()
        self.assertIsNotNone(cser.db.series_find_by_link(str(self.REVIEW_LINK)))

    def _make_review_ctx(self, reviewer_name='Test', reviewer_email='test@test.com',
                         author_name='', author_email='', date='', signoff=None,
                         diffstat=None):
        """Build a ReviewContext for testing format_review_email()"""
        from patman.review.common import ReviewContext

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
        from patman.review.emails import (parse_review_output,
                                          format_review_email)

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
        from patman.review.emails import parse_review_output

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
        with mock.patch.object(review_mod.core.claude_mod, 'run_agent_collect',
                               side_effect=mock_agent), \
             mock.patch.object(review.common, 'ClaudeAgentOptions',
                               mock.MagicMock()), \
             mock.patch.object(review_mod.prompts, 'build_review_prompt',
                               return_value='prompt'), \
             mock.patch.object(review_mod.core.gitutil, 'diff_stat',
                               return_value=''), \
             terminal.capture():
            result = loop.run_until_complete(
                review_mod.core._review_single_patch(
                    ctx, cmt, 1, [(1, 'abc1234', 'spl: pad')]))
        loop.close()
        self.assertIsNone(result)

    def test_review_comments_file(self):
        """Test writing the existing patchwork comments for the agent"""
        class FakePatchwork:
            """Provide comments on the cover letter and each patch"""
            async def get_cover_comments(self, _client, _cover_id):
                """Get the comments on the cover letter"""
                return [{'submitter': {'name': 'Ann', 'email': 'ann@x.org'},
                         'date': '2026-01-02', 'content': 'Nice series'}]

            async def get_patch_comments(self, _client, patch_id):
                """Get the comments on a patch"""
                return [{'submitter': {'name': 'Bob'},
                         'content': f'Comment on {patch_id}'}]

        data = {'cover_letter': {'id': 9},
                'patches': [{'id': 1, 'name': 'video: Fix it'}, {'id': 2}]}
        with terminal.capture():
            fname = review.lookup.write_comments_file(data, FakePatchwork())
        self.assertEqual(
            '=== Comment on cover letter ===\n'
            'From: Ann <ann@x.org>\n'
            'Date: 2026-01-02\n\n'
            'Nice series\n\n'
            '=== Comment on patch 1: video: Fix it ===\n'
            'From: Bob <>\n'
            'Date: \n\n'
            'Comment on 1\n\n'
            '=== Comment on patch 2:  ===\n'
            'From: Bob <>\n'
            'Date: \n\n'
            'Comment on 2\n', tools.read_file(fname, binary=False))

        # With no comments there is no file
        data = {'cover_letter': None, 'patches': []}
        self.assertIsNone(
            review.lookup.write_comments_file(data, FakePatchwork()))

    def test_review_is_code_comment(self):
        """Test code comments are told apart from commit-message comments"""
        from patman.review.emails import is_code_comment

        self.assertTrue(is_code_comment(
            '> diff --git a/foo.c b/foo.c\n> @@ -1 +1 @@\n> +code'))
        self.assertTrue(is_code_comment('> @@ -10,2 +10,3 @@ func()'))
        self.assertFalse(is_code_comment(
            '> When CONFIG_SPL_SEPARATE_BSS is enabled'))
        self.assertFalse(is_code_comment(''))

    def test_review_commit_msg_comment_first(self):
        """Test commit-message comments come before code comments"""
        from patman.review.emails import format_review_email

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

    def test_review_draft_location(self):
        """The draft summary names the branch and series link"""
        from patman.review.drafts import _draft_location
        from patman.review.common import ReviewContext

        ctx = ReviewContext(None, None, {'id': 511354})
        ctx.branch_name = 'rockchip-fixes'
        self.assertEqual(' (branch rockchip-fixes, link 511354)',
                         _draft_location(ctx))

        # Re-draft path: no branch, so just the link
        ctx = ReviewContext(None, None, {'id': 511354})
        self.assertEqual(' (link 511354)', _draft_location(ctx))

        # Nothing known -> no suffix
        self.assertEqual('', _draft_location(ReviewContext(None, None, {})))

    def test_review_commit_msg_comment_inline(self):
        """A commit-message comment is shown inline, not re-quoted below"""
        from patman.review.emails import format_review_email

        ctx = self._make_review_ctx(author_name='Anshul Dalal',
            author_email='anshuld@ti.com', date='2026-07-09',
            signoff='Regards,\nSimon')
        commit_message = ('fdt: fix phandles\n\n'
                          'The phandles were not being copied.')
        comments = [
            ('> The phandles were not being copied.',
             'Please use present tense.'),
            ('> diff --git a/x b/x\n> @@ -1 +1 @@\n> +code',
             'Drop the underscore.'),
        ]
        body = format_review_email(ctx, 'Anshul', 'changes_needed',
                                   comments, commit_message)

        # The quoted line appears once (in the top quote), with the comment
        # right after it -- not re-quoted lower down
        self.assertEqual(1, body.count('> The phandles were not being copied.'))
        quoted = body.index('> The phandles were not being copied.')
        msg_c = body.index('Please use present tense.')
        code_c = body.index('Drop the underscore.')
        self.assertLess(quoted, msg_c)
        self.assertLess(msg_c, code_c)
        # The code comment keeps its diff hunk and stays below the quote
        self.assertLess(body.index('> diff --git a/x b/x'), code_c)

    def test_review_commit_msg_comment_fallback(self):
        """A comment whose quote is not in the message is shown below"""
        from patman.review.emails import format_review_email

        ctx = self._make_review_ctx(author_name='Anshul Dalal',
            author_email='anshuld@ti.com', date='2026-07-09')
        commit_message = 'fdt: fix phandles\n\nThe body text.'
        # The quote does not match any commit-message line
        comments = [('> a line not in the message', 'A general note.')]
        body = format_review_email(ctx, 'Anshul', 'changes_needed',
                                   comments, commit_message)
        # It is preserved below the quote, with its own quoted line
        self.assertIn('> a line not in the message', body)
        self.assertIn('A general note.', body)
        self.assertLess(body.index('> The body text.'),
                        body.index('A general note.'))

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
                result = review_mod.core._run_coverity(ctx, args)
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
                mock.patch('patman.review.core.subprocess.run'), \
                mock.patch('patman.review.core.gitutil.remove_worktree'):
            with terminal.capture():
                text = review_mod.core._run_coverity(ctx, args)
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
        with mock.patch.object(
                review_mod.voice, 'get_voice', return_value=None):
            prompt = review_mod.prompts.build_review_prompt(
                ctx, 'abc1234', 1, [(1, 'abc1234', 'subj')], None)
        self.assertIn('COVERITY', prompt)
        self.assertIn('drivers/foo.c:42', prompt)

    def test_review_aligns_by_subject(self):
        """Test reviews attach to the patchwork patch by subject, not order"""
        from patman import review as review_mod
        from patman.review.common import ReviewContext

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
        with mock.patch.object(review_mod.core.claude_mod, 'check_available',
                               return_value=True), \
                mock.patch.object(review_mod.core.patchstream,
                                  'get_metadata_for_list',
                                  return_value=series), \
                mock.patch.object(review_mod.core, '_review_single_patch',
                                  side_effect=fake_single), \
                terminal.capture():
            bodies = loop.run_until_complete(
                review_mod.core.review_patches(ctx))
        loop.close()
        # beta is patchwork patch 2 and gamma is 3 -- not 1 and 2
        self.assertEqual({2: 'review-beta', 3: 'review-gamma'}, bodies)

    def test_format_findings(self):
        """Test findings are formatted for storage"""
        from patman.review.emails import format_findings
        self.assertEqual('Looks good; no issues found.',
                         format_findings('approved', []))
        body = format_findings('changes_needed', [('> code line', 'Do X.')])
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

        with mock.patch.object(review_mod.core.claude_mod, 'check_available',
                               return_value=True), \
                mock.patch.object(review_mod.core.gitutil, 'get_top_level',
                                  return_value=self.tmpdir), \
                mock.patch.object(review_mod.core, '_run_cover_review_sync',
                                  return_value=cover), \
                mock.patch.object(review_mod.core, '_run_patch_review_sync',
                                  side_effect=results), \
                terminal.capture():
            review_mod.core.review_series(cser, sid, svid, 1, 'foo', series)

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

    def test_review_guess_name(self):
        """Test guessing first name from email address"""
        from patman.review.emails import guess_name_from_email

        self.assertEqual('Simon', guess_name_from_email('simon.glass@xxx'))
        self.assertEqual('Michal', guess_name_from_email('michal@amd.com'))
        self.assertEqual('Marek',
                         guess_name_from_email('marek-vasut@denx.de'))
        self.assertEqual('', guess_name_from_email('j@posteo.net'))
        self.assertEqual('', guess_name_from_email(''))
        self.assertEqual('', guess_name_from_email('12345@test.com'))

    def test_review_cleanup(self):
        """Test mechanical cleanup of review text"""
        from patman.review.emails import cleanup_review_text

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
        from patman.review.emails import format_review_email

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

        from patman.review.emails import refine_reviews

        # An approved review with only structural lines
        approved = ('On 2026-04-01, A <a@b.com> wrote:\n'
                    '> Some commit message\n'
                    '>\n'
                    '> drivers/foo.c | 2 +-\n'
                    '\n'
                    'Reviewed-by: Test <test@test.com>\n')

        # Should be returned unchanged without calling the agent
        loop = asyncio.new_event_loop()
        with patch('patman.review.voice.get_voice', return_value=None):
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
        with patch.object(review_mod.voice, 'get_voice', return_value=None), \
             patch.object(review_mod.core.claude_mod, 'run_agent_collect',
                          side_effect=mock_agent), \
             patch.object(
                 review_mod.common, 'ClaudeAgentOptions', MagicMock()), \
             terminal.capture():
            result = loop.run_until_complete(
                review_mod.emails.refine_reviews({1: review_with_comments}))
        loop.close()
        self.assertIn('Fix this.', result[1])

    def test_review_parse_changes(self):
        """Test parsing a review with comments"""
        from patman.review.emails import (parse_review_output,
                                          format_review_email)

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
        from patman.review.emails import format_review_email

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
