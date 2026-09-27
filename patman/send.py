# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2025 Google LLC
#
"""Handles the 'send' subcommand
"""

import email
import email.utils
import os
import sys

from patman import checkpatch
from patman import patchstream
from patman import relay
from patman import settings
from u_boot_pylib import gitutil
from u_boot_pylib import terminal
from u_boot_pylib import tout


def check_patches(series, patch_files, run_checkpatch, verbose, use_tree, cwd):
    """Run some checks on a set of patches

    This santiy-checks the patman tags like Series-version and runs the patches
    through checkpatch

    Args:
        series (Series): Series object for this series (set of patches)
        patch_files (list): List of patch filenames, each a string, e.g.
            ['0001_xxx.patch', '0002_yyy.patch']
        run_checkpatch (bool): True to run checkpatch.pl
        verbose (bool): True to print out every line of the checkpatch output as
            it is parsed
        use_tree (bool): If False we'll pass '--no-tree' to checkpatch.
        cwd (str): Path to use for patch files (None to use current dir)

    Returns:
        bool: True if the patches had no errors, False if they did
    """
    # Do a few checks on the series
    series.DoChecks()

    # Check the patches
    if run_checkpatch:
        ok = checkpatch.check_patches(verbose, patch_files, use_tree, cwd)
    else:
        ok = True
    return ok


def _send_endpoint(args):
    """Return the web endpoint to send through, honouring --no-relay

    Args:
        args (argparse.Namespace): Command-line arguments

    Returns:
        str or None: The endpoint URL, or None to use git send-email
    """
    if getattr(args, 'no_relay', False):
        return None
    return getattr(args, 'send_endpoint_web', None)


def _require_endpoint(endpoint):
    """Return the endpoint, or raise if none is configured

    Args:
        endpoint (str or None): The configured web submission endpoint

    Returns:
        str: The endpoint URL

    Raises:
        ValueError: if no endpoint is set
    """
    if not endpoint:
        raise ValueError(
            'No web endpoint set; use --send-endpoint-web or set '
            'send_endpoint_web in .patman')
    return endpoint


def _parse_cc_file(cc_file):
    """Parse the Cc file written by Series.MakeCcFile()

    Each line is '<filename> <cc1>\\0<cc2>...' -- the filename, a space,
    then the Cc addresses joined by NUL (so addresses may contain spaces).

    Args:
        cc_file (str): Path to the Cc file

    Returns:
        dict: filename -> list of Cc addresses
    """
    result = {}
    with open(cc_file, encoding='utf-8') as fd:
        for line in fd:
            line = line.rstrip('\n')
            if not line:
                continue
            fname, _, rest = line.partition(' ')
            result[fname] = [cc for cc in rest.split('\0') if cc]
    return result


def _from_domain(msg):
    """Return the domain of a message's From address, or None"""
    addr = email.utils.parseaddr(msg.get('From', ''))[1]
    return addr.split('@', 1)[1] if '@' in addr else None


def _apply_threading(msgs, thread, in_reply_to):
    """Add In-Reply-To/References headers to thread the series

    The first message (cover letter, or first patch if there is none) is
    the root. With thread=True the remaining messages reply to it, as
    'git send-email --thread' does (shallow threading). If in_reply_to is
    set, the root replies to that message id.

    Args:
        msgs (list of email.message.Message): Messages in send order
        thread (bool): True to thread the patches under the root
        in_reply_to (str or None): Message id the series replies to
    """
    if not msgs:
        return
    root = msgs[0]
    if in_reply_to:
        irt = in_reply_to if in_reply_to.startswith('<') else f'<{in_reply_to}>'
        del root['In-Reply-To']
        del root['References']
        root['In-Reply-To'] = irt
        root['References'] = irt
    if not thread:
        return
    root_id = root['Message-ID']
    base_refs = root['References']
    for msg in msgs[1:]:
        del msg['In-Reply-To']
        del msg['References']
        msg['In-Reply-To'] = root_id
        msg['References'] = f'{base_refs} {root_id}' if base_refs else root_id


def send_via_relay(series, cover_fname, patch_files, cc_file, endpoint,
                   reflect, dry_run, thread=False, in_reply_to=None, cwd=None):
    """Send a prepared series through a web submission endpoint (relay)

    Builds an email for each patch (and the cover letter) with the To and
    Cc from the computed recipients, attests each with patatt and posts
    them to the web endpoint. Used instead of 'git send-email' when a
    send endpoint is configured.

    Args:
        series (Series): Series object for this series
        cover_fname (str or None): Cover-letter filename
        patch_files (list of str): Patch filenames
        cc_file (str): Cc file written by Series.MakeCcFile()
        endpoint (str): Web submission endpoint URL
        reflect (bool): True to reflect the series back to the sender only
        dry_run (bool): True to show what would be sent without posting
        thread (bool): True to thread the patches under the cover/first
            patch, as 'git send-email --thread' does
        in_reply_to (str or None): Message id the series replies to
        cwd (str): Directory holding the patch files (None for current)

    Returns:
        int: Number of messages sent (0 for a dry run)

    Raises:
        ValueError: if the endpoint errors
    """
    cc_map = _parse_cc_file(cc_file)
    to_list = gitutil.build_email_list(series.get('to') or [], settings.alias)

    fnames = ([cover_fname] if cover_fname else []) + list(patch_files)
    msgs = []
    for fname in fnames:
        path = os.path.join(cwd, fname) if cwd else fname
        with open(path, 'rb') as fd:
            msg = email.message_from_bytes(fd.read())
        del msg['To']
        del msg['Cc']
        if to_list:
            msg['To'] = ', '.join(to_list)
        cc = cc_map.get(fname)
        if cc:
            msg['Cc'] = ', '.join(cc)
        if not msg['X-Mailer']:
            msg['X-Mailer'] = 'patman'
        if not msg['Message-ID']:
            msg['Message-ID'] = email.utils.make_msgid(
                domain=_from_domain(msg))
        msgs.append(msg)

    _apply_threading(msgs, thread, in_reply_to)

    if dry_run:
        verb = 'reflect' if reflect else 'send'
        tout.notice(f'Dry run: would {verb} {len(msgs)} message(s) via '
                    f'{endpoint}')
        return 0

    # Sign only when actually sending -- signing invokes gpg/patatt
    messages = [relay.sign_message(msg.as_bytes()).decode() for msg in msgs]
    return relay.submit(endpoint, messages, reflect=reflect)


def email_patches(col, series, cover_fname, patch_files, process_tags, its_a_go,
                  ignore_bad_tags, add_maintainers, get_maintainer_script, limit,
                  dry_run, in_reply_to, thread, smtp_server, identity=None,
                  cwd=None, endpoint=None, reflect=False):
    """Email patches to the recipients

    This emails out the patches and cover letter using 'git send-email'. Each
    patch is copied to recipients identified by the patch tag and output from
    the get_maintainer.pl script. The cover letter is copied to all recipients
    of any patch.

    To make this work a CC file is created holding the recipients for each patch
    and the cover letter. See the main program 'cc_cmd' for this logic.

    Args:
        col (terminal.Color): Colour output object
        series (Series): Series object for this series (set of patches)
        cover_fname (str): Filename of the cover letter as a string (None if
            none)
        patch_files (list): List of patch filenames, each a string, e.g.
            ['0001_xxx.patch', '0002_yyy.patch']
        process_tags (bool): True to process subject tags in each patch, e.g.
            for 'dm: spi: Add SPI support' this would be 'dm' and 'spi'. The
            tags are looked up in the configured sendemail.aliasesfile and also
            in ~/.patman (see README)
        its_a_go (bool): True if we are going to actually send the patches,
            False if the patches have errors and will not be sent unless
            @ignore_errors
        ignore_bad_tags (bool): True to just print a warning for unknown tags,
            False to halt with an error
        add_maintainers (bool): Run the get_maintainer.pl script for each patch
        get_maintainer_script (str): The script used to retrieve which
            maintainers to cc
        limit (int): Limit on the number of people that can be cc'd on a single
            patch or the cover letter (None if no limit)
        dry_run (bool): Don't actually email the patches, just print out what
            would be sent
        in_reply_to (str): If not None we'll pass this to git as --in-reply-to.
            Should be a message ID that this is in reply to.
        thread (bool): True to add --thread to git send-email (make all patches
            reply to cover-letter or first patch in series)
        smtp_server (str): SMTP server to use to send patches (None for default)
        identity (str or None): Git sendemail identity to use
        cwd (str): Path to use for patch files (None to use current dir)

    Return:
        Git command that was/would be run
    """
    cc_file = series.MakeCcFile(process_tags, cover_fname, not ignore_bad_tags,
                                add_maintainers, limit, get_maintainer_script,
                                settings.alias, cwd)

    # Email the patches out (giving the user time to check / cancel)
    cmd = ''
    num_sent = 0
    if its_a_go:
        if endpoint:
            num_sent = send_via_relay(
                series, cover_fname, patch_files, cc_file, endpoint,
                reflect, dry_run, thread=thread, in_reply_to=in_reply_to,
                cwd=cwd)
            cmd = f'(web relay {endpoint})'
        else:
            cmd, num_sent = gitutil.email_patches(
                series, cover_fname, patch_files, dry_run, not ignore_bad_tags,
                cc_file, alias=settings.alias, in_reply_to=in_reply_to,
                thread=thread, smtp_server=smtp_server, identity=identity,
                cwd=cwd)
    else:
        print(col.build(col.RED, "Not sending emails due to errors/warnings"))

    # For a dry run, just show our actions as a sanity check
    if dry_run:
        series.ShowActions(patch_files, cmd, process_tags, settings.alias)
        if not its_a_go:
            print(col.build(col.RED, "Email would not be sent"))

    os.remove(cc_file)
    return cmd, num_sent


def prepare_patches(col, branch, count, start, end, ignore_binary, signoff,
                    keep_change_id=False, git_dir=None, cwd=None,
                    insert_base_commit=True):
    """Figure out what patches to generate, then generate them

    The patch files are written to the current directory, e.g. 0001_xxx.patch
    0002_yyy.patch

    Args:
        col (terminal.Color): Colour output object
        branch (str): Branch to create patches from (None = current)
        count (int): Number of patches to produce, or -1 to produce patches for
            the current branch back to the upstream commit
        start (int): Start patch to use (0=first / top of branch)
        end (int): End patch to use (0=last one in series, 1=one before that,
            etc.)
        ignore_binary (bool): Don't generate patches for binary files
        keep_change_id (bool): Preserve the Change-Id tag.
        git_dir (str): Path to git repository (None to use default)
        cwd (str): Path to use for git operations (None to use current dir)
        insert_base_commit (bool): True to add the 'base-commit'/'branch'
            trailers to the patches / cover letter

    Returns:
        Tuple:
            Series object for this series (set of patches)
            Filename of the cover letter as a string (None if none)
            patch_files: List of patch filenames, each a string, e.g.
                ['0001_xxx.patch', '0002_yyy.patch']
    """
    if count == -1:
        # Work out how many patches to send if we can
        count = (gitutil.count_commits_to_branch(branch, git_dir=git_dir) -
                 start)

    if not count:
        msg = 'No commits found to process - please use -c flag, or run:\n' \
              '  git branch --set-upstream-to remote/branch'
        sys.exit(col.build(col.RED, msg))

    # Read the metadata from the commits
    to_do = count - end
    series = patchstream.get_metadata(branch, start, to_do, git_dir)
    cover_fname, patch_files = gitutil.create_patches(
        branch, start, to_do, ignore_binary, series, signoff, git_dir=git_dir,
        cwd=cwd)

    # Fix up the patch files to our liking, and insert the cover letter
    patchstream.fix_patches(
        series, patch_files, keep_change_id,
        insert_base_commit=insert_base_commit and not cover_fname, cwd=cwd)
    if cover_fname and series.get('cover'):
        patchstream.insert_cover_letter(
            cover_fname, series, to_do, cwd=cwd,
            insert_base_commit=insert_base_commit)
    return series, cover_fname, patch_files


def send(args, git_dir=None, cwd=None):
    """Create, check and send patches by email

    Args:
        args (argparse.Namespace): Arguments to patman
        cwd (str): Path to use for git operations

    Return:
        bool: True if the patches were likely sent, else False
    """
    col = terminal.Color()

    # Web-endpoint registration is a standalone action; it does not
    # prepare or send any patches
    endpoint = getattr(args, 'send_endpoint_web', None)
    if getattr(args, 'web_auth_new', False):
        relay.auth_new(_require_endpoint(endpoint))
        return True
    if getattr(args, 'web_auth_verify', None):
        relay.auth_verify(_require_endpoint(endpoint), args.web_auth_verify)
        return True

    series, cover_fname, patch_files = prepare_patches(
        col, args.branch, args.count, args.start, args.end,
        args.ignore_binary, args.add_signoff,
        keep_change_id=args.keep_change_id, git_dir=git_dir, cwd=cwd,
        insert_base_commit=getattr(args, 'insert_base_commit', True))

    series_to = getattr(args, 'series_to', None)
    if series_to:
        to_list = series.get('to', [])
        if to_list and series_to not in to_list:
            raise ValueError(
                f"Series-to tag {to_list} does not match "
                f"expected '{series_to}' from upstream settings")
        if not to_list:
            series['to'] = [series_to]

    ok = check_patches(series, patch_files, args.check_patch,
                       args.verbose, args.check_patch_use_tree, cwd)

    ok = ok and gitutil.check_suppress_cc_config()

    identity = getattr(args, 'identity', None)
    if identity:
        print(f"Using sendemail identity '{identity}'")

    its_a_go = ok or args.ignore_errors
    cmd, num_sent = email_patches(
        col, series, cover_fname, patch_files, args.process_tags,
        its_a_go, args.ignore_bad_tags, args.add_maintainers,
        args.get_maintainer_script, args.limit, args.dry_run,
        args.in_reply_to, args.thread, args.smtp_server,
        identity=identity, cwd=cwd,
        endpoint=_send_endpoint(args),
        reflect=getattr(args, 'reflect', False))

    return num_sent > 0
