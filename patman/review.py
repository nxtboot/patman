# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2025 Canonical Ltd.
# Written by Simon Glass <simon.glass@canonical.com>
#

# pylint: disable=C0302,R0914

"""AI-powered patch review using Claude.

Fetches patches from patchwork, applies them to a local branch using
a Claude agent, and (in later stages) reviews each patch and creates
Gmail draft replies.
"""

import asyncio
from collections import namedtuple
from concurrent import futures
from datetime import datetime
import fcntl
import os
import re
import shutil
import subprocess
import sys
import tempfile

import aiohttp

from u_boot_pylib import claude as claude_mod
from u_boot_pylib import gitutil
from u_boot_pylib import terminal
from u_boot_pylib import tools
from u_boot_pylib import tout

from patman import coverity
from patman import cser_helper
from patman import database
from patman import gmail
from patman import patchstream
from patman import workflow

try:
    from claude_agent_sdk import ClaudeAgentOptions
except ImportError:
    ClaudeAgentOptions = None

# Claude model to use for the current review run, or None to let the SDK
# use whatever model the user's Claude default resolves to. Set once at the
# start of do_review() from --model / the 'model' setting, then read by
# _agent_options() so every agent in the run uses the same model
_AGENT_MODEL = None


def _agent_options(**kwargs):
    """Create ClaudeAgentOptions with the selected review model applied

    A model chosen with --model (or the 'model' setting) overrides the
    user's global Claude default for the whole review run, so a review
    always uses the intended model even if the user's default is something
    else. With no model selected the SDK default is used unchanged.

    The SDK prefers the Claude Code CLI bundled with it, which may be too
    old to use the current models. Use the installed 'claude' instead, if
    there is one, so that updating Claude Code is enough to keep reviews
    working.

    Args:
        kwargs: Fields to pass to ClaudeAgentOptions

    Returns:
        ClaudeAgentOptions: Configured options
    """
    if _AGENT_MODEL:
        kwargs.setdefault('model', _AGENT_MODEL)
    cli = shutil.which('claude')
    if cli:
        kwargs.setdefault('cli_path', cli)
    return ClaudeAgentOptions(**kwargs)


class ReviewContext:  # pylint: disable=R0902
    """Common context for review operations

    Attributes:
        reviewer_name (str): Reviewer's name
        reviewer_email (str): Reviewer's email
        series_data (dict): Series data from patchwork
        main_repo (str): Top-level of the user's main checkout (where
            the .git dir lives and where shared refs are resolved)
        repo_path (str): Path to the per-review worktree (used as cwd
            for apply/build/agent)
        signoff (str or None): Sign-off text for reviews with comments
        spelling (str): Spelling convention
        comments_path (str or None): Path to existing patchwork comments
        pwork (Patchwork or None): Patchwork instance
        cser (Cseries or None): Open cseries instance
        series_id (int or None): Series database ID
        svid (int or None): ser_ver database ID
        version (int or None): Series version number
        branch_name (str or None): Branch with applied patches
        upstream_branch (str or None): Upstream branch ref
        patch_count (int or None): Number of patches
        patch_selection (set of int or None): Patch indices to review
            (None means all)
        context (str or None): Extra user-supplied context to pass to
            the review agent (e.g. 'this is RFC, ignore whitespace')
    """

    def __init__(self, pwork, cser, series_data):
        self.pwork = pwork
        self.cser = cser
        self.series_data = series_data
        self.reviewer_name = None
        self.reviewer_email = None
        self.main_repo = None
        self.repo_path = None
        self.signoff = None
        self.spelling = 'British'
        self.comments_path = None
        self.series_id = None
        self.svid = None
        self.version = None
        self.branch_name = None
        self.upstream_branch = None
        self.patch_count = None
        self.patch_selection = None
        self.cover_content = None
        self.previous_reviews = {}
        self.diffstat = None
        self.context = None
        self.coverity_text = None

    @property
    def reviewer_tag(self):
        """Get 'Name <email>' string for the reviewer"""
        return f'{self.reviewer_name} <{self.reviewer_email}>'

    @property
    def author_name(self):
        """Get the series submitter's name"""
        return self.series_data.get('submitter', {}).get('name', '')

    @property
    def author_email(self):
        """Get the series submitter's email"""
        return self.series_data.get('submitter', {}).get('email', '')

    @property
    def date(self):
        """Get the series date string"""
        return self.series_data.get('date', '')


async def fetch_mbox(pwork, link):
    """Download the series mbox file from patchwork

    Args:
        pwork (Patchwork): Patchwork instance to fetch from
        link (str): Patchwork series link/ID

    Returns:
        str: Path to the downloaded mbox file

    Raises:
        ValueError: if the download fails
    """
    tout.notice(f'Downloading mbox for series {link} from {pwork.url}')
    mbox_path = os.path.join(tempfile.gettempdir(),
                             f'patman_review_{link}.mbox')
    async with aiohttp.ClientSession() as client:
        content = await pwork.get_series_mbox(client, link)
    if not content:
        raise ValueError(f'Empty mbox downloaded for series {link}')

    tools.write_file(mbox_path, content)
    tout.notice(f'Downloaded {len(content)} bytes to {mbox_path}')
    return mbox_path


def _build_apply_prompt(mbox_path, branch_name, upstream_branch):
    """Build the Claude agent prompt for applying patches

    The cwd is already a per-review worktree on branch '{branch_name}'
    reset to '{upstream_branch}', so the agent just needs to run git am
    and handle any conflicts.

    Args:
        mbox_path (str): Path to the downloaded mbox file
        branch_name (str): Name of the (already-checked-out) review branch
        upstream_branch (str): Upstream ref the branch is based on

    Returns:
        str: The prompt text for the agent
    """
    return f'''Apply a patch series from a patchwork mbox \
file to the current branch.

You are in a git worktree on branch '{branch_name}', already reset to
'{upstream_branch}'. Do NOT create a new branch.

TASK:
1. Apply the patches from the mbox file:
   git am {mbox_path}

2. If git am fails, try this recovery sequence:

   a. First, abort the failed git am:
      git am --abort

   b. Split the mbox into individual patches:
      mkdir -p /tmp/patches_{branch_name}
      git mailsplit -o /tmp/patches_{branch_name} {mbox_path}

   c. For each patch file (in order), try to apply it:
      git am /tmp/patches_{branch_name}/<file>

      If that fails, abort and try 'patch' with fuzz:
        git am --abort
        git mailinfo /tmp/msg /tmp/diff < /tmp/patches_{branch_name}/<file>
        patch -p1 --fuzz=3 < /tmp/diff

      If patch also fails or has rejects (.rej files), read the .rej
      file and the target source file, then apply the changes manually
      using the Edit tool. The .rej file shows what was expected and
      what to add/remove — find the corresponding location in the
      current source and make the equivalent change.

      After fixing up (whether via patch or manually):
        git add -A
        # Extract subject and body from the mail headers
        git commit --author="$(head -1 /tmp/msg | sed 's/^Author: //')" \
          -m "$(sed -n 's/^Subject: //p' /tmp/msg)" -m "$(tail -n+3 /tmp/msg)"

      If a patch is completely irrelevant (e.g. already applied),
      skip it and note which patch was skipped.

3. After all patches are applied (or skipped), run:
   git log --oneline {upstream_branch}..HEAD

4. Report the result:
   - How many patches were applied successfully
   - Which patches (if any) were skipped and why
   - The branch name: {branch_name}

IMPORTANT:
- Do NOT modify the patch content — apply it as-is, adapting only to
  context changes (moved lines, renamed variables, etc.)
- Do NOT use 'git am --abort' unless you are about to retry differently
- If you skip a patch, continue with the remaining patches
- The mbox file is at: {mbox_path}
'''


async def apply_series(pwork, link, branch_name, upstream_branch,
                       repo_path):
    """Download and apply a patch series to a new local branch

    Uses the Claude agent to handle the git am process, including conflict
    resolution.

    Args:
        pwork (Patchwork): Patchwork instance to fetch from
        link (str): Patchwork series link/ID
        branch_name (str): Name for the new branch
        upstream_branch (str): Branch to base from
        repo_path (str): Path to the git repository

    Returns:
        tuple: (success, branch_name) where success is bool and
            branch_name is the name of the created branch
    """

    if not claude_mod.check_available():
        return False, None

    # Download the mbox
    mbox_path = await fetch_mbox(pwork, link)

    # Build the prompt and run the agent
    prompt = _build_apply_prompt(mbox_path, branch_name, upstream_branch)
    options = _agent_options(
        allowed_tools=['Bash', 'Read', 'Grep', 'Edit', 'Write'],
        cwd=repo_path, max_buffer_size=claude_mod.MAX_BUFFER_SIZE)

    tout.notice(f'Applying series to branch {branch_name}...')
    success, _ = await claude_mod.run_agent_collect(prompt, options)

    if os.path.exists(mbox_path):
        os.unlink(mbox_path)

    return success, branch_name


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


def _build_review_prompt(ctx, commit_hash, seq, all_commits,
                         previous_review):
    """Build the Claude agent prompt for reviewing a single patch

    Args:
        ctx (ReviewContext): Review context (uses cover_content,
            comments_path, spelling)
        commit_hash (str): Git commit hash of the patch on the local branch
        seq (int): Patch sequence number (1-based)
        all_commits (list of tuple): (seq, hash, subject) for all patches
        previous_review (str or None): Previous review text (for v2+)

    Returns:
        str: The prompt text for the agent
    """
    cover_section = ''
    if ctx.cover_content:
        cover_section = f'''
SERIES CONTEXT (cover letter):
{ctx.cover_content}
'''

    prev_section = ''
    if previous_review:
        prev_section = f'''
PREVIOUS REVIEW (from earlier version):
{previous_review}

This is a FOLLOW-UP review. The reviewer convention is that the
initial review says what needs to be said; later versions should not
pile on with fresh nits the reviewer chose not to raise the first
time round. Apply that convention here:

- Confirm whether each point in the previous review was addressed
  (acknowledge it, or push back if it was not).
- Do NOT raise new issues in code that already existed in the
  previous version. If something was not flagged then, it is not
  fair to flag it now.
- It IS fair to comment on:
  * material the author added or substantially rewrote in this
    version (whether in response to feedback or otherwise), and
  * clear regressions introduced since the previous version.
- If the previous feedback is addressed and nothing new is wrong,
  approve. Prefer 'approved' over fishing for something to say.
'''

    voice = get_voice()
    voice_section = ''
    if voice:
        voice_section = f'''
WRITING STYLE:
Match this voice when writing your comments:
{voice}
'''

    # Build the series overview
    series_lines = []
    total = len(all_commits)
    for s, h, subj in all_commits:
        marker = '>>>' if s == seq else '   '
        series_lines.append(f"  {marker} {s}/{total} {h[:12]} {subj}")
    series_overview = '\n'.join(series_lines)

    comments_section = ''
    if ctx.comments_path:
        comments_section = f'''
EXISTING COMMENTS:
Other reviewers (or the author) may have already commented on this
series. Read the file below for context.
(see file: {ctx.comments_path})

CRITICAL: If another reviewer has already raised a point, do NOT repeat
it, rephrase it, or elaborate on it — even to add a suggestion. The
author has already been told. Simply skip that issue entirely and focus
on things that have NOT been said.
'''

    context_section = ''
    if ctx.context:
        context_section = f'''
USER CONTEXT (extra notes from the reviewer for this run):
{ctx.context}
'''

    coverity_section = ''
    if getattr(ctx, 'coverity_text', None):
        coverity_section = f'''
COVERITY (new static-analysis defects introduced by this series):
{ctx.coverity_text}

If any of these defects fall in the code this patch changes, raise them
in your review. Ignore defects unrelated to this patch.
'''

    return f'''You are an experienced U-Boot developer reviewing \
a patch submitted to
the U-Boot mailing list. This is patch {seq}/{len(all_commits)} in the series.
{voice_section}
SERIES OVERVIEW (all patches, >>> marks the one you are reviewing):
{series_overview}

You can run 'git show <hash>' on any of these to see the full diff.

TASK:
1. First, study the full series to understand the overall design and how
   the patches relate to each other. Run 'git show <hash>' for each
   patch in the series overview above, starting from patch 1. This
   gives you the context to spot cross-patch issues (e.g. something
   introduced in one patch that affects another).

2. Run: git show {commit_hash}
   Re-read the patch you are reviewing in detail.

3. Use Read and Grep tools to examine the surrounding source code for
   context. Look at the files being modified to understand existing
   patterns and verify the changes make sense.

IMPORTANT:
- Do NOT run 'git checkout' or switch branches
- Do NOT 'cd' to another directory
- The source tree is already on the correct branch with all patches applied
{comments_section}

4. Review the patch for:
   - Correctness: Does the code do what the commit message says? Are
     there logic errors, off-by-one errors, or missing error handling?
   - Style: Does it follow U-Boot coding conventions (kernel style,
     80-column lines, proper use of log categories, DM conventions)?
   - Commit message quality: Is it clear, using present/imperative
     tense? Does it explain the motivation?
   - API usage: Are U-Boot APIs used correctly?
{cover_section}{prev_section}{context_section}{coverity_section}
OUTPUT FORMAT:
Your response MUST use this exact structured format, with no other text
before or after. Start with a GREETING line containing the patch
author's first name (extracted from the commit's Author or
Signed-off-by line). If the name is not available, try to guess it from
the email address (e.g. 'simon.glass@xxx' -> Simon). If you still
cannot determine it, leave it empty.

Example for approved patch:

GREETING: Marek
VERDICT: approved

Example for patch with issues:

GREETING: J.
COMMENT:
> diff --git a/boot/bootm.c b/boot/bootm.c
> @@ -635,10 +633,12 @@ static int do_bootm_states(...)
> +	quoted diff line 1
> +	quoted diff line 2

Your comment about this code goes here. Be specific and constructive.

VERDICT: changes_needed

Rules:
- Always start with GREETING: (first name or empty)
- Each COMMENT: block MUST start with the two diff header lines
  copied VERBATIM from 'git show {commit_hash}':
    > diff --git a/<path> b/<path>
    > @@ -<old>,<n> +<new>,<m> @@ <function-context>
  then the quoted code lines (with '> ' prefix), then a blank line,
  then your comment. This is NOT optional — without the headers, the
  reader cannot tell which file or function the comment refers to.
  BAD:
    COMMENT:
    > +#include <foo.h>

    This include is unnecessary.
  GOOD:
    COMMENT:
    > diff --git a/drivers/clk/qcom/clock-ipq5210.c b/drivers/clk/qcom/clock-ipq5210.c
    > @@ -0,0 +1,97 @@
    > +#include <foo.h>

    This include is unnecessary.
- To comment on the commit message itself (not the code), quote the
  relevant commit-message line(s) with '> ' and no diff header. Such
  comments are placed before the code comments in the email
- Quote enough context from the diff to identify the location
- CRITICAL: Every quoted line MUST be copied EXACTLY from the output
  of 'git show {commit_hash}'. Do NOT reconstruct, paraphrase, or
  combine lines from memory. Do NOT mix content between nearby macros,
  functions or files. If you need to check a line before quoting it,
  run 'git show {commit_hash}' again and copy the characters exactly,
  including whitespace, punctuation, and backslashes. A comment that
  points at a problem in a quoted line that does not actually exist
  in the patch is worse than no comment at all.
- Be specific and constructive, but brief — use as few words as
  possible to make the point. Avoid restating what the code does;
- NEVER use backticks — this is plain-text email, not markdown.
  For functions, always use parentheses with no quotes: malloc() not
  'malloc()' or `malloc`. For all other quoting (identifiers,
  filenames, string literals, misspelled words, etc.) use single
  quotes: 'my_var' and 'handoff' not "my_var" or "handoff". Do not
  quote identifiers that are obviously code (e.g. CONFIG_FOO)
- These quoting and spelling conventions are for YOUR comment text
  only. In the '> ' quoted lines, reproduce the author's text exactly:
  never change double quotes to single, add or remove quotes, or
  restyle anything — a quote of "some_key" must stay "some_key"
- Never put a period directly after a code identifier — rephrase,
  omit the period, or use an em dash to start the next clause
- If another reviewer has already made a point, do NOT repeat it,
  rephrase it, or add to it — skip it entirely
- Focus on what is wrong and what to do instead
- Reference U-Boot conventions where applicable
- Do not nitpick trivial style issues that checkpatch would catch
- Focus on logic, correctness, and design issues
- If unsure about something, say so rather than guessing
- Use {ctx.spelling} spelling in your comments
- Write each comment paragraph on a single line — do not hard-wrap the
  prose; the reader's mail client will wrap it. Keep the quoted '> '
  lines and any indented code exactly as they are
- Always end with exactly one VERDICT: line (approved or changes_needed)
'''


def _build_cover_review_prompt(ctx, all_commits, previous_review=None):
    """Build prompt for reviewing the cover letter / series

    Args:
        ctx (ReviewContext): Review context (uses cover_content,
            comments_path, spelling)
        all_commits (list of tuple): (seq, hash, subject) for all patches
        previous_review (str or None): Previous cover-letter review text
            (for v2+ reviews)

    Returns:
        str: The prompt text
    """
    voice = get_voice()
    voice_section = ''
    if voice:
        voice_section = f'''
WRITING STYLE:
Match this voice when writing your comments:
{voice}
'''
    series_lines = []
    for s, h, subj in all_commits:
        series_lines.append(f"  {s}. {h[:12]} {subj}")
    series_overview = '\n'.join(series_lines)

    cover_section = ''
    if ctx.cover_content:
        cover_section = f'''
COVER LETTER:
{ctx.cover_content}
'''

    comments_section = ''
    if ctx.comments_path:
        comments_section = f'''
EXISTING COMMENTS:
(see file: {ctx.comments_path})
'''

    context_section = ''
    if ctx.context:
        context_section = f'''
USER CONTEXT (extra notes from the reviewer for this run):
{ctx.context}
'''

    prev_section = ''
    if previous_review:
        prev_section = f'''
PREVIOUS COVER-LETTER REVIEW (from earlier version):
{previous_review}

This is a FOLLOW-UP review. The reviewer convention is that the
initial review says what needs to be said; later versions should not
pile on with fresh nits the reviewer chose not to raise the first
time round. Confirm whether the previous points were addressed,
comment only on material that is new or substantially reworked in
this version, and prefer VERDICT: skip when there is nothing new to
add at the series level.
'''

    return f'''You are an experienced U-Boot developer reviewing a patch series
submitted to the U-Boot mailing list. Review the series as a whole,
replying to the cover letter.
{voice_section}
SERIES ({len(all_commits)} patches):
{series_overview}

You can run 'git show <hash>' on any of these to see the full diff.
{cover_section}{comments_section}{context_section}{prev_section}
TASK:
1. Read through all the patches (use 'git show <hash>' for each)
2. Review the series for:
   - Overall design and approach
   - Whether the series is split sensibly into patches
   - Whether the cover letter accurately describes what the series does
   - Any cross-patch issues (e.g. something introduced in patch 1 that
     is used incorrectly in patch 3)
   - Missing patches (e.g. documentation, tests, Kconfig updates)

IMPORTANT:
- Do NOT run 'git checkout' or switch branches
- Do NOT 'cd' to another directory
- Only comment on series-level issues; per-patch issues will be
  addressed in individual patch replies

OUTPUT FORMAT:
Same as for individual patches. Start with GREETING, then COMMENT
blocks (if any), then VERDICT.

If the series design is sound and there is nothing to add beyond
what will be said on individual patches, output ONLY:

VERDICT: skip

If there are series-level issues worth raising (design problems,
missing patches, poor splitting, cover letter inaccuracies), output:

GREETING: <name>
COMMENT:
> quoted text or description of the issue

Your comment.

VERDICT: changes_needed

Rules:
- NEVER use backticks — this is plain-text email, not markdown.
  For functions, always use parentheses with no quotes: malloc() not
  'malloc()' or `malloc`. For all other quoting (identifiers,
  filenames, string literals, misspelled words, etc.) use single
  quotes: 'my_var' and 'handoff' not "my_var" or "handoff". Do not
  quote identifiers that are obviously code (e.g. CONFIG_FOO)
- These quoting and spelling conventions are for YOUR comment text
  only. In the '> ' quoted lines, reproduce the author's text exactly:
  never change double quotes to single, add or remove quotes, or
  restyle anything — a quote of "some_key" must stay "some_key"
- Never put a period directly after a code identifier — rephrase,
  omit the period, or use an em dash to start the next clause
- Do NOT quote code fragments in the cover-letter reply — code
  belongs in the per-patch reviews. Describe series-level issues in
  prose only.
- Use {ctx.spelling} spelling
- Write each comment paragraph on a single line — do not hard-wrap the
  prose; the reader's mail client will wrap it
- Be brief — only raise series-level concerns, not per-patch nits
- Do NOT repeat issues that belong on individual patches
- VERDICT: skip means no cover letter reply will be sent
- Always end with exactly one VERDICT: line
'''


def _parse_greeting_verdict(text):
    """Extract greeting and verdict from agent output

    Args:
        text (str): Raw agent output text

    Returns:
        tuple: (greeting, verdict)
    """
    verdict = 'changes_needed'
    greeting = ''
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith('greeting:'):
            greeting = stripped.split(':', 1)[1].strip()
        elif stripped.lower().startswith('verdict:'):
            val = stripped.lower().split(':', 1)[1].strip()
            if val == 'approved':
                verdict = 'approved'
            elif val == 'skip':
                verdict = 'skip'
            break
    return greeting, verdict


def _parse_comments(text):
    """Extract COMMENT blocks from agent output

    Each COMMENT block contains optional quoted diff lines (prefixed with
    '> ') followed by the reviewer's comment.

    Args:
        text (str): Raw agent output text

    Returns:
        list of tuple: (hunk, comment) pairs
    """
    comments = []
    in_comment = False
    hunk_lines = []
    comment_lines = []
    past_hunk = False

    for line in text.splitlines():
        if line.strip().startswith('COMMENT:'):
            if hunk_lines or comment_lines:
                hunk = '\n'.join(hunk_lines)
                comment = '\n'.join(comment_lines).strip()
                comments.append((hunk, comment))
            hunk_lines = []
            comment_lines = []
            in_comment = True
            past_hunk = False
            continue

        if line.strip().lower().startswith('verdict:'):
            if in_comment and (hunk_lines or comment_lines):
                hunk = '\n'.join(hunk_lines)
                comment = '\n'.join(comment_lines).strip()
                comments.append((hunk, comment))
            in_comment = False
            continue

        if in_comment:
            if not past_hunk and line.startswith('> '):
                hunk_lines.append(line)
            elif not past_hunk and not line.strip() and hunk_lines:
                past_hunk = True
            else:
                past_hunk = True
                comment_lines.append(line)

    if in_comment and (hunk_lines or comment_lines):
        comments.append(('\n'.join(hunk_lines),
                         '\n'.join(comment_lines).strip()))
    return comments


def parse_review_output(text):
    """Parse structured review output from the agent

    Args:
        text (str): Raw agent output text

    Returns:
        tuple: (greeting, verdict, comments) where greeting is the author's
            first name (or ''), verdict is 'approved' or 'changes_needed',
            and comments is a
            list of (hunk, comment) tuples
    """
    greeting, verdict = _parse_greeting_verdict(text)
    comments = _parse_comments(text)
    return greeting, verdict, comments


def guess_name_from_email(email):
    """Guess a first name from an email address

    If the local part of the email contains a recognisable name (e.g.
    'simon.glass@xxx'), extract and capitalise it.

    Args:
        email (str): Email address

    Returns:
        str: Guessed first name, or '' if it cannot be determined
    """
    local = email.split('@')[0] if '@' in email else ''
    if not local:
        return ''
    part = local.split('.')[0].split('_')[0].split('-')[0]
    if part.isalpha() and len(part) >= 2:
        return part.capitalize()
    return ''


def _format_approved(ctx, commit_message=None, diffstat=None):
    """Format an approved review with no comments

    Quotes the commit message and diffstat, then adds a Reviewed-by tag

    Args:
        ctx (ReviewContext): Review context
        commit_message (str or None): Commit message to quote
        diffstat (str or None): Diffstat to quote

    Returns:
        str: Formatted email body
    """
    lines = [f'On {ctx.date}, {ctx.author_name} <{ctx.author_email}> wrote:']
    if commit_message:
        for cl in commit_message.strip().splitlines():
            lines.append(f'> {cl}')
    if diffstat:
        lines.append('>')
        for dl in diffstat.strip().splitlines():
            lines.append(f'> {dl}')
    lines += ['', f'Reviewed-by: {ctx.reviewer_tag}', '']
    return '\n'.join(lines)


def _is_code_comment(hunk):
    """Return True if a comment's quoted hunk refers to the code diff

    Code comments quote the diff and so include a 'diff --git' or '@@'
    header line; comments on the commit message quote prose without one.

    Args:
        hunk (str): Quoted lines for the comment (each prefixed with '> ')

    Returns:
        bool: True for a comment on the code, False for one on the commit
            message (or a general comment with no quoted code)
    """
    for line in hunk.splitlines():
        stripped = line.lstrip('> ').rstrip()
        if stripped.startswith('diff --git ') or stripped.startswith('@@ '):
            return True
    return False


def _match_quote_to_lines(hunk, msg_lines):
    """Find where a commit-message comment's quote ends in the message

    Args:
        hunk (str): The comment's quoted lines, each '> '-prefixed
        msg_lines (list of str): Lines of the commit message

    Returns:
        int or None: Index in msg_lines of the comment's last quoted line,
            so the comment can be shown right after it; None if the quote
            is not a commit-message quote or cannot be located there
    """
    if not hunk or not msg_lines:
        return None
    quoted = [re.sub(r'^> ?', '', ln) for ln in hunk.splitlines()]
    quoted = [q for q in quoted if q.strip()]
    if not quoted:
        return None
    target = quoted[-1]
    for i, cl in enumerate(msg_lines):
        if cl == target:
            return i
    return None


def _format_with_comments(ctx, greeting, verdict, comments,
                          commit_message=None):
    """Format a review that has comments

    Comments on the commit message are woven in right after the quoted
    line they refer to (inline-reply style), rather than re-quoting that
    line again lower down. Comments on the code (which quote a diff hunk)
    follow the quoted commit message.

    Args:
        ctx (ReviewContext): Review context
        greeting (str): Author's first name, or '' if unknown
        verdict (str): 'approved' or 'changes_needed'
        comments (list): List of (hunk, comment) tuples
        commit_message (str or None): Commit message to quote

    Returns:
        str: Formatted email body
    """
    if not greeting:
        greeting = guess_name_from_email(ctx.author_email)
    lines = [f'Hi {greeting},' if greeting else 'Hi,', '']

    lines.append(
        f'On {ctx.date}, {ctx.author_name} <{ctx.author_email}> wrote:')

    # Place each commit-message comment after the message line it quotes;
    # code comments (and any quote we cannot locate) go below the quote
    msg_lines = commit_message.strip().splitlines() if commit_message else []
    inline = {}
    below = []
    for hunk, comment in comments:
        idx = (None if _is_code_comment(hunk)
               else _match_quote_to_lines(hunk, msg_lines))
        if idx is None:
            below.append((hunk, comment))
        else:
            inline.setdefault(idx, []).append(comment)

    if commit_message:
        # Quote at least the first 20 lines, and always as far as the last
        # line that carries an inline comment
        max_quote = 20
        limit = max(max_quote, max(inline) + 1 if inline else 0)
        for i, cl in enumerate(msg_lines[:limit]):
            lines.append(f'> {cl}')
            for comment in inline.get(i, []):
                lines += ['', comment, '']
        if len(msg_lines) > limit:
            lines.append('> [...]')
    diffstat = getattr(ctx, 'diffstat', None)
    if diffstat:
        ds_lines = diffstat.strip().splitlines()
        if len(ds_lines) <= 30:
            lines.append('>')
            for dl in ds_lines:
                lines.append(f'> {dl}')
    lines.append('')

    # Keep commit-message comments (no diff header) before code comments;
    # sorted() is stable, so the order within each group is preserved
    below.sort(key=lambda hc: _is_code_comment(hc[0]))
    for hunk, comment in below:
        if hunk:
            lines.append(hunk)
            lines.append('')
        lines.append(comment)
        lines.append('')

    if verdict == 'approved':
        lines += [f'Reviewed-by: {ctx.reviewer_tag}', '']
    elif comments and ctx.signoff:
        lines += [ctx.signoff, '']
    # Collapse any run of blank lines left by the inline insertion
    return re.sub(r'\n{3,}', '\n\n', '\n'.join(lines))


def format_review_email(ctx, greeting, verdict, comments,
                        commit_message=None):
    """Format parsed review data into an email body

    Delegates to _format_approved() for clean approvals or
    _format_with_comments() for reviews with feedback.

    Args:
        ctx (ReviewContext): Review context with reviewer/author info
            and optional diffstat
        greeting (str): Author's first name, or '' if unknown
        verdict (str): 'approved', 'changes_needed' or 'skip'
        comments (list): List of (hunk, comment) tuples
        commit_message (str or None): Commit message to quote

    Returns:
        str: Formatted email body text
    """
    if verdict == 'approved' and not comments:
        return _format_approved(ctx, commit_message, ctx.diffstat)
    return _format_with_comments(ctx, greeting, verdict, comments,
                                 commit_message)


def cleanup_review_text(text):
    """Apply mechanical fixes to review email text

    Removes backticks, fixes function quoting style, and other
    formatting issues that the review agent sometimes produces despite
    prompt instructions.

    Args:
        text (str): Review email body

    Returns:
        str: Cleaned-up text
    """
    def fix_line(line):
        # Replace backtick-quoted code with plain text: `foo` -> foo
        line = re.sub(r'`([^`]+)`', r'\1', line)

        # Remove quotes around function references: 'func()' -> func()
        line = re.sub(r"'(\w+\(\))'", r'\1', line)

        # Remove double quotes around function references: "func()" -> func()
        line = re.sub(r'"(\w+\(\))"', r'\1', line)

        # Convert double-quoted short tokens to single quotes:
        # "handoff" -> 'handoff'. Leave longer quoted text (full sentences
        # or phrases) alone, since they may be intentional quotations.
        line = re.sub(r'"([^"\n]{1,40})"',
                      lambda m: f"'{m.group(1)}'"
                      if ' ' not in m.group(1) else m.group(0),
                      line)
        return line

    # Quoted lines ('> ...') reproduce the author's commit message and diff
    # verbatim, so must keep their exact characters -- including " vs ' and
    # any backticks -- rather than being restyled to our prose conventions.
    # Only clean up our own text
    return '\n'.join(line if line.startswith('>') else fix_line(line)
                     for line in text.split('\n'))


_REFINE_REVIEWS_PROMPT = '''You are editing draft code-review \
emails for the U-Boot
mailing list. Your job is to make them more concise and natural while
preserving all technical content.

DRAFTS TO EDIT:
{drafts}

{voice_section}
RULES:
- Make each review as succinct as possible. Remove filler, hedging and
  unnecessary preamble. Every sentence should earn its place.
- Remove duplicate points — if the same issue is raised on multiple
  patches, keep it only on the most relevant one and remove it from the
  others. Also remove any comment that restates, rephrases, or
  elaborates on a point already made by another reviewer — the author
  has already been told.
- NEVER use backticks — this is plain-text email, not markdown.
  For functions, always use parentheses with no quotes: malloc() not
  'malloc()' or `malloc`. For other identifiers do not quote them
  unless they are common English words that might confuse the reader.
- Use {spelling} spelling.
- Do not change the technical substance of any comment.
- Do not add new review comments or suggestions.
- Do not change Reviewed-by tags, attribution lines, quoted commit
  messages, or quoted diff hunks. These are structural parts of the
  email that must be preserved exactly.
- Do not hard-wrap the prose. Write each comment paragraph on a single
  line and let the reader's mail client wrap it; keep one blank line
  between paragraphs. Leave the quoted '> ' lines and any indented code
  block exactly as they are — do not join or re-wrap those.

OUTPUT FORMAT:
Return each review separated by a line containing only '---SEQ N---'
(where N is the patch number, 0 for cover letter). Include the full
edited email body after each separator. No other text before or after.

Example:
---SEQ 3---
Hi Marek,

On 2026-03-21, Marek Vasut <marex@denx.de> wrote:
...edited review body...

Regards, Simon
---SEQ 5---
...next review...
'''


def _needs_refinement(body):
    """Check whether a review body has reviewer comments

    Approved reviews with only structural lines (quoted text,
    attribution, Reviewed-by) do not need refinement.

    Args:
        body (str): Review email body

    Returns:
        bool: True if the review has comments to refine
    """
    return any(
        not l.startswith('>') and
        not l.startswith('On ') and
        not l.startswith('Reviewed-by:') and
        not l.startswith('Hi ') and l.strip()
        for l in body.splitlines())


def _parse_refined_output(log):
    """Parse ---SEQ N--- delimited output from the refinement agent

    Args:
        log (str): Raw agent output

    Returns:
        dict: Map of seq number to refined body text
    """
    refined = {}
    current_seq = None
    current_lines = []
    for line in log.splitlines():
        match = re.match(r'^---SEQ (\d+)---$', line)
        if match:
            if current_seq is not None and current_lines:
                refined[current_seq] = '\n'.join(current_lines).strip()
            current_seq = int(match.group(1))
            current_lines = []
        elif current_seq is not None:
            current_lines.append(line)
    if current_seq is not None and current_lines:
        refined[current_seq] = '\n'.join(current_lines).strip()
    return refined


async def refine_reviews(review_bodies, spelling='British'):
    """Run a refinement agent over all review drafts

    Makes reviews more concise, removes duplicates across
    patches, and enforces formatting conventions. Approved
    reviews without comments are excluded.

    Args:
        review_bodies (dict): Map of patch index to review
            body text
        spelling (str): Spelling convention

    Returns:
        dict: Updated review_bodies with refined text
    """
    to_refine = {seq: body
                 for seq, body in review_bodies.items()
                 if _needs_refinement(body)}
    if not to_refine:
        return review_bodies

    draft_parts = []
    for seq in sorted(to_refine):
        draft_parts.append(f'---SEQ {seq}---')
        draft_parts.append(to_refine[seq])
    drafts_text = '\n'.join(draft_parts)

    voice = get_voice()
    voice_section = ''
    if voice:
        voice_section = f'''WRITING STYLE:
Match this voice when editing the reviews:
{voice}
'''

    prompt = _REFINE_REVIEWS_PROMPT.format(drafts=drafts_text,
        voice_section=voice_section, spelling=spelling)

    options = _agent_options(allowed_tools=[],
        max_buffer_size=claude_mod.MAX_BUFFER_SIZE)

    tout.notice('Refining review drafts...')
    success, log = await claude_mod.run_agent_collect(prompt, options)
    if not success or not log.strip():
        tout.warning('Refinement failed; using original drafts')
        return review_bodies

    refined = _parse_refined_output(log)

    # Keep originals for any the agent missed
    result = dict(review_bodies)
    for seq, body in refined.items():
        if seq in result and body:
            result[seq] = body
    return result


def refine_reviews_sync(review_bodies, spelling='British'):
    """Synchronous wrapper for refine_reviews()"""
    loop = asyncio.get_event_loop()
    return loop.run_until_complete(refine_reviews(review_bodies, spelling))


def _write_comments_file(series_data, pwork):
    """Fetch and write existing patchwork comments to a temp file

    Args:
        series_data (dict): Series data from patchwork get_series()
        pwork: Patchwork instance

    Returns:
        str or None: Path to the comments file, or None if no comments
    """


    patches = series_data.get('patches', [])
    cover = series_data.get('cover_letter')

    async def _fetch_comments():
        all_comments = []
        async with aiohttp.ClientSession() as client:
            # Cover letter comments
            if cover:
                cover_comments = await pwork.get_cover_comments(
                    client, cover['id'])
                for comment in cover_comments:
                    sub = comment.get('submitter', {})
                    all_comments.append(f"=== Comment on cover letter ===\n"
                        f"From: {sub.get('name', '')} "
                        f"<{sub.get('email', '')}>\n"
                        f"Date: {comment.get('date', '')}\n\n"
                        f"{comment.get('content', '')}\n")

            # Patch comments
            for i, patch in enumerate(patches):
                patch_comments = await pwork.get_patch_comments(
                    client, str(patch['id']))
                for comment in patch_comments:
                    sub = comment.get('submitter', {})
                    all_comments.append(f"=== Comment on patch {i + 1}: "
                        f"{patch.get('name', '')} ===\n"
                        f"From: {sub.get('name', '')} "
                        f"<{sub.get('email', '')}>\n"
                        f"Date: {comment.get('date', '')}\n\n"
                        f"{comment.get('content', '')}\n")

        return all_comments

    loop = asyncio.get_event_loop()
    comments = loop.run_until_complete(_fetch_comments())

    if not comments:
        return None

    comments_path = os.path.join(tempfile.gettempdir(),
                                 'patman_review_comments.txt')
    tools.write_file(comments_path, '\n'.join(comments), binary=False)

    tout.notice(f'Found {len(comments)} existing comment(s)')
    return comments_path


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
    prompt = _build_cover_review_prompt(
        ctx, all_commits, previous_review=ctx.previous_reviews.get(0))
    options = _agent_options(
        allowed_tools=['Bash', 'Read', 'Grep'], cwd=ctx.repo_path,
        max_buffer_size=claude_mod.MAX_BUFFER_SIZE)
    success, log = await claude_mod.run_agent_collect(prompt, options)
    if not success or not log.strip():
        return None
    greeting, verdict, comments = parse_review_output(log)
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
    return format_review_email(ctx, greeting, verdict, comments)


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
    ctx.diffstat = gitutil.diff_stat(f'{cmt.hash}~..{cmt.hash}',
                                     ctx.repo_path).strip()

    previous_review = ctx.previous_reviews.get(seq)
    prompt = _build_review_prompt(ctx, cmt.hash, seq, all_commits,
                                  previous_review)
    options = _agent_options(allowed_tools=['Bash', 'Read', 'Grep'],
        cwd=ctx.repo_path, max_buffer_size=claude_mod.MAX_BUFFER_SIZE)
    success, log = await claude_mod.run_agent_collect(prompt, options)
    if not success or not log.strip():
        return None
    greeting, verdict, comments = parse_review_output(log)
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
    return format_review_email(ctx, greeting, verdict, comments, commit_msg)


def parse_patch_selection(spec):
    """Parse a patch selection string into a set of patch numbers

    Supports comma-separated numbers and ranges, e.g. '1,3,5' or '2-7'
    or '1,3-5,8'.

    Args:
        spec (str or None): Selection string, or None for all patches

    Returns:
        set of int or None: Selected patch numbers, or None for all
    """
    if not spec:
        return None
    result = set()
    for part in spec.split(','):
        if '-' in part:
            start, end = part.split('-', 1)
            result.update(range(int(start), int(end) + 1))
        else:
            result.add(int(part))
    return result


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
        subject = _clean_series_name(patch.get('name', ''))
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
    loop = asyncio.get_event_loop()
    return loop.run_until_complete(review_patches(ctx))


def _run_patch_review_sync(ctx, cmt, seq, all_commits):
    """Synchronous wrapper for _run_patch_review()"""
    loop = asyncio.get_event_loop()
    return loop.run_until_complete(
        _run_patch_review(ctx, cmt, seq, all_commits))


def _run_cover_review_sync(ctx, all_commits):
    """Synchronous wrapper for _run_cover_review()"""
    loop = asyncio.get_event_loop()
    return loop.run_until_complete(_run_cover_review(ctx, all_commits))


def _format_findings(verdict, comments):
    """Format review findings for one patch as a stored body

    Args:
        verdict (str): 'approved', 'changes_needed' or 'skip'
        comments (list): (hunk, comment) tuples, already ordered

    Returns:
        str: Body text to store
    """
    if not comments:
        return ('Looks good; no issues found.' if verdict == 'approved'
                else 'No specific comments.')
    lines = []
    for hunk, comment in comments:
        if hunk:
            lines.append(hunk)
            lines.append('')
        lines.append(comment)
        lines.append('')
    return '\n'.join(lines).rstrip() + '\n'


def _print_finding(seq, total, subject, verdict, comments):
    """Print the findings for one patch to the terminal

    Args:
        seq (int): Patch sequence number
        total (int): Total number of patches
        subject (str): Patch subject
        verdict (str): 'approved', 'changes_needed' or 'skip'
        comments (list): (hunk, comment) tuples
    """
    if comments:
        label = 'changes suggested'
    elif verdict == 'approved':
        label = 'looks good'
    else:
        label = 'no comments'
    tout.notice('')
    if seq == 0:
        tout.notice(f'=== Cover letter — {label} ===')
    else:
        tout.notice(f'=== Patch {seq}/{total}: {subject} — {label} ===')
    for hunk, comment in comments:
        for line in hunk.splitlines():
            tout.notice(f'  {line}')
        if hunk:
            tout.notice('')
        for line in comment.splitlines():
            tout.notice(f'  {line}')
        tout.notice('')


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
    ctx = ReviewContext(None, cser, {})
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
            comments = sorted(comments, key=lambda hc: _is_code_comment(hc[0]))
            body = cleanup_review_text(_format_findings(verdict, comments))
            cser.db.review_add(svid, 0, body, verdict == 'approved',
                               timestamp)
            n_comments += len(comments)
            _print_finding(0, total, 'cover letter', verdict, comments)

    for i, cmt in enumerate(commits):
        seq = i + 1
        tout.notice(f'Reviewing patch {seq}/{total}...')
        result = _run_patch_review_sync(ctx, cmt, seq, all_commits)
        if result is None:
            tout.warning(f'  Review failed for patch {seq}')
            continue
        _, verdict, comments, _ = result
        comments = sorted(comments, key=lambda hc: _is_code_comment(hc[0]))
        body = cleanup_review_text(_format_findings(verdict, comments))
        cser.db.review_add(svid, seq, body, verdict == 'approved', timestamp)
        n_comments += len(comments)
        _print_finding(seq, total, cmt.subject, verdict, comments)
    cser.commit()

    tout.notice('')
    tout.notice(f"Stored review of '{branch}' v{version}: {total} patch(es), "
                f"{n_comments} comment(s). View with 'patman series info -r'")


def apply_series_sync(pwork, link, branch_name, upstream_branch, repo_path):
    """Synchronous wrapper for apply_series()

    Args:
        pwork (Patchwork): Patchwork instance to fetch from
        link (str): Patchwork series link/ID
        branch_name (str): Name for the new branch
        upstream_branch (str): Branch to base from
        repo_path (str): Path to the git repository

    Returns:
        tuple: (success, branch_name)
    """
    loop = asyncio.get_event_loop()
    return loop.run_until_complete(apply_series(
        pwork, link, branch_name, upstream_branch, repo_path))


def search_patch(pwork, title):
    """Search patchwork for a patch by title and return its series and index

    Queries the patchwork patches API by title, picks the most recent
    match, then looks up its series.

    Args:
        pwork (Patchwork): Configured patchwork instance
        title (str): Patch title text to search for

    Returns:
        tuple: (series_link, patch_seq)

    Raises:
        ValueError: if no matching patch is found
    """
    from urllib.parse import quote_plus

    async def _query():
        query = quote_plus(title, safe=':')
        async with aiohttp.ClientSession() as client:
            return await pwork.search_patches(client, query)

    loop = asyncio.get_event_loop()
    results = loop.run_until_complete(_query())

    if not results:
        raise ValueError(f"No patch found matching '{title}'")

    if len(results) > 1:
        tout.notice(f"Found {len(results)} matching patches:")
        for i, p in enumerate(results[:10]):
            tout.notice(f"  {i + 1}. [{p['id']}] {p['name']}")

    best = results[0]
    patch_id = best['id']
    tout.notice(f"Using: [{patch_id}] {best['name']}")
    return lookup_patch_series(pwork, patch_id)


def lookup_patch_series(pwork, patch_id):
    """Look up a patch on patchwork and return its series link and position

    Args:
        pwork (Patchwork): Configured patchwork instance
        patch_id (int): Patchwork patch ID

    Returns:
        tuple: (series_link, patch_seq) where series_link is the series
            ID as a string and patch_seq is the 1-based position

    Raises:
        ValueError: if the patch or its series cannot be found
    """
    async def _query():
        async with aiohttp.ClientSession() as client:
            return await pwork.get_patch(client, patch_id)

    loop = asyncio.get_event_loop()
    data = loop.run_until_complete(_query())

    series_list = data.get('series', [])
    if not series_list:
        raise ValueError(f'Patch {patch_id} has no associated series')

    series_link = str(series_list[0]['id'])
    patch_name = data.get('name', '')
    tout.notice(f"Patch {patch_id}: '{patch_name}'")
    tout.notice(f"Series: {series_list[0].get('name', '')} "
                f"(link {series_link})")

    # Fetch the series to find the patch position
    series_data = _fetch_series(pwork, series_link)[0]
    patches = series_data.get('patches', [])
    for i, patch in enumerate(patches):
        if patch.get('id') == patch_id:
            return series_link, i + 1
    return series_link, 1


def search_series(pwork, title, version=None):
    """Search patchwork for a series by cover-letter title

    Queries the patchwork API and returns the link for the best match.
    If multiple matches are found, shows them and picks the most recent,
    unless a specific version is requested.

    Args:
        pwork (Patchwork): Configured patchwork instance
        title (str): Title text to search for
        version (int or None): Specific version to select, or None for the
            most recent

    Returns:
        str: Patchwork series link/ID

    Raises:
        ValueError: if no matching series (or version) is found
    """
    async def _query():
        if not pwork.proj_id:
            raise ValueError('Patchwork project not configured; use '
                "'patman patchwork set-project' or provide -l <link>")
        async with aiohttp.ClientSession() as client:
            return await pwork.query_series(client, title)

    loop = asyncio.get_event_loop()
    results = loop.run_until_complete(_query())

    if not results:
        raise ValueError(f"No series found matching '{title}'")

    if version is not None:
        matches = [r for r in results if r.get('version') == version]
        if not matches:
            avail = ', '.join(
                f"v{r.get('version')}"
                for r in sorted(results, key=lambda r: r.get('version', 0)))
            raise ValueError(
                f"No v{version} of '{title}' found (available: {avail})")
        # If several share the version, take the most recent by date
        best = max(matches, key=lambda r: r.get('date', ''))
        tout.notice(f"Using v{version}: {best['name']} (link {best['id']})")
        return str(best['id'])

    if len(results) == 1:
        match = results[0]
        tout.notice(f"Found: {match['name']} (v{match['version']}, "
                  f"link {match['id']})")
        return str(match['id'])

    # Multiple matches - show them and pick the most recent
    tout.notice(f"Found {len(results)} matching series:")
    for i, match in enumerate(results):
        tout.notice(f"  {i + 1}. [{match['id']}] {match['name']} "
                  f"(v{match['version']}, {match['date']})")

    best = max(results, key=lambda r: (r.get('version', 0), r.get('date', '')))
    tout.notice(f"Using most recent: {best['name']} (link {best['id']})")
    return str(best['id'])


def _draft_location(ctx):
    """Describe the branch and series a batch of drafts belongs to

    Args:
        ctx (ReviewContext): Review context

    Returns:
        str: A ' (branch <name>, link <id>)' suffix with whichever parts
            are known, or '' if neither is (e.g. a re-draft with no branch)
    """
    parts = []
    if getattr(ctx, 'branch_name', None):
        parts.append(f'branch {ctx.branch_name}')
    link = ctx.series_data.get('id') if ctx.series_data else None
    if link:
        parts.append(f'link {link}')
    return f' ({", ".join(parts)})' if parts else ''


def create_drafts(ctx, args, review_bodies, review_ids):
    """Create Gmail drafts for review emails

    If the reviewer's email differs from the Gmail account, a From header is
    set on the draft so the email is sent with the correct identity.

    Args:
        ctx (ReviewContext): Review context
        args (Namespace): Command-line arguments (for gmail_account, dry_run)
        review_bodies (dict): Map of seq to review body text
        review_ids (dict): Map of seq to review record ID
    """
    to_draft = dict(review_bodies)
    if not to_draft:
        tout.notice('All reviews already have Gmail drafts')
        return

    patch_headers = {}
    patches = ctx.series_data.get('patches', [])

    async def _fetch_patch_headers():
        async with aiohttp.ClientSession() as client:
            for i, patch in enumerate(patches):
                data = await ctx.pwork.get_patch(client, str(patch['id']))
                patch_headers[i + 1] = data.get('headers', {})

    loop = asyncio.get_event_loop()
    loop.run_until_complete(_fetch_patch_headers())

    sender = None
    if ctx.reviewer_email and args.gmail_account:
        if ctx.reviewer_email.lower() != args.gmail_account.lower():
            sender = ctx.reviewer_tag

    draft_ids = gmail.create_review_drafts(ctx.series_data, to_draft,
        patch_headers=patch_headers, dry_run=args.dry_run,
        account=args.gmail_account, sender=sender)
    where = _draft_location(ctx)
    if args.dry_run:
        tout.notice(f'Dry run: would create {len(to_draft)} draft(s){where}')
    else:
        for seq, draft_id in draft_ids.items():
            if seq in review_ids:
                ctx.cser.db.review_set_draft_id(review_ids[seq], draft_id)
        ctx.cser.commit()
        tout.notice(f'Created {len(draft_ids)} Gmail draft(s){where}')


def _parse_reviewer(args):
    """Extract reviewer name and email from args

    Uses --reviewer if provided, otherwise falls back to git config.

    Args:
        args (Namespace): Command-line arguments

    Returns:
        tuple: (reviewer_name, reviewer_email)

    Raises:
        ValueError: if the reviewer identity cannot be determined
    """

    if args.reviewer:
        match = re.match(r'(.+?)\s*<(.+?)>', args.reviewer)
        if not match:
            raise ValueError(
                f"Invalid reviewer format '{args.reviewer}';"
                " use 'Name <email>'")
        return match.group(1).strip(), match.group(2).strip()
    name = gitutil.get_default_user_name()
    email = gitutil.get_default_user_email()
    if not name or not email:
        raise ValueError(
            'Cannot determine reviewer identity; set git user.name'
            ' and user.email, or use --reviewer')
    return name, email


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


def _do_learn_voice(args, pwork):
    """Build a voice profile from past reviews

    Args:
        args (Namespace): Command-line arguments
        pwork (Patchwork or None): Configured patchwork instance

    Returns:
        int: 0 on success, 1 on failure
    """

    source = args.learn_voice
    account = getattr(args, 'gmail_account', None)
    user_email = None
    list_email = None

    if pwork and pwork.proj_id:
        async def _get_list_email():
            projects = await pwork.get_projects()
            for proj in projects:
                if proj['id'] == pwork.proj_id:
                    return proj.get('list_email')
            return None

        loop = asyncio.get_event_loop()
        list_email = loop.run_until_complete(_get_list_email())

    if args.reviewer:
        match = re.match(r'(.+?)\s*<(.+?)>', args.reviewer)
        if match:
            user_email = match.group(2).strip()
    if not user_email:
        user_email = gitutil.get_default_user_email()

    count = getattr(args, 'voice_count', 20)
    vp = VoiceParams(source, account, pwork, user_email, list_email, count)
    return 0 if learn_voice_sync(vp) else 1


def _sync_drafts(service, cser):
    """Sync draft status with Gmail

    Detects sent and deleted drafts, updates the database, and refines the voice
    profile if sent text differs from the draft.

    Args:
        service: Gmail API service
        cser (Cseries): Open cseries instance

    Returns:
        bool: True if there were drafts to sync
    """
    reviews_with_drafts = cser.db.review_get_by_status('draft')

    if not reviews_with_drafts:
        tout.notice('No pending drafts to sync')
        return False

    sent, deleted = gmail.sync_drafts(service, reviews_with_drafts)
    draft_bodies = {r.idnum: r.body for r in reviews_with_drafts}
    for review_id, (body, msg_id, thread_id) in sent.items():
        cser.db.review_set_sent(review_id, body, msg_id, thread_id)
        tout.notice(f'Review {review_id}: sent')

        draft = draft_bodies.get(review_id, '')
        if draft and body.strip() != draft.strip():
            tout.notice(f'Review {review_id}: edits detected, '
                        'refining voice...')
            refine_voice_sync(draft, body)
    for review_id in deleted:
        cser.db.review_set_deleted(review_id)
        tout.notice(f'Review {review_id}: deleted (not sent)')
    pending = len(reviews_with_drafts) - len(sent) - len(deleted)
    if pending:
        tout.notice(f'{pending} draft(s) still pending')
    if sent or deleted:
        tout.notice(f'Synced {len(sent)} sent, {len(deleted)} deleted')
    cser.commit()
    return True


def _handle_replies(service, account, ctx):
    """Check for replies to sent reviews and generate responses

    Args:
        service: Gmail API service
        account (str or None): Gmail account for creating drafts
        ctx (ReviewContext): Review context
    """
    sent_reviews = ctx.cser.db.review_get_by_status('sent',
                                                     need_thread=True)

    reply_count = 0

    for rev in sent_reviews:
        replies = gmail.fetch_thread_replies(
            service, rev.gmail_thread_id, rev.gmail_msg_id)
        if not replies:
            continue

        tout.notice(f'Review {rev.idnum}: {len(replies)} reply(ies)')
        ctx.cser.db.review_set_replied(rev.idnum)

        for reply in replies:
            response = handle_reply_sync(ctx, rev.body, reply['from'],
                                         reply['body'])
            if response:
                reply_count += 1
                tout.notice(f"  Draft response to {reply['from']}")
                terminal.tprint(f"\n--- Reply to {reply['from']} ---",
                                colour=terminal.Color.CYAN)
                print('---email start---')
                print(response)
                print('---email end---')
                print()

                if account:
                    gmail.create_draft(gmail.get_service(account),
                        reply['from'], 'Re: ', response)
                    tout.notice('  Draft created')
            else:
                tout.notice(
                    f"  No response needed to {reply['from']}")

    if reply_count:
        tout.notice(f'Created {reply_count} response draft(s)')
    ctx.cser.commit()


def _do_sync(args, cser):
    """Sync sent drafts and handle replies

    Args:
        args (Namespace): Command-line arguments
        cser (Cseries): Open cseries instance

    Returns:
        int: 0 on success, 1 on failure
    """
    if not gmail.check_available():
        return 1
    account = getattr(args, 'gmail_account', None)
    service = gmail.get_service(account)

    _sync_drafts(service, cser)

    reviewer_name, reviewer_email = _parse_reviewer(args)
    ctx = ReviewContext(None, cser, {})
    ctx.reviewer_name = reviewer_name
    ctx.reviewer_email = reviewer_email
    ctx.repo_path = gitutil.get_top_level()
    ctx.signoff = getattr(args, 'signoff', '') or None
    if ctx.signoff:
        ctx.signoff = ctx.signoff.replace('\\n', '\n')
    ctx.spelling = getattr(args, 'spelling', 'British')

    _handle_replies(service, account, ctx)
    return 0


def _clean_series_name(name):
    """Strip the [U-Boot,v2,0/4] prefix from a series name

    Args:
        name (str): Raw series name from patchwork

    Returns:
        str: Cleaned name
    """
    if name.startswith('['):
        bracket_end = name.find(']')
        if bracket_end != -1:
            return name[bracket_end + 1:].strip()
    return name


def _make_review_name(link, upstream=None):
    """Build a series name for a review branch

    Args:
        link (str): Patchwork series link/ID
        upstream (str or None): Upstream name

    Returns:
        str: Branch-style name, e.g. 'us-498633-review'
    """
    ups = upstream or 'pw'
    return f'{ups}-{link}-review'


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
            branch_name = _make_review_name(link, upstream)
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


def _fetch_series(pwork, link):
    """Fetch and validate series metadata from patchwork

    Args:
        pwork (Patchwork): Configured patchwork instance
        link (str): Patchwork series link/ID

    Returns:
        tuple: (series_data, clean_name, version, patch_count)

    Raises:
        ValueError: if the series is incomplete
    """
    async def _fetch():
        async with aiohttp.ClientSession() as client:
            return await pwork.get_series(client, link)

    loop = asyncio.get_event_loop()
    series_data = loop.run_until_complete(_fetch())

    series_name = series_data.get('name', f'series-{link}')
    version = series_data.get('version', 1)
    patch_count = series_data.get('received_total', 0)
    total = series_data.get('total', patch_count)

    clean_name = _clean_series_name(series_name)

    tout.notice(f"Series: {clean_name}")
    tout.notice(f"Version: {version}, Patches: {patch_count}")

    if patch_count < total:
        raise ValueError('Incomplete series: patchwork received '
            f'{patch_count} of {total} patches')

    return series_data, clean_name, version, patch_count


def _delete_gmail_drafts(args, reviews):
    """Delete the Gmail drafts recorded for the given reviews

    Used when reviews are being replaced -- a forced re-review or a
    redraft -- so the previous drafts do not linger in Gmail as
    duplicates alongside the new ones. Reviews with no recorded draft
    are ignored, and nothing happens if Gmail is unavailable.

    Args:
        args (Namespace): Command-line arguments (for gmail_account)
        reviews (list of Review): Reviews whose recorded drafts to delete
    """
    draft_ids = [rev.draft_id for rev in reviews if rev.draft_id]
    if not draft_ids:
        return
    if not gmail.check_available():
        return
    service = gmail.get_service(getattr(args, 'gmail_account', None))
    for draft_id in draft_ids:
        gmail.delete_draft(service, draft_id)
    tout.notice(f'Deleted {len(draft_ids)} old Gmail draft(s)')


def _draft_stored_reviews(args, reviews, series_data, pwork, cser):
    """Create Gmail drafts from stored review records

    Drafts reviews that do not already have a draft_id. With --redraft it
    drafts every stored review, recreating drafts that already exist so a
    failed or lost draft can be regenerated from the database.

    Args:
        args (Namespace): Command-line arguments
        reviews (list): Review records
        series_data (dict): Series data from patchwork
        pwork (Patchwork): Patchwork instance
        cser (Cseries): Cseries instance
    """
    if args.redraft:
        need_draft = list(reviews)
    else:
        need_draft = [rev for rev in reviews
                      if not rev.draft_id]
    if not need_draft:
        tout.notice('All reviews already have Gmail drafts')
        return
    if args.redraft:
        # Remove the drafts we are about to recreate so they do not linger
        # as duplicates in the same Gmail thread
        _delete_gmail_drafts(args, need_draft)
    review_bodies = {rev.seq: rev.body for rev in need_draft}
    review_ids = {rev.seq: rev.idnum
                  for rev in need_draft}
    rname = remail = None
    for rev in need_draft:
        match = re.search(r'Reviewed-by:\s*(.+?)\s*<(.+?)>', rev.body)
        if match:
            rname = match.group(1).strip()
            remail = match.group(2).strip()
            break
    ctx = ReviewContext(pwork, cser, series_data)
    ctx.reviewer_name = rname or ''
    ctx.reviewer_email = remail or ''
    create_drafts(ctx, args, review_bodies, review_ids)


def _get_upstream_branch(args, cser):
    """Determine the base branch for applying patches

    Honours --base-branch if the user supplied one. Otherwise prefers
    '<upstream>/next' when it has commits ahead of '<upstream>/master',
    falling back to '<upstream>/master' when next is empty (e.g. just
    after a release, when next has been merged into master and has not
    yet been reopened for the next cycle).

    Args:
        args (Namespace): Command-line arguments
        cser (Cseries): Open cseries instance

    Returns:
        str: Base branch ref, e.g. 'us/next' or 'us/master'
    """
    if args.base_branch:
        return args.base_branch
    ups = args.upstream
    if not ups:
        ups = cser.db.upstream_get_default()
    if ups:
        next_branch = f'{ups}/next'
        master_branch = f'{ups}/master'
        if gitutil.ref_exists(next_branch):
            ahead = gitutil.count_revs(
                None, f'{master_branch}..{next_branch}')
            if ahead:
                return next_branch
        return master_branch
    return 'origin/master'


def _apply_and_check(ctx, link):
    """Download, apply patches and verify they applied

    Runs in the per-review worktree at ctx.repo_path; the branch
    ctx.branch_name has already been created and reset to upstream by
    worktree.ensure_worktree(), so the agent only needs to 'git am'.

    Args:
        ctx (ReviewContext): Review context (uses pwork, cser, series_id,
            version, upstream_branch, branch_name, repo_path)
        link (str): Patchwork series link/ID

    Returns:
        True if the patches applied cleanly, None if there was nothing to
        apply because every patch is already upstream (a clean skip), or
        False if applying failed
    """
    success, _ = apply_series_sync(ctx.pwork, link, ctx.branch_name,
        ctx.upstream_branch, ctx.repo_path)

    if not success:
        # The agent hit a real problem, or was interrupted (which leaves
        # the branch missing); either way there is nothing to review
        tout.error('Failed to apply patches to branch')
        ctx.cser.db.ser_ver_remove(ctx.series_id, ctx.version)
        ctx.cser.commit()
        return False

    applied = gitutil.count_revs(
        ctx.repo_path, f'{ctx.upstream_branch}..{ctx.branch_name}')
    if not applied:
        # The agent ran to completion but applied nothing: every patch is
        # already present upstream (a fresh checkout of an obsolete
        # series). That is a valid outcome, not a failure -- there is
        # simply nothing to review
        tout.notice(f"Nothing to review for '{ctx.branch_name}': every "
                    'patch is already applied upstream')
        ctx.cser.db.ser_ver_remove(ctx.series_id, ctx.version)
        ctx.cser.commit()
        return None
    if applied != ctx.patch_count:
        # Common with kernel-import series: the agent legitimately skips
        # patches that are already applied upstream. Warn and proceed
        # rather than discarding the apply
        tout.warning(f'Only {applied} of {ctx.patch_count} patches applied '
                     f'to {ctx.branch_name}; reviewing what is there')
    tout.notice(f'Patches applied to branch: {ctx.branch_name}')
    return True


def _fetch_cover_content(pwork, series_data):
    """Fetch cover letter content from patchwork

    Args:
        pwork (Patchwork): Patchwork instance
        series_data (dict): Series data from patchwork

    Returns:
        str or None: Cover letter text
    """
    cover = series_data.get('cover_letter')
    if not cover:
        return None

    async def _fetch():
        async with aiohttp.ClientSession() as client:
            data = await pwork.get_cover(client, cover['id'])
            return data.get('content', '')

    loop = asyncio.get_event_loop()
    return loop.run_until_complete(_fetch())


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

    ctx.cover_content = _fetch_cover_content(ctx.pwork, ctx.series_data)

    review_bodies = review_patches_sync(ctx)

    if ctx.comments_path and os.path.exists(ctx.comments_path):
        os.unlink(ctx.comments_path)

    for seq in review_bodies:
        review_bodies[seq] = cleanup_review_text(review_bodies[seq])
    review_bodies = refine_reviews_sync(review_bodies, ctx.spelling)

    timestamp = datetime.now().isoformat()
    review_ids = {}
    for seq, body in review_bodies.items():
        approved = 'Reviewed-by:' in body
        review_ids[seq] = ctx.cser.db.review_add(ctx.svid, seq, body, approved,
                                                 timestamp)
    ctx.cser.commit()

    _show_reviews(review_bodies, ctx.series_data)

    if args.create_drafts:
        create_drafts(ctx, args, review_bodies, review_ids)
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
    patch_sel = parse_patch_selection(args.patches)
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
            _draft_stored_reviews(args, reviews, ctx.series_data, ctx.pwork,
                                  ctx.cser)
        return None

    if args.create_drafts:
        # The old drafts would otherwise be orphaned in Gmail when the
        # review records that track them are deleted below
        _delete_gmail_drafts(args, reviews)
    ctx.cser.db.review_delete_for_version(svid)
    ctx.cser.commit()
    tout.notice('Re-reviewing (forced)')
    return series_id, svid


# Patchwork patch states worth reviewing: a series is reviewed only when at
# least one of its patches is in one of these states
ACTIVE_STATES = {'new', 'rfc', 'under-review', 'changes-requested',
                 'needs-review-ack'}


class ReviewInProgressError(Exception):
    """Raised when another review of the same series is already running"""


def _get_patch_states(pwork, link):
    """Get the patchwork state of each patch in a series

    Args:
        pwork (Patchwork): Configured patchwork instance
        link (str): Patchwork series link/ID

    Returns:
        list of str: State slug for each patch
    """
    async def _fetch():
        async with aiohttp.ClientSession() as client:
            return await pwork.get_series_patch_states(client, link)

    loop = asyncio.get_event_loop()
    return loop.run_until_complete(_fetch())


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
    branch_name = _make_review_name(link, ups)
    main_repo = gitutil.get_top_level()
    try:
        lock_fd = _acquire_review_lock(main_repo, branch_name)
    except ReviewInProgressError:
        tout.warning(
            f"A review of '{branch_name}' is already in progress; skipping")
        return 0

    try:
        series_data, clean_name, version, patch_count = \
            _fetch_series(pwork, link)

        if not getattr(args, 'any_state', False):
            states = _get_patch_states(pwork, link)
            if not any(state in ACTIVE_STATES for state in states):
                shown = ', '.join(sorted({s for s in states if s})) or 'none'
                raise ValueError(
                    f"Series '{clean_name}' is not active on patchwork "
                    f'(patch states: {shown}); use --any-state to review it '
                    'anyway')

        ctx = ReviewContext(pwork, cser, series_data)
        ctx.version = version
        ctx.patch_count = patch_count

        result = _find_or_register(ctx, args, clean_name, link)
        if result is None:
            return 0
        ctx.series_id, ctx.svid = result

        ctx.upstream_branch = _get_upstream_branch(args, cser)
        ctx.main_repo = main_repo
        ctx.branch_name = branch_name
        wt_path = cser_helper.review_worktree_path(ctx.main_repo,
                                                   ctx.branch_name)
        tout.notice(f'Using review worktree {wt_path}')
        ctx.repo_path = gitutil.ensure_worktree(
            ctx.main_repo, wt_path, ctx.branch_name, ctx.upstream_branch)

        apply_ok = _apply_and_check(ctx, link)
        if apply_ok is None:
            # Every patch is already upstream; nothing to review
            return 0
        if not apply_ok:
            return 1

        if args.apply_only:
            tout.notice('Apply-only mode; skipping review')
            return 0

        ctx.patch_selection = parse_patch_selection(args.patches)
        ctx.reviewer_name, ctx.reviewer_email = _parse_reviewer(args)
        ctx.signoff = args.signoff or None
        if ctx.signoff:
            ctx.signoff = ctx.signoff.replace('\\n', '\n')
        ctx.spelling = args.spelling
        ctx.context = _read_context(args.context) if args.context else None
        ctx.comments_path = _write_comments_file(series_data, pwork)

        if getattr(args, 'coverity', False):
            ctx.coverity_text = _run_coverity(ctx, args)

        _run_and_store_reviews(ctx, args)
        workflow.reviewed(cser, ctx.series_id, ctx.svid)
        gitutil.remove_worktree(ctx.main_repo, wt_path)

        return 0
    finally:
        _release_review_lock(lock_fd)


NewVersion = namedtuple('NewVersion', 'desc,version,link,complete,active')

ScanResult = namedtuple('ScanResult', 'desc,version,link,returncode,output')


def _scan_new_versions(pwork, cser):
    """Find newer patchwork versions of already-reviewed series

    For each reviewed series, search patchwork by its cover-letter title
    for the highest version present that is newer than the latest reviewed
    one. The highest version is reported even when it is still incomplete,
    along with whether it has fully appeared, so the caller can wait for it
    rather than reviewing an older, now-superseded version.

    Args:
        pwork (Patchwork): Configured patchwork instance
        cser (Cseries): Open cseries instance

    Returns:
        list of NewVersion: one entry per series that has a newer version
    """
    series = cser.db.series_get_dict(reviews_only=True)
    found = []

    async def _scan():
        async with aiohttp.ClientSession() as client:
            for ser in series.values():
                max_ver = cser.db.series_get_max_version(ser.idnum)
                # Without a recorded reviewed version there is nothing to
                # compare against, so skip rather than treat every version
                # on patchwork as newer
                if not max_ver:
                    continue
                matches = await pwork.query_series(client, ser.desc)
                newer = [pws for pws in matches
                         if pws['name'] == ser.desc and
                         int(pws['version']) > max_ver]
                if not newer:
                    continue
                latest = max(newer, key=lambda pws: int(pws['version']))
                data = await pwork.get_series(client, latest['id'])
                received = data.get('received_total', 0)
                total = data.get('total', received)
                states = await pwork.get_series_patch_states(
                    client, latest['id'])
                active = any(state in ACTIVE_STATES for state in states)
                found.append(NewVersion(ser.desc, int(latest['version']),
                                        latest['id'], received >= total,
                                        active))

    loop = asyncio.get_event_loop()
    loop.run_until_complete(_scan())
    return found


def _build_review_command(args, link):
    """Build the argv to review one series in a child process

    Passes through the global and review options given to --scan so each
    child behaves like a manual 'patman review -s <link>'. Global options
    (project, patchwork URL) come before the 'review' subcommand; review
    options come after it.

    Args:
        args (Namespace): Command-line arguments
        link (str): Patchwork series link/ID

    Returns:
        list of str: Command to run
    """
    cmd = [sys.executable, '-m', 'patman']
    if getattr(args, 'project', None):
        cmd += ['-p', args.project]
    if getattr(args, 'patchwork_url', None):
        cmd += ['-P', args.patchwork_url]
    if getattr(args, 'verbose', False):
        cmd.append('-v')
    if getattr(args, 'debug', False):
        cmd.append('-D')
    # The scan has already checked the patch state, so the child need not
    cmd += ['review', '-s', str(link), '--any-state']
    if getattr(args, 'upstream', None):
        cmd += ['-U', args.upstream]
    if getattr(args, 'reviewer', None):
        cmd += ['--reviewer', args.reviewer]
    if getattr(args, 'base_branch', None):
        cmd += ['-b', args.base_branch]
    if getattr(args, 'gmail_account', None):
        cmd += ['--gmail-account', args.gmail_account]
    if getattr(args, 'signoff', None):
        cmd += ['--signoff', args.signoff]
    if getattr(args, 'spelling', None):
        cmd += ['--spelling', args.spelling]
    if getattr(args, 'context', None):
        cmd += ['-c', args.context]
    if getattr(args, 'model', None):
        cmd += ['--model', args.model]
    if getattr(args, 'create_drafts', False):
        cmd.append('--create-drafts')
    return cmd


def _child_env():
    """Build the environment for a 'python -m patman' child process

    The child re-invokes patman with 'sys.executable -m patman'. When
    patman is run from a source checkout rather than an installed package,
    a fresh interpreter cannot import it, so put the directory that holds
    the patman package on PYTHONPATH. This is where the parent found it:
    two levels up from this module (patman/review.py -> patman -> parent).
    For an installed patman the directory is already importable, so adding
    it changes nothing.

    Returns:
        dict: Environment for the child process
    """
    env = os.environ.copy()
    pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    existing = env.get('PYTHONPATH', '')
    env['PYTHONPATH'] = (pkg_parent + os.pathsep + existing if existing
                         else pkg_parent)
    return env


def _review_one_subprocess(args, desc, version, link):
    """Review a single series in a child process

    Runs 'patman review -s <link>' so the review has its own database
    connection, event loop and worktree, capturing its combined output to
    print as one block once it finishes.

    Args:
        args (Namespace): Command-line arguments
        desc (str): Series description (cover-letter title)
        version (int): Series version being reviewed
        link (str): Patchwork series link/ID

    Returns:
        ScanResult: outcome of the review
    """
    cmd = _build_review_command(args, link)
    proc = subprocess.run(cmd, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True, check=False,
                          env=_child_env())
    return ScanResult(desc, version, link, proc.returncode, proc.stdout)


def _print_scan_result(result, done, total):
    """Print the buffered output of a finished review as a labelled block

    Args:
        result (ScanResult): Result to print
        done (int): Number of reviews finished so far, including this one
        total (int): Total number of reviews being run
    """
    status = 'ok' if not result.returncode else f'failed ({result.returncode})'
    tout.notice('')
    tout.notice(f"===== [{done}/{total}] v{result.version} of "
                f"'{result.desc}' (link {result.link}): {status} =====")
    if result.output:
        tout.notice(result.output.rstrip())


def _review_stats(cser, link):
    """Count patches, comments and approvals for a reviewed series version

    Args:
        cser (Cseries): Open cseries instance
        link (str or int): Patchwork link of the reviewed version

    Returns:
        tuple or None: (num_patches, num_commented, num_approved), or None
            if the version is not in the database. A patch is 'commented'
            when its stored review requested changes and 'approved' when it
            was approved; the cover letter (seq 0) is not counted, and a
            patch the review had nothing to say about counts in neither
    """
    found = cser.db.series_find_by_link(link)
    if not found:
        return None
    _, _, _, svid = found
    num_patches = len(cser.db.pcommit_get_list(svid))
    approved = commented = 0
    for rev in cser.db.review_get_for_version(svid):
        if not rev.seq:
            continue
        if rev.approved:
            approved += 1
        else:
            commented += 1
    return num_patches, commented, approved


def _draft_undrafted(args, pwork, cser, dry_run=False):
    """Draft reviewed series whose reviews have no Gmail draft yet

    Finds each reviewed series whose latest version has stored reviews
    that are not yet in Gmail and creates the drafts from them, without
    re-running the review. This catches up a series reviewed by '--scan'
    without -d: run '--scan -d' again and the drafts are created.

    Args:
        args (Namespace): Command-line arguments
        pwork (Patchwork): Configured patchwork instance
        cser (Cseries): Open cseries instance
        dry_run (bool): True to report what would be drafted, not draft it

    Returns:
        int: Number of series drafted (or that would be drafted)
    """
    review_ids = {ser.idnum for ser in
                  cser.db.series_get_dict(reviews_only=True).values()}
    count = 0
    for svid, series_id, version in cser.db.series_get_all_max_versions():
        if series_id not in review_ids:
            continue
        reviews = cser.db.review_get_for_version(svid)
        if not reviews or not any(not rev.draft_id for rev in reviews):
            continue
        link = cser.db.ser_ver_get_link(series_id, version)
        if not link:
            continue
        count += 1
        if dry_run:
            tout.notice(f'Would create Gmail drafts for link {link} '
                        f'(v{version})')
            continue
        series_data = _fetch_series(pwork, link)[0]
        _draft_stored_reviews(args, reviews, series_data, pwork, cser)
    return count


def _do_scan(args, pwork, cser):
    """Scan patchwork for new versions of already-reviewed series

    For every reviewed series, look for a version on patchwork higher than
    the latest one reviewed. Only the highest version is considered: if it
    has not fully appeared on patchwork yet, the series is left to wait
    rather than reviewing an older, superseded version. Complete versions
    are reviewed, each in its own child process, up to --jobs at a time.

    Args:
        args (Namespace): Command-line arguments
        pwork (Patchwork): Configured patchwork instance
        cser (Cseries): Open cseries instance

    Returns:
        int: 0 on success, 1 if any review failed
    """
    found = _scan_new_versions(pwork, cser)
    make_drafts = getattr(args, 'create_drafts', False)

    # With -d we also catch up drafts for series reviewed earlier without
    # it, so an empty scan is not the end of the story
    if not found and not make_drafts:
        tout.notice('No new versions found')
        return 0

    any_state = getattr(args, 'any_state', False)
    to_review = []
    waiting = 0
    skipped = 0
    for new in found:
        if not new.active and not any_state:
            skipped += 1
            tout.notice(f"Skipping v{new.version} of '{new.desc}' "
                        '(not active on patchwork)')
        elif new.complete:
            tout.notice(f"New version v{new.version} of '{new.desc}'")
            to_review.append(new)
        else:
            waiting += 1
            tout.notice(f"Waiting for v{new.version} of '{new.desc}' "
                        'to fully appear')

    total = len(to_review)
    if getattr(args, 'dry_run', False):
        for new in to_review:
            tout.notice(f"Would review v{new.version} of '{new.desc}'")
        drafted = (_draft_undrafted(args, pwork, cser, dry_run=True)
                   if make_drafts else 0)
        draft_str = f', {drafted} to draft' if make_drafts else ''
        tout.notice(f'Dry run: {len(found)} new, {total} to review, '
                    f'{waiting} waiting, {skipped} skipped{draft_str}')
        return 0

    failed = 0
    if total:
        jobs = max(1, getattr(args, 'jobs', 1))
        tout.notice(f'Launching {total} review(s), {min(jobs, total)} '
                    'at a time')
        done = 0
        ok_links = set()
        with futures.ThreadPoolExecutor(max_workers=jobs) as pool:
            pending = [pool.submit(_review_one_subprocess, args, new.desc,
                                   new.version, new.link)
                       for new in to_review]
            for future in futures.as_completed(pending):
                result = future.result()
                done += 1
                _print_scan_result(result, done, total)
                if result.returncode:
                    failed += 1
                else:
                    ok_links.add(result.link)

        # Long-form summary of what each review found, in the order the
        # versions were listed above, before the one-line totals
        summarised = [new for new in to_review if new.link in ok_links]
        if summarised:
            tout.notice('')
            tout.notice('Review summary:')
            for new in summarised:
                stats = _review_stats(cser, new.link)
                if stats is None:
                    continue
                n_patches, commented, approved = stats
                tout.notice(
                    f'  {new.link}: {n_patches} patches, {commented} with '
                    f'comments, {approved} approved - {new.desc}')

    # Catch up drafts for series reviewed earlier without -d (the new
    # versions just reviewed are drafted by their child run already)
    drafted = _draft_undrafted(args, pwork, cser) if make_drafts else 0

    draft_str = f', {drafted} drafted' if make_drafts else ''
    tout.notice(f'Scanned: {len(found)} new, {total - failed} reviewed, '
                f'{waiting} waiting, {skipped} skipped, {failed} failed'
                f'{draft_str}')
    return 1 if failed else 0


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
        groups.setdefault(_clean_series_name(ser.desc), []).append(ser)

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
    global _AGENT_MODEL
    _AGENT_MODEL = getattr(args, 'model', None)

    if args.learn_voice:
        return _do_learn_voice(args, pwork)

    if args.sync:
        return _do_sync(args, cser)

    if args.relink:
        return _do_relink(args, cser)

    if args.scan:
        return _do_scan(args, pwork, cser)

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
        link, patch_seq = lookup_patch_series(pwork, args.patch)
        if not whole:
            args.patches = str(patch_seq)
    elif not link and has_patch_title:
        link, patch_seq = search_patch(pwork, args.patch_title)
        if not whole:
            args.patches = str(patch_seq)
    elif not link:
        link = search_series(pwork, args.title, getattr(args, 'version', None))

    return _review_link(args, pwork, cser, link)


VOICE_PATH = os.path.join(os.path.expanduser('~/.config/patman.d'),
                          'voice.md')

_VOICE_PROMPT = '''Analyse the following email reviews written by a single
person. These are code-review replies on the U-Boot mailing list.

Study the writing style carefully and produce a concise style guide that
captures this person's voice. Focus on:

- Tone (formal, casual, terse, friendly, etc.)
- Typical greeting and sign-off patterns
- How they quote code and structure comments
- Common phrases or idioms they use
- Level of detail in explanations
- How they phrase requests for changes vs suggestions
- How they express approval

Output ONLY the style guide in markdown, with no preamble, explanation,
or commentary before or after it. It should be usable as-is by an AI to
replicate this person's voice when writing reviews. Keep it under 50 lines.

=== EMAILS ===
{emails}
'''


VoiceParams = namedtuple('VoiceParams',
                         'source account pwork user_email list_email count')


async def _fetch_voice_gmail(account, list_email, user_email, max_results=20):
    """Fetch review emails from Gmail for voice learning

    Args:
        account (str or None): Gmail account to read from
        list_email (str): Mailing list email to filter by
        user_email (str): Reviewer's email, to skip own patch threads
        max_results (int): Maximum emails to fetch

    Returns:
        list of str: Review email bodies, or None on failure
    """

    if not gmail.check_available():
        return None

    tout.notice(f'Fetching sent review emails to {list_email}...')
    service = gmail.get_service(account)
    return gmail.fetch_sent_reviews(service, list_email, user_email,
                                   max_results)


async def _fetch_voice_patchwork(pwork, user_email, max_comments=20):
    """Fetch review comments from patchwork for voice learning

    Args:
        pwork (Patchwork): Configured patchwork instance
        user_email (str): Email to search for in comments
        max_comments (int): Number of comments to collect

    Returns:
        list of str: Comment bodies, or None on failure
    """

    if not pwork or not pwork.proj_id:
        tout.error('Patchwork project not configured')
        return None

    tout.notice(f'Fetching comments by {user_email} from patchwork...')
    async with aiohttp.ClientSession() as client:
        return await pwork.fetch_user_comments(client, user_email,
                                               max_comments)


async def learn_voice(vp):
    """Analyse past reviews and create a voice profile

    Fetches review text from the chosen source, sends it to a Claude agent
    for style analysis, and saves the result as voice.md

    Args:
        vp (VoiceParams): Voice learning parameters

    Returns:
        bool: True if the voice profile was created successfully
    """
    if not claude_mod.check_available():
        return False

    if vp.source == 'patchwork':
        bodies = await _fetch_voice_patchwork(vp.pwork, vp.user_email,
                                               vp.count)
    else:
        if not vp.list_email:
            tout.error('No mailing list email known; use -U to specify '
                       'an upstream')
            return False
        if not vp.user_email:
            tout.error('No reviewer email known; use --reviewer')
            return False
        bodies = await _fetch_voice_gmail(vp.account, vp.list_email,
                                          vp.user_email, vp.count)

    if not bodies:
        tout.error(f'No review text found from {vp.source}')
        return False

    tout.notice(f'Found {len(bodies)} reviews, analysing style...')

    # Write reviews to a temp file so the agent can read them
    # (avoids exceeding prompt size limits with many reviews)
    reviews_path = os.path.join(tempfile.gettempdir(),
                                'patman_voice_reviews.txt')
    combined = '\n\n--- NEXT EMAIL ---\n\n'.join(bodies)
    tools.write_file(reviews_path, combined, binary=False)

    prompt = _VOICE_PROMPT.format(emails=f'(see file: {reviews_path})')
    options = _agent_options(allowed_tools=['Read'],
        max_buffer_size=claude_mod.MAX_BUFFER_SIZE)

    success, result = await claude_mod.run_agent_collect(prompt, options)

    # Clean up temp file
    try:
        os.unlink(reviews_path)
    except OSError:
        pass

    if not success or not result.strip():
        tout.error('Failed to analyse writing style')
        return False

    os.makedirs(os.path.dirname(VOICE_PATH), exist_ok=True)
    tools.write_file(VOICE_PATH, result.strip() + '\n', binary=False)

    tout.notice(f'Voice profile saved to {VOICE_PATH}')
    return True


def learn_voice_sync(vp):
    """Synchronous wrapper for learn_voice()"""
    loop = asyncio.get_event_loop()
    return loop.run_until_complete(learn_voice(vp))


def get_voice():
    """Load the voice profile if it exists

    Returns:
        str or None: Voice profile text, or None if not configured
    """
    if os.path.exists(VOICE_PATH):
        return tools.read_file(VOICE_PATH, binary=False).strip()
    return None


_REFINE_PROMPT = '''Compare the AI-generated draft review with the review
that was actually sent by the reviewer. The differences reveal the
reviewer's preferences.

Read the two files below, then analyse what changed and why. Focus on:
- Phrasing the reviewer preferred over the AI's version
- Content the reviewer added or removed
- Tone or style adjustments
- Structural changes (ordering, quoting style, etc.)

AI DRAFT:
(see file: {draft_path})

ACTUALLY SENT:
(see file: {sent_path})

CURRENT VOICE PROFILE:
(see file: {voice_path})

Now output ONLY an updated voice profile in markdown. Keep everything
from the current profile that is still accurate, and add or revise
entries based on the draft-vs-sent differences. Keep it under 60 lines.
Do not include any preamble or commentary.
'''


async def refine_voice(draft_body, sent_body):
    """Refine the voice profile by comparing a draft with what was sent

    Args:
        draft_body (str): The AI-generated review text
        sent_body (str): The review text actually sent by the user

    Returns:
        bool: True if the voice profile was updated
    """

    if not claude_mod.check_available():
        return False

    voice = get_voice()
    if not voice:
        tout.notice('No voice profile to refine; run --learn-voice first')
        return False

    # Write files for the agent to read
    draft_path = os.path.join(tempfile.gettempdir(), 'patman_voice_draft.txt')
    sent_path = os.path.join(tempfile.gettempdir(), 'patman_voice_sent.txt')
    tools.write_file(draft_path, draft_body, binary=False)
    tools.write_file(sent_path, sent_body, binary=False)

    prompt = _REFINE_PROMPT.format(draft_path=draft_path,
        sent_path=sent_path, voice_path=VOICE_PATH)
    options = _agent_options(allowed_tools=['Read'],
        max_buffer_size=claude_mod.MAX_BUFFER_SIZE)

    tout.notice('Analysing draft vs sent differences...')
    success, result = await claude_mod.run_agent_collect(prompt, options)

    for path in (draft_path, sent_path):
        if os.path.exists(path):
            os.unlink(path)

    if not success or not result.strip():
        tout.error('Failed to refine voice profile')
        return False

    tools.write_file(VOICE_PATH, result.strip() + '\n', binary=False)

    tout.notice(f'Voice profile updated: {VOICE_PATH}')
    return True


def refine_voice_sync(draft_body, sent_body):
    """Synchronous wrapper for refine_voice()"""
    loop = asyncio.get_event_loop()
    return loop.run_until_complete(refine_voice(draft_body, sent_body))


_REPLY_PROMPT = '''You are a U-Boot maintainer. You previously reviewed a
patch and the author (or another reviewer) has replied to your review.
Decide whether a response is needed, and if so, draft one.

YOUR ORIGINAL REVIEW:
(see file: {review_path})

REPLY FROM {reply_from}:
(see file: {reply_path})

The patches are applied to the current source tree. You can use
'git show', Read, and Grep to examine the code.

IMPORTANT:
- Do NOT run 'git checkout' or switch branches
- Do NOT 'cd' to another directory

TASK:
1. Read the reply carefully
2. Decide if a response is needed:
   - If the author agrees and will fix → no response needed
   - If the author asks a question → research in the code and answer
   - If the author pushes back → evaluate their argument; agree if
     they are right, explain why if they are wrong
   - If another reviewer comments → only respond if you disagree or
     have additional information
3. If no response is needed, output ONLY: VERDICT: skip
4. If a response is needed, output in this format:

GREETING: <first name>
COMMENT:
> quoted text from their reply

Your response.

VERDICT: changes_needed

Rules:
- NEVER use backticks — this is plain-text email, not markdown.
  For functions, always use parentheses with no quotes: malloc() not
  'malloc()' or `malloc`. For other identifiers do not quote them
  unless they are common English words that might confuse the reader
- Use {spelling} spelling
- Be brief and direct
- If the author is right, concede gracefully
- If you need to push back, explain concisely with evidence from code
'''


async def handle_reply(ctx, review_body, reply_from, reply_body):
    """Generate a response to a reply on our review

    Args:
        ctx (ReviewContext): Review context (uses reviewer_name,
            reviewer_email, repo_path, signoff, spelling)
        review_body (str): Our original review text
        reply_from (str): Who replied (name <email>)
        reply_body (str): The reply text

    Returns:
        str or None: Response email body, or None if no response needed
    """

    if not claude_mod.check_available():
        return None

    # Write our review and the reply to temp files
    review_path = os.path.join(tempfile.gettempdir(),
                               'patman_reply_our_review.txt')
    reply_path = os.path.join(tempfile.gettempdir(),
                              'patman_reply_their_reply.txt')
    tools.write_file(review_path, review_body, binary=False)
    tools.write_file(reply_path, reply_body, binary=False)

    prompt = _REPLY_PROMPT.format(review_path=review_path,
        reply_path=reply_path, reply_from=reply_from,
        spelling=ctx.spelling)
    options = _agent_options(allowed_tools=['Bash', 'Read', 'Grep'],
        cwd=ctx.repo_path, max_buffer_size=claude_mod.MAX_BUFFER_SIZE)

    success, log = await claude_mod.run_agent_collect(prompt, options)

    # Clean up
    for path in (review_path, reply_path):
        if os.path.exists(path):
            os.unlink(path)

    if not success or not log.strip():
        return None

    greeting, verdict, comments = parse_review_output(log)
    if verdict == 'skip':
        return None

    if '<' in reply_from:
        reply_name = reply_from.split('<')[0].strip()
        reply_email = reply_from.split('<')[1].rstrip('>')
    else:
        reply_name = reply_from
        reply_email = ''

    # Build a context for the reply — the "author" here is the person
    # we are replying to, not the original series submitter
    reply_ctx = ReviewContext(ctx.pwork, ctx.cser,
        {'submitter': {'name': reply_name, 'email': reply_email}, 'date': ''})
    reply_ctx.reviewer_name = ctx.reviewer_name
    reply_ctx.reviewer_email = ctx.reviewer_email
    reply_ctx.signoff = ctx.signoff
    return format_review_email(reply_ctx, greeting, verdict, comments)


def handle_reply_sync(ctx, review_body, reply_from, reply_body):
    """Synchronous wrapper for handle_reply()"""
    loop = asyncio.get_event_loop()
    return loop.run_until_complete(
        handle_reply(ctx, review_body, reply_from, reply_body))
