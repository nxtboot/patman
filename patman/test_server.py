# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2026 Simon Glass <sjg@chromium.org>
#
"""Tests for the 'patman serve' web server"""

import asyncio
import json
import os
import signal
import subprocess
import sys
import unittest
from unittest import mock

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from u_boot_pylib import terminal
from u_boot_pylib import tools
from patman import cmdline
from patman import control
from patman import cseries
from patman import database
from patman.database import Pcommit
from patman import patchwork
from patman.patchwork import Patchwork
from patman import server
from patman.test_common import TestCommon
from patman.workflow import Wtype

#pylint: disable=protected-access

class TestWebServer(unittest.TestCase, TestCommon):
    """Tests for the server's JSON API and periodic sync"""
    def setUp(self):
        TestCommon.setUp(self)
        self.cser = cseries.Cseries(self.tmpdir, terminal.COLOR_NEVER)
        with terminal.capture():
            self.cser.open_database()
        self.requests = []

    def tearDown(self):
        self.cser.close_database()
        TestCommon.tearDown(self)

    # pylint: disable=R0913,R0917,R0914
    def add_series(self, name, desc, versions, ups=None, source=None):
        """Add a series to the database

        Args:
            name (str): Series name
            desc (str): Series description
            versions (dict): key (int): version number
                value (list of tuple): one per patch: (subject, state,
                    patch_id, num_comments)
            ups (str or None): Upstream name
            source (str or None): 'review' for a series being reviewed

        Return:
            tuple:
                int: Series ID
                dict: key (int): version, value (int): svid
        """
        db = self.cser.db
        series_id = db.series_add(name, desc, ups)
        if source:
            db.series_set_source(series_id, source)
        svids = {}
        for version, patches in versions.items():
            svid = db.ser_ver_add(series_id, version, link=f'{version}00')
            svids[version] = svid
            db.pcommit_add_list(svid, [
                Pcommit(None, seq, subject, None, None, None, None, None)
                for seq, (subject, *_) in enumerate(patches)])
            for pcm, (_, state, patch_id, ncomments) in zip(
                    db.pcommit_get_list(svid), patches):
                db.pcommit_update(Pcommit(pcm.idnum, None, None, None, None,
                                          state, patch_id, ncomments))
        db.commit()
        return series_id, svids

    def make_server(self, **kwargs):
        """Create a server using a fake patchwork

        Return:
            Server: New server
        """
        srv = server.Server(self.cser, **kwargs)
        srv.pwork_for_testing = Patchwork.for_testing(self._fake_patchwork)
        return srv

    def _fake_patchwork(self, subpath):
        """Handle a request to the fake patchwork server"""
        self.requests.append(subpath)
        if subpath == 'patches/1234/comments/':
            return [{
                'date': '2026-10-01T10:00:00',
                'subject': 'Re: [PATCH 1/2] video: Add a driver',
                'submitter': {'name': 'Tom Rini', 'email': 'trini@k.com'},
                'content': '> some code\n\nReviewed-by: Tom Rini',
                'web_url': 'https://pw/comment/1/',
                'headers': {'ignored': 'yes'},
            }]
        if subpath == 'patches/1234/':
            return {
                'id': 1234, 'name': '[1/2] video: Add a driver',
                'date': '2026-10-01T09:00:00', 'state': 'new',
                'submitter': {'name': 'Fred', 'email': 'f@b.com'},
                'content': 'Add a driver\n\nSigned-off-by: Fred',
                'diff': '--- a/x.c\n+++ b/x.c\n@@ -1 +1 @@\n-old\n+new\n',
                'web_url': 'https://pw/patch/1234/', 'headers': {'big': 1},
                'series': [{'id': 99, 'name': 'video', 'version': 2,
                            'url': 'https://pw/api/series/99/'}],
            }
        if subpath == 'covers/77/':
            return {'id': 77, 'name': '[0/2] video', 'content': 'Cover text',
                    'date': '2026-10-01T09:00:00', 'submitter': {}}
        if subpath == 'covers/77/comments/':
            return []
        if subpath == 'series/200/':
            return {'id': 200, 'cover_letter': {'id': 77, 'name': '[0/2] rev'}}
        raise ValueError(f"Unexpected request '{subpath}'")

    def call(self, srv, method, path, **kwargs):
        """Make a request to a server and return the response

        Args:
            srv (Server): Server to use
            method (str): HTTP method, e.g. 'GET'
            path (str): Path to request

        Return:
            tuple:
                int: HTTP status
                dict or list or str: decoded JSON, or text if not JSON
        """
        async def _go():
            async with TestClient(TestServer(srv.make_app())) as client:
                resp = await client.request(method, path, **kwargs)
                if resp.content_type == 'application/json':
                    return resp.status, await resp.json()
                return resp.status, await resp.text()

        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(_go())
        finally:
            loop.close()

    def test_series_list(self):
        """The series list shows status for the latest version only"""
        ser_id, _ = self.add_series('video', 'Video improvements', {
            1: [('video: Add a driver', 'superseded', 1000, 2)],
            2: [('video: Add a driver', 'accepted', 1234, 1),
                ('video: Tidy up', 'changes-requested', 1235, 3)],
            }, ups='us')
        self.cser.db.workflow_add(Wtype.SENT, ser_id, '2026-10-01 10:00:00')
        self.cser.db.workflow_add(Wtype.SENT, ser_id, '2026-10-05 10:00:00')
        self.add_series('rev', 'Somebody else', {1: [('x', 'new', 5, 0)]},
                        source='review')

        status, data = self.call(self.make_server(), 'GET', '/api/series')
        self.assertEqual(200, status)
        self.assertEqual(1, len(data))
        item = data[0]
        self.assertEqual('video', item['name'])
        self.assertEqual(2, item['version'])
        self.assertEqual(2, item['count'])
        self.assertEqual({'accepted': 1, 'changes-requested': 1},
                         item['states'])
        self.assertEqual(4, item['num_comments'])
        self.assertEqual('2026-10-05 10:00:00', item['sent'])
        self.assertEqual('us', item['upstream'])

    def test_series_detail(self):
        """Series detail shows each version's patches, newest first"""
        ser_id, _ = self.add_series('video', 'Video improvements', {
            1: [('video: Add a driver', 'superseded', 1000, 2)],
            2: [('video: Add a driver', 'accepted', 1234, 1),
                ('video: Tidy up', None, None, None)],
            })
        status, data = self.call(self.make_server(), 'GET',
                                 f'/api/series/{ser_id}')
        self.assertEqual(200, status)
        self.assertEqual('https://my-fake-url', data['patchwork_url'])
        self.assertEqual([2, 1], [v['version'] for v in data['versions']])
        latest = data['versions'][0]
        self.assertEqual('200', latest['link'])
        self.assertEqual(
            [{'seq': 1, 'subject': 'video: Add a driver', 'state': 'accepted',
              'patch_id': 1234, 'num_comments': 1},
             {'seq': 2, 'subject': 'video: Tidy up', 'state': None,
              'patch_id': None, 'num_comments': 0}],
            latest['patches'])
        self.assertEqual({'accepted': 1, 'unknown': 1}, latest['states'])

        status, data = self.call(self.make_server(), 'GET', '/api/series/999')
        self.assertEqual(404, status)
        self.assertIn('No series found', data['error'])

    def test_reviews(self):
        """Reviews written with 'patman review' can be listed and read"""
        ser_id, svids = self.add_series(
            'rev', 'Somebody else', {1: [('a', 'new', 5, 0)],
                                     2: [('a', 'new', 6, 0)]},
            source='review')
        db = self.cser.db
        db.review_add(svids[1], 1, 'old review', False, '2026-09-01 10:00:00')
        db.review_add(svids[2], 0, 'Cover comments', False,
                      '2026-10-01 10:00:00')
        rid = db.review_add(svids[2], 1, '> code\nLooks good', True,
                            '2026-10-01 10:00:00')
        db.review_set_draft_id(rid, 'draft1')
        db.ser_ver_set_notes(svids[2], 'Some notes')
        db.commit()
        self.add_series('video', 'Mine', {1: [('x', 'new', 7, 0)]})

        srv = self.make_server()
        status, data = self.call(srv, 'GET', '/api/reviews')
        self.assertEqual(200, status)
        self.assertEqual(1, len(data))
        self.assertEqual(
            {'id': ser_id, 'name': 'rev', 'desc': 'Somebody else',
             'upstream': None, 'version': 2, 'link': '200', 'num_reviews': 2,
             'approved': 1, 'statuses': {'new': 1, 'draft': 1},
             'timestamp': '2026-10-01 10:00:00', 'replies_waiting': 0},
            data[0])

        status, data = self.call(srv, 'GET', f'/api/reviews/{ser_id}')
        self.assertEqual(200, status)
        latest = data['versions'][0]
        self.assertEqual(2, latest['version'])
        self.assertEqual('Some notes', latest['notes'])
        self.assertEqual([0, 1], [r['seq'] for r in latest['reviews']])
        self.assertEqual(
            {'id': rid, 'seq': 1, 'body': '> code\nLooks good',
             'approved': True, 'timestamp': '2026-10-01 10:00:00',
             'status': 'draft', 'replies': [], 'job': None},
            latest['reviews'][1])
        self.assertEqual(['old review'],
                         [r['body'] for r in data['versions'][1]['reviews']])
        self.assertEqual([(1, 'a', 6)],
                         [(p['seq'], p['subject'], p['patch_id'])
                          for p in latest['patches']])

        # The cover letter of the version with a cover-letter review is
        # looked up once and recorded
        self.assertEqual(['series/200/'], self.requests)
        self.assertEqual((77, '[0/2] rev'),
                         (latest['cover_id'], latest['cover_name']))
        self.assertEqual(77, db.ser_ver_get_by_id(svids[2]).cover_id)
        self.assertIsNone(data['versions'][1]['cover_id'])
        _, data = self.call(srv, 'GET', f'/api/reviews/{ser_id}')
        self.assertEqual(77, data['versions'][0]['cover_id'])
        self.assertEqual(['series/200/'], self.requests)

    def test_review_default_upstream(self):
        """A review with no upstream uses the default upstream's patchwork"""
        self.cser.db.upstream_add('us', 'https://us.git',
                                  patchwork_url='https://pw.us')
        self.cser.db.upstream_set_default('us')
        ser_id, _ = self.add_series('rev', 'Somebody else',
                                    {1: [('a', 'new', 5, 0)]}, source='review')
        srv = server.Server(self.cser, pw_url='https://pw.default')
        _, data = self.call(srv, 'GET', f'/api/reviews/{ser_id}')
        self.assertEqual(('us', 'https://pw.us'),
                         (data['upstream'], data['patchwork_url']))

    def test_review_no_cover(self):
        """A version without a cover letter is only looked up once"""
        ser_id, svids = self.add_series(
            'rev', 'Somebody else', {3: [('a', 'new', 5, 0)]},
            source='review')
        self.cser.db.review_add(svids[3], 0, 'Cover comments', False,
                                '2026-10-01 10:00:00')
        self.cser.db.commit()
        srv = self.make_server()
        srv.pwork_for_testing = Patchwork.for_testing(
            lambda subpath: self.requests.append(subpath) or {'id': 300})
        for _ in range(2):
            _, data = self.call(srv, 'GET', f'/api/reviews/{ser_id}')
            self.assertIsNone(data['versions'][0]['cover_id'])
        self.assertEqual(['series/300/'], self.requests)

    def test_review_db(self):
        """Reviews can be read from a separate database"""
        rdir = os.path.join(self.tmpdir, 'rev')
        os.mkdir(rdir)
        rcser = cseries.Cseries(self.tmpdir, terminal.COLOR_NEVER,
                                db_dir=rdir)
        with terminal.capture():
            rcser.open_database()
        try:
            rdb = rcser.db
            series_id = rdb.series_add('rev', 'In the review db', None)
            rdb.series_set_source(series_id, 'review')
            svid = rdb.ser_ver_add(series_id, 1, link='123')
            rdb.review_add(svid, 1, 'Looks good', True, '2026-10-01 10:00:00')
            rdb.commit()
            self.add_series('mine', 'In the main db', {1: [('x', 'new', 7, 0)]},
                            source='review')

            srv = server.Server(self.cser, review_sync=True, rcser=rcser)
            _, data = self.call(srv, 'GET', '/api/reviews')
            self.assertEqual(['In the review db'], [r['desc'] for r in data])
            _, data = self.call(srv, 'GET', f'/api/reviews/{series_id}')
            self.assertEqual('Looks good',
                             data['versions'][0]['reviews'][0]['body'])
            self.assertEqual(['--review-db-dir', rdir],
                             srv.get_sync_cmds()[1][-2:])
        finally:
            rcser.close_database()

    def test_todo(self):
        """Todos are listed with whether they are due yet"""
        ser_id, _ = self.add_series('video', 'Mine', {1: []})
        self.cser.db.workflow_add(Wtype.TODO, ser_id, '2020-01-01 00:00:00')
        ser2, _ = self.add_series('usb', 'Later', {1: []})
        self.cser.db.workflow_add(Wtype.TODO, ser2, '2099-01-01 00:00:00')
        self.cser.db.commit()

        status, data = self.call(self.make_server(), 'GET', '/api/todo')
        self.assertEqual(200, status)
        self.assertEqual([('video', True), ('usb', False)],
                         [(t['name'], t['due']) for t in data])
        self.assertEqual(ser_id, data[0]['series_id'])

    def test_comments(self):
        """Comments are read from patchwork and trimmed down"""
        srv = self.make_server()
        status, data = self.call(srv, 'GET', '/api/comments/patch/1234')
        self.assertEqual(200, status)
        self.assertEqual(
            [{'date': '2026-10-01T10:00:00',
              'name': 'Tom Rini', 'email': 'trini@k.com',
              'subject': 'Re: [PATCH 1/2] video: Add a driver',
              'content': '> some code\n\nReviewed-by: Tom Rini',
              'web_url': 'https://pw/comment/1/'}], data)

        status, data = self.call(srv, 'GET', '/api/comments/cover/77')
        self.assertEqual((200, []), (status, data))
        self.assertEqual(['patches/1234/comments/', 'covers/77/comments/'],
                         self.requests)

        status, _ = self.call(srv, 'GET', '/api/comments/fred/77')
        self.assertEqual(404, status)

    def test_patch(self):
        """A patch or cover letter can be read from patchwork"""
        srv = self.make_server()
        status, data = self.call(srv, 'GET', '/api/patch/1234')
        self.assertEqual(200, status)
        self.assertEqual(
            {'id': 1234, 'name': '[1/2] video: Add a driver',
             'date': '2026-10-01T09:00:00', 'submitter': 'Fred',
             'state': 'new', 'content': 'Add a driver\n\nSigned-off-by: Fred',
             'diff': '--- a/x.c\n+++ b/x.c\n@@ -1 +1 @@\n-old\n+new\n',
             'web_url': 'https://pw/patch/1234/',
             'series': [{'id': 99, 'name': 'video', 'version': 2}]}, data)

        status, data = self.call(srv, 'GET', '/api/cover/77')
        self.assertEqual(200, status)
        self.assertEqual(('Cover text', '', None),
                         (data['content'], data['diff'], data['submitter']))

    def test_blob(self):
        """A file can be read from the git repo by its blob hash"""
        def add_blob(data):
            return subprocess.run(
                ['git', 'hash-object', '-w', '--stdin'], cwd=self.tmpdir,
                input=data, capture_output=True, check=True).stdout.decode(
                    ).strip()

        subprocess.run(['git', 'init', '-q'], cwd=self.tmpdir, check=True)
        oid = add_blob(b'line 1\nline 2\n')
        binary = add_blob(b'a\0b')
        srv = self.make_server()

        # The first blob which the repo holds is used
        status, data = self.call(srv, 'GET', '/api/blob',
                                 params=[('hash', '1234567'),
                                         ('hash', oid[:10])])
        self.assertEqual((200, {'hash': oid[:10], 'text': 'line 1\nline 2\n'}),
                         (status, data))

        for params, msg in (
                ([('hash', '1234567')], 'The file is not in the git repo'),
                ([], 'The file is not in the git repo'),
                ([('hash', '--help')], "Invalid blob hash '--help'"),
                ([('hash', binary)], 'File is not text')):
            status, data = self.call(srv, 'GET', '/api/blob', params=params)
            self.assertEqual((404, {'error': msg}), (status, data))

    def test_pwork_per_upstream(self):
        """Each upstream uses its own patchwork URL"""
        self.cser.db.upstream_add('us', 'https://us.git',
                                  patchwork_url='https://pw.us')
        srv = server.Server(self.cser, pw_url='https://pw.default')
        self.assertEqual('https://pw.us', srv.get_pwork('us').url)
        self.assertIs(srv.get_pwork('us'), srv.get_pwork('us'))
        self.assertEqual('https://pw.default', srv.get_pwork(None).url)
        self.assertEqual('https://pw.default', srv.get_pwork('other').url)

        srv = server.Server(self.cser)
        with self.assertRaises(ValueError) as exc:
            srv.get_pwork('other')
        self.assertIn("No patchwork URL for upstream 'other'",
                      str(exc.exception))

    def test_token(self):
        """A token is needed for the API when one is set"""
        srv = self.make_server(token='secret')
        status, data = self.call(srv, 'GET', '/api/todo')
        self.assertEqual((401, {'error': 'Unauthorised'}), (status, data))
        status, _ = self.call(srv, 'GET', '/api/todo',
                              headers={'Authorization': 'Bearer wrong'})
        self.assertEqual(401, status)
        status, _ = self.call(srv, 'GET', '/api/todo',
                              headers={'Authorization': 'Bearer secret'})
        self.assertEqual(200, status)
        status, _ = self.call(srv, 'GET', '/api/todo?token=secret')
        self.assertEqual(200, status)

        # The page itself does not need the token, since it asks for it
        status, text = self.call(srv, 'GET', '/')
        self.assertEqual(200, status)
        self.assertIn('<title>patman</title>', text)
        status, _ = self.call(srv, 'GET', '/static/manifest.json')
        self.assertEqual(200, status)

        # So do its script and style sheet
        self.assertIn('static/app.js', text)
        self.assertIn('static/app.css', text)
        status, text = self.call(srv, 'GET', '/static/app.js')
        self.assertEqual(200, status)
        self.assertIn('function route()', text)
        status, _ = self.call(srv, 'GET', '/static/app.css')
        self.assertEqual(200, status)

    def test_no_cache(self):
        """The page and its files are checked before a browser reuses them"""
        srv = server.Server(self.cser)

        async def _go():
            async with TestClient(TestServer(srv.make_app())) as client:
                out = {}
                for path in ('/', '/static/app.js', '/api/series'):
                    resp = await client.get(path)
                    out[path] = resp.headers.get('Cache-Control')
                return out

        loop = asyncio.new_event_loop()
        try:
            got = loop.run_until_complete(_go())
        finally:
            loop.close()
        self.assertEqual('no-cache', got['/'])
        self.assertEqual('no-cache', got['/static/app.js'])
        self.assertIsNone(got['/api/series'])

    def test_sync_cmds(self):
        """The sync never gathers tags into branches"""
        srv = server.Server(self.cser)
        patman = [sys.executable, '-m', 'patman', '-v']
        self.assertEqual([patman + ['series', 'gather-all', '-G']],
                         srv.get_sync_cmds())
        self.assertEqual('patman series gather-all -G',
                         srv._cmd_label(srv.get_sync_cmds()[0]))
        self.assertEqual('sh -c true', srv._cmd_label(['sh', '-c', 'true']))
        srv = server.Server(self.cser, review_sync=True)
        self.assertEqual([patman + ['series', 'gather-all', '-G'],
                          patman + ['review', '--sync']],
                         srv.get_sync_cmds())

    def test_sync_fetch(self):
        """The sync fetches the upstreams which reviews use"""
        db = self.cser.db
        db.upstream_add('us', 'https://us', 'https://pw.us')
        db.upstream_set_default('us')
        db.commit()
        self.add_series('rev', 'Review', {1: []}, ups='other',
                        source='review')
        self.add_series('rev2', 'Review', {1: []}, ups='ci', source='review')
        self.add_series('mine', 'Mine', {1: []}, ups='mine')
        srv = server.Server(self.cser)

        # Without the remotes, there is nothing to fetch
        self.assertIsNone(srv.get_fetch_cmd())

        subprocess.run(['git', 'init', '-q'], cwd=self.tmpdir, check=True)
        for name in ['us', 'other', 'mine']:
            subprocess.run(['git', 'remote', 'add', name, f'https://{name}'],
                           cwd=self.tmpdir, check=True)
        self.assertEqual(['git', 'fetch', '-q', '--multiple', 'other', 'us'],
                         srv.get_sync_cmds()[0])

    def test_sync(self):
        """A sync runs each command and records its output"""
        srv = self.make_server()
        srv.sync_cmds_for_testing = [
            ['sh', '-c', 'echo one; pwd'],
            ['sh', '-c', 'echo bad >&2; exit 3'],
            ['/no/such/program'],
        ]
        status, data = self.call(srv, 'GET', '/api/sync')
        self.assertEqual(200, status)
        self.assertFalse(data['running'])
        self.assertIsNone(data['started'])

        loop = asyncio.new_event_loop()
        try:
            self.assertTrue(loop.run_until_complete(srv.run_sync()))
        finally:
            loop.close()
        state = srv.sync_state
        self.assertFalse(state['running'])
        self.assertTrue(state['started'])
        self.assertTrue(state['finished'])
        res = state['results']
        self.assertEqual(3, len(res))
        self.assertEqual(
            {'cmd': 'sh -c echo one; pwd', 'returncode': 0,
             'output': f'one\n{self.tmpdir}\n'}, res[0])
        self.assertEqual((3, 'bad\n'), (res[1]['returncode'], res[1]['output']))
        self.assertEqual(-1, res[2]['returncode'])
        self.assertIn('No such file', res[2]['output'])

    def test_sync_post(self):
        """POST /api/sync starts a sync, but only one at a time"""
        srv = self.make_server()
        srv.sync_cmds_for_testing = [['sh', '-c', 'echo hi']]

        async def _go():
            async with TestClient(TestServer(srv.make_app())) as client:
                resp = await client.post('/api/sync')
                first = resp.status, (await resp.json())['running']
                resp = await client.post('/api/sync')
                second = resp.status
                while srv.sync_state['running']:
                    await asyncio.sleep(0.01)
                return first, second

        loop = asyncio.new_event_loop()
        try:
            first, second = loop.run_until_complete(_go())
        finally:
            loop.close()
        self.assertEqual((202, True), first)
        self.assertEqual(409, second)
        self.assertEqual('hi\n', srv.sync_state['results'][0]['output'])

    def test_sync_progress(self):
        """Output is visible while a command is still running"""
        srv = self.make_server()
        srv.sync_cmds_for_testing = [['sh', '-c', 'echo first; sleep 10']]

        async def _go():
            task = asyncio.create_task(srv.run_sync())
            while (not srv.sync_state['results'] or
                   not srv.sync_state['results'][0]['output']):
                await asyncio.sleep(0.01)
            seen = dict(srv.sync_state['results'][0])
            srv.cancel_sync()
            await task
            return seen

        loop = asyncio.new_event_loop()
        try:
            seen = loop.run_until_complete(_go())
        finally:
            loop.close()
        self.assertEqual('first\n', seen['output'])
        self.assertIsNone(seen['returncode'])

    def test_sync_cancel(self):
        """POST /api/sync/cancel stops the sync and skips later commands"""
        srv = self.make_server()
        srv.sync_cmds_for_testing = [['sh', '-c', 'echo start; sleep 10'],
                                     ['sh', '-c', 'echo never']]

        async def _go():
            async with TestClient(TestServer(srv.make_app())) as client:
                resp = await client.post('/api/sync/cancel')
                idle = resp.status
                await client.post('/api/sync')
                while not srv.sync_state['results'][0]['output']:
                    await asyncio.sleep(0.01)
                resp = await client.post('/api/sync/cancel')
                busy = resp.status
                while srv.sync_state['running']:
                    await asyncio.sleep(0.01)
                return idle, busy

        loop = asyncio.new_event_loop()
        try:
            idle, busy = loop.run_until_complete(_go())
        finally:
            loop.close()
        self.assertEqual((409, 202), (idle, busy))
        state = srv.sync_state
        self.assertTrue(state['cancelled'])
        self.assertEqual(1, len(state['results']))
        self.assertEqual('start\n', state['results'][0]['output'])
        self.assertEqual(-signal.SIGTERM, state['results'][0]['returncode'])

    def test_parse_review_req(self):
        """Review requests are turned into 'patman review' arguments"""
        ups = ['us', 'ci']
        parse = server.parse_review_req
        self.assertEqual((['-s', '123'], 'Review series 123'),
                         parse({'target': ' 123 '}, ups))
        self.assertEqual(
            (['-s', '456', '-i', '1,3-5', '-U', 'us', '-d', '-f'],
             'Review series 456'),
            parse({'target': 'https://pw/project/uboot/list/?series=456',
                   'index': '1, 3-5', 'ups': 'us', 'drafts': True,
                   'force': True}, ups))
        self.assertEqual((['-s', '457'], 'Review series 457'),
                         parse({'target': 'https://pw/series/457/'}, ups))
        self.assertEqual(
            (['-p', '2000', '-w'], 'Review series of patch 2000'),
            parse({'target': 'https://pw/project/uboot/patch/2000/',
                   'whole': True}, ups))
        self.assertEqual((['-p', '2001'], 'Review patch 2001'),
                         parse({'patch': 2001}, ups))
        self.assertEqual((['-S', 'video: Fix', '-V', '3'],
                          "Review 'video: Fix'"),
                         parse({'title': 'video: Fix', 'version': '3'}, ups))

        for data, msg in (
                ({}, 'Please give a series'),
                ({'target': 'fred'}, 'Cannot find a series'),
                ({'target': '1', 'title': 'x'}, 'only one of'),
                ({'target': '1', 'index': '1;rm'}, 'Invalid patch selection'),
                ({'target': '1', 'ups': 'zz'}, "Unknown upstream 'zz'"),
                ({'patch': 'x'}, "Invalid patch 'x'"),
                ({'patch': True}, "Invalid patch 'True'")):
            with self.assertRaises(ValueError) as exc:
                parse(data, ups)
            self.assertIn(msg, str(exc.exception))

    def run_jobs(self, srv, calls, each=False):
        """Make requests to a server, then wait for its jobs to finish

        Args:
            srv (Server): Server to use
            calls (list of tuple): (method, path, json) for each request
            each (bool): True to wait for the jobs after each request, so
                that each one is free to act on the reviews

        Return:
            list of tuple: (status, data) for each request
        """
        async def _wait():
            while any(job.active for job in srv.get_jobs()):
                await asyncio.sleep(0.01)

        async def _go():
            out = []
            async with TestClient(TestServer(srv.make_app())) as client:
                for method, path, data in calls:
                    resp = await client.request(method, path, json=data)
                    out.append((resp.status, await resp.json()))
                    if each:
                        await _wait()
                await _wait()
            return out

        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(_go())
        finally:
            loop.close()

    def test_review_jobs(self):
        """Reviews and redrafts run as jobs, one at a time"""
        series_id, _ = self.add_series('rev', 'Somebody else',
                                       {1: [('a', 'new', 5, 0)]}, ups='us',
                                       source='review')
        srv = self.make_server()
        srv.get_patman_cmd = lambda *args: ['echo', *args]
        res = self.run_jobs(srv, [
            ('POST', '/api/review', {'target': '123', 'drafts': True}),
            ('POST', '/api/review/scan', {}),
            ('POST', f'/api/reviews/{series_id}/redraft', {'seqs': [2, 0]}),
            ('POST', '/api/review', {'title': ''}),
        ])
        self.assertEqual([202, 202, 202, 404], [status for status, _ in res])
        self.assertEqual('Review series 123', res[0][1]['title'])
        self.assertTrue(res[0][1]['queued'] or res[0][1]['running'])
        self.assertIn('Please give a series', res[3][1]['error'])

        _, jobs = self.call(srv, 'GET', '/api/jobs')
        self.assertEqual([3, 2, 1], [job['id'] for job in jobs])
        self.assertEqual(
            ['review -s 123 -d', 'review --scan',
             'review -s 100 --redraft -i 0,2 -U us'],
            [job['results'][0]['output'].strip() for job in reversed(jobs)])
        self.assertEqual("Redraft 'Somebody else' v1", jobs[0]['title'])

        status, job = self.call(srv, 'GET', '/api/jobs/2')
        self.assertEqual((200, 'review'), (status, job['kind']))
        status, _ = self.call(srv, 'GET', '/api/jobs/99')
        self.assertEqual(404, status)
        status, _ = self.call(srv, 'POST', '/api/jobs/1/cancel')
        self.assertEqual(409, status)

    def test_send_reviews(self):
        """The drafts of one review, or all of them, can be sent"""
        series_id, _ = self.add_series('rev', 'Somebody else',
                                       {1: [('a', 'new', 5, 0)]}, ups='us',
                                       source='review')
        srv = self.make_server()
        srv.get_patman_cmd = lambda *args: ['echo', *args]
        path = f'/api/reviews/{series_id}/send'
        res = self.run_jobs(srv, [
            ('POST', path, {'seqs': [0]}),
            ('POST', path, {}),
            ('POST', path, {'seqs': 'all'}),
        ], each=True)
        self.assertEqual([202, 202, 404], [status for status, _ in res])
        self.assertEqual(["Send the cover letter of 'Somebody else' v1",
                          "Send reviews of 'Somebody else' v1"],
                         [res[0][1]['title'], res[1][1]['title']])
        self.assertEqual(
            ['review -s 100 --send -i 0 -U us', 'review -s 100 --send -U us'],
            [job.state['results'][0]['output'].strip()
             for job in reversed(srv.get_jobs())])

    def test_revise_review(self):
        """The agent can be asked to change a review, part of one, or all"""
        series_id, _ = self.add_series('rev', 'Somebody else',
                                       {1: [('a', 'new', 5, 0)]}, ups='us',
                                       source='review')
        srv = self.make_server()
        srv.get_patman_cmd = lambda *args: ['echo', *args]
        path = f'/api/reviews/{series_id}/revise'
        res = self.run_jobs(srv, [
            ('POST', path, {'seqs': [1], 'note': ' Drop this ',
                            'part': '> code\nNit', 'drafts': True}),
            ('POST', path, {'note': 'Approve them all'}),
            ('POST', path, {'note': ' '}),
            ('POST', path, {'note': 'x', 'seqs': ['1']}),
        ], each=True)
        self.assertEqual([202, 202, 404, 404], [status for status, _ in res])
        self.assertEqual("Revise patch 1 of 'Somebody else' v1",
                         res[0][1]['title'])
        self.assertEqual("Revise reviews of 'Somebody else' v1",
                         res[1][1]['title'])
        self.assertIn('Please say what to change', res[2][1]['error'])
        self.assertIn('Invalid seqs', res[3][1]['error'])
        self.assertEqual(
            ['review -s 100 --revise About this part of the review:\n\n'
             '> code\nNit\n\nDrop this -i 1 -U us -d',
             'review -s 100 --revise Approve them all -U us'],
            [job.state['results'][0]['output'].strip()
             for job in reversed(srv.get_jobs())])

    def test_review_lock(self):
        """A review cannot be changed while a job is acting on it"""
        series_id, svids = self.add_series(
            'rev', 'Somebody else', {1: [('a', 'new', 5, 0)]}, ups='us',
            source='review')
        db = self.cser.db
        cover = db.review_add(svids[1], 0, 'Cover', False,
                              '2026-10-01 10:00:00')
        rid = db.review_add(svids[1], 1, 'Looks good', True,
                            '2026-10-01 10:00:00')
        db.commit()
        srv = self.make_server()
        flag = os.path.join(self.tmpdir, 'flag')
        srv.get_patman_cmd = lambda *args: [
            'sh', '-c', f'while [ ! -e {flag} ]; do sleep 0.05; done']
        path = f'/api/reviews/{series_id}'

        async def _go():
            out = {}
            async with TestClient(TestServer(srv.make_app())) as client:
                async def call(method, url, data=None):
                    resp = await client.request(method, url, json=data)
                    return resp.status, await resp.json()

                # Revise patch 1, which locks just its review
                status, job = await call('POST', f'{path}/revise',
                                         {'seqs': [1], 'note': 'Fix it'})
                out['started'] = status, job['target']
                _, data = await call('GET', path)
                out['marked'] = [rev['job'] for rev in
                                 data['versions'][0]['reviews']]
                out['again'] = (await call('POST', f'{path}/redraft',
                                           {'seqs': [1]}))[0]
                out['all'] = (await call('POST', f'{path}/send', {}))[0]
                out['edit'] = await call('POST', f'/api/review-items/{rid}',
                                         {'body': 'Changed'})
                out['other'] = (await call('POST',
                                           f'/api/review-items/{cover}',
                                           {'body': 'New cover'}))[0]

                # Once the job finishes, the review can be changed again
                tools.write_file(flag, b'')
                while any(job.active for job in srv.get_jobs()):
                    await asyncio.sleep(0.01)
                _, data = await call('GET', path)
                out['after'] = [rev['job'] for rev in
                                data['versions'][0]['reviews']]
                out['free'] = (await call('POST', f'/api/review-items/{rid}',
                                          {'body': 'Changed'}))[0]
            return out

        loop = asyncio.new_event_loop()
        try:
            out = loop.run_until_complete(_go())
        finally:
            loop.close()
        self.assertEqual((202, {'series': series_id, 'version': 1,
                                'seqs': [1]}), out['started'])
        self.assertEqual([None, 1], out['marked'])
        self.assertEqual(409, out['again'])
        self.assertEqual(409, out['all'])
        self.assertEqual(409, out['edit'][0])
        self.assertIn('already acting on this review', out['edit'][1]['error'])
        self.assertEqual(200, out['other'])
        self.assertEqual([None, None], out['after'])
        self.assertEqual(200, out['free'])
        self.assertEqual('Changed', db.review_get(rid).body)

    def test_edit_review(self):
        """A review which has not been sent can be changed and redrafted"""
        series_id, svids = self.add_series(
            'rev', 'Somebody else', {1: [('a', 'new', 5, 0)]}, ups='us',
            source='review')
        db = self.cser.db
        rid = db.review_add(svids[1], 1, 'Looks good', True,
                            '2026-10-01 10:00:00')
        db.review_set_draft_id(rid, 'draft1')
        sent = db.review_add(svids[1], 0, 'Cover', False,
                             '2026-10-01 10:00:00')
        db.review_set_sent(sent, 'Cover')
        db.commit()

        srv = self.make_server()
        srv.get_patman_cmd = lambda *args: ['echo', *args]
        res = self.run_jobs(srv, [
            ('POST', f'/api/review-items/{rid}',
             {'body': 'Please fix', 'approved': False}),
            ('POST', f'/api/review-items/{rid}',
             {'body': 'Please fix this', 'redraft': True}),
            ('POST', f'/api/review-items/{rid}',
             {'body': 'Fine\n\nReviewed-by: Me <me@b.com>'}),
            ('POST', f'/api/review-items/{sent}', {'body': 'x'}),
            ('POST', f'/api/review-items/{rid}', {'body': ' '}),
            ('POST', '/api/review-items/999', {'body': 'x'}),
        ], each=True)
        self.assertEqual([200, 202, 200, 409, 404, 404],
                         [status for status, _ in res])
        self.assertTrue(res[2][1]['approved'])
        res = res[:2] + res[3:]
        self.assertEqual(('Please fix', False),
                         (res[0][1]['body'], res[0][1]['approved']))
        self.assertIn('already been sent', res[2][1]['error'])
        self.assertIn('cannot be empty', res[3][1]['error'])
        rev = db.review_get(rid)
        self.assertEqual(1, rev.approved)
        self.assertEqual(['review -s 100 --redraft -i 1 -U us'],
                         [job.state['results'][0]['output'].strip()
                          for job in srv.get_jobs()])
        self.assertEqual(series_id, db.ser_ver_get_by_id(svids[1]).series_id)

    def test_replies(self):
        """Replies to reviews can be seen, and responses changed and drafted"""
        series_id, svids = self.add_series(
            'rev', 'Somebody else', {1: [('a', 'new', 5, 0)]}, ups='us',
            source='review')
        db = self.cser.db
        rid = db.review_add(svids[1], 1, 'Please fix', False,
                            '2026-10-01 10:00:00')
        db.review_set_sent(rid, 'Please fix', 'm1', 't1')
        first = db.reply_add(rid, 'm2', '<a2@x>', '<r1@me>', 'Fred <f@x>',
                             'me@x', '', 'Re: [PATCH] a', 'today', 'Why?',
                             '2026-10-02 10:00:00')
        second = db.reply_add(rid, 'm3', '<a3@x>', '<r1@me>', 'Tom <t@x>',
                              'me@x', '', 'Re: [PATCH] a', 'today', 'OK',
                              '2026-10-02 11:00:00')
        db.reply_set_response(second, 'Thanks', database.REPLY_SENT)
        db.commit()

        srv = self.make_server()
        srv.get_patman_cmd = lambda *args: ['echo', *args]
        _, data = self.call(srv, 'GET', '/api/reviews')
        self.assertEqual(1, data[0]['replies_waiting'])
        _, data = self.call(srv, 'GET', f'/api/reviews/{series_id}')
        replies = data['versions'][0]['reviews'][0]['replies']
        self.assertEqual(
            {'id': first, 'sender': 'Fred <f@x>', 'subject': 'Re: [PATCH] a',
             'date': 'today', 'body': 'Why?', 'response': None,
             'status': 'new', 'timestamp': '2026-10-02 10:00:00'},
            replies[0])
        self.assertEqual(('sent', 'Thanks'),
                         (replies[1]['status'], replies[1]['response']))

        res = self.run_jobs(srv, [
            ('POST', f'/api/replies/{first}', {'response': 'Because'}),
            ('POST', f'/api/replies/{first}',
             {'response': 'Because of this', 'draft': True}),
            ('POST', f'/api/replies/{first}/suggest', None),
            ('POST', f'/api/replies/{first}/send', None),
            ('POST', f'/api/replies/{second}', {'response': 'x'}),
            ('POST', f'/api/replies/{second}/send', None),
            ('POST', '/api/replies/999', {'skip': True}),
        ])
        self.assertEqual([200, 202, 202, 202, 409, 409, 404],
                         [status for status, _ in res])
        self.assertEqual(('suggested', 'Because'),
                         (res[0][1]['status'], res[0][1]['response']))
        self.assertEqual(
            [f'review --draft-reply {first}', f'review --suggest-reply {first}',
             f'review --send-reply {first}'],
            [job.state['results'][0]['output'].strip()
             for job in reversed(srv.get_jobs())])
        self.assertEqual('Because of this', db.reply_get(first).response)

        _, data = self.call(srv, 'POST', f'/api/replies/{first}',
                            json={'skip': True})
        self.assertEqual('skip', data['status'])
        _, data = self.call(srv, 'GET', '/api/reviews')
        self.assertEqual(0, data[0]['replies_waiting'])

    def test_cancel_queued_job(self):
        """A job which is waiting for another can be cancelled"""
        srv = self.make_server()
        cmds = iter([['sh', '-c', 'echo busy; sleep 10'], ['echo', 'never']])
        srv.get_patman_cmd = lambda *args: next(cmds)

        async def _go():
            async with TestClient(TestServer(srv.make_app())) as client:
                await client.post('/api/review', json={'target': '1'})
                await client.post('/api/review', json={'target': '2'})
                first, second = srv.get_job(1), srv.get_job(2)
                while not first.state['results'] or \
                        not first.state['results'][0]['output']:
                    await asyncio.sleep(0.01)
                queued = second.state['queued']
                await client.post('/api/jobs/2/cancel')
                await client.post('/api/jobs/1/cancel')
                while first.active or second.active:
                    await asyncio.sleep(0.01)
                return queued

        loop = asyncio.new_event_loop()
        try:
            self.assertTrue(loop.run_until_complete(_go()))
        finally:
            loop.close()
        self.assertTrue(srv.get_job(1).state['cancelled'])
        second = srv.get_job(2).state
        self.assertTrue(second['cancelled'])
        self.assertEqual([], second['results'])

    def test_resume_jobs(self):
        """Jobs which do not finish are run again when the server restarts"""
        fname = os.path.join(self.tmpdir, 'jobs.json')
        flag = os.path.join(self.tmpdir, 'flag')
        srv = self.make_server(jobs_fname=fname)
        cmds = iter([
            ['sh', '-c', f'echo busy; [ -e {flag} ] || sleep 10'],
            ['sh', '-c', 'echo cancel; sleep 10'],
            ['echo', 'done']])
        srv.get_patman_cmd = lambda *args: next(cmds)

        def read_jobs():
            with open(fname, encoding='utf-8') as inf:
                return json.load(inf)

        async def wait_output(job):
            while not job.state['results'] or \
                    not job.state['results'][0]['output']:
                await asyncio.sleep(0.01)

        async def _go():
            async with TestClient(TestServer(srv.make_app())) as client:
                for target in '123':
                    await client.post('/api/review', json={'target': target})
                await wait_output(srv.get_job(1))
                saved = [job['title'] for job in read_jobs()]

                # Stop the first job as if the server were stopping, then
                # cancel the second
                os.killpg(srv.get_job(1).proc.pid,
                          signal.SIGTERM)
                await wait_output(srv.get_job(2))
                await client.post('/api/jobs/2/cancel')
                while any(job.active for job in srv.get_jobs()):
                    await asyncio.sleep(0.01)
                return saved

        loop = asyncio.new_event_loop()
        try:
            with terminal.capture():
                saved = loop.run_until_complete(_go())
        finally:
            loop.close()
        self.assertEqual(['Review series 1', 'Review series 2',
                          'Review series 3'], saved)
        self.assertEqual(
            [{'kind': 'review', 'title': 'Review series 1',
              'cmds': [['sh', '-c', f'echo busy; [ -e {flag} ] || sleep 10']],
              'resumed': 0, 'target': None}], read_jobs())

        # A new server runs the first job again
        with open(flag, 'w', encoding='utf-8'):
            pass
        srv = self.make_server(jobs_fname=fname)
        with terminal.capture() as (out, _):
            self.run_jobs(srv, [])
        self.assertIn("Resuming job 'Review series 1'", out.getvalue())
        job = srv.get_jobs()[0].state
        self.assertEqual(('Review series 1', 1, 0, 'busy\n'),
                         (job['title'], job['resumed'],
                          job['results'][0]['returncode'],
                          job['results'][0]['output']))
        self.assertEqual([], read_jobs())

        # A job is not resumed too often
        with open(fname, 'w', encoding='utf-8') as outf:
            json.dump([{'kind': 'review', 'title': 'Bad', 'cmds': [['false']],
                        'resumed': server.MAX_RESUMES}], outf)
        srv = self.make_server(jobs_fname=fname)
        with terminal.capture() as (_, err):
            self.run_jobs(srv, [])
        self.assertIn("Not resuming job 'Bad' again", err.getvalue())
        self.assertEqual([], srv.get_jobs())
        self.assertEqual([], read_jobs())

    def test_inbox(self):
        """The inbox lists recent series, marking those already reviewed"""
        rev_id, _ = self.add_series('rev', 'video: Add a driver',
                                    {2: [('a', 'new', 6, 0)]},
                                    source='review')
        self.add_series('mine', 'My series', {1: [('x', 'new', 7, 0)]})
        db = self.cser.db
        db.upstream_add('us', 'https://us', 'https://pw.us')
        db.upstream_set_default('us')
        db.patchwork_update('U-Boot', 6, 'uboot', 'us')
        db.commit()

        def series(idnum, name, version):
            return {'id': idnum, 'name': name, 'version': version,
                    'date': '2026-10-01T10:00:00', 'total': 1,
                    'received_all': True, 'web_url': f'https://pw/{idnum}',
                    'submitter': {'name': 'Fred', 'email': 'f@b.com'},
                    'cover_letter': None,
                    'patches': [{'id': idnum * 10, 'name': name}]}

        pages = {
            'series/?project=uboot&order=-date&page=1&per_page=50': [
                series(300, '[v3] video: Add a driver', 3),
                series(200, 'video: Add a driver', 2),
                series(100, 'My series', 1),
                series(400, 'net: Something new', 1)],
        }
        srv = self.make_server()
        srv.pwork_for_testing.fake_request = pages.get
        status, data = self.call(srv, 'GET', '/api/inbox')
        self.assertEqual(200, status)
        self.assertEqual(('us', 1), (data['ups'], data['page']))
        items = {item['id']: item for item in data['series']}
        self.assertEqual([300, 200, 100, 400], list(items))

        # A new version of a reviewed series is linked to the review
        self.assertEqual((None, rev_id, False),
                         (items[300]['reviewed'], items[300]['earlier'],
                          items[300]['mine']))
        self.assertEqual((rev_id, None), (items[200]['reviewed'],
                                          items[200]['earlier']))
        self.assertTrue(items[100]['mine'])
        self.assertEqual((None, None, False),
                         (items[400]['reviewed'], items[400]['earlier'],
                          items[400]['mine']))
        self.assertEqual('Fred', items[400]['submitter'])
        self.assertEqual([{'id': 4000, 'name': 'net: Something new'}],
                         items[400]['patches'])

        status, data = self.call(srv, 'GET', '/api/inbox?ups=xx')
        self.assertEqual(404, status)
        self.assertIn("No patchwork project for upstream 'xx'", data['error'])

    def test_patchwork_retry(self):
        """Patchwork requests are retried when the server is slow or busy"""
        calls = []
        real_sleep = asyncio.sleep

        async def handler(request):
            calls.append(request.path)
            if len(calls) == 1:
                await real_sleep(1)
            if len(calls) == 2:
                return web.Response(status=502)
            return web.json_response({'ok': len(calls)})

        app = web.Application()
        app.router.add_get('/api/1.2/{tail:.*}', handler)

        async def _go():
            async with TestServer(app) as tserver:
                pwork = Patchwork(str(tserver.make_url('')).rstrip('/'),
                                  show_progress=False)
                async with aiohttp.ClientSession() as client:
                    return await pwork._request(client, 'patches/1/')

        loop = asyncio.new_event_loop()
        try:
            with mock.patch.object(patchwork, 'REQUEST_TIMEOUT', 0.2), \
                    mock.patch.object(asyncio, 'sleep',
                                      side_effect=self._no_sleep(asyncio.sleep)):
                self.assertEqual({'ok': 3}, loop.run_until_complete(_go()))
        finally:
            loop.close()
        self.assertEqual(3, len(calls))

    def test_patchwork_connect_retry(self):
        """A failed connection is retried, but a name which fails is not"""
        sleeps = []

        async def fake_sleep(secs):
            sleeps.append(secs)

        async def _go(url):
            pwork = Patchwork(url, show_progress=False)
            async with aiohttp.ClientSession() as client:
                return await pwork._request(client, 'patches/1/')

        for url, expect in (('http://127.0.0.1:1', [1, 2, 3]),
                            ('http://no-such-host.invalid', [])):
            sleeps.clear()
            loop = asyncio.new_event_loop()
            try:
                with mock.patch.object(patchwork.asyncio, 'sleep', fake_sleep):
                    with self.assertRaises(aiohttp.ClientConnectorError):
                        loop.run_until_complete(_go(url))
            finally:
                loop.close()
            self.assertEqual(expect, sleeps)

    def test_patchwork_timeout_setting(self):
        """The patchwork_timeout setting sets the request timeout"""
        args = cmdline.parse_args(['--patchwork-timeout', '7', 'upstream',
                                   'ls'], config_fname=False)
        with mock.patch.object(patchwork, 'REQUEST_TIMEOUT', 30), \
                mock.patch.object(control, 'upstream'):
            control.do_patman(args)
            self.assertEqual(7, patchwork.REQUEST_TIMEOUT)

    @staticmethod
    def _no_sleep(real_sleep):
        """Make retry delays short, without affecting other sleeps"""
        async def fake(secs, *args):
            await real_sleep(min(secs, 0.01) if secs >= 1 else secs, *args)
        return fake

    def test_upstreams(self):
        """Upstreams with a patchwork server are listed"""
        with terminal.capture():
            self.cser.upstream_add('us', 'https://us',
                                   patchwork_url='https://pw.us')
            self.cser.upstream_add('ci', 'https://ci')
            self.cser.upstream_set_default('us')
        _, data = self.call(self.make_server(), 'GET', '/api/upstreams')
        self.assertEqual([{'name': 'us', 'default': True,
                           'patchwork_url': 'https://pw.us'}], data)

    def test_periodic_sync(self):
        """The periodic sync starts with the server and runs repeatedly"""
        srv = self.make_server(sync_interval=1)
        srv.sync_cmds_for_testing = [['true']]
        sleeps = []

        async def fake_sleep(secs):
            sleeps.append(secs)
            if len(sleeps) == 2:
                raise asyncio.CancelledError()

        srv._sleep = fake_sleep

        async def _go():
            async with TestClient(TestServer(srv.make_app())):
                try:
                    await srv._sync_task
                except asyncio.CancelledError:
                    pass

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_go())
        finally:
            loop.close()
        self.assertEqual([60, 60], sleeps)
        self.assertEqual(0, srv.sync_state['results'][0]['returncode'])

    def test_is_loopback(self):
        """Loopback addresses are detected"""
        self.assertTrue(server.is_loopback('127.0.0.1'))
        self.assertTrue(server.is_loopback('::1'))
        self.assertTrue(server.is_loopback('localhost'))
        self.assertFalse(server.is_loopback('0.0.0.0'))
        self.assertFalse(server.is_loopback('100.64.1.2'))
        self.assertFalse(server.is_loopback('myhost'))

    def test_serve_cmdline(self):
        """The serve command passes its options through to the server"""
        # Use a separate database, since setUp() already has one open
        dbdir = os.path.join(self.tmpdir, 'serve')
        os.mkdir(dbdir)
        args = cmdline.parse_args(
            ['serve', '--host', '0.0.0.0', '--port', '1234', '-s', '-r',
             '-t', 'tok'], config_fname=False)
        with mock.patch.object(server.web, 'run_app') as run_app, \
                terminal.capture() as (out, err):
            control.do_serve(args, dbdir)
        run_app.assert_called_once()
        self.assertEqual(('0.0.0.0', 1234),
                         (run_app.call_args.kwargs['host'],
                          run_app.call_args.kwargs['port']))
        self.assertIn('Serving on http://0.0.0.0:1234/?token=tok',
                      out.getvalue() + err.getvalue())
        self.assertIn('Syncing (gather-all and review --sync) every 60 '
                      'minutes', out.getvalue() + err.getvalue())

        args = cmdline.parse_args(['serve', '--host', '0.0.0.0'],
                                  config_fname=False)
        with mock.patch.object(server.web, 'run_app'), \
                terminal.capture() as (out, err):
            control.do_serve(args, dbdir)
        self.assertIn('no --token', out.getvalue() + err.getvalue())


if __name__ == '__main__':
    unittest.main()
