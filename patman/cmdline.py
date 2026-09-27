# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2023 Google LLC
#

"""Handles parsing of buildman arguments

This creates the argument parser and uses it to parse the arguments passed in
"""

import argparse
import os
import pathlib
import sys

from u_boot_pylib import gitutil
from patman import project
from patman import settings

PATMAN_DIR = pathlib.Path(__file__).parent
HAS_TESTS = os.path.exists(PATMAN_DIR / "func_test.py")

# Aliases for subcommands
ALIASES = {
    'series': ['s', 'ser'],
    'status': ['st'],
    'patchwork': ['pw'],
    'review': ['r', 'rev'],
    'upstream': ['us'],
    'workflow': ['wf'],

    # Subcommand aliases
    'archive': ['ar'],
    'autolink': ['au'],
    'gather': ['g'],
    'ls': ['list'],
    'open': ['o'],
    'progress': ['p', 'pr', 'prog'],
    'rm-version': ['rmv'],
    'todo-list': ['tl'],
    'workflow-list': ['wl'],
    'unarchive': ['unar'],
    }


class ErrorCatchingArgumentParser(argparse.ArgumentParser):
    def __init__(self, **kwargs):
        self.exit_state = None
        self.catch_error = False
        super().__init__(**kwargs)

    def error(self, message):
        if self.catch_error:
            self.message = message
        else:
            super().error(message)

    def exit(self, status=0, message=None):
        if self.catch_error:
            self.exit_state = True
        else:
            super().exit(status, message)


def add_send_args(par):
    """Add arguments for the 'send' command

    Arguments:
        par (ArgumentParser): Parser to add to
    """
    par.add_argument(
        '-c', '--count', dest='count', type=int, default=-1,
        help='Automatically create patches from top n commits')
    par.add_argument(
        '-e', '--end', type=int, default=0,
        help='Commits to skip at end of patch list')
    par.add_argument(
        '-i', '--ignore-errors', action='store_true',
        dest='ignore_errors', default=False,
        help='Send patches email even if patch errors are found')
    par.add_argument(
        '-l', '--limit-cc', dest='limit', type=int, default=None,
        help='Limit the cc list to LIMIT entries [default: %(default)s]')
    par.add_argument(
        '-m', '--no-maintainers', action='store_false',
        dest='add_maintainers', default=True,
        help="Don't cc the file maintainers automatically")
    default_arg = None
    top_level = gitutil.get_top_level()
    if top_level:
        default_arg = os.path.join(top_level, 'scripts',
                                   'get_maintainer.pl') + ' --norolestats'
    par.add_argument(
        '--get-maintainer-script', dest='get_maintainer_script', type=str,
        action='store',
        default=default_arg,
        help='File name of the get_maintainer.pl (or compatible) script.')
    par.add_argument(
        '-r', '--in-reply-to', type=str, action='store',
        help="Message ID that this series is in reply to")
    par.add_argument(
        '-s', '--start', dest='start', type=int, default=0,
        help='Commit to start creating patches from (0 = HEAD)')
    par.add_argument(
        '-t', '--ignore-bad-tags', action='store_true', default=False,
        help='Ignore bad tags / aliases (default=warn)')
    par.add_argument(
        '--no-binary', action='store_true', dest='ignore_binary',
        default=False,
        help="Do not output contents of changes in binary files")
    par.add_argument(
        '--no-check', action='store_false', dest='check_patch', default=True,
        help="Don't check for patch compliance")
    par.add_argument(
        '--tree', dest='check_patch_use_tree', default=False,
        action='store_true',
        help=("Set `tree` to True. If `tree` is False then we'll pass "
              "'--no-tree' to checkpatch (default: tree=%(default)s)"))
    par.add_argument(
        '--no-tree', dest='check_patch_use_tree', action='store_false',
        help="Set `tree` to False")
    par.add_argument(
        '--no-tags', action='store_false', dest='process_tags', default=True,
        help="Don't process subject tags as aliases")
    par.add_argument(
        '--no-signoff', action='store_false', dest='add_signoff',
        default=True, help="Don't add Signed-off-by to patches")
    par.add_argument(
        '--smtp-server', type=str,
        help="Specify the SMTP server to 'git send-email'")
    par.add_argument(
        '--send-endpoint-web', dest='send_endpoint_web', type=str,
        default=None,
        help='Web submission endpoint to relay patches through instead of '
             "git send-email (needs patatt; can be set as 'send_endpoint_web' "
             'in .patman)')
    par.add_argument(
        '--reflect', action='store_true',
        help='With a web relay, reflect the series back to yourself only '
             '(a safe test) instead of sending it')
    par.add_argument(
        '--no-relay', action='store_true', dest='no_relay', default=False,
        help='Send with git send-email even if a web relay '
             '(send_endpoint_web) is configured')
    par.add_argument(
        '--web-auth-new', action='store_true',
        help='Register your identity and signing key with the web endpoint '
             '(one-time), then exit')
    par.add_argument(
        '--web-auth-verify', type=str, default=None, metavar='CHALLENGE',
        help='Complete web-endpoint registration using the emailed '
             'challenge, then exit')
    par.add_argument(
        '--keep-change-id', action='store_true',
        help='Preserve Change-Id tags in patches to send.')
    par.add_argument(
        '--no-base-commit', action='store_false', dest='insert_base_commit',
        default=True,
        help="Don't add 'base-commit'/'branch' trailers to the patches or "
             "cover letter (useful when the base commit is a local/downstream "
             "sha that does not exist upstream)")


def _add_show_comments(parser):
    parser.add_argument('-c', '--show-comments', action='store_true',
                        help='Show comments from each patch')


def _add_show_cover_comments(parser):
    parser.add_argument('-C', '--show-cover-comments', action='store_true',
                        help='Show comments from the cover letter')


def _add_archived(parser):
    parser.add_argument('-A', '--include-archived', action='store_true',
                        help='Show archived series as well')


def add_patchwork_subparser(subparsers):
    """Add the 'patchwork' subparser

    Args:
        subparsers (argparse action): Subparser parent

    Return:
        ArgumentParser: patchwork subparser
    """
    patchwork = subparsers.add_parser(
        'patchwork', aliases=ALIASES['patchwork'],
        help='Manage patchwork connection')
    patchwork.defaults_cmds = [
        ['set-project', 'U-Boot', 'us'],
    ]
    patchwork_subparsers = patchwork.add_subparsers(dest='subcmd')
    gproj = patchwork_subparsers.add_parser('get-project')
    gproj.add_argument(
        'remote', nargs='?',
        help='Remote to get the project for')
    uset = patchwork_subparsers.add_parser('set-project')
    uset.add_argument(
        'project_name', help="Patchwork project name, e.g. 'U-Boot'")
    uset.add_argument(
        'remote', nargs='?',
        help='Remote to associate with this project')
    pdel = patchwork_subparsers.add_parser('rm')
    pdel.add_argument(
        'remote', nargs='?',
        help='Remote to delete the project for, or omit for the default')
    patchwork_subparsers.add_parser('ls', aliases=['list'])
    return patchwork


def add_series_subparser(subparsers):
    """Add the 'series' subparser

    Args:
        subparsers (argparse action): Subparser parent

    Return:
        ArgumentParser: series subparser
    """
    def _add_allow_unmarked(parser):
        parser.add_argument('-M', '--allow-unmarked', action='store_true',
                            default=False,
                            help="Don't require commits to be marked")

    def _add_mark(parser):
        parser.add_argument(
            '-m', '--mark', action='store_true',
            help='Mark unmarked commits with a Change-Id field')

    def _add_update(parser):
        parser.add_argument('-u', '--update', action='store_true',
                            help='Update the branch commit')

    def _add_wait(parser, default_s):
        """Add a -w option to a parser

        Args:
            parser (ArgumentParser): Parser to adjust
            default_s (int): Default value to use, in seconds
        """
        parser.add_argument(
            '-w', '--autolink-wait', type=int, default=default_s,
            help='Seconds to wait for patchwork to get a sent series')

    def _upstream_add(parser):
        parser.add_argument('-U', '--upstream', help='Commit to end before')

    def _add_gather(parser):
        parser.add_argument(
            '-G', '--no-gather-tags', dest='gather_tags', default=True,
            action='store_false',
            help="Don't gather review/test tags / update local series")

    series = subparsers.add_parser('series', aliases=ALIASES['series'],
                                   help='Manage series of patches')
    series.defaults_cmds = [
        ['set-link', 'fred'],
        ['find', 'dummy'],
        ['changes', 'dummy'],
    ]
    series.add_argument(
        '-n', '--dry-run', action='store_true', dest='dry_run', default=False,
        help="Do a dry run (create but don't email patches)")
    series.add_argument('-s', '--series', help='Name of series')
    series.add_argument('-V', '--version', type=int,
                        help='Version number to link')
    series_subparsers = series.add_subparsers(dest='subcmd')

    # This causes problem at present, perhaps due to the 'defaults' handling in
    # settings
    # series_subparsers.required = True

    add = series_subparsers.add_parser('add')
    add.add_argument('-D', '--desc',
                     help='Series description / cover-letter title')
    add.add_argument(
        '-1', '--use-first-commit', action='store_true',
        help="Use the first commit's subject as series description if needed")
    add.add_argument(
        '-f', '--force-version', action='store_true',
        help='Change the Series-version on a series to match its branch')
    add.add_argument('-S', '--set-upstream',
                     help='Set the upstream for this series')
    _add_mark(add)
    _add_allow_unmarked(add)
    _upstream_add(add)

    series_subparsers.add_parser('archive', aliases=ALIASES['archive'])

    auto = series_subparsers.add_parser('autolink',
                                        aliases=ALIASES['autolink'])
    auto.add_argument('-u', '--update', action='store_true', default=True,
                      help='Update the branch commit (default)')
    auto.add_argument('--no-update', action='store_false', dest='update',
                      help='Do not update the branch commit')
    _add_wait(auto, 0)

    aall = series_subparsers.add_parser('autolink-all')
    aall.add_argument('-a', '--link-all-versions', action='store_true',
                      help='Link all series versions, not just the latest')
    aall.add_argument('-r', '--replace-existing', action='store_true',
                      help='Replace existing links')
    aall.add_argument('-u', '--update', action='store_true', default=True,
                      help='Update the branch commits (default)')
    aall.add_argument('--no-update', action='store_false', dest='update',
                      help='Do not update the branch commits')

    chg = series_subparsers.add_parser(
        'changes',
        help='Add a Series-changes / Cover-changes bullet to the HEAD '
             'commit and amend it')
    chg.add_argument('text', help='Bullet text')
    chg.add_argument('-c', '--cover', action='store_true',
                     help='Use Cover-changes (cover-letter only) instead '
                          'of Series-changes')

    series_subparsers.add_parser('dec')

    gat = series_subparsers.add_parser('gather', aliases=ALIASES['gather'])
    _add_gather(gat)
    _add_show_comments(gat)
    _add_show_cover_comments(gat)

    sall = series_subparsers.add_parser('gather-all')
    sall.add_argument(
        '-a', '--gather-all-versions', action='store_true',
        help='Gather tags from all series versions, not just the latest')
    _add_gather(sall)
    _add_show_comments(sall)
    _add_show_cover_comments(sall)

    find = series_subparsers.add_parser(
        'find', help='Search for series by subject fragment')
    find.add_argument('query', help='Text to search for')
    _add_archived(find)

    series_subparsers.add_parser('get-link')
    series_subparsers.add_parser('inc')
    info = series_subparsers.add_parser('info')
    info.add_argument('-r', '--reviews', nargs='*', type=int, default=None,
                      help='Show review text (optionally for specific patches)')
    ls = series_subparsers.add_parser('ls', aliases=['list'])
    _add_archived(ls)
    ls.add_argument('-r', '--reviews', action='store_true',
                    help='Show only review series')

    mar = series_subparsers.add_parser('mark')
    mar.add_argument('-m', '--allow-marked', action='store_true',
                     default=False,
                     help="Don't require commits to be unmarked")

    series_subparsers.add_parser('open', aliases=ALIASES['open'])
    pat = series_subparsers.add_parser(
        'patches', epilog='Show a list of patches and optional details')
    pat.add_argument('-t', '--commit', action='store_true',
                     help='Show the commit and diffstat')
    pat.add_argument('-p', '--patch', action='store_true',
                     help='Show the patch body')

    prog = series_subparsers.add_parser('progress',
                                        aliases=ALIASES['progress'])
    prog.add_argument('-a', '--show-all-versions', action='store_true',
                      help='Show all series versions, not just the latest')
    prog.add_argument('-l', '--list-patches', action='store_true',
                      help='List patch subject and status')
    _add_archived(prog)

    ren = series_subparsers.add_parser('rename')
    ren.add_argument('-N', '--new-name', help='New name for the series')

    rev = series_subparsers.add_parser(
        'review', help='AI-review the series and store the result')
    rev.add_argument('-f', '--force', action='store_true',
                     help='Re-review even if reviews are already stored')
    rev.add_argument('--spelling', type=str, default='British',
                     help='Spelling convention for review comments')
    rev.add_argument('-c', '--context', type=str, default=None,
                     help="Extra context for the review agent, or '@path' "
                          'to read it from a file')

    series_subparsers.add_parser('rm')

    snotes = series_subparsers.add_parser('save-notes')
    snotes.add_argument(
        'notes_file', nargs='?', default='review-notes.txt',
        help='Path to the review notes file (default: review-notes.txt)')

    series_subparsers.add_parser('show-notes')

    sup = series_subparsers.add_parser('set-upstream')
    sup.add_argument('upstream_name', nargs='?',
                     help='Name of the upstream for this series')
    series_subparsers.add_parser('rm-version', aliases=ALIASES['rm-version'])

    scan = series_subparsers.add_parser('scan')
    _add_mark(scan)
    _add_allow_unmarked(scan)
    _upstream_add(scan)

    ssend = series_subparsers.add_parser('send')
    add_send_args(ssend)
    ssend.add_argument(
        '--no-autolink', action='store_false', default=True, dest='autolink',
        help='Monitor patchwork after sending so the series can be autolinked')
    _add_wait(ssend, 120)

    setl = series_subparsers.add_parser('set-link')
    _add_update(setl)

    setl.add_argument(
        'link', help='Link to use, i.e. patchwork series number (e.g. 452329)')
    stat = series_subparsers.add_parser('status', aliases=ALIASES['status'])
    _add_show_comments(stat)
    _add_show_cover_comments(stat)

    series_subparsers.add_parser('summary')

    series_subparsers.add_parser('unarchive', aliases=ALIASES['unarchive'])

    unm = series_subparsers.add_parser('unmark')
    _add_allow_unmarked(unm)

    ver = series_subparsers.add_parser(
        'version-change', help='Change a version to a different version')
    ver.add_argument('--new-version', type=int,
                     help='New version number to change this one too')

    return series


def add_send_subparser(subparsers):
    """Add the 'send' subparser

    Args:
        subparsers (argparse action): Subparser parent

    Return:
        ArgumentParser: send subparser
    """
    send = subparsers.add_parser(
        'send', help='Format, check and email patches (default command)')
    send.add_argument(
        '-b', '--branch', type=str,
        help="Branch to process (by default, the current branch)")
    send.add_argument(
        '-n', '--dry-run', action='store_true', dest='dry_run',
        default=False, help="Do a dry run (create but don't email patches)")
    send.add_argument(
        '--cc-cmd', dest='cc_cmd', type=str, action='store',
        default=None, help='Output cc list for patch file (used by git)')
    add_send_args(send)
    send.add_argument('patchfiles', nargs='*')
    return send


def add_status_subparser(subparsers):
    """Add the 'status' subparser

    Args:
        subparsers (argparse action): Subparser parent

    Return:
        ArgumentParser: status subparser
    """
    status = subparsers.add_parser('status', aliases=ALIASES['status'],
                                   help='Check status of patches in patchwork')
    _add_show_comments(status)
    status.add_argument(
        '-d', '--dest-branch', type=str,
        help='Name of branch to create with collected responses')
    status.add_argument('-f', '--force', action='store_true',
                        help='Force overwriting an existing branch')
    status.add_argument('-T', '--single-thread', action='store_true',
                        help='Disable multithreading when reading patchwork')
    return status


def add_upstream_subparser(subparsers):
    """Add the 'status' subparser

    Args:
        subparsers (argparse action): Subparser parent

    Return:
        ArgumentParser: status subparser
    """
    upstream = subparsers.add_parser('upstream', aliases=ALIASES['upstream'],
                                     help='Manage upstream destinations')
    upstream.defaults_cmds = [
        ['add', 'us', 'http://fred', '-p', 'http://pw', 'U-Boot'],
        ['delete', 'us'],
        ['set', 'us'],
    ]
    upstream_subparsers = upstream.add_subparsers(dest='subcmd')
    uadd = upstream_subparsers.add_parser('add')
    uadd.add_argument('remote_name',
                      help="Git remote name used for this upstream, e.g. 'us'")
    uadd.add_argument(
        'url', help='URL to use for this upstream, e.g. '
                    "'https://gitlab.denx.de/u-boot/u-boot.git'")
    uadd.add_argument(
        '-p', '--patchwork-url',
        help='URL of patchwork server for this upstream, e.g. '
             "'https://patchwork.ozlabs.org'")
    uadd.add_argument(
        '-I', '--identity',
        help="Git sendemail identity to use, e.g. 'chromium'")
    uadd.add_argument(
        '-t', '--series-to',
        help="Patman alias for the To address, e.g. 'u-boot'")
    uadd.add_argument(
        '-m', '--no-maintainers', action='store_true', default=False,
        help='Skip get_maintainer.pl for this upstream')
    uadd.add_argument(
        '--no-tags', action='store_true', default=False,
        help='Skip subject-tag alias processing for this upstream')
    uadd.add_argument(
        'project_name', nargs='?',
        help="Patchwork project name, e.g. 'U-Boot'")
    udel = upstream_subparsers.add_parser('delete')
    udel.add_argument(
        'remote_name',
        help="Git remote name used for this upstream, e.g. 'us'")
    upstream_subparsers.add_parser('ls', aliases=['list'])
    uset = upstream_subparsers.add_parser('set')
    uset.add_argument('remote_name',
                      help="Git remote name used for this upstream, e.g. 'us'")
    uset.add_argument(
        '-p', '--patchwork-url',
        help='URL of patchwork server for this upstream')
    uset.add_argument(
        '-I', '--identity',
        help="Git sendemail identity to use, e.g. 'chromium'")
    uset.add_argument(
        '-t', '--series-to',
        help="Patman alias for the To address, e.g. 'u-boot'")
    uset.add_argument(
        '-m', '--no-maintainers', action='store_true', default=None,
        help='Skip get_maintainer.pl for this upstream')
    uset.add_argument(
        '--maintainers', action='store_true', default=None,
        help='Enable get_maintainer.pl for this upstream')
    uset.add_argument(
        '--no-tags', action='store_true', default=None,
        help='Skip subject-tag alias processing for this upstream')
    uset.add_argument(
        '--tags', action='store_true', default=None,
        help='Enable subject-tag alias processing for this upstream')
    udef = upstream_subparsers.add_parser('default')
    udef.add_argument('-u', '--unset', action='store_true',
                      help='Unset the default upstream')
    udef.add_argument('remote_name', nargs='?',
                      help="Git remote name used for this upstream, e.g. 'us'")
    return upstream


def add_workflow_subparser(subparsers):
    """Add the 'workflow' subparser

    Args:
        subparsers (argparse action): Subparser parent

    Return:
        ArgumentParser: workflow subparser
    """
    workflow = subparsers.add_parser('workflow', aliases=ALIASES['workflow'],
                                     help='Manage workflow items')
    workflow_subparsers = workflow.add_subparsers(dest='subcmd')
    todo = workflow_subparsers.add_parser('todo')
    todo.add_argument('-s', '--series', help='Name of series')
    todo.add_argument('days', nargs='?', type=int, default=14,
                      help='Number of days until due (default: 14)')
    todo.add_argument('--clear', action='store_true',
                      help='Clear the todo marker instead of setting it')

    tlist = workflow_subparsers.add_parser('todo-list',
                                           aliases=ALIASES['todo-list'])
    tlist.add_argument('--all', action='store_true', dest='show_all',
                       help='Show all scheduled todos, not just due ones')

    wlist = workflow_subparsers.add_parser('list',
                                           aliases=[*ALIASES['workflow-list'],
                                                    'ls'])
    wlist.add_argument('-a', '--all', action='store_true', dest='show_all',
                       help='Include archived entries')
    return workflow


def add_review_subparser(subparsers):
    """Add the 'review' subparser

    Args:
        subparsers (argparse action): Subparser parent

    Return:
        ArgumentParser: review subparser
    """
    review = subparsers.add_parser(
        'review', aliases=ALIASES['review'],
        help='AI-powered review of a patchwork series')
    review.add_argument(
        '-s', '--series', type=str, dest='pw_link',
        help='Patchwork series link/ID number')
    review.add_argument(
        '-S', '--series-title', type=str, dest='title',
        help='Search for a series by cover-letter title')
    review.add_argument(
        '-V', '--version', type=int, default=None,
        help='Series version to review when searching by title (-S); '
             'defaults to the most recent')
    review.add_argument(
        '-p', '--patch', type=int,
        help='Patchwork patch ID (finds the series and reviews just '
        'that patch, or the whole series with -w)')
    review.add_argument(
        '-P', '--patch-title', type=str,
        help='Search for a patch by title (finds its series and reviews '
        'just that patch, or the whole series with -w)')
    review.add_argument(
        '-w', '--whole-series', action='store_true',
        help='With -p/-P, review the whole series the patch belongs to, '
        'rather than just that patch')
    review.add_argument(
        '-i', '--index', type=str, dest='patches',
        help='Review only specific patches by index (e.g. 3 or 1,3,5 '
        'or 2-7)')
    review.add_argument(
        '-n', '--dry-run', action='store_true', dest='dry_run',
        default=False,
        help='Show what would be done without creating drafts')
    review.add_argument(
        '-d', '--create-drafts', action='store_true',
        help='Create Gmail draft emails for each review')
    review.add_argument(
        '--redraft', action='store_true',
        help='Recreate Gmail drafts from the stored reviews even when a '
             'draft already exists, to recover after an error')
    review.add_argument(
        '--gmail-account', type=str, default=None,
        help='Gmail account to create drafts in (e.g. user@gmail.com)')
    review.add_argument(
        '--no-cover', action='store_true',
        help='Skip reviewing the cover letter')
    review.add_argument(
        '--reviewer', type=str, default=None,
        help="Override reviewer identity (format: 'Name <email>')")
    review.add_argument(
        '-U', '--upstream', type=str, default=None,
        help='Upstream name (for patchwork URL lookup)')
    review.add_argument(
        '-b', '--base-branch', type=str, default=None,
        help="Base branch to apply review patches onto (e.g. 'us/master', "
             "'us/next'). If unset, picks the upstream's '/next' branch "
             "when it has commits ahead of '/master', otherwise '/master'.")
    review.add_argument(
        '--apply-only', action='store_true',
        help='Only download and apply patches, skip AI review')
    review.add_argument(
        '--coverity', action='store_true',
        help='Run Coverity on the base and the series, and feed the new '
             'defects into the review (needs the cov-* tools on PATH)')
    review.add_argument(
        '--coverity-defconfig', type=str, default=None,
        help='Board defconfig to build for --coverity '
             '(default: sandbox_defconfig)')
    review.add_argument(
        '--signoff', type=str, default='',
        help="Sign-off for reviews with comments (from .patman settings)")
    review.add_argument(
        '--spelling', type=str, default='British',
        help="Spelling convention for review comments (from .patman "
             "settings)")
    review.add_argument(
        '--learn-voice', type=str, nargs='?', const='gmail',
        choices=['gmail', 'patchwork'],
        help="Analyse past reviews to build a voice profile "
             "(from 'gmail' or 'patchwork', default: gmail)")
    review.add_argument(
        '--voice-count', type=int, default=20,
        help='Number of review emails/comments to collect for '
             '--learn-voice (default: 20)')
    review.add_argument(
        '--sync', action='store_true',
        help='Check if review drafts have been sent and record the '
             'final email content')
    review.add_argument(
        '--scan', action='store_true',
        help='Scan patchwork for new versions of already-reviewed series '
             'and review the latest version once it has fully appeared')
    review.add_argument(
        '--relink', action='store_true',
        help='Repair the database by merging review series that were split '
             'across versions, so follow-up reviews see earlier feedback')
    review.add_argument(
        '-j', '--jobs', type=int, default=4,
        help='Number of series to review in parallel with --scan '
             '(default: 4)')
    review.add_argument(
        '-f', '--force', action='store_true',
        help='Force re-review even if the series was already reviewed')
    review.add_argument(
        '--any-state', action='store_true',
        help='Review the series even if no patch is in an active state '
             '(new, RFC, under-review, changes-requested or '
             'needs-review-ack)')
    review.add_argument(
        '-c', '--context', type=str, default=None,
        help="Extra context for the review agent — e.g. 'this is RFC, "
             "ignore whitespace'. Use '@path' to read from a file.")
    review.add_argument(
        '--model', type=str, default=None,
        help="Claude model to review with (e.g. 'sonnet', 'opus' or a full "
             "model id). Overrides your global Claude default; set 'model' "
             "in .patman to pin it. Defaults to your Claude default.")
    review.add_argument(
        '--list-models', action='store_true',
        help='List the model aliases that --model accepts, then exit')
    return review


def setup_parser():
    """Set up command-line parser

    Returns:
        argparse.Parser object
    """
    epilog = '''Create patches from commits in a branch, check them and email
        them as specified by tags you place in the commits. Use -n to do a dry
        run first.'''

    parser = ErrorCatchingArgumentParser(epilog=epilog)
    parser.add_argument(
        '-D', '--debug', action='store_true',
        help='Enabling debugging (provides a full traceback on error)')
    parser.add_argument(
        '-N', '--no-capture', action='store_true',
        help='Disable capturing of console output in tests')
    parser.add_argument('-p', '--project', default=project.detect_project(),
                        help="Project name; affects default option values and "
                        "aliases [default: %(default)s]")
    parser.add_argument('-P', '--patchwork-url',
                        default='https://patchwork.ozlabs.org',
                        help='URL of patchwork server [default: %(default)s]')
    parser.add_argument(
        '-T', '--thread', action='store_true', dest='thread',
        default=False, help='Create patches as a single thread')
    parser.add_argument(
        '-v', '--verbose', action='store_true', dest='verbose', default=False,
        help='Verbose output of errors and warnings')
    parser.add_argument(
        '-X', '--test-preserve-dirs', action='store_true',
        help='Preserve and display test-created directories')
    parser.add_argument(
        '-H', '--full-help', action='store_true', dest='full_help',
        default=False, help='Display the README file')

    subparsers = parser.add_subparsers(dest='cmd')
    add_send_subparser(subparsers)
    patchwork = add_patchwork_subparser(subparsers)
    review = add_review_subparser(subparsers)
    series = add_series_subparser(subparsers)
    add_status_subparser(subparsers)
    upstream = add_upstream_subparser(subparsers)
    workflow = add_workflow_subparser(subparsers)

    # Only add the 'test' action if the test data files are available.
    if HAS_TESTS:
        test_parser = subparsers.add_parser('test', help='Run tests')
        test_parser.add_argument('testname', type=str, default=None, nargs='?',
                                 help="Specify the test to run")

    parsers = {
        'main': parser,
        'review': review,
        'series': series,
        'patchwork': patchwork,
        'upstream': upstream,
        'workflow': workflow,
        }
    return parsers


def parse_args(argv=None, config_fname=None, parsers=None):
    """Parse command line arguments from sys.argv[]

    Args:
        argv (str or None): Arguments to process, or None to use sys.argv[1:]
        config_fname (str): Config file to read, or None for default, or False
            for an empty config

    Returns:
        tuple containing:
            options: command line options
            args: command lin arguments
    """
    if not parsers:
        parsers = setup_parser()
    parser = parsers['main']

    # Parse options twice: first to get the project and second to handle
    # defaults properly (which depends on project)
    # Use parse_known_args() in case 'cmd' is omitted
    if not argv:
        argv = sys.argv[1:]

    args, rest = parser.parse_known_args(argv)
    if hasattr(args, 'project'):
        settings.Setup(parser, args.project, argv, config_fname)
        args, rest = parser.parse_known_args(argv)

    # If we have a command, it is safe to parse all arguments
    if args.cmd:
        args = parser.parse_args(argv)
    elif not args.full_help:
        # No command, so insert it after the known arguments and before the ones
        # that presumably relate to the 'send' subcommand
        nargs = len(rest)
        argv = argv[:-nargs] + ['send'] + rest
        args = parser.parse_args(argv)

    # Resolve aliases
    for full, aliases in ALIASES.items():
        if args.cmd in aliases:
            args.cmd = full
        if 'subcmd' in args and args.subcmd in aliases:
            args.subcmd = full
    if args.cmd in ['series', 'upstream', 'patchwork', 'workflow'] and not args.subcmd:
        parser.parse_args([args.cmd, '--help'])

    return args
