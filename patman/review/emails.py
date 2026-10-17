# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2025 Canonical Ltd.
# Written by Simon Glass <simon.glass@canonical.com>
#

# pylint: disable=R0914

"""Parsing the agents' reviews and formatting them as emails

The review agents write their findings in a structured form, with a
greeting, quoted code with comments, and a verdict. This parses that form,
turns it into the text of a review email, tidies the text up and has an
agent refine the reviews of a series together.
"""

import re

from u_boot_pylib import claude as claude_mod
from u_boot_pylib import tout

from patman.review import common
from patman.review import prompts
from patman.review import voice


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
        for dl in diffstat.strip('\n').splitlines():
            lines.append(f'> {dl}')
    lines += ['', f'Reviewed-by: {ctx.reviewer_tag}', '']
    return '\n'.join(lines)


def is_code_comment(hunk):
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
        idx = (None if is_code_comment(hunk)
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
        ds_lines = diffstat.strip('\n').splitlines()
        if len(ds_lines) <= 30:
            lines.append('>')
            for dl in ds_lines:
                lines.append(f'> {dl}')
    lines.append('')

    # Keep commit-message comments (no diff header) before code comments;
    # sorted() is stable, so the order within each group is preserved
    below.sort(key=lambda hc: is_code_comment(hc[0]))
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


def parse_refined_output(log):
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

    profile = voice.get_voice()
    voice_section = ''
    if profile:
        voice_section = f'''WRITING STYLE:
Match this voice when editing the reviews:
{profile}
'''

    prompt = prompts.REFINE_REVIEWS_PROMPT.format(drafts=drafts_text,
        voice_section=voice_section, spelling=spelling)

    options = common.agent_options(allowed_tools=[],
        max_buffer_size=claude_mod.MAX_BUFFER_SIZE)

    tout.notice('Refining review drafts...')
    success, log = await claude_mod.run_agent_collect(prompt, options)
    if not success or not log.strip():
        tout.warning('Refinement failed; using original drafts')
        return review_bodies

    refined = parse_refined_output(log)

    # Keep originals for any the agent missed
    result = dict(review_bodies)
    for seq, body in refined.items():
        if seq in result and body:
            result[seq] = body
    return result


def refine_reviews_sync(review_bodies, spelling='British'):
    """Synchronous wrapper for refine_reviews()"""
    return common.run(refine_reviews(review_bodies, spelling))


def format_findings(verdict, comments):
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


def print_finding(seq, total, subject, verdict, comments):
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
