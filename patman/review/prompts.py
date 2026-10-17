# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2025 Canonical Ltd.
# Written by Simon Glass <simon.glass@canonical.com>
#

# pylint: disable=R0914

"""The prompts given to the review agents

This holds the prompts for applying a series, reviewing each patch and the
cover letter, refining and revising the reviews, and responding to replies,
so that they can be read and changed in one place.
"""

from patman.review import voice


def build_apply_prompt(mbox_path, branch_name, upstream_branch):
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


# Rules for quoting and punctuation in the agent's comments, which apply to
# both patch and cover-letter reviews
QUOTING_RULES = """\
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
  omit the period, or use an em dash to start the next clause"""


def voice_section():
    """Get the prompt section asking the agent to write in the user's voice

    Returns:
        str: Section text, or '' if no voice has been learned
    """
    profile = voice.get_voice()
    if not profile:
        return ''
    return f'''
WRITING STYLE:
Match this voice when writing your comments:
{profile}
'''


def _context_section(ctx):
    """Get the prompt section with the reviewer's notes for this run

    Args:
        ctx (ReviewContext): Review context (uses context)

    Returns:
        str: Section text, or '' if there are no notes
    """
    if not ctx.context:
        return ''
    return f'''
USER CONTEXT (extra notes from the reviewer for this run):
{ctx.context}
'''


def build_review_prompt(ctx, commit_hash, seq, all_commits,
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

    style = voice_section()

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

    context_section = _context_section(ctx)

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
{style}
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
{QUOTING_RULES}
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


def build_cover_review_prompt(ctx, all_commits, previous_review=None):
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
    style = voice_section()
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

    context_section = _context_section(ctx)

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
{style}
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
{QUOTING_RULES}
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


REFINE_REVIEWS_PROMPT = '''You are editing draft code-review \
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
''' + QUOTING_RULES + '''
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


def revise_context(cover, others, comments_path):
    """Get the prompt section with the rest of the series, for a revision

    Args:
        cover (str or None): Text of the cover letter, if any
        others (dict): Map of seq to the body of each review which is not
            being revised
        comments_path (str or None): File holding the comments which the
            series has received on patchwork, if any

    Returns:
        str: Section text, or '' if there is nothing to add
    """
    parts = []
    if cover:
        parts.append(f'''
COVER LETTER:
{cover}
''')
    if others:
        reviews = '\n'.join(f'---SEQ {seq}---\n{body}'
                            for seq, body in sorted(others.items()))
        parts.append(f'''
OTHER REVIEWS OF THE SERIES (for context only; do NOT change or output these):
{reviews}
''')
    if comments_path:
        parts.append(f'''
EXISTING COMMENTS:
Other people may have commented on this series. Read the file below for
context, and do not add points which they have already made.
(see file: {comments_path})
''')
    return ''.join(parts)


REVISE_PROMPT = '''You are a U-Boot maintainer. You drafted the code
reviews in the file below, but they have not been sent yet. The reviewer has
read them and wants them changed, as described in their note.

NOTE FROM THE REVIEWER:
{note}

REVIEWS:
(see file: {reviews_path})

Each review in the file starts with a line '---SEQ N---', where N is the
patch number (0 is the cover letter).

{patches}
{context}
TASK:
- Change the reviews as the note asks. If the note quotes part of a review,
  it is about that part.
- If the note applies to more than one review, e.g. 'drop the comments about
  style', apply it to each of them
- Change only what the note asks for, keeping the rest of each review as it
  is, word for word
- Check the code if you need to, with 'git show' on the commits above. Do not
  read files in the working tree, since it does not hold the patches.
- Do NOT run 'git checkout', switch branches or change any files
- Keep the email format: the greeting, the quoted lines starting with '>',
  the comments after each quote and the closing lines
- If the note says a patch is fine and its review has no comments left,
  approve it by ending the review with: Reviewed-by: {tag}
- If the note asks for a change to a patch which its review approves, remove
  the Reviewed-by line
- Use {spelling} spelling
{voice}
OUTPUT:
Output ONLY the reviews you changed, each as the line '---SEQ N---' followed
by the complete new text of that review. Leave out reviews which need no
change. Do not add any commentary.
'''


REPLY_PROMPT = '''You are a U-Boot maintainer. You previously reviewed a
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
''' + QUOTING_RULES + '''
- Use {spelling} spelling
- Be brief and direct
- If the author is right, concede gracefully
- If you need to push back, explain concisely with evidence from code
'''
