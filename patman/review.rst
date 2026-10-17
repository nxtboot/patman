.. SPDX-License-Identifier: GPL-2.0+

Assisted patch review
=====================

Patman can review other people's patches from Patchwork using a Claude
agent, and create Gmail draft replies with the review comments. Nothing
is sent until you have checked a draft and chosen to send it.

:doc:`How patch review works </review-internals>` describes what the agents
do with each series.

Setting up
----------

Packages
~~~~~~~~

Patch review needs the Claude Agent SDK, and creating Gmail drafts needs the
Google API packages. Install them along with patman::

    pip install patch-manager[review,gmail]

On Debian and Ubuntu, the Google packages are also available with apt::

    sudo apt install python3-google-auth python3-google-auth-oauthlib \
        python3-google-auth-httplib2 python3-googleapi

Gmail API access
~~~~~~~~~~~~~~~~

1. Go to https://console.cloud.google.com and create (or select) a project.

2. Enable the **Gmail API**:

   - Navigate to **APIs & Services > Enabled APIs & services**
   - Click **Enable APIs and services**, search for "Gmail API", enable it

3. Configure the **OAuth consent screen**:

   - Navigate to **APIs & Services > OAuth consent screen**
   - Set publishing status to **Testing**
   - Under **Test users**, add your email address (e.g. sjg@chromium.org)

4. Create **OAuth client credentials**:

   - Navigate to **APIs & Services > Credentials**
   - Click **Create Credentials > OAuth client ID**
   - Application type: **Desktop app**
   - Download the JSON file

5. Save the credentials::

    mkdir -p ~/.config/patman.d
    mv ~/Downloads/client_secret_*.json ~/.config/patman.d/client_secret.json

The first time you create drafts, a browser window opens for OAuth
consent. After authentication, the token is cached in
``~/.config/patman.d/`` and reused for future runs.

Patchwork
~~~~~~~~~

Configure an upstream with its Patchwork URL::

    patman upstream add us https://source.denx.de/u-boot/u-boot.git \
        --patchwork-url https://patchwork.ozlabs.org
    patman patchwork set-project U-Boot us

:doc:`The Patchwork chapter </patchwork>` describes the patchwork commands.

Settings
~~~~~~~~

Add these to your ``~/.patman`` file to avoid repeating flags:

.. code-block:: ini

    [settings]
    signoff: Regards,\nSimon
    spelling: British

The ``signoff`` is appended to reviews that have comments (not to
clean Reviewed-by-only replies). The ``spelling`` setting controls
the spelling convention used in review comments.

Reviewing a series
------------------

The review applies the series to a branch in its own worktree, has an agent
review each patch and the cover letter, and stores the reviews in the
database.

Choosing what to review
~~~~~~~~~~~~~~~~~~~~~~~

Review a series by Patchwork link::

    patman review -s 497923 -U us --reviewer 'Your Name <your@email>'

Or search by cover-letter title::

    patman review -S 'boot/bootm: Disable interrupts' -U us \
        --reviewer 'Your Name <your@email>'

A title search reviews the most recent version by default. Use ``-V``
to review a specific version::

    patman review -S 'boot/bootm: Disable interrupts' -V 2 -U us \
        --reviewer 'Your Name <your@email>'

To review a single patch by its Patchwork patch ID (the series is
found automatically)::

    patman review -p 2219748

Or search for a patch by title::

    patman review -P 'Add SPL support for Qualcomm'

Both ``-p`` and ``-P`` review just the patch found. When you know a
patch but not the cover letter, add ``-w`` to review the whole series
it belongs to instead::

    patman review -P 'Add SPL support for Qualcomm' -w

To review only specific patches by index within the series::

    patman review -s 497923 -i 1,3,5

Choosing the model
~~~~~~~~~~~~~~~~~~

By default the review uses whatever your Claude default model resolves
to. To review with a particular model instead, use ``--model``::

    patman review -s 497923 --model sonnet

This overrides your global Claude default for the review, so if your
default is set to something you would rather not review with, a review
still uses the model you name. Put ``model`` in ``.patman`` to pin it
for a project::

    [u-boot_settings]
    model: sonnet

To see the aliases ``--model`` accepts::

    patman review --list-models

The aliases (``opus``, ``sonnet``, ``haiku``) each select the latest
model in their tier; a full model id works too.

Choosing the base branch
~~~~~~~~~~~~~~~~~~~~~~~~

Use ``--apply-only`` to download and apply patches without running the
review -- useful for checking that patches apply cleanly.

By default the review branch is created from ``<upstream>/next`` when
that branch has commits ahead of the main branch, and from the main
branch otherwise. The main branch is ``<upstream>/main``, or
``<upstream>/master`` if the upstream has no ``main``. This tracks
U-Boot's release cadence: right after a release ``next`` is merged into
``main`` and stays empty until the next cycle's RC1, so reviews land on
``main`` during that window and switch back to ``next`` automatically
once it diverges.

Series often say what they are based on, which matters during the merge
window, when some are written against ``main`` and do not apply to
``next``. If the cover letter (or the last patch, if there is no cover
letter) has a ``base-commit:`` line, as added by ``git format-patch
--base`` and by patman, the review uses whichever of ``next`` and
``main`` holds that commit, or the commit itself if neither does. The
upstream is fetched first if the commit is not known yet. Without a base
commit, a cover letter which says it is 'based on' or 'applies to'
``main``, ``master`` or ``next`` picks that branch.

Use ``-b`` / ``--base-branch`` to override the choice for a particular
run::

    patman review -s 497923 -U us -b us/main \
        --reviewer 'Your Name <your@email>'

Active series only
~~~~~~~~~~~~~~~~~~

By default patman only reviews a series when at least one of its
patches is in an active patchwork state: new, RFC, under-review,
changes-requested or needs-review-ack. Reviewing a series
that patchwork has finished with (accepted, superseded, rejected and so
on) fails with a message naming the override. Use ``--any-state`` to
review regardless::

    patman review -s 497923 --any-state

Coverity static analysis
~~~~~~~~~~~~~~~~~~~~~~~~

With ``--coverity``, patman runs Coverity on the series and feeds the
new defects into the review. Coverity analyses a whole build rather
than a diff, so patman builds and analyses the base branch and the
patched branch separately and reports only the defects the series
introduces (matched by Coverity's mergeKey)::

    patman review -s 497923 -U us --coverity \
        --reviewer 'Your Name <your@email>'

This needs the ``cov-build``, ``cov-analyze`` and ``cov-format-errors``
tools on your PATH; if they are missing the check is skipped with a
warning. The build uses ``sandbox_defconfig`` by default; use
``--coverity-defconfig`` to pick another board. Building twice makes
this much slower than a normal review.

The new defects are passed to the review agent as context, so it can
raise the ones that fall in the code each patch changes.

New versions and reviewing again
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

When reviewing a new version of a previously reviewed series, patman
loads the most recent earlier version that has reviews as context for
the agent, so it can check whether earlier issues were addressed and avoid
raising fresh points the earlier versions did not. Versions are linked
by their cleaned cover-letter title.

An older database may have stored each version as a separate, unlinked
record, leaving follow-up reviews without this context. Repair it by
merging the split records, which backs up the database (to
``<db>.bak``) first::

    patman review --relink

Use ``-f`` / ``--force`` to re-review a series that has already been
reviewed. This deletes the old review records and runs the review
again::

    patman review -s 497923 -U us -f --reviewer 'Your Name <your@email>'

With ``-d``, the Gmail drafts from the previous review are deleted too,
so the re-review does not leave duplicates in the thread.

Reviewing your own series before sending
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

To have your own series reviewed before sending it, add the series with
``patman series add`` and then run::

    patman series review

This reviews the commits on the current branch (or ``-s <name>``) with
the same agent, along with the cover letter (stored as patch 0), and
saves the findings in the database, keyed by version. Because the
review is expensive, it is saved so you can read it later without
re-running::

    patman series info -r          # show the stored review
    patman series info -r 2 3      # just patches 2 and 3

Re-running is refused when a review is already stored; pass ``-f`` to
review again, replacing the stored one. Unlike ``patman review``, this
needs no patchwork link and creates no drafts -- it is purely a local
pre-send check. Reviews from the previous version are used as context,
so a follow-up review focuses on what changed.

Working with the reviews
------------------------

Reviews are kept in the database until you send them, as Gmail drafts which
reply to each patch.

Gmail drafts
~~~~~~~~~~~~

To create Gmail drafts threaded under the original emails::

    patman review -s 497923 -U us \
        --reviewer 'Your Name <your@email>' \
        -d --gmail-account your@email

Use ``-n`` with ``-d`` for a dry run that shows what would be created
without calling the Gmail API.

If a draft failed to create or was lost, ``--redraft`` recreates the
Gmail drafts from the stored reviews, even when the series has already
been reviewed and the drafts already exist::

    patman review -s 497923 --redraft --gmail-account your@email

Any existing draft is deleted first, so it is not left as a duplicate
in the thread.

If the reviewer email (from ``--reviewer`` or git config) differs from
the ``--gmail-account``, patman sets the From header on the draft so
the email is sent with the correct identity.

Revising reviews
~~~~~~~~~~~~~~~~

To have the agent change reviews which have not been sent yet, say what you
want in a note with ``--revise``. It changes only what the note asks for,
and can check the code using the review branch. It also sees the cover
letter, the other reviews of the series, the comments from patchwork and
your writing voice, so that the new text fits with the rest. Use ``-i`` to pick which
patches' reviews it looks at (0 is the cover letter), and ``-d`` to update
the Gmail drafts of the reviews which change::

    patman review -s 497923 -U us -i 4 -d \
        --revise 'Ask for a timeout rather than an unbounded loop'
    patman review -s 497923 -U us --revise 'Drop the comments about style'

The review approves the patch (with a ``Reviewed-by`` tag) if the note says
the patch is fine, and stops approving it if the note asks for a change.

Sending the reviews
~~~~~~~~~~~~~~~~~~~

Once you are happy with the drafts, you can send them without opening Gmail.
``--send`` sends the Gmail drafts of a series' reviews, the cover letter's
first, and records each review as sent, with the text which was sent, in
case you changed it in Gmail. Use ``-i`` to send only some of them, and
``-n`` to see what would be sent::

    patman review -s 497923 -U us --send -n
    patman review -s 497923 -U us --send -i 0,2

Syncing drafts
~~~~~~~~~~~~~~

After editing and sending (or deleting) Gmail drafts::

    patman review --sync --gmail-account your@email \
        --reviewer 'Your Name <your@email>'

This:

- Detects which drafts have been sent and records the final email
  content in the database (for context when reviewing future versions)
- Detects deleted drafts (review not sent)
- Refines the voice profile if the sent text differs from the draft,
  for up to three reviews written in the last two weeks
- Records replies to your reviews, from the patch author or other
  reviewers, in reviews sent in the last 90 days
- Suggests a response to each new reply from the last 14 days when one
  is needed (e.g. answering questions, pushing back on objections, or
  conceding gracefully), or notes that none is needed. Older replies are
  recorded without a suggestion, so that a backlog does not cause a lot of
  work for the agent; use ``--suggest-reply`` for any that need one
- Records replies which you have already answered as answered
- Detects when a draft response has been sent

A review counts as sent once you have sent a message about the same patch
(the same subject, version and patch number) after the review was written,
whether that is the draft itself or a reply you wrote instead. The message is
looked for in the Gmail thread of the draft, which is noted each time
``--sync`` runs while the draft is pending, or else by searching your sent
mail for a distinctive phrase from the review or for the patch's subject. A
message which only shares the subject must be sent within 14 days of the
review. Pending drafts are checked like this for reviews from the last 90
days; older ones are only checked to see whether the draft has gone. A review
is only marked as deleted if its draft has gone and no such message is found.

Only messages which reply to your review are recorded; other messages in
the same thread, and your own, are ignored.

Responding to replies
~~~~~~~~~~~~~~~~~~~~~

To see the replies which still need a response, and act on them::

    patman review --replies             # list them, with their IDs
    patman review --suggest-reply 12    # ask the agent (again) for a response
    patman review --draft-reply 12      # create a Gmail draft of it
    patman review --send-reply 12       # send it

The draft is threaded as a reply to the reply, with a 'Re:' subject, and is
copied to everyone the reply went to. ``--send-reply`` sends the draft as it
is in Gmail, so any changes made there are kept, and records the text which
was sent. If the response has no draft yet, or has been changed since the
draft was created, the draft is created first. If the draft has been deleted
in Gmail, nothing is sent.

The response can also be edited, drafted and sent from the Reviews tab of
``patman serve``: tap 'Send' under it, or 'Edit response' then 'Save and
send'. Both need a second tap to confirm.

Scanning for new versions
~~~~~~~~~~~~~~~~~~~~~~~~~

Rather than reviewing one series at a time, ``--scan`` looks on
patchwork for new versions of series you have already reviewed and
reviews them::

    patman review --scan -U us --reviewer 'Your Name <your@email>'

For each reviewed series it takes the highest version on patchwork
above the latest one reviewed. A version is reviewed only once it has
fully appeared on patchwork; if the newest version is still arriving,
the series is left to wait rather than reviewing an older, now
superseded one. Inactive series (see `Active series only`_) are
skipped.

Reviews run in parallel, each in its own child process and review
worktree, up to ``--jobs`` (``-j``) at a time (default 4).

Each review's output is buffered and printed as one block when it
finishes, prefixed with a ``[done/total]`` counter. Once all the
reviews finish, a per-series summary lists, for each reviewed series,
its link, patch count, how many patches drew comments, how many were
approved and the title, followed by the one-line totals::

    Review summary:
      511354: 11 patches, 0 with comments, 3 approved - Qualcomm IPQ5210 SoC bringup
      511094: 6 patches, 5 with comments, 0 approved - Improve U-Boot's TPM handling
    Scanned: 3 new, 1 reviewed, 1 waiting, 1 skipped, 0 failed

A patch counts as approved when its review approved it and as commented
when the review requested changes; a patch the review had nothing to
say about counts as neither.

Add ``-d`` to create Gmail drafts as it goes. This also catches up any
series reviewed by an earlier scan without ``-d``: their reviews are
already stored, so ``patman review --scan -d`` drafts them from the
database without reviewing them again. The one-line summary then ends
with how many were drafted::

    Scanned: 0 new, 0 reviewed, 0 waiting, 2 skipped, 0 failed, 13 drafted

Use ``-n`` / ``--dry-run`` to see which series would be reviewed,
waiting or skipped -- and, with ``-d``, which would be drafted --
without launching any reviews or creating any drafts.

Learning your writing voice
---------------------------

Build a voice profile so draft reviews match your writing style::

    # From Gmail (searches your sent reviews to the mailing list)
    patman review --learn-voice gmail -U us \
        --gmail-account your@email --reviewer 'Your Name <your@email>'

    # From Patchwork (scans your comments on recent patches)
    patman review --learn-voice patchwork -U us \
        --reviewer 'Your Name <your@email>'

Use ``--voice-count N`` to control how many reviews to collect
(default: 20). The voice profile is saved to
``~/.config/patman.d/voice.md`` and automatically used in subsequent
reviews.
