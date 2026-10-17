# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2020 Google LLC
#
"""Handles the main control logic of patman

This module provides various functions called by the main program to implement
the features of patman.
"""

import os
import re
import shutil
import sqlite3
import tempfile
import traceback

try:
    from importlib import resources
except ImportError:
    # for Python 3.6
    import importlib_resources as resources

from u_boot_pylib import command
from u_boot_pylib import gitutil
from u_boot_pylib import terminal
from u_boot_pylib import tools
from u_boot_pylib import tout

from patman import cseries
from patman import dbmerge
from patman import patchstream
from patman import review as review_mod
from patman import send
from patman import server
from patman import settings
from patman import status
from patman import workflow
from patman.patchwork import Patchwork

# The parts of the manual, in the order 'patman -H' shows them
HELP_FILES = ('patman.rst', 'configuration.rst', 'sending.rst', 'tags.rst',
              'patchwork.rst', 'series.rst', 'review.rst', 'serve.rst',
              'db.rst')


# A link to another page of the docs, e.g. :doc:`the guide </guide/index>`
RE_DOC_LINK = re.compile(r':doc:`([^`<]+?)\s*<[^>`]+>`')


def plain_links(text):
    """Turn links to other pages of the docs into plain text

    The manual links to other pages, such as the guide, which are not part of
    the text shown in the terminal, so show just the text of each link.

    Args:
        text (str): Text of a part of the manual

    Returns:
        str: Text with each link replaced by its text
    """
    return RE_DOC_LINK.sub(r'\1', text)


def full_help_text():
    """Get the text of the whole manual, as shown by 'patman -H'

    Returns:
        str: The parts of the manual, one after the other
    """
    return '\n'.join(plain_links(resources.files('patman').joinpath(
        fname).read_text(encoding='utf-8')) for fname in HELP_FILES)


def setup():
    """Do required setup before doing anything"""
    gitutil.setup()
    alias_fname = gitutil.get_alias_file()
    if alias_fname:
        settings.ReadGitAliases(alias_fname)


def _use_series_upstream(args, test_db=None):
    """Use the send settings of the series' upstream, if patman tracks it

    'patman series send' uses the settings of the series' upstream, such as
    who to send to and whether to copy the maintainers. Do the same when the
    branch being sent belongs to a series which patman tracks, so that the two
    agree. If there is no database, nothing is done, so that sending never
    creates one.

    Args:
        args (argparse.Namespace): 'send' arguments, which are updated
        test_db (str or None): Directory containing the test database, None
            to use the normal one
    """
    topdir = gitutil.get_top_level()
    db_dir = test_db or os.environ.get('PATMAN_DB_DIR') or topdir
    if not db_dir or not os.path.exists(os.path.join(
            os.path.expanduser(db_dir), '.patman.db')):
        return
    branch = args.branch or gitutil.get_branch()
    if not branch:
        return
    cser = cseries.Cseries(topdir, db_dir=test_db)
    cser.open_database()
    try:
        name, _ = patchstream.split_name_version(branch)
        ser = cser.get_series_by_name(name)
        if ser and ser.upstream:
            tout.notice(f"Using the send settings of upstream '{ser.upstream}', "
                        f"for series '{name}'")
            cser.apply_send_settings(args, ser.upstream)
    finally:
        cser.close_database()


def do_send(args, test_db=None):
    """Create, check and send patches by email

    Args:
        args (argparse.Namespace): Arguments to patman
        test_db (str or None): Directory containing the test database, None
            to use the normal one
    """
    setup()
    _use_series_upstream(args, test_db)
    send.send(args)


# pylint: disable=R0913,R0917
def patchwork_status(branch, count, start, end, dest_branch, force,
                     show_comments, url, single_thread=False,
                     show_cover_comments=False):
    """Check the status of patches in patchwork

    This finds the series in patchwork using the Series-link tag, checks for new
    comments and review tags, displays them and creates a new branch with the
    review tags.

    Args:
        branch (str): Branch to create patches from (None = current)
        count (int): Number of patches to produce, or -1 to produce patches for
            the current branch back to the upstream commit
        start (int): Start patch to use (0=first / top of branch)
        end (int): End patch to use (0=last one in series, 1=one before that,
            etc.)
        dest_branch (str): Name of new branch to create with the updated tags
            (None to not create a branch)
        force (bool): With dest_branch, force overwriting an existing branch
        show_comments (bool): True to display snippets from the comments
            provided by reviewers
        url (str): URL of patchwork server, e.g.
            'https://patchwork.ozlabs.org'. Ignored if the series
            provides a Series-patchwork-url tag.
        single_thread (bool): True to use a single thread for
            patchwork access
        show_cover_comments (bool): True to display snippets from the
            comments on the cover letter

    Raises:
        ValueError: if the branch has no Series-link value
    """
    if not branch:
        branch = gitutil.get_branch()
    if count == -1:
        # Work out how many patches to send if we can
        count = gitutil.count_commits_to_branch(branch) - start

    series = patchstream.get_metadata(branch, start, count - end)
    warnings = 0
    for cmt in series.commits:
        if cmt.warn:
            print(f'{len(cmt.warn)} warnings for {cmt.hash}:')
            for warn in cmt.warn:
                print('\t', warn)
                warnings += 1
            print()
    if warnings:
        raise ValueError('Please fix warnings before running status')
    links = series.get('links')
    if not links:
        raise ValueError('Branch has no Series-links value')

    status.find_link_and_show_status(
        series, branch, url, dest_branch, force, show_comments,
        show_cover_comments, single_thread)



def _setup_patchwork(cser, pwork, ups, pw_url):
    """Set up a Patchwork instance from upstream and project settings

    Args:
        cser (Cseries): Open cseries instance
        pwork (Patchwork or None): Existing instance, or None to create
        ups (str or None): Upstream name
        pw_url (str or None): Patchwork URL override

    Returns:
        Patchwork: Configured instance

    Raises:
        ValueError: if the URL or project cannot be resolved
    """
    if pwork:
        return pwork
    tout.debug(f'_setup_patchwork: ups={ups!r} pw_url={pw_url!r}')
    if ups:
        ups_url = cser.db.upstream_get_patchwork_url(ups)
        if ups_url:
            if pw_url and pw_url != ups_url:
                tout.info(f'  Overriding {pw_url!r} with upstream'
                          f' {ups!r} URL {ups_url!r}')
            pw_url = ups_url
        tout.debug(f'  URL from upstream {ups!r}: {pw_url!r}')
    if not pw_url:
        raise ValueError(
            'No patchwork URL found; use -U/--upstream or '
            "configure with 'patman upstream add'")
    pwork = Patchwork(pw_url)
    proj = cser.project_get(ups)
    tout.debug(f'  project_get({ups!r}): {proj!r}')
    if not proj:
        proj = cser.project_get()
        tout.debug(f'  project_get(None) fallback: {proj!r}')
        if proj:
            tout.warning(f"No patchwork project for upstream '{ups}';"
                         f' using default project {proj[0]} (ID {proj[1]})')
    if not proj:
        raise ValueError(
            "Patchwork project not configured; use "
            "'patman patchwork set-project'")
    _, proj_id, link_name = proj
    pwork.project_set(proj_id, link_name)
    pwork.upstream = ups
    return pwork


# pylint: disable=R0912,R0915
def do_series(args, test_db=None, pwork=None, cser=None):
    """Process a series subcommand

    Args:
        args (Namespace): Arguments to process
        test_db (str or None): Directory containing the test database, None to
            use the normal one
        pwork (Patchwork): Patchwork object to use, None to create one if
            needed
        cser (Cseries): Cseries object to use, None to create one
    """
    if not cser:
        cser = cseries.Cseries(test_db)
    needs_patchwork = [
        'autolink', 'autolink-all', 'open', 'send', 'status', 'gather',
        'gather-all'
        ]
    try:
        cser.open_database()
        if args.subcmd in needs_patchwork:
            ups = cser.get_series_upstream(args.series)
            tout.debug(f'Series upstream: {ups!r}')
            pwork = _setup_patchwork(
                cser, pwork, ups, args.patchwork_url)
        elif pwork and pwork is not True:
            raise ValueError(
                "Internal error: command "
                f"'{args.subcmd}' should not have patchwork")
        if args.subcmd == 'add':
            cser.add(args.series, args.desc, mark=args.mark,
                     allow_unmarked=args.allow_unmarked, end=args.upstream,
                     use_commit=args.use_first_commit,
                     ups=args.set_upstream, dry_run=args.dry_run)
        elif args.subcmd == 'archive':
            cser.archive(args.series)
        elif args.subcmd == 'autolink':
            cser.link_auto(pwork, args.series, args.version, args.update,
                           args.autolink_wait)
        elif args.subcmd == 'autolink-all':
            cser.link_auto_all(pwork, update_commit=args.update,
                               link_all_versions=args.link_all_versions,
                               replace_existing=args.replace_existing,
                               dry_run=args.dry_run, show_summary=True)
        elif args.subcmd == 'changes':
            cser.add_change(args.series, args.version, args.text,
                            cover=args.cover, dry_run=args.dry_run)
        elif args.subcmd == 'dec':
            cser.decrement(args.series, args.dry_run)
        elif args.subcmd == 'find':
            cser.series_find(args.query, args.include_archived)
        elif args.subcmd == 'info':
            cser.show_info(args.series,
                           show_reviews=getattr(args, 'reviews', None))
        elif args.subcmd == 'gather':
            cser.gather(pwork, args.series, args.version, args.show_comments,
                        args.show_cover_comments, args.gather_tags,
                        dry_run=args.dry_run)
        elif args.subcmd == 'gather-all':
            cser.gather_all(
                pwork, args.show_comments, args.show_cover_comments,
                args.gather_all_versions, args.gather_tags, args.dry_run)
        elif args.subcmd == 'get-link':
            link = cser.link_get(args.series, args.version)
            print(link)
        elif args.subcmd == 'inc':
            cser.increment(args.series, args.dry_run)
        elif args.subcmd == 'ls':
            cser.series_list(args.include_archived,
                             reviews_only=getattr(args, 'reviews', False))
        elif args.subcmd == 'open':
            cser.open(pwork, args.series, args.version)
        elif args.subcmd == 'mark':
            cser.mark(args.series, args.allow_marked, dry_run=args.dry_run)
        elif args.subcmd == 'patches':
            cser.list_patches(args.series, args.version, args.commit,
                              args.patch)
        elif args.subcmd == 'progress':
            cser.progress(args.series, args.show_all_versions,
                          args.list_patches, args.include_archived)
        elif args.subcmd == 'rm':
            cser.remove(args.series, dry_run=args.dry_run)
        elif args.subcmd == 'rm-version':
            cser.version_remove(args.series, args.version, dry_run=args.dry_run)
        elif args.subcmd == 'save-notes':
            cser.save_notes(args.series, args.notes_file)
        elif args.subcmd == 'show-notes':
            cser.show_notes(args.series)
        elif args.subcmd == 'rename':
            cser.rename(args.series, args.new_name, dry_run=args.dry_run)
        elif args.subcmd == 'review':
            cser.review(args.series, args.version, force=args.force,
                        spelling=args.spelling, context=args.context)
        elif args.subcmd == 'set-upstream':
            cser.set_upstream(args.series, args.upstream_name,
                              dry_run=args.dry_run)
        elif args.subcmd == 'scan':
            cser.scan(args.series, mark=args.mark,
                      allow_unmarked=args.allow_unmarked, end=args.upstream,
                      dry_run=args.dry_run)
        elif args.subcmd == 'send':
            cser.send(pwork, args.series, args.autolink, args.autolink_wait,
                      args)
        elif args.subcmd == 'set-link':
            cser.link_set(args.series, args.version, args.link, args.update)
        elif args.subcmd == 'status':
            cser.status(pwork, args.series, args.version, args.show_comments,
                        args.show_cover_comments)
        elif args.subcmd == 'summary':
            cser.summary(args.series)
        elif args.subcmd == 'unarchive':
            cser.unarchive(args.series)
        elif args.subcmd == 'unmark':
            cser.unmark(args.series, args.allow_unmarked, dry_run=args.dry_run)
        elif args.subcmd == 'version-change':
            cser.version_change(args.series, args.version, args.new_version,
                                dry_run=args.dry_run)
        else:
            raise ValueError(f"Unknown series subcommand '{args.subcmd}'")
    finally:
        cser.close_database()


# pylint: disable=R0912
def upstream(args, test_db=None):
    """Process an 'upstream' subcommand

    Args:
        args (Namespace): Arguments to process
        test_db (str or None): Directory containing the test database, None to
            use the normal one
    """
    cser = cseries.Cseries(test_db)
    try:
        cser.open_database()
        if args.subcmd == 'add':
            cser.upstream_add(args.remote_name, args.url,
                              args.project_name,
                              patchwork_url=args.patchwork_url,
                              identity=args.identity,
                              series_to=args.series_to,
                              no_maintainers=args.no_maintainers,
                              no_tags=args.no_tags)
        elif args.subcmd == 'default':
            if args.unset:
                cser.upstream_set_default(None)
            elif args.remote_name:
                cser.upstream_set_default(args.remote_name)
            else:
                result = cser.upstream_get_default()
                print(result if result else 'unset')
        elif args.subcmd == 'delete':
            cser.upstream_delete(args.remote_name)
        elif args.subcmd == 'set':
            kwargs = {}
            if args.patchwork_url is not None:
                kwargs['patchwork_url'] = args.patchwork_url
            if args.identity is not None:
                kwargs['identity'] = args.identity
            if args.series_to is not None:
                kwargs['series_to'] = args.series_to
            if args.no_maintainers:
                kwargs['no_maintainers'] = True
            elif args.maintainers:
                kwargs['no_maintainers'] = False
            if args.no_tags:
                kwargs['no_tags'] = True
            elif args.tags:
                kwargs['no_tags'] = False
            if not kwargs:
                raise ValueError('No settings to update')
            cser.upstream_set(args.remote_name, **kwargs)
        elif args.subcmd == 'ls':
            cser.upstream_list()
        else:
            raise ValueError(f"Unknown upstream subcommand '{args.subcmd}'")
    finally:
        cser.close_database()


def _default_remote():
    """Work out which remote to use when the user does not give one

    This is the remote which the current branch tracks, or else the only
    remote, if there is just one.

    Returns:
        tuple:
            str or None: Remote to use, or None if it is not clear which
            list of str: Names of all the remotes
    """
    try:
        remotes = command.output('git', 'remote').split()
    except command.CommandExc:
        return None, []
    try:
        branch = command.output_one_line('git', 'symbolic-ref', '--short',
                                         'HEAD')
        remote = command.output_one_line('git', 'config',
                                         f'branch.{branch}.remote')
        if remote in remotes:
            return remote, remotes
    except command.CommandExc:
        pass
    if len(remotes) == 1:
        return remotes[0], remotes
    return None, remotes


def _get_remote(args):
    """Get the remote for a patchwork subcommand

    Args:
        args (Namespace): Arguments to process, using the 'remote' value if
            the user gave one

    Returns:
        str: Remote to use

    Raises:
        ValueError: if no remote is given and it is not clear which to use
    """
    if args.remote:
        return args.remote
    remote, remotes = _default_remote()
    if not remote:
        raise ValueError('Please specify the remote name (remotes: '
                         f"{', '.join(remotes) or 'none'})")
    return remote


# pylint: disable=R0912
def patchwork(args, test_db=None, pwork=None):
    """Process a 'patchwork' subcommand
    Args:
        args (Namespace): Arguments to process
        test_db (str or None): Directory containing the test database, None to
            use the normal one
        pwork (Patchwork): Patchwork object to use
    """
    cser = cseries.Cseries(test_db)
    try:
        cser.open_database()
        if args.subcmd == 'set-project':
            ups = _get_remote(args)
            if not pwork:
                pw_url = cser.db.upstream_get_patchwork_url(ups)
                if not pw_url:
                    pw_url = args.patchwork_url
                if not pw_url:
                    raise ValueError(
                        f"No patchwork URL for remote '{ups}'"
                        "; use 'patman upstream add' with -p"
                        ' or pass --patchwork-url')
                pwork = Patchwork(pw_url)
            cser.project_set(pwork, args.project_name, ups=ups)
        elif args.subcmd == 'get-project':
            ups = _get_remote(args)
            info = cser.project_get(ups)
            if not info:
                raise ValueError(
                    "Project has not been set; use "
                    "'patman patchwork set-project'")
            name, pwid, link_name = info
            msg = (f"Project '{name}' patchwork-ID {pwid} "
                   f"link-name '{link_name}'")
            if ups:
                msg += f" remote '{ups}'"
            print(msg)
        elif args.subcmd == 'rm':
            cser.db.patchwork_delete(args.remote)
            cser.commit()
            ups_str = f" for upstream '{args.remote}'" if args.remote else ''
            tout.notice(f'Deleted patchwork project{ups_str}')
        elif args.subcmd == 'ls':
            cser.project_list()
        else:
            raise ValueError(f"Unknown patchwork subcommand '{args.subcmd}'")
    finally:
        cser.close_database()

def do_workflow(args, test_db=None):
    """Process a 'workflow' subcommand

    Args:
        args (Namespace): Arguments to process
        test_db (str or None): Directory containing the test database, None to
            use the normal one
    """
    cser = cseries.Cseries(test_db)
    try:
        cser.open_database()
        if args.subcmd == 'todo':
            if args.clear:
                workflow.todo_clear(cser, args.series)
            else:
                workflow.todo(cser, args.series, args.days)
        elif args.subcmd == 'todo-list':
            workflow.todo_list(cser, args.show_all)
        elif args.subcmd in ['list', 'wl', 'ls']:
            workflow.list_entries(cser, args.show_all)
        else:
            raise ValueError(f"Unknown workflow subcommand '{args.subcmd}'")
    finally:
        cser.close_database()


def do_review(args, test_db=None, pwork=None, cser=None):
    """Process the 'review' command

    Sets up patchwork and cseries, then delegates to
    review.do_review().

    Args:
        args (Namespace): Arguments to process
        test_db (str or None): Directory containing the test
            database, None to use the normal one
        pwork (Patchwork): Patchwork object to use, or None to
            create one
        cser (Cseries): Cseries object to use, or None to create
            one
    """
    # Listing models needs no database or patchwork URL; answer and return
    # before either is set up (opening the database would create one)
    if getattr(args, 'list_models', False):
        return review_mod.do_review(args, None, None)

    if not cser:
        cser = cseries.Cseries(test_db)
    review_db_dir = getattr(args, 'review_db_dir', None)
    try:
        cser.open_database()

        # Resolve patchwork URL
        if not pwork and not args.learn_voice and not args.sync \
                and not args.relink \
                and not getattr(args, 'list_replies', False) \
                and not getattr(args, 'suggest_reply', None) \
                and not getattr(args, 'draft_reply', None) \
                and not getattr(args, 'send_reply', None):
            ups = args.upstream
            if not ups:
                ups = cser.db.upstream_get_default()
            pwork = _setup_patchwork(
                cser, pwork, ups, args.patchwork_url)

        if not review_db_dir:
            return review_mod.do_review(args, pwork, cser)

        # Upstreams are only held in the main database, so note the default
        # one before switching to the review database
        if not args.upstream:
            args.upstream = cser.db.upstream_get_default()
        rcser = cseries.Cseries(cser.topdir, db_dir=review_db_dir)
        try:
            rcser.open_database()
            return review_mod.do_review(args, pwork, rcser)
        finally:
            rcser.close_database()
    finally:
        cser.close_database()


def _open_review_db(cser, args):
    """Open the review database named by the settings or arguments

    Args:
        cser (Cseries): Open cseries instance for the main database
        args (Namespace): Arguments, including review_db_dir

    Return:
        Cseries: Open cseries instance for the review database

    Raises:
        ValueError: There is no review database, or it is the main one
    """
    if not args.review_db_dir:
        raise ValueError("No review database: set 'review_db_dir' in "
                         '~/.patman or use --review-db-dir')
    rdir = os.path.expanduser(args.review_db_dir)
    if not os.path.isdir(rdir):
        raise ValueError(f"Review database directory '{rdir}' does not exist")
    if os.path.realpath(os.path.join(rdir, '.patman.db')) == \
            os.path.realpath(cser.db.db_path):
        raise ValueError('The review database is the same as the main one')
    rcser = cseries.Cseries(cser.topdir, db_dir=rdir)
    rcser.open_database()
    return rcser


def _copy_source_db(source, tmpdir):
    """Make a private copy of a database to read from

    The copy is made with SQLite's backup API, reading the source without
    changing it, so it includes any changes not yet checkpointed. Opening the
    copy may then upgrade its schema, leaving the source alone.

    Args:
        source (str): Directory holding .patman.db, or the database file
        tmpdir (str): Directory to hold the copy

    Return:
        str: tmpdir, which now holds .patman.db
    """
    fname = os.path.expanduser(source)
    if os.path.isdir(fname):
        fname = os.path.join(fname, '.patman.db')
    if not os.path.isfile(fname):
        raise ValueError(f"No database found at '{fname}'")
    src = sqlite3.connect(f'file:{fname}?mode=ro', uri=True)
    dest = sqlite3.connect(os.path.join(tmpdir, '.patman.db'))
    try:
        src.backup(dest)
    finally:
        dest.close()
        src.close()
    return tmpdir


# pylint: disable=R0913,R0917
def _transfer(src, dst, kind, move, dry_run, upstreams=False):
    """Copy or move series between two open databases

    Args:
        src (Cseries): Instance to copy from
        dst (Cseries): Instance to copy to
        kind (str): dbmerge.ALL, OWN or REVIEWS
        move (bool): True to remove each series from src once copied
        dry_run (bool): True to report what would be done, changing nothing
        upstreams (bool): True to copy upstreams missing from dst
    """
    if not dry_run:
        dbmerge.backup(dst.db)
        if move:
            dbmerge.backup(src.db)
    stats = dbmerge.copy_series(src.db, dst.db, kind, move)
    if upstreams:
        dbmerge.copy_upstreams(src.db, dst.db, stats)
    if dry_run:
        dst.rollback()
        src.rollback()
    else:
        dst.commit()
        if move:
            src.commit()
    stats.report(move)
    if stats.left:
        tout.warning(f'{len(stats.left)} series left in place in '
                     f"{src.db.db_path}: {', '.join(stats.left)}")
    if dry_run:
        tout.notice('Dry run completed')


def do_database(args, test_db=None):
    """Process a 'database' subcommand

    Args:
        args (Namespace): Arguments to process
        test_db (str or None): Directory containing the test database, None to
            use the normal one
    """
    cser = cseries.Cseries(test_db)
    others = []
    tmpdir = None
    try:
        cser.open_database()
        if args.subcmd == 'merge':
            tmpdir = tempfile.mkdtemp(prefix='patman-db.')
            src = cseries.Cseries(cser.topdir,
                                  db_dir=_copy_source_db(args.source, tmpdir))
            src.open_database()
            others.append(src)
            dst = cser
            if args.to_review_db:
                dst = _open_review_db(cser, args)
                others.append(dst)
            kind = (dbmerge.OWN if args.own else
                    dbmerge.REVIEWS if args.reviews else dbmerge.ALL)
            _transfer(src, dst, kind, False, args.dry_run,
                      upstreams=not args.to_review_db)
        elif args.subcmd in ('split', 'join'):
            rcser = _open_review_db(cser, args)
            others.append(rcser)
            src, dst = (cser, rcser) if args.subcmd == 'split' else \
                (rcser, cser)
            _transfer(src, dst, dbmerge.REVIEWS, True, args.dry_run)
    finally:
        for other in others:
            other.close_database()
        cser.close_database()
        if tmpdir:
            shutil.rmtree(tmpdir)


def do_serve(args, test_db=None):
    """Process the 'serve' command

    Args:
        args (Namespace): Arguments to process
        test_db (str or None): Directory containing the test database, None to
            use the normal one
    """
    cser = cseries.Cseries(test_db)
    rcser = None
    try:
        cser.open_database()
        if args.review_db_dir:
            rcser = cseries.Cseries(cser.topdir, db_dir=args.review_db_dir)
            rcser.open_database()
        server.serve(cser, args.host, args.port, args.patchwork_url,
                     args.sync_interval, args.review_sync, args.token, rcser)
    finally:
        if rcser:
            rcser.close_database()
        cser.close_database()


# pylint: disable=R0912
def _report_error(exc, debug):
    """Report an exception which stops a command

    Args:
        exc (Exception): Exception to report
        debug (bool): True to show the traceback too
    """
    terminal.tprint(f'patman: {type(exc).__name__}: {exc}',
                    colour=terminal.Color.RED)
    if debug:
        print()
        traceback.print_exc()


def do_patman(args, test_db=None, pwork=None, cser=None):
    """Process a patman command

    Args:
        args (Namespace): Arguments to process
        test_db (str or None): Directory containing the test database, None to
            use the normal one
        pwork (Patchwork): Patchwork object to use, or None to create one
        cser (Cseries): Cseries object to use when executing the command,
            or None to create one
    """
    if getattr(args, 'patchwork_timeout', None):
        Patchwork.set_timeout(args.patchwork_timeout)
    if args.full_help:
        # The pager takes a single file, so put the parts of the manual
        # together in one
        with tempfile.NamedTemporaryFile('w', prefix='patman-help-',
                                         suffix='.rst', encoding='utf-8',
                                         delete=False) as outf:
            outf.write(full_help_text())
        try:
            tools.print_full_help(outf.name)
        finally:
            os.unlink(outf.name)
        return 0
    if args.cmd == 'send':
        # Called from git with a patch filename as argument
        # Printout a list of additional CC recipients for this patch
        if args.cc_cmd:
            re_line = re.compile(r'(\S*) (.*)')
            with open(args.cc_cmd, 'r', encoding='utf-8') as inf:
                for line in inf.readlines():
                    match = re_line.match(line)
                    if match and match.group(1) == args.patchfiles[0]:
                        for cca in match.group(2).split('\0'):
                            cca = cca.strip()
                            if cca:
                                print(cca)
        else:
            # If we are not processing tags, no need to warning about bad ones
            if not args.process_tags:
                args.ignore_bad_tags = True
            try:
                do_send(args, test_db)
            except Exception as exc:  # pylint: disable=W0718
                _report_error(exc, args.debug)
                return 1
        return 0

    ret_code = 0
    try:
        # Check status of patches in patchwork
        if args.cmd == 'status':
            patchwork_status(args.branch, args.count, args.start, args.end,
                             args.dest_branch, args.force, args.show_comments,
                             args.patchwork_url,
                             single_thread=args.single_thread,
                             show_cover_comments=args.show_cover_comments)
        elif args.cmd == 'series':
            do_series(args, test_db, pwork, cser)
        elif args.cmd == 'upstream':
            upstream(args, test_db)
        elif args.cmd == 'patchwork':
            patchwork(args, test_db, pwork)
        elif args.cmd == 'review':
            ret_code = do_review(args, test_db, pwork, cser) or 0
        elif args.cmd == 'workflow':
            do_workflow(args, test_db)
        elif args.cmd == 'serve':
            do_serve(args, test_db)
        elif args.cmd == 'database':
            do_database(args, test_db)
    except Exception as exc:  # pylint: disable=W0718
        _report_error(exc, args.debug)
        ret_code = 1
    return ret_code
