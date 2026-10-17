# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2025 Canonical Ltd.
# Written by Simon Glass <simon.glass@canonical.com>
#

# pylint: disable=R0914

"""The main flow of 'patman review' and 'patman series review'

This finds a series on patchwork, applies it to a local branch, has a Claude
agent review each patch and the cover letter, and stores the reviews,
optionally as Gmail drafts. It also reviews a local series, and passes the
other review commands on to the modules which handle them.
"""

from datetime import datetime
import fcntl
import os
import shutil
import subprocess
import tempfile

from u_boot_pylib import claude as claude_mod
from u_boot_pylib import gitutil
from u_boot_pylib import terminal
from u_boot_pylib import tools
from u_boot_pylib import tout

from patman import coverity
from patman import cser_helper
from patman import database
from patman import patchstream
from patman import workflow
from patman.review import apply
from patman.review import common
from patman.review import drafts
from patman.review import emails
from patman.review import lookup
from patman.review import prompts
from patman.review import replies
from patman.review import scan
from patman.review import stored
from patman.review import voice


def _read_context(spec):
    """Resolve a --context argument value to literal text

    Args:
        spec (str): Either a literal string, or '@path' to read from
            a file (path is expanded for '~')

    Return:
        str: The context text to pass to the agent
    """
    if spec.startswith('@'):
        path = os.path.expanduser(spec[1:])
        return tools.read_file(path, binary=False).rstrip()
    return spec


async def _run_cover_review(ctx, all_commits):
    """Run the review agent on the cover letter and parse its output

    Args:
        ctx (ReviewContext): Review context (uses cover_content etc.)
        all_commits (list): (seq, hash, subject) tuples

    Returns:
        tuple or None: (greeting, verdict, comments), or None if the agent
            failed or chose to skip
    """
    tout.notice('Reviewing series (cover letter)...')
    prompt = prompts.build_cover_review_prompt(
        ctx, all_commits, previous_review=ctx.previous_reviews.get(0))
    options = common.session_options(
        ctx, 0, allowed_tools=['Bash', 'Read', 'Grep'], cwd=ctx.repo_path,
        max_buffer_size=claude_mod.MAX_BUFFER_SIZE)
    success, log = await claude_mod.run_agent_collect(prompt, options)
    if not success or not log.strip():
        return None
    greeting, verdict, comments = emails.parse_review_output(log)
    if verdict == 'skip':
        return None
    return greeting, verdict, comments


async def _review_cover_letter(ctx, all_commits):
    """Review the cover letter / series as a whole

    Args:
        ctx (ReviewContext): Review context (uses cover_content etc.)
        all_commits (list): (seq, hash, subject) tuples

    Returns:
        str or None: Review body, or None if skipped
    """
    result = await _run_cover_review(ctx, all_commits)
    if result is None:
        return None
    greeting, verdict, comments = result
    return emails.format_review_email(ctx, greeting, verdict, comments)


async def _run_patch_review(ctx, cmt, seq, all_commits):
    """Run the review agent on one patch and parse its output

    Args:
        ctx (ReviewContext): Review context
        cmt: Commit object with hash, subject, msg, rtags
        seq (int): Patch sequence number (1-based)
        all_commits (list): (seq, hash, subject) tuples

    Returns:
        tuple or None: (greeting, verdict, comments, commit_msg), or None
            if the agent failed
    """
    body = cmt.msg.strip()
    if body.startswith(cmt.subject):
        commit_msg = body
    else:
        commit_msg = (cmt.subject + '\n' + body).strip()
    # Keep the leading space of each line, so the quoted lines line up
    ctx.diffstat = gitutil.diff_stat(f'{cmt.hash}~..{cmt.hash}',
                                     ctx.repo_path).strip('\n')

    previous_review = ctx.previous_reviews.get(seq)
    prompt = prompts.build_review_prompt(ctx, cmt.hash, seq, all_commits,
                                         previous_review)
    options = common.session_options(
        ctx, seq, allowed_tools=['Bash', 'Read', 'Grep'], cwd=ctx.repo_path,
        max_buffer_size=claude_mod.MAX_BUFFER_SIZE)
    success, log = await claude_mod.run_agent_collect(prompt, options)
    if not success or not log.strip():
        return None
    greeting, verdict, comments = emails.parse_review_output(log)
    return greeting, verdict, comments, commit_msg


async def _review_single_patch(ctx, cmt, seq, all_commits):
    """Review a single patch

    Args:
        ctx (ReviewContext): Review context
        cmt: Commit object with hash, subject, msg, rtags
        seq (int): Patch sequence number (1-based)
        all_commits (list): (seq, hash, subject) tuples

    Returns:
        str or None: Review body text, or None if there is nothing to say
    """
    result = await _run_patch_review(ctx, cmt, seq, all_commits)
    if result is None:
        return '(Review failed — please review manually)'
    greeting, verdict, comments, commit_msg = result
    # A non-approval with no comments has nothing to say (just a greeting
    # and the quoted commit message); drop it rather than sending an empty
    # review
    if verdict != 'approved' and not comments:
        return None
    return emails.format_review_email(
        ctx, greeting, verdict, comments, commit_msg)


async def review_patches(ctx):
    """Run AI review on each patch in the applied branch

    Args:
        ctx (ReviewContext): Review context (uses branch_name,
            upstream_branch, patch_count, cover_content,
            previous_reviews, repo_path, patch_selection, etc.)

    Returns:
        dict: Map of patch index (1-based) to review body
    """
    if not claude_mod.check_available():
        return {}

    commit_range = f'{ctx.upstream_branch}..{ctx.branch_name}'
    # The branch is a shared ref, so use the main repo's .git directory
    # — the worktree's .git is a pointer file, not a real git_dir
    git_dir = os.path.join(ctx.main_repo, '.git')
    series = patchstream.get_metadata_for_list(commit_range, git_dir=git_dir)
    all_commits = [(i + 1, cmt.hash, cmt.subject)
                   for i, cmt in enumerate(series.commits)]
    commits = [c[1] for c in all_commits]

    if len(commits) != ctx.patch_count:
        tout.warning(f'Expected {ctx.patch_count} patches but found '
                     f'{len(commits)} commits on {ctx.branch_name}')

    review_bodies = {}

    patch_sel = getattr(ctx, 'patch_selection', None)

    if ctx.cover_content and ctx.patch_count > 1 and not patch_sel:
        body = await _review_cover_letter(ctx, all_commits)
        if body:
            review_bodies[0] = body
    # Check which patches already have stored reviews
    existing_reviews = set()
    if hasattr(ctx, 'svid') and ctx.svid:
        for rev in ctx.cser.db.review_get_for_version(ctx.svid):
            existing_reviews.add(rev.seq)

    # Map each commit to its patchwork patch number by subject. The
    # applied branch may hold fewer commits than the series has patches
    # (e.g. one failed to apply), so a positional index would attach a
    # review to the wrong patchwork patch
    patches = ctx.series_data.get('patches', [])
    seq_by_subject = {}
    for i, patch in enumerate(patches):
        subject = lookup.clean_series_name(patch.get('name', ''))
        seq_by_subject.setdefault(subject, i + 1)

    total = ctx.patch_count or len(commits)
    reviewer_tag = ctx.reviewer_tag
    for i, cmt in enumerate(series.commits):
        seq = seq_by_subject.get(cmt.subject)
        if seq is None:
            tout.warning(f"Commit '{cmt.subject}' matches no patchwork "
                         'patch; attaching by position')
            seq = i + 1

        if patch_sel and seq not in patch_sel:
            continue

        if seq in existing_reviews:
            tout.notice(f'Skipping patch {seq}/{total}'
                        ' (already in database)')
            continue

        if (reviewer_tag in cmt.rtags.get('Reviewed-by', set()) or
                reviewer_tag in cmt.rtags.get('Tested-by', set())):
            tout.notice(f'Skipping patch {seq}/{total}'
                        ' (already reviewed)')
            continue

        tout.notice(f'Reviewing patch {seq}/{total}...')

        body = await _review_single_patch(ctx, cmt, seq, all_commits)
        if body:
            review_bodies[seq] = body
        else:
            tout.notice(f'  Nothing to say on patch {seq}; skipping')

    return review_bodies


def review_patches_sync(ctx):
    """Synchronous wrapper for review_patches()

    Returns:
        dict: Map of patch index (1-based) to review body text
    """
    return common.run(review_patches(ctx))


def _run_patch_review_sync(ctx, cmt, seq, all_commits):
    """Synchronous wrapper for _run_patch_review()"""
    return common.run(_run_patch_review(ctx, cmt, seq, all_commits))


def _run_cover_review_sync(ctx, all_commits):
    """Synchronous wrapper for _run_cover_review()"""
    return common.run(_run_cover_review(ctx, all_commits))


def _store_finding(ctx, seq, subject, verdict, comments, timestamp):
    """Store and show the findings of a quick review of one part of a series

    Commit-message comments are put before code comments.

    Args:
        ctx (ReviewContext): Review context (uses cser, svid, patch_count)
        seq (int): Patch number, or 0 for the cover letter
        subject (str): Subject of the patch, or 'cover letter'
        verdict (str): Verdict from the agent, e.g. 'approved'
        comments (list of tuple): (quote, comment) for each comment
        timestamp (str): Time of the review

    Returns:
        int: Number of comments
    """
    comments = sorted(comments, key=lambda hc: emails.is_code_comment(hc[0]))
    body = emails.cleanup_review_text(emails.format_findings(verdict,
                                                             comments))
    rid = ctx.cser.db.review_add(ctx.svid, seq, body, verdict == 'approved',
                                 timestamp)
    ctx.cser.db.review_set_session(rid, ctx.session_ids.get(seq))
    emails.print_finding(seq, ctx.patch_count, subject, verdict, comments)
    return len(comments)


def review_series(cser, series_id, svid, version, branch, series,
                  spelling='British', context=None):
    """AI-review a local series' commits and store the reviews

    Reviews each commit on the branch in place (no worktree, since the
    branch is already checked out) and stores the findings in the
    database, keyed by the ser_ver id, so they can be viewed later with
    'patman series info -r'. Reviews from the previous version, if any,
    are provided as context.

    Args:
        cser (Cseries): Open cseries instance
        series_id (int): Series ID
        svid (int): ser_ver id to attach the reviews to
        version (int): Series version number
        branch (str): Branch holding the series
        series (Series): Series object with a .commits list
        spelling (str): Spelling convention for comments
        context (str or None): Extra context for the review agent
    """
    if not claude_mod.check_available():
        raise ValueError(
            "The review agent is not available; install the 'review' extra "
            '(pip install patch-manager[review])')

    commits = series.commits
    if not commits:
        tout.notice('No commits to review')
        return

    repo = gitutil.get_top_level()
    ctx = common.ReviewContext(None, cser, {})
    ctx.main_repo = repo
    ctx.repo_path = repo
    ctx.branch_name = branch
    ctx.upstream_branch = f'{branch}~{len(commits)}'
    ctx.patch_count = len(commits)
    ctx.series_id = series_id
    ctx.svid = svid
    ctx.version = version
    ctx.spelling = spelling
    ctx.context = _read_context(context) if context else None
    if series.cover:
        ctx.cover_content = '\n'.join(series.cover)
    for rev in cser.db.review_get_previous(series_id, version):
        ctx.previous_reviews[rev.seq] = rev.body

    all_commits = [(i + 1, cmt.hash, cmt.subject)
                   for i, cmt in enumerate(commits)]
    timestamp = datetime.now().isoformat()
    total = len(commits)
    n_comments = 0

    if ctx.cover_content and total > 1:
        result = _run_cover_review_sync(ctx, all_commits)
        if result is not None:
            _, verdict, comments = result
            n_comments += _store_finding(ctx, 0, 'cover letter', verdict,
                                         comments, timestamp)

    for i, cmt in enumerate(commits):
        seq = i + 1
        tout.notice(f'Reviewing patch {seq}/{total}...')
        result = _run_patch_review_sync(ctx, cmt, seq, all_commits)
        if result is None:
            tout.warning(f'  Review failed for patch {seq}')
            continue
        _, verdict, comments, _ = result
        n_comments += _store_finding(ctx, seq, cmt.subject, verdict,
                                     comments, timestamp)
    cser.commit()

    tout.notice('')
    tout.notice(f"Stored review of '{branch}' v{version}: {total} patch(es), "
                f"{n_comments} comment(s). View with 'patman series info -r'")


def _show_reviews(reviews, series_data):
    """Display review bodies

    Args:
        reviews: Either an iterable of Review records (with seq, body,
            approved attributes) or a dict mapping seq to body text
        series_data (dict): Series data from patchwork
    """
    patches = series_data.get('patches', [])
    cover = series_data.get('cover_letter')

    if isinstance(reviews, dict):
        items = [(seq, body, 'Reviewed-by:' in body)
                 for seq, body in sorted(reviews.items())]
    else:
        items = [(rev.seq, rev.body, rev.approved)
                 for rev in reviews]

    for seq, body, approved in items:
        if seq == 0:
            label = 'Cover letter'
            name = cover.get('name', '') if cover else 'Cover'
        elif seq <= len(patches):
            label = f'Patch {seq}/{len(patches)}'
            name = patches[seq - 1].get('name', '')
        else:
            label = f'Patch {seq}'
            name = ''
        colour = (terminal.Color.GREEN if approved else
                  terminal.Color.YELLOW)
        terminal.tprint(f'\n--- {label}: {name} ---', colour=colour)
        print('---email start---')
        print(body)
        print('---email end---')
        print()


def _register_series(cser, clean_name, version, link, series_data,
                     upstream=None):
    """Register a series in the database for review

    Creates or finds the series record, adds a ser_ver entry and pcommit
    records for each patch.

    Args:
        cser (Cseries): Open cseries instance
        clean_name (str): Cleaned series name
        version (int): Series version number
        link (str): Patchwork series link/ID
        series_data (dict): Series data from patchwork
        upstream (str or None): Upstream name for branch naming

    Returns:
        tuple or None: (series_id, svid) or None if already reviewed
    """
    existing = cser.db.series_find_by_link(link)
    if existing:
        return None

    # Adding the series row, its version and its patches must be atomic.
    # These are all uncommitted until the commit() below, so if anything
    # in between raises we roll back; otherwise a freshly-added series row
    # would linger uncommitted and be flushed by the next unrelated
    # commit() as an orphan -- a series with no ser_ver row
    try:
        prev = cser.db.series_find_review_by_name(clean_name)
        if prev:
            series_id, db_name, prev_version = prev
            tout.notice(f"Previously reviewed '{db_name}' v{prev_version};"
                        f" adding v{version}")
        else:
            branch_name = lookup.make_review_name(link, upstream)
            series_id = cser.db.series_find_by_name(
                branch_name, include_archived=True)
            if not series_id:
                # Store the cleaned title (stable across versions) as the
                # desc, not the raw '[vN,0/M] ...' cover-letter subject, so
                # later versions link via series_find_review_by_name()
                series_id = cser.db.series_add(branch_name, clean_name,
                                               ups=upstream)
            cser.db.series_set_source(series_id, 'review')

        svid = cser.db.ser_ver_add(series_id, version, link=str(link))
        cover = series_data.get('cover_letter')
        if cover and cover.get('id'):
            cser.db.ser_ver_set_info(database.SerVer(
                svid, None, None, None, cover['id'], 0, cover.get('name'),
                None, None, None))

        patches = series_data.get('patches', [])
        pcommits = []
        for i, patch in enumerate(patches):
            pcommits.append(database.Pcommit(idnum=None, seq=i,
                subject=patch.get('name', ''), svid=svid, change_id=None,
                state=None, patch_id=patch.get('id'), num_comments=0))
        if pcommits:
            cser.db.pcommit_add_list(svid, pcommits)

            # pcommit_add_list only stores seq/subject/change_id; update
            # patch_id from the patchwork data
            pclist = cser.db.pcommit_get_list(svid)
            for pcm, patch in zip(pclist, patches):
                patch_id = patch.get('id')
                if patch_id:
                    cser.db.pcommit_update(database.Pcommit(
                        idnum=pcm.idnum, seq=pcm.seq, subject=pcm.subject,
                        svid=svid, change_id=pcm.change_id, state=pcm.state,
                        patch_id=patch_id, num_comments=pcm.num_comments))

        cser.commit()
    except Exception:
        cser.rollback()
        raise
    tout.notice(f"Added series '{clean_name}' v{version} to database")
    return series_id, svid


def _run_coverity(ctx, args):
    """Analyse the series with Coverity and summarise the new defects

    Builds and analyses the base branch and the patched branch, then
    returns a bullet-list summary of the defects the series introduces,
    for use as review context.

    Args:
        ctx (ReviewContext): Review context (uses main_repo, repo_path,
            upstream_branch)
        args (Namespace): Command-line arguments (coverity_defconfig)

    Returns:
        str or None: Summary of new defects, or None if Coverity is
            unavailable or finds nothing new
    """
    if not coverity.check_available():
        tout.warning('Coverity tools (cov-build/cov-analyze) not found on '
                     'PATH; skipping --coverity')
        return None

    defconfig = getattr(args, 'coverity_defconfig', None) or \
        coverity.DEFAULT_DEFCONFIG
    tout.notice(f'Running Coverity ({defconfig}) on the base and the '
                'series; this builds twice and may take a while...')
    with tempfile.TemporaryDirectory() as tmp:
        base_wt = os.path.join(tmp, 'base')
        # Check out the base detached in its own worktree so the build
        # does not disturb the review worktree
        subprocess.run(
            ['git', '-C', ctx.main_repo, 'worktree', 'add', '--detach',
             base_wt, ctx.upstream_branch],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True)
        try:
            base = coverity.analyze(base_wt, defconfig,
                                    os.path.join(tmp, 'cov-base'))
            patched = coverity.analyze(ctx.repo_path, defconfig,
                                       os.path.join(tmp, 'cov-patched'))
        finally:
            gitutil.remove_worktree(ctx.main_repo, base_wt)

    new = coverity.find_new_defects(base, patched)
    if not new:
        tout.notice('Coverity: no new defects introduced by the series')
        return None
    tout.notice(f'Coverity: {len(new)} new defect(s) introduced by the '
                'series')
    return coverity.format_defects(new)


def _run_and_store_reviews(ctx, args):
    """Run AI review, refine, store and display results

    Args:
        ctx (ReviewContext): Review context with all fields populated
        args (Namespace): Command-line arguments (for create_drafts flag)
    """
    prev_db = ctx.cser.db.review_get_previous(ctx.series_id, ctx.version)
    for rev in prev_db:
        ctx.previous_reviews[rev.seq] = rev.body

    if ctx.cover_content is None:
        ctx.cover_content = lookup.fetch_cover_content(
            ctx.pwork, ctx.series_data)

    review_bodies = review_patches_sync(ctx)

    if ctx.comments_path and os.path.exists(ctx.comments_path):
        os.unlink(ctx.comments_path)

    for seq in review_bodies:
        review_bodies[seq] = emails.cleanup_review_text(review_bodies[seq])
    review_bodies = emails.refine_reviews_sync(review_bodies, ctx.spelling)

    timestamp = datetime.now().isoformat()
    review_ids = {}
    for seq, body in review_bodies.items():
        approved = 'Reviewed-by:' in body
        review_ids[seq] = ctx.cser.db.review_add(ctx.svid, seq, body, approved,
                                                 timestamp)
        ctx.cser.db.review_set_session(review_ids[seq],
                                       ctx.session_ids.get(seq))
    ctx.cser.commit()

    _show_reviews(review_bodies, ctx.series_data)

    if args.create_drafts:
        drafts.create_drafts(ctx, args, review_bodies, review_ids)
    else:
        tout.notice('Use --create-drafts to create Gmail review drafts.')


def _find_or_register(ctx, args, clean_name, link):
    """Register a series, handling existing reviews

    If the series was already reviewed, shows the stored reviews and
    optionally creates drafts. With --force, deletes old reviews and
    re-registers.

    Args:
        ctx (ReviewContext): Review context (uses pwork, cser, series_data,
            version)
        args (Namespace): Command-line arguments
        clean_name (str): Cleaned series name
        link (str): Patchwork series link/ID

    Returns:
        tuple or None: (series_id, svid) or None if already reviewed and
            not forcing
    """
    ups = ctx.pwork.upstream if ctx.pwork else None
    result = _register_series(ctx.cser, clean_name, ctx.version, link,
                              ctx.series_data, upstream=ups)
    if result is not None:
        return result

    existing = ctx.cser.db.series_find_by_link(link)
    if not existing:
        return None

    series_id, _, _, svid = existing
    reviews = ctx.cser.db.review_get_for_version(svid)

    if not reviews:
        # Interrupted previous attempt — resume with existing record
        tout.notice('Resuming incomplete review')
        return series_id, svid

    # When reviewing specific patches, allow adding to existing reviews
    patch_sel = common.parse_patch_selection(args.patches)
    if patch_sel:
        reviewed_seqs = {r.seq for r in reviews}
        new_seqs = patch_sel - reviewed_seqs
        if new_seqs:
            tout.notice(f'Adding review for patch(es) '
                        f'{", ".join(str(s) for s in sorted(new_seqs))}')
            return series_id, svid

    if not args.force:
        _, db_name, db_version, _ = existing
        tout.notice(f"Already reviewed: '{db_name}' v{db_version}")
        _show_reviews(reviews, ctx.series_data)
        if args.create_drafts or args.redraft:
            drafts.draft_stored_reviews(args, reviews, ctx.series_data,
                                        ctx.pwork, ctx.cser)
        return None

    if args.create_drafts:
        # The old drafts would otherwise be orphaned in Gmail when the
        # review records that track them are deleted below
        drafts.delete_gmail_drafts(args, reviews)
    ctx.cser.db.review_delete_for_version(svid)
    ctx.cser.commit()
    tout.notice('Re-reviewing (forced)')
    return series_id, svid


class ReviewInProgressError(Exception):
    """Raised when another review of the same series is already running"""


def _acquire_review_lock(repo, branch_name):
    """Take an exclusive lock for a series review

    The lock is a file locked with flock(), so the kernel releases it
    automatically when patman exits or crashes, leaving no stale locks.
    It stops two reviews of the same series running at once, whether from
    --scan, parallel workers or a separate patman invocation.

    Args:
        repo (str): Top-level dir of the main checkout
        branch_name (str): Review branch name, identifying the series

    Returns:
        int: Open file descriptor holding the lock; pass it to
            _release_review_lock() when done

    Raises:
        ReviewInProgressError: if the lock is already held
    """
    path = os.path.join(repo, '.git', 'patman', 'locks',
                        f'{branch_name}.lock')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        raise ReviewInProgressError(branch_name) from exc
    return fd


def _release_review_lock(fd):
    """Release a review lock taken with _acquire_review_lock()

    Args:
        fd (int): File descriptor returned by _acquire_review_lock()
    """
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)


def _review_link(args, pwork, cser, link):
    """Run the main review flow for a single patchwork series

    Fetches the series, registers it (detecting an already-reviewed series
    or a new version), applies the patches in a worktree and reviews them.
    The series is locked for the duration so a concurrent review of the
    same series is refused rather than corrupting its worktree or records.

    Args:
        args (Namespace): Command-line arguments
        pwork (Patchwork): Configured patchwork instance
        cser (Cseries): Open cseries instance
        link (str): Patchwork series link/ID

    Returns:
        int: 0 on success, 1 if applying the patches failed

    Raises:
        ValueError: if the series is incomplete (not all patches present)
    """
    ups = pwork.upstream if pwork else None
    branch_name = lookup.make_review_name(link, ups)
    main_repo = gitutil.get_top_level()
    try:
        lock_fd = _acquire_review_lock(main_repo, branch_name)
    except ReviewInProgressError:
        tout.warning(
            f"A review of '{branch_name}' is already in progress; skipping")
        return 0

    try:
        series_data, clean_name, version, patch_count = \
            lookup.fetch_series(pwork, link)

        if not getattr(args, 'any_state', False):
            states = lookup.get_patch_states(pwork, link)
            if not any(state in lookup.ACTIVE_STATES for state in states):
                shown = ', '.join(sorted({s for s in states if s})) or 'none'
                raise ValueError(
                    f"Series '{clean_name}' is not active on patchwork "
                    f'(patch states: {shown}); use --any-state to review it '
                    'anyway')

        ctx = common.ReviewContext(pwork, cser, series_data)
        ctx.version = version
        ctx.patch_count = patch_count

        result = _find_or_register(ctx, args, clean_name, link)
        if result is None:
            return 0
        ctx.series_id, ctx.svid = result

        ctx.upstream_branch = apply.get_upstream_branch(args, cser)
        ctx.main_repo = main_repo
        if not args.base_branch:
            ctx.upstream_branch = apply.series_base(
                ctx, args.upstream or cser.db.upstream_get_default(),
                ctx.upstream_branch) or ctx.upstream_branch
        ctx.branch_name = branch_name
        wt_path = cser_helper.review_worktree_path(ctx.main_repo,
                                                   ctx.branch_name)
        tout.notice(f'Using review worktree {wt_path}')
        ctx.repo_path = gitutil.ensure_worktree(
            ctx.main_repo, wt_path, ctx.branch_name, ctx.upstream_branch)

        apply_ok = apply.apply_and_check(ctx, link)
        if apply_ok is None:
            # Every patch is already upstream; nothing to review
            return 0
        if not apply_ok:
            return 1

        if args.apply_only:
            tout.notice('Apply-only mode; skipping review')
            return 0

        ctx.patch_selection = common.parse_patch_selection(args.patches)
        ctx.reviewer_name, ctx.reviewer_email = common.parse_reviewer(args)
        ctx.signoff = args.signoff or None
        if ctx.signoff:
            ctx.signoff = ctx.signoff.replace('\\n', '\n')
        ctx.spelling = args.spelling
        ctx.context = _read_context(args.context) if args.context else None
        ctx.comments_path = lookup.write_comments_file(series_data, pwork)

        if getattr(args, 'coverity', False):
            ctx.coverity_text = _run_coverity(ctx, args)

        _run_and_store_reviews(ctx, args)
        workflow.reviewed(cser, ctx.series_id, ctx.svid)
        gitutil.remove_worktree(ctx.main_repo, wt_path)

        return 0
    finally:
        _release_review_lock(lock_fd)


def _do_relink(args, cser):
    """Merge review series that were split across versions

    Older patman stored each version of a series under its own record,
    keyed by the raw '[vN,0/M] ...' cover-letter subject, so the versions
    of one series did not link and follow-up reviews had no earlier
    feedback for context. Group the review series by their cleaned title,
    merge each group into a single series holding all the versions and
    clean the stored description. The database is backed up first.

    Args:
        args (Namespace): Command-line arguments (unused)
        cser (Cseries): Open cseries instance

    Returns:
        int: 0 on success
    """
    backup = f'{cser.db.db_path}.bak'
    shutil.copy2(cser.db.db_path, backup)
    tout.notice(f'Backed up database to {backup}')

    series = cser.db.series_get_dict(include_archived=True, reviews_only=True)
    groups = {}
    for ser in series.values():
        groups.setdefault(lookup.clean_series_name(ser.desc), []).append(ser)

    merged = 0
    cleaned = 0
    for clean, group in groups.items():
        group.sort(key=lambda ser: ser.idnum)
        canon = group[0]
        if canon.desc != clean:
            cser.db.series_set_desc(canon.idnum, clean)
            cleaned += 1
        for other in group[1:]:
            for svid in cser.db.ser_ver_get_svids(other.idnum):
                cser.db.ser_ver_set_series(svid, canon.idnum)
            cser.db.series_remove(other.idnum)
            merged += 1
    cser.commit()
    tout.notice(f'Relinked {merged} duplicate series record(s); cleaned '
                f'{cleaned} description(s)')
    return 0


# Model aliases accepted by --model, most to least capable. Each resolves
# to the latest model in its tier, so this list does not go stale as new
# generations ship. The Claude SDK and CLI expose no way to enumerate
# models, and the REST models API needs an API key that need not match the
# subscription a review authenticates with, so a fixed alias list is both
# the most reliable answer and exactly what --model expects
_MODEL_ALIASES = (
    ('opus', 'Most capable; best for a thorough review'),
    ('sonnet', 'Balanced capability and speed'),
    ('haiku', 'Fastest and cheapest; light reviews'),
)


def _list_models():
    """Print the model aliases that --model accepts

    Returns:
        int: 0
    """
    print("Models you can pass to --model (or set as the 'model' setting):")
    print()
    for alias, desc in _MODEL_ALIASES:
        print(f'  {alias:<8} {desc}')
    print()
    print('Each alias selects the latest model in its tier. A full model')
    print('id (e.g. claude-sonnet-5) also works. With none set, your global')
    print('Claude default is used.')
    return 0


def do_review(args, pwork, cser):
    """Run the review command

    Dispatches to learn-voice, sync, scan, relink, or the main review
    flow which fetches, applies, reviews and optionally drafts.

    Args:
        args (Namespace): Command-line arguments
        pwork (Patchwork): Configured patchwork instance
        cser (Cseries): Open cseries instance
    """
    if getattr(args, 'list_models', False):
        return _list_models()

    # Fix the model for the whole run, so every agent uses the one chosen
    # with --model / the 'model' setting rather than the user's default
    common.set_agent_model(getattr(args, 'model', None))

    if args.learn_voice:
        return voice.do_learn_voice(args, pwork)

    if args.sync:
        return replies.do_sync(args, cser)

    if (getattr(args, 'list_replies', False) or
            getattr(args, 'suggest_reply', None) or
            getattr(args, 'draft_reply', None) or
            getattr(args, 'send_reply', None)):
        return replies.do_reply_cmd(args, cser)

    if args.relink:
        return _do_relink(args, cser)

    if args.scan:
        return scan.do_scan(args, pwork, cser)

    has_patch = getattr(args, 'patch', None)
    has_patch_title = getattr(args, 'patch_title', None)
    if not args.pw_link and not args.title and not has_patch and \
            not has_patch_title:
        raise ValueError("Please provide -s <series>, -S <title>, "
            "-p <patch-id> or -P <patch-title>")

    # -p/-P locate the series via one of its patches. By default the review
    # is then restricted to just that patch; -w reviews the whole series
    whole = getattr(args, 'whole_series', False)
    link = args.pw_link
    if not link and has_patch:
        link, patch_seq = lookup.lookup_patch_series(pwork, args.patch)
        if not whole:
            args.patches = str(patch_seq)
    elif not link and has_patch_title:
        link, patch_seq = lookup.search_patch(pwork, args.patch_title)
        if not whole:
            args.patches = str(patch_seq)
    elif not link:
        link = lookup.search_series(
            pwork, args.title, getattr(args, 'version', None))

    if getattr(args, 'send', False) or getattr(args, 'revise', None):
        return stored.do_stored(args, pwork, cser, link)
    return _review_link(args, pwork, cser, link)
