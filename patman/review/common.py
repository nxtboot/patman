# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2025 Canonical Ltd.
# Written by Simon Glass <simon.glass@canonical.com>
#

"""Common code for the review modules

This holds the review context, the options for the Claude agents and helpers
for running coroutines and calling patchwork, which the other review modules
share.
"""

import asyncio
import re
import shutil
import uuid

import aiohttp

from u_boot_pylib import gitutil

try:
    from claude_agent_sdk import ClaudeAgentOptions
except ImportError:
    ClaudeAgentOptions = None


# Claude model to use for the current review run, or None to let the SDK
# use whatever model the user's Claude default resolves to. Set once at the
# start of do_review() from --model / the 'model' setting, then read by
# agent_options() so every agent in the run uses the same model
_AGENT_MODEL = None


def run(coro):
    """Run a coroutine on the event loop and wait for it to finish

    Args:
        coro (coroutine): Coroutine to run

    Returns:
        The coroutine's result
    """
    return asyncio.get_event_loop().run_until_complete(coro)


def pw_call(method, *args):
    """Call a Patchwork method which needs a client session, and wait for it

    Args:
        method (coroutine function): Patchwork method, which takes the client
            session as its first argument
        args: Further arguments for the method

    Returns:
        The method's result
    """
    async def _call():
        async with aiohttp.ClientSession() as client:
            return await method(client, *args)
    return run(_call())


def set_agent_model(model):
    """Set the Claude model for the agents in this review run

    Args:
        model (str or None): Model to use, or None for the SDK default
    """
    global _AGENT_MODEL  # pylint: disable=W0603
    _AGENT_MODEL = model


def agent_options(**kwargs):
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


def session_options(ctx, seq, **kwargs):
    """Create agent options for reviewing a patch in a new, recorded session

    The session ID is kept in ctx.session_ids so that it can be stored with
    the review and the session resumed later, e.g. to revise the review.

    Args:
        ctx (ReviewContext): Review context
        seq (int): Patch number, or 0 for the cover letter
        kwargs: Fields to pass to ClaudeAgentOptions

    Returns:
        ClaudeAgentOptions: Configured options
    """
    ctx.session_ids[seq] = str(uuid.uuid4())
    return agent_options(session_id=ctx.session_ids[seq], **kwargs)


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
        # Agent session ID used to review each patch (0 for the cover letter)
        self.session_ids = {}
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


def parse_reviewer(args):
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
