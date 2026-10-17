# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2025 Canonical Ltd.
# Written by Simon Glass <simon.glass@canonical.com>
#

# pylint: disable=R0914

"""Sending and revising reviews which are already stored

Once a series has been reviewed, its reviews are kept in the database. They
can be sent as replies to the patches, or revised by an agent following a
note from the reviewer, with their Gmail drafts recreated to match.
"""

import os
import subprocess
import tempfile

from u_boot_pylib import claude as claude_mod
from u_boot_pylib import gitutil
from u_boot_pylib import tout

from patman import database
from patman import gmail
from patman.review import apply
from patman.review import common
from patman.review import drafts
from patman.review import emails
from patman.review import lookup
from patman.review import prompts


def do_stored(args, pwork, cser, link):
    """Handle --send and --revise, which act on stored reviews

    Args:
        args (Namespace): Command-line arguments
        pwork (Patchwork): Configured patchwork instance
        cser (Cseries): Open cseries instance
        link (str): Patchwork series link/ID

    Returns:
        int: 0 on success, 1 on failure
    """
    if args.send:
        return _do_send(args, cser, link)
    return _do_revise(args, pwork, cser, link)


def _do_send(args, cser, link):
    """Send the Gmail drafts of the reviews of a series

    The cover letter's review is sent first, then the patches' in order.
    With -i, only those patches' drafts are sent. Each review is recorded as
    sent, with the text which was sent, since the draft may have been
    changed in Gmail.

    Args:
        args (Namespace): Command-line arguments
        cser (Cseries): Open cseries instance
        link (str): Patchwork series link/ID

    Returns:
        int: 0 on success, 1 if any draft could not be sent
    """
    existing = cser.db.series_find_by_link(link)
    if not existing:
        raise ValueError(f'Series {link} has not been reviewed')
    _, name, version, svid = existing
    sel = common.parse_patch_selection(args.patches)
    reviews = sorted((rev for rev in cser.db.review_get_for_version(svid)
                      if rev.status == database.REVIEW_DRAFT and
                      rev.draft_id and (not sel or rev.seq in sel)),
                     key=lambda rev: rev.seq)
    if not reviews:
        raise ValueError(f"No Gmail drafts to send in '{name}' v{version}")
    if args.dry_run:
        for rev in reviews:
            tout.notice(f'Dry run: would send the review of '
                        f'{_review_label(rev.seq)}')
        return 0
    if not gmail.check_available():
        return 1

    service = gmail.get_service(getattr(args, 'gmail_account', None))
    failed = 0
    for rev in reviews:
        label = _review_label(rev.seq)
        try:
            result = gmail.send_draft(service, rev.draft_id)
        except Exception as exc:  # pylint: disable=W0718
            tout.error(f'Cannot send the review of {label}: {exc}')
            failed += 1
            continue
        if not result:
            tout.warning(f'The draft of the review of {label} has gone; '
                         'not sent')
            failed += 1
            continue
        body, msg_id, thread_id = result
        cser.db.review_set_sent(rev.idnum, body or rev.body, msg_id,
                                thread_id)
        cser.commit()
        tout.notice(f'Sent the review of {label}')
    tout.notice(f'Sent {len(reviews) - failed} of {len(reviews)} review(s)')
    return 1 if failed else 0


def _review_label(seq):
    """Describe which part of a series a review is for

    Args:
        seq (int): Patch number, 0 for the cover letter

    Returns:
        str: e.g. 'patch 2' or 'the cover letter'
    """
    return f'patch {seq}' if seq else 'the cover letter'


def _review_commits(args, cser, link, ups):
    """Get the commits holding the patches of a reviewed series

    The review branch is left in place after a review, so the patches can be
    read from it. It may be based on next or master (see series_base()), so
    the base is whichever of these leaves the fewest commits on the branch.

    Args:
        args (Namespace): Command-line arguments (for base_branch)
        cser (Cseries): Open cseries instance
        link (str): Patchwork series link/ID
        ups (str or None): Upstream name

    Returns:
        list of str: One line for each commit, 'hash subject', oldest first,
            or an empty list if the branch is not present
    """
    branch = lookup.make_review_name(link, ups)
    git_dir = gitutil.get_top_level()
    if not gitutil.ref_exists(branch, git_dir):
        return []
    bases = [apply.get_upstream_branch(args, cser)]
    if ups:
        bases += [f'{ups}/next', apply.main_branch(ups, git_dir)]
    counts = {}
    for base in bases:
        count = gitutil.count_revs(git_dir, f'{base}..{branch}')
        if count:
            counts[base] = count
    if not counts:
        return []
    base = min(counts, key=counts.get)
    out = subprocess.run(
        ['git', 'log', '--reverse', '--format=%H %s', f'{base}..{branch}'],
        capture_output=True, text=True, check=False, cwd=git_dir).stdout
    return out.splitlines()


async def revise_reviews(ctx, reviews, note, commits, others=None,
                         session=None):
    """Ask the agent to change some reviews, following a note from the reviewer

    The agent is also given the cover letter, the other reviews of the series,
    the comments it has received and the reviewer's writing voice, if known,
    so that it writes as the original review did. When a single review is
    revised, the agent can carry on from the session which wrote it, seeing
    everything that session did; a copy of the session is used, so the
    original is left as it was. If the session cannot be resumed, e.g.
    because Claude Code has removed it, a new session is used.

    Args:
        ctx (ReviewContext): Review context (uses repo_path, reviewer_tag,
            spelling, cover_content and comments_path)
        reviews (dict): Map of seq to review body text
        note (str): What the reviewer wants changed
        commits (list of str): 'hash subject' for each patch, in order
        others (dict or None): Map of seq to the body of each other review of
            the series, which the agent sees but does not change
        session (str or None): ID of the agent session which wrote the
            review, to resume, or None to start a new session

    Returns:
        dict or None: Map of seq to new review body text, for each review
            which changed, or None if the agent is not available or failed
    """
    if not claude_mod.check_available():
        return None
    parts = []
    for seq in sorted(reviews):
        parts += [f'---SEQ {seq}---', reviews[seq]]
    if commits:
        lines = '\n'.join(f'Patch {seq}: {line}'
                          for seq, line in enumerate(commits, 1))
        patches = f'The patches are these commits:\n{lines}'
    else:
        patches = 'The patches are not available in the repository.'
    with tempfile.NamedTemporaryFile('w', prefix='patman_revise_',
                                     suffix='.txt', delete=False) as outf:
        outf.write('\n'.join(parts))
        reviews_path = outf.name
    try:
        context = prompts.revise_context(ctx.cover_content, others,
                                         ctx.comments_path)
        prompt = prompts.REVISE_PROMPT.format(
            note=note, reviews_path=reviews_path, patches=patches,
            context=context, tag=ctx.reviewer_tag, spelling=ctx.spelling,
            voice=prompts.voice_section())
        kwargs = {'allowed_tools': ['Bash', 'Read', 'Grep'],
                  'cwd': ctx.repo_path,
                  'max_buffer_size': claude_mod.MAX_BUFFER_SIZE}
        success = False
        if session:
            tout.notice('Revising reviews, carrying on from the review...')
            options = common.agent_options(resume=session, fork_session=True,
                                           **kwargs)
            success, log = await claude_mod.run_agent_collect(prompt, options)
            if not success:
                tout.notice('Could not resume the review session; starting '
                            'a new one')
        if not success:
            tout.notice('Revising reviews...')
            options = common.agent_options(**kwargs)
            success, log = await claude_mod.run_agent_collect(prompt, options)
    finally:
        os.unlink(reviews_path)
    if not success:
        return None
    return {seq: body for seq, body in emails.parse_refined_output(log).items()
            if seq in reviews and body and body != reviews[seq].strip()}


def revise_reviews_sync(ctx, reviews, note, commits, others=None,
                        session=None):
    """Synchronous wrapper for revise_reviews()"""
    return common.run(revise_reviews(ctx, reviews, note, commits, others,
                                     session))


def _read_series_context(ctx, pwork, link):
    """Read the cover letter and comments of a series, for a revision

    Problems reading patchwork are reported but do not stop the revision,
    which can go ahead without this context.

    Args:
        ctx (ReviewContext): Review context, whose series_data,
            cover_content and comments_path are set
        pwork (Patchwork): Configured patchwork instance
        link (str): Patchwork series link/ID
    """
    try:
        ctx.series_data = lookup.fetch_series(pwork, link)[0]
        ctx.cover_content = lookup.fetch_cover_content(pwork, ctx.series_data)
        ctx.comments_path = lookup.write_comments_file(ctx.series_data, pwork)
    except (ValueError, OSError) as exc:
        tout.warning(f'Could not read the series from patchwork: {exc}')


def _do_revise(args, pwork, cser, link):
    """Change stored reviews of a series, following a note from the reviewer

    Only reviews which have not been sent are changed. With -i, only those
    patches' reviews are given to the agent. With -d, the Gmail drafts of the
    reviews which change are recreated.

    Args:
        args (Namespace): Command-line arguments
        pwork (Patchwork): Configured patchwork instance
        cser (Cseries): Open cseries instance
        link (str): Patchwork series link/ID

    Returns:
        int: 0 on success, 1 on failure
    """
    existing = cser.db.series_find_by_link(link)
    if not existing:
        raise ValueError(f'Series {link} has not been reviewed')
    _, name, version, svid = existing
    sel = common.parse_patch_selection(args.patches)
    all_reviews = cser.db.review_get_for_version(svid)
    reviews = [rev for rev in all_reviews
               if rev.status not in (database.REVIEW_SENT,
                                     database.REVIEW_REPLIED) and
               (not sel or rev.seq in sel)]
    if not reviews:
        raise ValueError(f"No unsent reviews to revise in '{name}' "
                         f'v{version}')

    ctx = common.ReviewContext(pwork, cser, {})
    ctx.reviewer_name, ctx.reviewer_email = common.parse_reviewer(args)
    ctx.repo_path = gitutil.get_top_level()
    ctx.spelling = args.spelling
    ups = pwork.upstream if pwork else None
    commits = _review_commits(args, cser, link, ups)
    if pwork:
        _read_series_context(ctx, pwork, link)
    chosen = {rev.seq for rev in reviews}
    others = {rev.seq: rev.body for rev in all_reviews
              if rev.seq not in chosen}
    # A single review can carry on from the session which wrote it
    session = (cser.db.review_get_session(reviews[0].idnum)
               if len(reviews) == 1 else None)
    try:
        changed = revise_reviews_sync(
            ctx, {rev.seq: rev.body for rev in reviews}, args.revise, commits,
            others, session)
    finally:
        if ctx.comments_path and os.path.exists(ctx.comments_path):
            os.unlink(ctx.comments_path)
    if changed is None:
        tout.error('Could not revise the reviews')
        return 1
    if not changed:
        tout.notice('No reviews needed changing')
        return 0

    for rev in reviews:
        if rev.seq in changed:
            body = changed[rev.seq]
            cser.db.review_set_body(rev.idnum, body,
                                    'Reviewed-by:' in body)
            tout.notice(f'Revised the review of {_review_label(rev.seq)}')
            print(body)
            print()
    cser.commit()

    if args.create_drafts:
        redo = [cser.db.review_get(rev.idnum) for rev in reviews
                if rev.seq in changed]
        drafts.delete_gmail_drafts(args, redo)
        if not ctx.series_data:
            ctx.series_data = lookup.fetch_series(pwork, link)[0]
        drafts.create_drafts(ctx, args, {rev.seq: rev.body for rev in redo},
                             {rev.seq: rev.idnum for rev in redo})
    return 0
