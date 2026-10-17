.. SPDX-License-Identifier: GPL-2.0+

Reviewing a series
==================

The earlier pages are about sending your own patches. ``patman review`` is
for the other side: it finds a series on patchwork, applies it to a branch,
has a Claude agent review each patch and the cover letter, and keeps the
reviews, ready to send as replies.

This uses a real series of two patches on the U-Boot concept tree,
``debian: Clean up qemu package targets`` (series 155 on
patchwork.deinde.dev). All the output is from running patman on it.

Setting up
----------

Install patman with the extra packages for review::

    pip install patch-manager[review]

The agent runs through Claude Code. The Claude Agent SDK, which the
``review`` extra installs, has its own copy, but patman uses the ``claude``
command if you have it installed, so that updating Claude Code is enough to
use the newest models. Log in to Claude Code once by running ``claude``, or
set ``ANTHROPIC_API_KEY``.

patman finds the series on the upstream's patchwork server, so add the
upstream and set its patchwork project, as in :doc:`upstreams`::

    $ patman upstream add ci https://concept.u-boot.org/u-boot/u-boot.git \
        U-Boot -p https://patchwork.deinde.dev -t concept -m --no-tags
    $ patman patchwork set-project U-Boot ci
    Project 'U-Boot' patchwork-ID 1 link-name 'uboot' remote 'ci'

Reviewing the series
--------------------

Give the series by its ID on patchwork, along with the upstream and how you
sign your reviews. This series has since been merged, so to review it on the
tree as it was when it was sent, ``-b`` gives the branch to apply it to,
here one made at the commit before it::

    $ git branch review-base b03ebd44ad1
    $ patman review -s 155 -U ci -b review-base \
        --reviewer 'Simon Glass <sjg@chromium.org>'
    Series: debian: Clean up qemu package targets
    Version: 1, Patches: 2
    Added series 'debian: Clean up qemu package targets' v1 to database
    Using review worktree .../.git/patman/worktrees/pw-155-review
    Downloading mbox for series 155 from https://patchwork.deinde.dev
    Downloaded 15522 bytes to /tmp/patman_review_155.mbox
    Applying series to branch pw-155-review...
    Both patches applied cleanly with `git am`, so I didn't need the recovery steps.

    - **Applied:** 2 of 2
      - `529c0419451` debian: Drop the uboot.elf target from qemu builds
      - `36679967683` debian: Drop u-boot.bin from x86 qemu targets
    - **Skipped:** none
    - **Branch:** pw-155-review (on top of review-base)
    Patches applied to branch: pw-155-review

Without ``-b``, patman applies the series to the commit given by its
``base-commit:`` line, if it has one, or to the upstream's ``next`` or main
branch. It does this in a worktree of its own, so your checkout is left
alone, and an agent applies the patches, dealing with any which need help.

Then an agent reviews the cover letter and each patch, with the whole
series and the code around it to look at::

    Reviewing series (cover letter)...
    VERDICT: skip
    Reviewing patch 1/2...
    GREETING: Simon
    COMMENT:
    ...
    > -  qemu-ppce500_targets := u-boot.bin uboot.elf
    > +  qemu-ppce500_targets := u-boot.bin

    One thing I'd like to check before this goes in.. QEMU's ppce500 machine
    loads its -bios image with load_elf() and then falls back to
    load_uimage(). It cannot load a raw binary, which is why
    doc/board/emulation/qemu-ppce500.rst uses '-bios u-boot'. ...

    VERDICT: changes_needed
    Reviewing patch 2/2...
    GREETING: Simon
    VERDICT: approved
    Refining review drafts...

The cover letter has nothing to add beyond the patches, so it gets no
reply. Patch 1 needs changes: the agent found that one of the QEMU boards
cannot boot the image which the patch leaves it with. Patch 2 is approved.
A last agent then reads the reviews together, to tidy them up and remove
points made twice.

patman then shows each review as the email it would send, e.g. for patch 2::

    --- Patch 2/2: [Concept,2/2] debian: Drop u-boot.bin from x86 qemu targets ---
    ---email start---
    On 2026-03-26T18:27:45, Simon Glass <sjg@u-boot.org> wrote:
    > debian: Drop u-boot.bin from x86 qemu targets
    >
    > The x86 qemu builds only need u-boot.rom, since that is what qemu
    > loads via the -bios option.
    ...

    Reviewed-by: Simon Glass <sjg@chromium.org>

    ---email end---

    Use --create-drafts to create Gmail review drafts.

The reviews are kept in the database, so running the same command again
shows them without reviewing the series again; ``-f`` reviews it afresh. To
keep reviews of other people's series apart from your own series, set
``review_db_dir`` in ``~/.patman`` (see :doc:`/db`).

Changing a review
-----------------

Before sending, you may want a review changed. Say what you want in a note
with ``--revise``, using ``-i`` to pick the patch::

    $ patman review -s 155 -U ci -i 1 \
        --revise 'Drop the comment about the lintian override'
    Series: debian: Clean up qemu package targets
    Version: 1, Patches: 2
    Revising reviews, carrying on from the review...
    ---SEQ 1---
    Hi Simon,
    ...
    Revised the review of patch 1

When one review is revised, the agent carries on from the session which
wrote it, so it knows what it found while reviewing; here it dropped the
last comment and left the rest of the review word for word. It also sees
the cover letter, the other reviews and the comments from patchwork.

Sending the reviews
-------------------

To send the reviews, patman creates a Gmail draft of each, threaded as a
reply to the patch, which you can check and change in Gmail. This needs the
``gmail`` extra and access to the Gmail API, which
:doc:`the review chapter </review>` explains how to set up. Then::

    $ patman review -s 155 -U ci --create-drafts --gmail-account you@example.com

creates the drafts from the stored reviews, and::

    $ patman review -s 155 -U ci --send

sends them, recording the text which was sent. ``patman review --sync``
later notices drafts you sent from Gmail yourself, and replies to your
reviews.

Following new versions
----------------------

When a new version of a series you reviewed appears on patchwork,
``patman review --scan`` finds it and reviews it, giving the agent your
review of the earlier version, so that it checks whether your points were
dealt with rather than starting again.

:doc:`/review` describes all of this in full, and :doc:`serve` shows how to
follow and work on reviews from a phone.
