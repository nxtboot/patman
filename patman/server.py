# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2026 Simon Glass <sjg@chromium.org>
#
"""Serve series status and reviews over HTTP for viewing on a phone

'patman serve' runs a small web server in the git tree. It provides a JSON
API over the patman database plus a mobile-friendly page which uses it.
Comments on patches and cover letters are not stored in the database, so
these are read live from patchwork when asked for.

The server can optionally keep the database up to date by running
'patman series gather-all -G' (and optionally 'patman review --sync')
periodically in a child process. The -G flag matters: without it,
gather-all writes review tags into the local branches, which must never
happen behind the user's back.

The JSON API is described in doc/serve-protocol.rst

Jobs (syncs and reviews) run one at a time, in the order they are started.
"""

import asyncio
from collections import Counter, OrderedDict
from datetime import datetime
import hmac
import ipaddress
import json
import os
import re
import signal
import subprocess
import sys

import aiohttp
from aiohttp import web

from u_boot_pylib import tout

from patman import database
from patman import review as review_mod
from patman.patchwork import Patchwork
from patman.workflow import Wtype

# Directory holding the web page and its assets
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'web')

# Maximum amount of output to keep from each command run by a job
MAX_JOB_OUTPUT = 20000

# Maximum number of jobs to remember
MAX_JOBS = 20

# File, next to the database, holding the jobs which have not finished, so
# that they can be resumed when the server restarts
JOBS_FNAME = '.patman-serve-jobs.json'

# Maximum number of times to resume a job, in case it is the job which is
# causing the server to stop
MAX_RESUMES = 3

# Number of series in each page of the inbox
INBOX_PAGE = 50

def _state_counts(pcommits):
    """Count the patchwork states of a list of patches

    Args:
        pcommits (list of Pcommit): Patches to count

    Return:
        dict: key (str): state, or 'unknown' if not set
            value (int): number of patches in that state
    """
    return dict(Counter(pcm.state or 'unknown' for pcm in pcommits))


class BadRequest(ValueError):
    """A request is not valid, e.g. a missing field or a bad value"""


class JobBusyError(Exception):
    """A job is already acting on a review which a request would change"""


def _patch_info(pcm):
    """Convert a pcommit record to a JSON-friendly dict

    Args:
        pcm (Pcommit): Record to convert

    Return:
        dict: Patch information
    """
    return {
        'seq': pcm.seq + 1,
        'subject': pcm.subject,
        'state': pcm.state,
        'patch_id': pcm.patch_id,
        'num_comments': pcm.num_comments or 0,
    }


def _version_info(svr):
    """Convert a ser_ver record to a JSON-friendly dict

    Args:
        svr (SerVer): Record to convert

    Return:
        dict: Version information, without the patches
    """
    return {
        'svid': svr.idnum,
        'version': svr.version,
        'link': svr.link,
        'desc': svr.desc,
        'cover_id': svr.cover_id,
        'cover_name': svr.name,
        'cover_num_comments': svr.cover_num_comments or 0,
        'archive_tag': svr.archive_tag,
    }


def _review_info(rev):
    """Convert a review record to a JSON-friendly dict

    Args:
        rev (Review): Record to convert

    Return:
        dict: Review information
    """
    return {
        'id': rev.idnum,
        'seq': rev.seq,
        'body': rev.body,
        'approved': bool(rev.approved),
        'timestamp': rev.timestamp,
        'status': rev.status,
    }


def _mail_info(data):
    """Pick out the useful fields from a patchwork patch or cover letter

    Args:
        data (dict): Patch or cover letter as returned by the patchwork API

    Return:
        dict: Information about the patch or cover letter
    """
    submitter = data.get('submitter') or {}
    return {
        'id': data.get('id'),
        'name': data.get('name'),
        'date': data.get('date'),
        'submitter': submitter.get('name') or submitter.get('email'),
        'state': data.get('state'),
        'content': data.get('content') or '',
        'diff': data.get('diff') or '',
        'web_url': data.get('web_url'),
        'series': [{'id': ser.get('id'), 'name': ser.get('name'),
                    'version': ser.get('version')}
                   for ser in data.get('series') or []],
    }


# A blob hash, as in the 'index' line of a diff
RE_BLOB = re.compile(r'^[0-9a-f]{7,40}$')

# Largest file which can be read from the git repo
MAX_BLOB = 4 << 20


async def read_blob(topdir, hashes):
    """Read a file from the git repo

    A diff names the blobs of each file before and after the change, but
    the repo may only hold one of them, so this tries each in turn.

    Args:
        topdir (str): Top directory of the git repo
        hashes (list of str): Blob hashes, possibly abbreviated, to try in
            order

    Return:
        tuple:
            str: Hash of the blob found, as given
            str: Contents of the file

    Raises:
        ValueError: A hash is invalid, none of the blobs is in the repo or
            the file is too large or not text
    """
    for oid in hashes:
        if not RE_BLOB.match(oid):
            raise BadRequest(f"Invalid blob hash '{oid}'")
        proc = await asyncio.create_subprocess_exec(
            'git', 'cat-file', 'blob', oid, cwd=topdir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL)
        data, _ = await proc.communicate()
        if proc.returncode:
            continue
        if len(data) > MAX_BLOB:
            raise ValueError(f'File is too large ({len(data)} bytes)')
        if b'\0' in data:
            raise ValueError('File is not text')
        return oid, data.decode('utf-8', errors='replace')
    raise ValueError('The file is not in the git repo')


def _reply_info(reply):
    """Convert a reply record to a JSON-friendly dict

    Args:
        reply (Reply): Record to convert

    Return:
        dict: Reply information
    """
    return {
        'id': reply.idnum,
        'sender': reply.sender,
        'subject': reply.subject,
        'date': reply.date,
        'body': reply.body,
        'response': reply.response,
        'status': reply.status,
        'timestamp': reply.timestamp,
    }


def _comment_info(comment):
    """Pick out the useful fields from a patchwork comment

    Args:
        comment (dict): Comment as returned by the patchwork API

    Return:
        dict: Comment information
    """
    submitter = comment.get('submitter') or {}
    return {
        'date': comment.get('date'),
        'name': submitter.get('name'),
        'email': submitter.get('email'),
        'subject': comment.get('subject'),
        'content': comment.get('content'),
        'web_url': comment.get('web_url'),
    }


def _latest_versions(cser):
    """Get the ser_ver record for the latest version of each series

    Args:
        cser (Cseries): Open cseries instance

    Return:
        dict: key (int): series ID
            value (SerVer): latest version of that series
    """
    svdict = cser.get_ser_ver_dict()
    return {series_id: svdict[svid]
            for svid, series_id, _ in cser.db.series_get_all_max_versions()}


def _last_sent(cser):
    """Get the time that each series was last sent

    Args:
        cser (Cseries): Open cseries instance

    Return:
        dict: key (int): series ID
            value (str): timestamp, 'YYYY-MM-DD HH:MM:SS'
    """
    # Entries come back in time order, so later ones win
    return {series_id: timestamp for series_id, _, _, timestamp in
            cser.db.workflow_get_by_type(Wtype.SENT)}


def get_series_list(cser):
    """Get a summary of the user's own (non-archived) series

    Args:
        cser (Cseries): Open cseries instance

    Return:
        list of dict: One item for each series, sorted by name
    """
    latest = _latest_versions(cser)
    sent = _last_sent(cser)
    out = []
    for ser in cser.db.series_get_dict().values():
        svr = latest.get(ser.idnum)
        pcommits = cser.db.pcommit_get_list(svr.idnum) if svr else []
        out.append({
            'id': ser.idnum,
            'name': ser.name,
            'desc': ser.desc,
            'upstream': ser.upstream,
            'version': svr.version if svr else None,
            'link': svr.link if svr else None,
            'count': len(pcommits),
            'states': _state_counts(pcommits),
            'num_comments': sum(pcm.num_comments or 0 for pcm in pcommits),
            'cover_num_comments': (svr.cover_num_comments or 0) if svr else 0,
            'sent': sent.get(ser.idnum),
        })
    return sorted(out, key=lambda item: item['name'])


def get_series_detail(cser, idnum):
    """Get all versions of a series, with their patches

    Args:
        cser (Cseries): Open cseries instance
        idnum (int): Series ID

    Return:
        dict: Series information, with 'versions' newest first

    Raises:
        ValueError: The series does not exist
    """
    name, desc, ups = cser.db.series_get_info(idnum)
    versions = []
    for svr in cser.db.ser_ver_get_for_series(idnum):
        info = _version_info(svr)
        pcommits = cser.db.pcommit_get_list(svr.idnum)
        info['patches'] = [_patch_info(pcm)
                           for pcm in sorted(pcommits, key=lambda p: p.seq)]
        info['states'] = _state_counts(pcommits)
        versions.append(info)
    versions.sort(key=lambda item: item['version'], reverse=True)
    return {'id': idnum, 'name': name, 'desc': desc, 'upstream': ups,
            'versions': versions}


def get_review_list(cser):
    """Get a summary of the series reviewed with 'patman review'

    Args:
        cser (Cseries): Open cseries instance

    Return:
        list of dict: One item for each series, most recent review first
    """
    latest = _latest_versions(cser)
    waiting = cser.db.reply_count_waiting()
    out = []
    for ser in cser.db.series_get_dict(reviews_only=True).values():
        svr = latest.get(ser.idnum)
        reviews = cser.db.review_get_for_version(svr.idnum) if svr else []
        out.append({
            'id': ser.idnum,
            'name': ser.name,
            'desc': ser.desc,
            'upstream': ser.upstream,
            'version': svr.version if svr else None,
            'link': svr.link if svr else None,
            'num_reviews': len(reviews),
            'approved': sum(1 for rev in reviews if rev.approved),
            'statuses': dict(Counter(rev.status or 'new' for rev in reviews)),
            'timestamp': max((rev.timestamp for rev in reviews
                              if rev.timestamp), default=None),
            'replies_waiting': waiting.get(ser.idnum, 0),
        })
    return sorted(out, key=lambda item: item['timestamp'] or '',
                  reverse=True)


def get_review_detail(cser, idnum):
    """Get the reviews for each version of a reviewed series

    Args:
        cser (Cseries): Open cseries instance
        idnum (int): Series ID

    Return:
        dict: Series information, with 'versions' newest first

    Raises:
        ValueError: The series does not exist
    """
    name, desc, ups = cser.db.series_get_info(idnum)
    versions = []
    for svr in cser.db.ser_ver_get_for_series(idnum):
        info = _version_info(svr)
        info['notes'] = svr.notes
        info['reviews'] = []
        for rev in cser.db.review_get_for_version(svr.idnum):
            item = _review_info(rev)
            item['replies'] = [_reply_info(reply) for reply in
                               cser.db.reply_get_for_review(rev.idnum)]
            info['reviews'].append(item)
        info['patches'] = [_patch_info(pcm) for pcm in sorted(
            cser.db.pcommit_get_list(svr.idnum), key=lambda pcm: pcm.seq)]
        versions.append(info)
    versions.sort(key=lambda item: item['version'], reverse=True)
    return {'id': idnum, 'name': name, 'desc': desc, 'upstream': ups,
            'versions': versions}


def _norm_title(title):
    """Normalise a series title so that versions of a series can be matched

    Args:
        title (str): Title, e.g. '[v2] video: Add a driver'

    Return:
        str: Normalised title, e.g. 'video: add a driver'
    """
    return re.sub(r'^(\[[^]]*\]\s*)+', '', title or '').strip().lower()


def get_inbox(cser, rcser, series_list):
    """Get information about recent series on patchwork, for review

    Args:
        cser (Cseries): Open cseries instance with your own series
        rcser (Cseries): Open cseries instance with the reviews
        series_list (list of dict): Series from patchwork, newest first

    Return:
        list of dict: One item for each series
    """
    reviews = rcser.db.series_get_dict(include_archived=True,
                                       reviews_only=True).values()
    review_ids = {ser.idnum for ser in reviews}
    by_title = {}
    for ser in reviews:
        by_title.setdefault(_norm_title(ser.desc), ser.idnum)
    out = []
    for data in series_list:
        link = str(data['id'])
        found = rcser.db.series_find_by_link(link)
        reviewed = found[0] if found and found[0] in review_ids else None
        own = cser.db.series_find_by_link(link)
        cover = data.get('cover_letter')
        submitter = data.get('submitter') or {}
        out.append({
            'id': data['id'],
            'name': data.get('name') or '',
            'version': data.get('version'),
            'date': data.get('date'),
            'submitter': submitter.get('name') or submitter.get('email'),
            'total': data.get('total'),
            'received_all': data.get('received_all'),
            'web_url': data.get('web_url'),
            'cover_id': cover['id'] if cover else None,
            'patches': [{'id': pat['id'], 'name': pat.get('name')}
                        for pat in data.get('patches') or []],
            'reviewed': reviewed,
            'earlier': (None if reviewed else
                        by_title.get(_norm_title(data.get('name')))),
            'mine': bool(own) and (rcser is not cser or
                                   own[0] not in review_ids),
        })
    return out


def get_todo_list(cser):
    """Get the scheduled todos, whether due or not

    Args:
        cser (Cseries): Open cseries instance

    Return:
        list of dict: One item per todo, earliest first
    """
    now = cser.get_now()
    out = []
    for series_id, name, desc, timestamp in cser.db.workflow_get_by_type(
            Wtype.TODO):
        when = datetime.strptime(timestamp, '%Y-%m-%d %H:%M:%S')
        out.append({'series_id': series_id, 'name': name, 'desc': desc,
                    'timestamp': timestamp, 'due': when <= now})
    return out


# Patch selection for 'review -i', e.g. '3' or '1,3-5'
RE_INDEX = re.compile(r'^\d+(-\d+)?(,\d+(-\d+)?)*$')

# Tag given in a review which approves the patch
RE_REVIEWED_BY = re.compile(r'^Reviewed-by:', re.MULTILINE)

# Patchwork series or patch in a URL, e.g. '.../series/123/', '?series=123'
RE_SERIES_URL = re.compile(r'series[=/](\d+)')
RE_PATCH_URL = re.compile(r'/patch/(\d+)')


def _get_int(data, key):
    """Get an optional positive integer from a request

    Args:
        data (dict): Request data
        key (str): Key to read

    Return:
        int or None: Value, or None if missing

    Raises:
        ValueError: The value is not a positive integer
    """
    val = data.get(key)
    if val in (None, ''):
        return None
    if isinstance(val, bool) or not str(val).isdigit() or not int(val):
        raise BadRequest(f"Invalid {key} '{val}'")
    return int(val)


def _parts_text(seqs):
    """Describe which parts of a review a job acts on

    Args:
        seqs (list of int): Patches (0 for the cover letter), or empty for all

    Return:
        str: Description, e.g. 'patch 2', 'the cover letter' or 'reviews'
    """
    if len(seqs) != 1:
        return 'reviews'
    return f'patch {seqs[0]}' if seqs[0] else 'the cover letter'


def parse_review_req(data, upstreams):
    """Work out the 'patman review' arguments for a review request

    The request is a dict with these keys, all optional except that one of
    'target' and 'title' is needed:
        target (str): Patchwork series ID, or the URL of a series or patch
        title (str): Search for a series by its cover-letter title
        version (int): Version to review, with 'title'
        patch (int): Patchwork patch ID, to review just that patch
        whole (bool): With a patch, review its whole series
        index (str): Review only these patches, e.g. '1,3-5'
        ups (str): Upstream whose patchwork server to use
        drafts (bool): Create Gmail drafts for the reviews
        force (bool): Review again, even if already reviewed

    Args:
        data (dict): Request
        upstreams (list of str): Names of the known upstreams

    Return:
        tuple:
            list of str: Arguments for 'patman review'
            str: Description of the review, for display

    Raises:
        ValueError: The request is not valid
    """
    target = str(data.get('target') or '').strip()
    title = str(data.get('title') or '').strip()
    patch = _get_int(data, 'patch')
    args = []
    if target:
        mpatch = RE_PATCH_URL.search(target)
        mseries = RE_SERIES_URL.search(target)
        if target.isdigit():
            args += ['-s', target]
            desc = f'series {target}'
        elif mpatch:
            patch = int(mpatch.group(1))
        elif mseries:
            args += ['-s', mseries.group(1)]
            desc = f'series {mseries.group(1)}'
        else:
            raise BadRequest(f"Cannot find a series or patch in '{target}'")
    if patch:
        args += ['-p', str(patch)]
        desc = f'patch {patch}'
        if data.get('whole'):
            args.append('-w')
            desc = f'series of patch {patch}'
    if title:
        args += ['-S', title]
        desc = f"'{title}'"
        version = _get_int(data, 'version')
        if version:
            args += ['-V', str(version)]
    if not args:
        raise BadRequest('Please give a series, patch or title to review')
    if len([1 for opt in ('-s', '-p', '-S') if opt in args]) != 1:
        raise BadRequest('Please give only one of a series, patch or title')
    index = str(data.get('index') or '').replace(' ', '')
    if index:
        if not RE_INDEX.match(index):
            raise BadRequest(f"Invalid patch selection '{index}'")
        args += ['-i', index]
    ups = data.get('ups')
    if ups:
        if ups not in upstreams:
            raise BadRequest(f"Unknown upstream '{ups}'")
        args += ['-U', ups]
    if data.get('drafts'):
        args.append('-d')
    if data.get('force'):
        args.append('-f')
    return args, f'Review {desc}'


class Job:
    """A background task which runs patman commands one after the other

    Properties:
        idnum (int): Job number, starting at 1
        kind (str): Kind of job, e.g. 'sync' or 'review'
        cmds (list of list of str): Commands to run
        cancel (bool): True if the job has been asked to stop
        proc (asyncio.subprocess.Process or None): Command which is running
        state (dict): State of the job, as provided by the API
    """
    # pylint: disable=R0913,R0917
    def __init__(self, idnum, kind, title, cmds, resumed=0, target=None):
        """Set up a new job

        Args:
            idnum (int): Job number
            kind (str): Kind of job
            title (str): Description of the job, for display
            cmds (list of list of str): Commands to run
            resumed (int): Number of times the job has been resumed after
                the server stopped while it was waiting or running
            target (dict or None): Reviews which the job acts on, with the
                review series ID ('series'), its version ('version') and the
                patches ('seqs', empty for all unsent reviews), or None
        """
        self.idnum = idnum
        self.kind = kind
        self.cmds = cmds
        self.cancel = False
        self.proc = None
        self.state = {
            'id': idnum,
            'kind': kind,
            'title': title,
            'queued': True,
            'running': False,
            'cancelled': False,
            'started': None,
            'finished': None,
            'results': [],
            'resumed': resumed,
            'target': target,
        }

    @property
    def active(self):
        """True if the job is waiting to run, or running"""
        return not self.state['finished']

    @property
    def interrupted(self):
        """True if a command was stopped part-way, other than by cancelling

        This happens when the server stops while the job is running, since
        the command is stopped too
        """
        return not self.cancel and any(
            res['returncode'] is None or res['returncode'] < 0
            for res in self.state['results'])

    @property
    def unfinished(self):
        """True if the job should be run again when the server restarts"""
        return self.active or self.interrupted


# pylint: disable=R0902
class Server:
    """Web server providing patman information as JSON

    Properties:
        cser (Cseries): Open cseries instance
        rcser (Cseries): Open cseries instance holding the reviews, which is
            cser unless reviews are kept in a separate database
        pw_url (str or None): Patchwork URL to use when a series has no
            upstream with its own URL
        sync_interval (int): Minutes between syncs, 0 to disable
        review_sync (bool): True to run 'review --sync' as part of a sync
        token (str or None): Token which clients must provide, or None
        pwork_for_testing (Patchwork or None): Patchwork object to use for
            all upstreams, for testing
        sync_cmds_for_testing (list of list of str or None): Commands to
            run for a sync, for testing
        jobs_fname (str or None): File holding the jobs which have not
            finished, or None to not keep them
    """
    # pylint: disable=R0913,R0917
    def __init__(self, cser, pw_url=None, sync_interval=0, review_sync=False,
                 token=None, rcser=None, jobs_fname=None):
        self.cser = cser
        self.rcser = rcser or cser
        self.pw_url = pw_url
        self.sync_interval = sync_interval
        self.review_sync = review_sync
        self.token = token
        self.jobs_fname = jobs_fname
        self.pwork_for_testing = None
        self.sync_cmds_for_testing = None
        self._pworks = {}
        self._sleep = asyncio.sleep
        self._sync_task = None

        # Reviewed versions which patchwork says have no cover letter
        self._no_cover = set()

        # Jobs run one at a time, in the order they are added, since they
        # may all write to the database
        self._job_lock = asyncio.Lock()
        self._jobs = OrderedDict()
        self._last_job = 0

    def get_pw_url(self, ups):
        """Get the patchwork URL for an upstream

        Args:
            ups (str or None): Upstream name

        Return:
            str or None: URL, or None if not known
        """
        if self.pwork_for_testing:
            return self.pwork_for_testing.url
        return (ups and self.cser.db.upstream_get_patchwork_url(ups)) or \
            self.pw_url

    def get_pwork(self, ups):
        """Get a Patchwork object for an upstream

        Args:
            ups (str or None): Upstream name

        Return:
            Patchwork: Object to use for that upstream's patchwork server

        Raises:
            ValueError: No patchwork URL is known
        """
        if self.pwork_for_testing:
            return self.pwork_for_testing
        url = self.get_pw_url(ups)
        if not url:
            raise ValueError(f"No patchwork URL for upstream '{ups}'")
        if url not in self._pworks:
            self._pworks[url] = Patchwork(url, show_progress=False)
        return self._pworks[url]

    def _review_db_args(self):
        """Get the arguments which select the review database

        Return:
            list of str: Arguments for a 'patman review' command
        """
        if self.rcser is self.cser or not self.rcser.db_dir:
            return []
        return ['--review-db-dir', self.rcser.db_dir]

    def get_sync_cmds(self):
        """Get the commands to run for a sync

        Return:
            list of list of str: Commands, each a list of arguments
        """
        if self.sync_cmds_for_testing is not None:
            return self.sync_cmds_for_testing
        fetch = self.get_fetch_cmd()
        cmds = [fetch] if fetch else []
        cmds.append(self.get_patman_cmd('series', 'gather-all', '-G'))
        if self.review_sync:
            cmds.append(self.get_patman_cmd('review', '--sync',
                                            *self._review_db_args()))
        return cmds

    def get_fetch_cmd(self):
        """Get a command which fetches the upstreams which reviews use

        Reviews apply patches to the upstream's branches, so these must be
        kept up to date. The default upstream and the upstreams of reviewed
        series are fetched, where they are git remotes.

        Return:
            list of str or None: Command, or None if there is nothing to
                fetch
        """
        wanted = {ser.upstream for ser in
                  self.rcser.db.series_get_dict(reviews_only=True).values()
                  if ser.upstream}
        default = self.cser.db.upstream_get_default()
        if default:
            wanted.add(default)
        res = subprocess.run(['git', 'remote'], cwd=self.cser.topdir,
                             capture_output=True, text=True, check=False)
        remotes = sorted(wanted & set(res.stdout.split()))
        if not remotes:
            return None
        return ['git', 'fetch', '-q', '--multiple', *remotes]

    def get_patman_cmd(self, *args):
        """Get a command which runs patman in a child process

        Args:
            args (list of str): Arguments for patman

        Return:
            list of str: Command to run
        """
        return [sys.executable, '-m', 'patman', '-v', *args]

    def add_job(self, kind, title, cmds, target=None):
        """Add a job and start it once the jobs before it have finished

        Args:
            kind (str): Kind of job
            title (str): Description of the job, for display
            cmds (list of list of str): Commands to run
            target (dict or None): Reviews which the job acts on (see Job)

        Return:
            Job: New job
        """
        job = self._new_job(kind, title, cmds, target=target)
        asyncio.create_task(self.run_job(job))
        return job

    # pylint: disable=R0913,R0917
    def _new_job(self, kind, title, cmds, resumed=0, target=None):
        """Create a job and remember it, dropping old jobs if needed"""
        self._last_job += 1
        job = Job(self._last_job, kind, title, cmds, resumed, target)
        self._jobs[job.idnum] = job
        for old in list(self._jobs.values()):
            if len(self._jobs) <= MAX_JOBS:
                break
            if not old.active:
                del self._jobs[old.idnum]
        self.save_jobs()
        return job

    def save_jobs(self):
        """Write out the jobs which should run again if the server restarts

        Syncs are not included, since a sync runs when the server starts
        anyway. The file is replaced in one step, so that it is complete
        even if the server stops while writing it.
        """
        if not self.jobs_fname:
            return
        jobs = [{'kind': job.kind, 'title': job.state['title'],
                 'cmds': job.cmds, 'resumed': job.state['resumed'],
                 'target': job.state['target']}
                for job in self._jobs.values()
                if job.kind != 'sync' and job.unfinished]
        tmp = f'{self.jobs_fname}.tmp'
        with open(tmp, 'w', encoding='utf-8') as outf:
            json.dump(jobs, outf, indent=1)
        os.replace(tmp, self.jobs_fname)

    def resume_jobs(self):
        """Start the jobs which had not finished when the server stopped

        Return:
            list of Job: Jobs which were started
        """
        if not self.jobs_fname or not os.path.exists(self.jobs_fname):
            return []
        try:
            with open(self.jobs_fname, encoding='utf-8') as inf:
                saved = json.load(inf)
        except (OSError, ValueError) as exc:
            tout.warning(f"Cannot read jobs from '{self.jobs_fname}': {exc}")
            return []
        jobs = []
        for item in saved:
            title = item['title']
            resumed = item.get('resumed', 0) + 1
            if resumed > MAX_RESUMES:
                tout.warning(f"Not resuming job '{title}' again: it has "
                             f'been resumed {MAX_RESUMES} times')
                continue
            tout.notice(f"Resuming job '{title}'")
            job = self._new_job(item['kind'], title, item['cmds'], resumed,
                                item.get('target'))
            asyncio.create_task(self.run_job(job))
            jobs.append(job)
        self.save_jobs()
        return jobs

    def get_job(self, idnum):
        """Get a job by its number

        Args:
            idnum (int): Job number

        Return:
            Job: Job

        Raises:
            ValueError: The job does not exist
        """
        job = self._jobs.get(idnum)
        if not job:
            raise ValueError(f'No job {idnum}')
        return job

    def get_jobs(self):
        """Get the jobs, newest first

        Return:
            list of Job: Jobs
        """
        return list(reversed(self._jobs.values()))

    async def run_job(self, job):
        """Run a job's commands one after the other, once it is its turn

        Output from each command is recorded in the job's state as it
        arrives. If the job is cancelled, the command which is running is
        stopped and the rest are not run.

        Args:
            job (Job): Job to run
        """
        state = job.state
        async with self._job_lock:
            state['queued'] = False
            state['running'] = True
            state['started'] = _now_str()
            try:
                for cmd in job.cmds:
                    if job.cancel:
                        break
                    result = {
                        'cmd': self._cmd_label(cmd),
                        'returncode': None,
                        'output': '',
                    }
                    state['results'].append(result)
                    await self._run_cmd(job, cmd, result)
            finally:
                state['running'] = False
                state['cancelled'] = job.cancel
                state['finished'] = _now_str()
                self.save_jobs()

    @staticmethod
    def _cmd_label(cmd):
        """Get a short description of a command, for display

        Args:
            cmd (list of str): Command

        Return:
            str: Description, e.g. 'patman series gather-all -G'
        """
        patman = [sys.executable, '-m', 'patman']
        if cmd[:3] == patman:
            cmd = ['patman'] + [arg for arg in cmd[3:4] if arg != '-v'] + \
                cmd[4:]
        return ' '.join(cmd)

    def cancel_job(self, job):
        """Stop a job, whether it is running or waiting to run

        Args:
            job (Job): Job to stop

        Return:
            bool: True if the job was stopped, False if it had finished
        """
        if not job.active:
            return False
        job.cancel = True
        if job.proc and job.proc.returncode is None:
            # Stop any child processes too, since they hold the output open
            tout.info(f'Job {job.idnum}: cancelling')
            try:
                os.killpg(job.proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        return True

    def _sync_job(self):
        """Get the most recent sync job

        Return:
            Job or None: Job, or None if there has not been a sync
        """
        for job in reversed(self._jobs.values()):
            if job.kind == 'sync':
                return job
        return None

    @property
    def sync_state(self):
        """dict: State of the most recent sync, with the sync settings"""
        job = self._sync_job()
        state = dict(job.state) if job else {
            'queued': False, 'running': False, 'cancelled': False,
            'started': None, 'finished': None, 'results': []}
        state['interval'] = self.sync_interval
        state['review_sync'] = self.review_sync
        return state

    def _start_sync(self):
        """Create a sync job, unless one is already waiting or running

        Return:
            Job or None: New job, or None if a sync is already active
        """
        job = self._sync_job()
        if job and job.active:
            return None
        return self._new_job('sync', 'Sync', self.get_sync_cmds())

    async def run_sync(self):
        """Run a sync now, waiting for any other job to finish first

        Return:
            bool: True if a sync was run, False if one was already active
        """
        job = self._start_sync()
        if not job:
            return False
        await self.run_job(job)
        return True

    def cancel_sync(self):
        """Stop the sync which is running or waiting, if any

        Return:
            bool: True if a sync was stopped, False if none was active
        """
        job = self._sync_job()
        return bool(job) and self.cancel_job(job)

    async def _run_cmd(self, job, cmd, result):
        """Run a single command in the git tree

        The output is added to the result as each line arrives, so that a
        client can show the progress of the job.

        Args:
            job (Job): Job which is running the command
            cmd (list of str): Command to run
            result (dict): Information about the command, updated with its
                output and return code
        """
        tout.info(f"Job {job.idnum}: running {result['cmd']}")
        env = review_mod.scan.child_env()
        env['PYTHONUNBUFFERED'] = '1'
        # Fail rather than wait for a password which nobody can type
        env['GIT_TERMINAL_PROMPT'] = '0'
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=self.cser.topdir, env=env, start_new_session=True)
        except OSError as exc:
            result['returncode'] = -1
            result['output'] = str(exc)
            return
        job.proc = proc
        try:
            async for line in proc.stdout:
                output = result['output'] + line.decode('utf-8',
                                                        errors='replace')
                result['output'] = output[-MAX_JOB_OUTPUT:]
            result['returncode'] = await proc.wait()
        finally:
            job.proc = None
        if result['returncode']:
            tout.warning(f"Job {job.idnum}: '{result['cmd']}' failed "
                         f"(code {result['returncode']})")

    async def _sync_loop(self):
        """Run a sync every sync_interval minutes, starting straight away"""
        while True:
            await self.run_sync()
            await self._sleep(self.sync_interval * 60)

    async def _start_background(self, _app):
        """Resume any unfinished jobs, then start the periodic sync"""
        self.resume_jobs()
        if self.sync_interval:
            self._sync_task = asyncio.create_task(self._sync_loop())

    async def _stop_background(self, _app):
        """Stop the periodic sync, if running"""
        if self._sync_task:
            self._sync_task.cancel()
            try:
                await self._sync_task
            except asyncio.CancelledError:
                pass

    @web.middleware
    async def _auth(self, request, handler):
        """Check that the client provided the token, if one is needed"""
        if self.token and request.path.startswith('/api/'):
            given = request.headers.get('Authorization', '')
            if given.startswith('Bearer '):
                given = given[7:]
            else:
                given = request.query.get('token', '')
            if not hmac.compare_digest(given.encode(), self.token.encode()):
                return web.json_response({'error': 'Unauthorised'},
                                         status=401)
        return await handler(request)

    @web.middleware
    async def _no_cache(self, request, handler):
        """Have the browser check the page and its files before reusing them

        The page is split into several files, so a browser which kept an old
        app.js after patman is updated would run it with the new page
        """
        resp = await handler(request)
        if request.path == '/' or request.path.startswith('/static/'):
            resp.headers['Cache-Control'] = 'no-cache'
        return resp

    @web.middleware
    async def _errors(self, request, handler):
        """Turn errors into a JSON response"""
        try:
            return await handler(request)
        except JobBusyError as exc:
            return web.json_response({'error': str(exc)}, status=409)
        except BadRequest as exc:
            return web.json_response({'error': str(exc)}, status=400)
        except ValueError as exc:
            return web.json_response({'error': str(exc)}, status=404)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            return web.json_response(
                {'error': f'Patchwork: {type(exc).__name__}: {exc}'},
                status=502)

    async def _series_list(self, _request):
        return web.json_response(get_series_list(self.cser))

    async def _series_detail(self, request):
        data = get_series_detail(self.cser, int(request.match_info['idnum']))
        data['patchwork_url'] = self.get_pw_url(data['upstream'])
        return web.json_response(data)

    async def _review_list(self, _request):
        return web.json_response(get_review_list(self.rcser))

    async def _find_covers(self, data):
        """Look up the cover letters which reviewed versions lack

        Reviews added before cover-letter IDs were recorded have none, so
        the cover letter which was reviewed cannot be shown. Read each such
        version's series from patchwork and record its cover letter, so
        this only happens once.

        Args:
            data (dict): Review detail, as from get_review_detail(); the
                versions' cover_id and cover_name are filled in
        """
        todo = [ver for ver in data['versions']
                if not ver['cover_id'] and ver['link'] and
                ver['svid'] not in self._no_cover and
                any(not rev['seq'] for rev in ver['reviews'])]
        if not todo:
            return
        try:
            pwork = self.get_pwork(data['upstream'])
        except ValueError:
            return
        async with aiohttp.ClientSession() as client:
            results = await asyncio.gather(
                *[pwork.get_series(client, ver['link']) for ver in todo],
                return_exceptions=True)
        for ver, result in zip(todo, results):
            if isinstance(result, Exception):
                tout.warning(f"Cannot read series {ver['link']}: {result}")
                continue
            cover = (result or {}).get('cover_letter')
            if not cover:
                self._no_cover.add(ver['svid'])
                continue
            ver['cover_id'] = cover['id']
            ver['cover_name'] = cover.get('name')
            self.rcser.db.ser_ver_set_info(database.SerVer(
                ver['svid'], None, None, None, cover['id'], 0,
                cover.get('name'), None, None, None))
        self.rcser.commit()

    async def _review_detail(self, request):
        idnum = int(request.match_info['idnum'])
        data = get_review_detail(self.rcser, idnum)
        # Mark the reviews which a job is acting on, so the page can wait
        for ver in data['versions']:
            pending = self.pending_jobs(idnum, ver['version'])
            for item in ver['reviews']:
                done = item['status'] in ('sent', 'replied')
                item['job'] = pending.get(item['seq'],
                                          None if done else pending.get(None))
        # 'patman review' uses the default upstream when none is given, so
        # a series reviewed that way is on its patchwork server
        data['upstream'] = (data['upstream'] or
                            self.cser.db.upstream_get_default())
        await self._find_covers(data)
        data['patchwork_url'] = self.get_pw_url(data['upstream'])
        proj = self.cser.project_get(data['upstream'])
        data['project'] = proj[2] if proj else None
        return web.json_response(data)

    async def _todo_list(self, _request):
        return web.json_response(get_todo_list(self.cser))

    async def _comments(self, request):
        kind = request.match_info['kind']
        item_id = int(request.match_info['item_id'])
        pwork = self.get_pwork(request.query.get('ups') or None)
        async with aiohttp.ClientSession() as client:
            if kind == 'patch':
                comments = await pwork.get_patch_comments(client, item_id)
            else:
                comments = await pwork.get_cover_comments(client, item_id)
        return web.json_response([_comment_info(c) for c in comments or []])

    async def _mail(self, request):
        item_id = int(request.match_info['item_id'])
        pwork = self.get_pwork(request.query.get('ups') or None)
        async with aiohttp.ClientSession() as client:
            if request.match_info['kind'] == 'patch':
                data = await pwork.get_patch(client, item_id)
            else:
                data = await pwork.get_cover(client, item_id)
        return web.json_response(_mail_info(data))

    async def _blob(self, request):
        oid, text = await read_blob(self.cser.topdir,
                                    request.query.getall('hash', []))
        return web.json_response({'hash': oid, 'text': text})

    async def _sync_get(self, _request):
        return web.json_response(self.sync_state)

    async def _sync_post(self, _request):
        job = self._start_sync()
        if not job:
            return web.json_response(self.sync_state, status=409)
        asyncio.create_task(self.run_job(job))

        # Let the sync start, so the response shows it running
        await asyncio.sleep(0)
        return web.json_response(self.sync_state, status=202)

    async def _sync_cancel_post(self, _request):
        if not self.cancel_sync():
            return web.json_response(self.sync_state, status=409)
        return web.json_response(self.sync_state, status=202)

    async def _upstreams(self, _request):
        out = []
        for name, (_, is_default, pw_url, *_) in \
                self.cser.db.upstream_get_dict().items():
            if pw_url:
                out.append({'name': name, 'default': bool(is_default),
                            'patchwork_url': pw_url})
        return web.json_response(out)

    async def _inbox(self, request):
        ups = request.query.get('ups') or self.cser.db.upstream_get_default()
        page = _get_int(request.query, 'page') or 1
        proj = self.cser.project_get(ups)
        if not proj:
            raise ValueError(f"No patchwork project for upstream '{ups}'")
        pwork = self.get_pwork(ups)
        async with aiohttp.ClientSession() as client:
            series_list = await pwork.get_recent_series(client, proj[2], page,
                                                        INBOX_PAGE)
        return web.json_response({
            'ups': ups, 'page': page, 'per_page': INBOX_PAGE,
            'series': get_inbox(self.cser, self.rcser, series_list)})

    async def _job_list(self, _request):
        return web.json_response([job.state for job in self.get_jobs()])

    async def _job_detail(self, request):
        job = self.get_job(int(request.match_info['idnum']))
        return web.json_response(job.state)

    async def _job_cancel(self, request):
        job = self.get_job(int(request.match_info['idnum']))
        status = 202 if self.cancel_job(job) else 409
        return web.json_response(job.state, status=status)

    async def _read_json(self, request):
        """Read the JSON body of a request, which may be empty

        Return:
            dict: Request data

        Raises:
            ValueError: The body is not a JSON object
        """
        if not request.can_read_body:
            return {}
        try:
            data = await request.json()
        except ValueError as exc:
            raise BadRequest('Invalid JSON in request') from exc
        if not isinstance(data, dict):
            raise BadRequest('Request must be a JSON object')
        return data

    def _review_job(self, title, *args, target=None):
        """Start a job which runs 'patman review'

        Args:
            title (str): Description of the job
            args (list of str): Arguments for 'patman review'
            target (dict or None): Reviews which the job acts on (see Job)

        Return:
            web.Response: Response giving the job state
        """
        cmd = self.get_patman_cmd('review', *args, *self._review_db_args())
        job = self.add_job('review', title, [cmd], target)
        return web.json_response(job.state, status=202)

    def pending_jobs(self, idnum, version):
        """Find the jobs which are waiting or running for a review series

        Args:
            idnum (int): Series ID in the review database
            version (int): Version of the series

        Return:
            dict: Map of patch number to the ID of the job acting on its
                review, with None for a job acting on all unsent reviews
        """
        out = {}
        for job in self._jobs.values():
            target = job.state['target']
            if (job.active and target and target['series'] == idnum and
                    target['version'] == version):
                for seq in target['seqs'] or [None]:
                    out.setdefault(seq, job.idnum)
        return out

    def check_free(self, idnum, version, seqs, unsent):
        """Check that no job is acting on some reviews

        Args:
            idnum (int): Series ID in the review database
            version (int): Version of the series
            seqs (list of int): Patches whose reviews are to be changed, or
                empty for all unsent reviews
            unsent (list of int): Patches whose reviews have not been sent

        Raises:
            ValueError: A job is waiting or running for one of the reviews
        """
        pending = self.pending_jobs(idnum, version)
        busy = None in pending or any(seq in pending
                                      for seq in (seqs or unsent))
        if not busy and not seqs:
            busy = bool(pending)
        if busy:
            raise JobBusyError('A job is already acting on this review; '
                               'wait for it to finish')

    async def _review_post(self, request):
        data = await self._read_json(request)
        args, title = parse_review_req(
            data, list(self.cser.db.upstream_get_dict()))
        return self._review_job(title, *args)

    async def _review_scan(self, request):
        data = await self._read_json(request)
        args = ['--scan'] + (['-d'] if data.get('drafts') else [])
        return self._review_job('Scan for new versions', *args)

    async def _redraft(self, request):
        idnum = int(request.match_info['idnum'])
        data = await self._read_json(request)
        return self._redraft_job(idnum, _get_int(data, 'version'),
                                 data.get('seqs') or [])

    def _series_review_args(self, idnum, version, seqs, *action):
        """Get the 'patman review' arguments to act on a reviewed series

        Args:
            idnum (int): Series ID in the review database
            version (int or None): Version, or None for the latest
            seqs (list of int): Patches to act on (0 for the cover letter),
                or empty for all
            action (list of str): Arguments giving the action, e.g. '--send'

        Return:
            tuple:
                list of str: Arguments for 'patman review'
                str: Series and version, for the job title, e.g. "'foo' v2"
                dict: Reviews which the job acts on, as a job target

        Raises:
            ValueError: The seqs are invalid, or the version has no link
            JobBusyError: A job is already acting on one of the reviews
        """
        if not isinstance(seqs, list) or not all(
                isinstance(seq, int) and seq >= 0 for seq in seqs):
            raise BadRequest('Invalid seqs')
        name, desc, ups = self.rcser.db.series_get_info(idnum)
        version = version or self.rcser.db.series_get_max_version(idnum)
        link = self.rcser.db.ser_ver_get_link(idnum, version)
        if not link:
            raise ValueError(f"Series '{name}' v{version} has no link")
        self.check_free(idnum, version, seqs, self._unsent(idnum, version))
        args = ['-s', str(link), *action]
        if seqs:
            args += ['-i', ','.join(str(seq) for seq in sorted(seqs))]
        if ups:
            args += ['-U', ups]
        target = {'series': idnum, 'version': version, 'seqs': sorted(seqs)}
        return args, f"'{desc or name}' v{version}", target

    def _unsent(self, idnum, version):
        """Get the patches whose reviews have not been sent

        Args:
            idnum (int): Series ID in the review database
            version (int): Version of the series

        Return:
            list of int: Patch numbers, 0 for the cover letter
        """
        svr = self.rcser.get_ser_ver(idnum, version)
        return [rev.seq for rev in self.rcser.db.review_get_for_version(
            svr.idnum) if rev.status not in ('sent', 'replied')]

    def _redraft_job(self, idnum, version, seqs):
        """Start a job which recreates the Gmail drafts for a review

        Args:
            idnum (int): Series ID in the review database
            version (int or None): Version, or None for the latest
            seqs (list of int): Patches to redraft (0 for the cover letter),
                or empty for all

        Return:
            web.Response: Response giving the job state
        """
        args, what, target = self._series_review_args(idnum, version, seqs,
                                                      '--redraft')
        return self._review_job(f'Redraft {what}', *args, target=target)

    async def _send(self, request):
        idnum = int(request.match_info['idnum'])
        data = await self._read_json(request)
        seqs = data.get('seqs') or []
        args, what, target = self._series_review_args(
            idnum, _get_int(data, 'version'), seqs, '--send')
        return self._review_job(f'Send {_parts_text(seqs)} of {what}', *args,
                                target=target)

    async def _revise(self, request):
        idnum = int(request.match_info['idnum'])
        data = await self._read_json(request)
        note = data.get('note')
        if not isinstance(note, str) or not note.strip():
            raise BadRequest('Please say what to change')
        note = note.strip()
        part = data.get('part')
        if part:
            if not isinstance(part, str):
                raise BadRequest('Invalid part')
            note = f'About this part of the review:\n\n{part.strip()}' \
                   f'\n\n{note}'
        seqs = data.get('seqs') or []
        args, what, target = self._series_review_args(
            idnum, _get_int(data, 'version'), seqs, '--revise', note)
        if data.get('drafts'):
            args.append('-d')
        return self._review_job(f'Revise {_parts_text(seqs)} of {what}',
                                *args, target=target)

    async def _review_item(self, request):
        db = self.rcser.db
        rev = db.review_get(int(request.match_info['idnum']))
        data = await self._read_json(request)
        if rev.status in ('sent', 'replied'):
            return web.json_response(
                {'error': 'This review has already been sent'}, status=409)
        body = data.get('body', rev.body)
        if not isinstance(body, str) or not body.strip():
            raise BadRequest('The review cannot be empty')
        svr = db.ser_ver_get_by_id(rev.svid)
        self.check_free(svr.series_id, svr.version, [rev.seq], [rev.seq])
        # Unless told otherwise, the review approves if it gives the tag
        approved = data.get('approved')
        if approved is None:
            approved = bool(RE_REVIEWED_BY.search(body))
        db.review_set_body(rev.idnum, body, approved)
        self.rcser.commit()
        if data.get('redraft'):
            return self._redraft_job(svr.series_id, svr.version, [rev.seq])
        return web.json_response(_review_info(db.review_get(rev.idnum)))

    async def _reply(self, request):
        db = self.rcser.db
        reply = db.reply_get(int(request.match_info['idnum']))
        data = await self._read_json(request)
        if reply.status == database.REPLY_SENT:
            return web.json_response(
                {'error': 'The response has already been sent'}, status=409)
        if 'response' in data:
            response = data['response']
            if not isinstance(response, str):
                raise BadRequest('Invalid response')
            db.reply_set_response(
                reply.idnum, response if response.strip() else None,
                database.REPLY_SUGGESTED if response.strip() else
                database.REPLY_NEW)
        if data.get('skip'):
            db.reply_set_status(reply.idnum, database.REPLY_SKIP,
                                reply.draft_id)
        self.rcser.commit()
        if data.get('draft'):
            return self._review_job(f'Draft response to reply {reply.idnum}',
                                    '--draft-reply', str(reply.idnum))
        return web.json_response(_reply_info(db.reply_get(reply.idnum)))

    async def _reply_suggest(self, request):
        reply = self.rcser.db.reply_get(int(request.match_info['idnum']))
        return self._review_job(f'Suggest a response to reply {reply.idnum}',
                                '--suggest-reply', str(reply.idnum))

    async def _reply_send(self, request):
        reply = self.rcser.db.reply_get(int(request.match_info['idnum']))
        if reply.status == database.REPLY_SENT:
            return web.json_response(
                {'error': 'The response has already been sent'}, status=409)
        if not (reply.response or '').strip():
            raise BadRequest('There is no response to send')
        return self._review_job(f'Send the response to reply {reply.idnum}',
                                '--send-reply', str(reply.idnum))

    async def _index(self, _request):
        return web.FileResponse(os.path.join(WEB_DIR, 'index.html'))

    def make_app(self):
        """Create the aiohttp application

        Return:
            web.Application: Application, ready to run
        """
        app = web.Application(
            middlewares=[self._errors, self._auth, self._no_cache])
        app.router.add_get('/api/series', self._series_list)
        app.router.add_get(r'/api/series/{idnum:\d+}', self._series_detail)
        app.router.add_get('/api/reviews', self._review_list)
        app.router.add_get(r'/api/reviews/{idnum:\d+}', self._review_detail)
        app.router.add_get('/api/todo', self._todo_list)
        app.router.add_get(r'/api/comments/{kind:patch|cover}/{item_id:\d+}',
                           self._comments)
        app.router.add_get(r'/api/{kind:patch|cover}/{item_id:\d+}',
                           self._mail)
        app.router.add_get('/api/blob', self._blob)
        app.router.add_get('/api/sync', self._sync_get)
        app.router.add_post('/api/sync', self._sync_post)
        app.router.add_post('/api/sync/cancel', self._sync_cancel_post)
        app.router.add_get('/api/upstreams', self._upstreams)
        app.router.add_get('/api/inbox', self._inbox)
        app.router.add_get('/api/jobs', self._job_list)
        app.router.add_get(r'/api/jobs/{idnum:\d+}', self._job_detail)
        app.router.add_post(r'/api/jobs/{idnum:\d+}/cancel', self._job_cancel)
        app.router.add_post('/api/review', self._review_post)
        app.router.add_post('/api/review/scan', self._review_scan)
        app.router.add_post(r'/api/reviews/{idnum:\d+}/redraft',
                            self._redraft)
        app.router.add_post(r'/api/reviews/{idnum:\d+}/revise',
                            self._revise)
        app.router.add_post(r'/api/reviews/{idnum:\d+}/send', self._send)
        app.router.add_post(r'/api/review-items/{idnum:\d+}',
                            self._review_item)
        app.router.add_post(r'/api/replies/{idnum:\d+}', self._reply)
        app.router.add_post(r'/api/replies/{idnum:\d+}/send',
                            self._reply_send)
        app.router.add_post(r'/api/replies/{idnum:\d+}/suggest',
                            self._reply_suggest)
        app.router.add_get('/', self._index)
        app.router.add_static('/static', WEB_DIR)
        app.on_startup.append(self._start_background)
        app.on_cleanup.append(self._stop_background)
        return app


def _now_str():
    """Get the current time as a string

    Return:
        str: Time in the form 'YYYY-MM-DD HH:MM:SS'
    """
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def is_loopback(host):
    """Check whether a host address only accepts local connections

    Args:
        host (str): Host name or address

    Return:
        bool: True if the address is a loopback one
    """
    if host == 'localhost':
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


# pylint: disable=R0913,R0917
def serve(cser, host, port, pw_url, sync_interval, review_sync, token,
          rcser=None):
    """Run the web server until interrupted

    Args:
        cser (Cseries): Open cseries instance
        host (str): Address to listen on
        port (int): Port to listen on
        pw_url (str or None): Default patchwork URL
        sync_interval (int): Minutes between syncs, 0 to disable
        review_sync (bool): True to run 'review --sync' as part of a sync
        token (str or None): Token which clients must provide, or None
        rcser (Cseries or None): Open cseries instance holding the reviews,
            or None if they are in the main database
    """
    if not token and not is_loopback(host):
        tout.warning(f'Listening on {host} with no --token: anyone who can '
                     'reach this port can read your series and reviews')
    jobs_fname = os.path.join(os.path.dirname(cser.db.db_path), JOBS_FNAME)
    server = Server(cser, pw_url, sync_interval, review_sync, token, rcser,
                    jobs_fname)
    query = f'?token={token}' if token else ''
    tout.notice(f'Serving on http://{host}:{port}/{query}')
    if sync_interval:
        jobs = 'gather-all' + (' and review --sync' if review_sync else '')
        tout.notice(f'Syncing ({jobs}) every {sync_interval} minutes')
    web.run_app(server.make_app(), host=host, port=port, print=None)
