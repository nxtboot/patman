Changelog
=========

All notable changes to this project are documented here. The format is
based on `Keep a Changelog <https://keepachangelog.com/en/1.1.0/>`_ and
the project follows `Semantic Versioning <https://semver.org/>`_.

Unreleased
----------

0.3.1 - 2026-10-07
------------------

Fixed
~~~~~
- The bundled ``u_boot_pylib`` library is installed inside the ``patman``
  package rather than as a top-level package, where it shadowed U-Boot's own
  copy for U-Boot's tools in the same environment and clashed with other
  packages which provide it, such as ``binary-manager`` and ``buildman``.
- The cover-letter review reply now goes to the whole original audience.
  Its headers were never fetched, so the reply listed only the submitter
  and the mailing list and dropped the original To and Cc. Fetch the
  cover's headers, as the per-patch replies already do, so the reply is
  a proper reply-all.
- ``review`` no longer fails with "Failed to apply patches to branch"
  when a series is already applied upstream. If the apply step runs to
  completion but applies nothing (every patch is already present), that
  is now treated as "nothing to review" and the command exits cleanly
  instead of reporting an error.

0.3.0 - 2026-09-27
------------------

Added
~~~~~
- ``send -I`` / ``--identity`` selects the git send-email identity (the
  ``[sendemail "<identity>"]`` config section) for a single run,
  overriding any identity configured on the upstream.

Changed
~~~~~~~
- ``review --scan -d`` now also creates Gmail drafts for series reviewed
  by an earlier scan without ``-d``, drafting them from the stored
  reviews without reviewing again. ``-n`` previews which would be
  drafted, and the summary reports how many were.
- Review comments are no longer hard-wrapped: the agent is told to write
  each paragraph on a single line so the reader's mail client wraps it.
  Quoted ``> `` lines and indented code are left as they are.
- The ``Created N Gmail draft(s)`` line (and the dry-run equivalent) now
  names the review branch and the series link, so it is clear which
  series the drafts belong to.
- A review now answers each commit-message comment inline, right after
  the quoted line it refers to, instead of quoting the whole message at
  the top and then re-quoting individual lines again lower down. Code
  comments still follow the quoted message with their diff hunk.

Fixed
~~~~~
- Creating Gmail drafts no longer fails with "Invalid Cc header" when a
  patch's To/Cc headers were stored by patchwork as folded RFC 2047
  ``unknown-8bit`` encoded words: the headers are now unfolded and
  decoded (treating ``unknown-8bit`` as UTF-8), and the addresses
  de-duplicated and re-encoded.
- A review no longer restyles the author's quoted code: the cleanup
  that converts double quotes to single (to match our prose) skipped
  the ``> `` quoted lines, so a string literal such as ``"some_key"`` is
  reproduced faithfully instead of becoming ``'some_key'``.

0.2.0 - 2026-07-15
------------------

Added
~~~~~
- ``review --scan`` prints a per-series summary before its totals: one
  line per reviewed series giving the link, patch count, how many
  patches drew comments, how many were approved and the title.
- ``review --model`` chooses the Claude model to review with (e.g.
  ``sonnet``, ``opus`` or a full model id), overriding your global
  Claude default so a review need not use a personal default such as
  Fable. Set ``model`` in ``.patman`` to pin it for a project.
  ``review --list-models`` prints the aliases it accepts.
- ``review -w`` / ``--whole-series`` reviews the whole series when you
  locate it with ``-p`` / ``-P``, instead of just the one patch. Useful
  when you know a patch but not the cover letter.
- ``review -V`` selects which version of a series to review when
  searching by title (``-S``); it defaults to the most recent.
- ``send`` can post a series to a web submission endpoint (a relay, as
  b4 uses) instead of ``git send-email``, for contributors without
  working outbound SMTP. Set ``send_endpoint_web`` (or
  ``--send-endpoint-web``); each message is attested with patatt, and
  ``--reflect`` sends the series back to you only as a test.
  ``send --web-auth-new`` / ``--web-auth-verify`` register your key and
  identity with the endpoint, and ``--no-relay`` sends with git
  send-email for one run. Threading (``--thread``) and ``--in-reply-to``
  are honoured. Signing uses patatt, now a core dependency.

Fixed
~~~~~
- A branch whose name has digits in the middle, such as ``rv1106e2``,
  now takes its version from the trailing digits like any other. Its
  version was read as 1 rather than 2, so ``series add`` failed with
  "suggests version 1 but Series-version tag indicates 2".

0.1.0 - 2026-07-05
------------------

Added
~~~~~
- A tracked ``.patman-defaults`` file at the repo root provides
  project-shipped config defaults that ``~/.patman``, a local ``.patman``
  and command-line arguments all override, per key. It is the new
  lowest-priority config layer, so a project can ship a working setup
  without taking control away from the user.
- ``series review`` runs an AI review of your own series (added with
  ``series add``) -- the patches and the cover letter -- and stores the
  findings in the database, so you can check a series before sending it
  and re-read the review later with ``series info -r`` without paying
  for it again.
- ``review --coverity`` runs Coverity on the base and the patched series
  and feeds the newly introduced defects into the review as context.
  ``--coverity-defconfig`` selects the board to build (default
  sandbox_defconfig). Needs the cov-* tools on PATH.
- ``review --scan`` looks on patchwork for new versions of series that
  have already been reviewed and reviews the latest version, once it has
  fully appeared on patchwork. Reviews run in parallel, each in its own
  child process and worktree, up to ``--jobs`` at a time (default 4).
  ``-n`` / ``--dry-run`` shows which series would be reviewed, waiting
  or skipped, without launching any reviews.
- ``review --redraft`` recreates the Gmail drafts from the stored
  reviews even when a draft already exists, to recover after an error.
- ``review --relink`` repairs a database where versions of a series were
  stored as separate records, merging them so follow-up reviews see the
  earlier feedback. It backs up the database first.

Fixed
~~~~~
- A review stored each series version under its raw '[vN,0/M] ...' title,
  so versions of one series did not link and each follow-up review ran
  without the earlier feedback and raised fresh points. Store the cleaned
  title instead, so versions link. Existing databases can be repaired
  with ``review --relink``.
- Prior-review context now comes from the most recent earlier version
  that has reviews, rather than strictly the immediately previous one, so
  a gap in the version history no longer drops the context.
- ``review -f`` and ``review --redraft`` delete the Gmail drafts they
  replace, instead of leaving the old ones orphaned as duplicates in the
  same thread.
- A patch the review agent has nothing to say about (no comments and not
  an approval) no longer produces an empty greeting-only review; the
  patch is left unreviewed instead.
- Comments on the commit message are now placed before comments on the
  code in a review, rather than in whatever order the agent emitted them.
- Reviews attach to the correct patchwork patch by subject rather than by
  position. When a patch fails to apply, leaving fewer commits on the
  branch than the series has patches, the remaining reviews no longer
  shift onto the wrong patches.

Changed
~~~~~~~
- ``review`` only reviews a series when at least one of its patches is in
  an active patchwork state (new, RFC, under-review, changes-requested or
  needs-review-ack); reviewing an inactive series fails with a message
  naming ``--any-state``, which overrides the check. ``review --scan``
  skips inactive series.
- A review now takes an exclusive lock on its series, so a second review
  of the same series is refused rather than corrupting its worktree and
  records.

0.0.11 - 2026-06-18
-------------------

Added
~~~~~
- A project logo, shown in the documentation and the README.

Fixed
~~~~~
- ``autolink`` finds a series whose cover-letter title changed in a
  later version: it searches patchwork by the current title rather than
  the title recorded when the version was created.

0.0.10 - 2026-06-15
-------------------

Fixed
~~~~~
- ``status --dest-branch`` (building a branch with the gathered review
  tags) works again on pygit2 1.16 and newer, where ``merge_trees()``
  no longer accepts a branch object.

0.0.9 - 2026-06-15
------------------

Fixed
~~~~~
- ``series status`` and ``gather`` now work on archived series, reading
  the patches from the archive tag rather than the deleted branch.

Changed
~~~~~~~
- Update the pinned dependencies to current releases and support the
  pygit2 1.15 API.
- Reorganise the documentation: it lives under ``doc/``, includes the
  changelog, can be built with ``make -C doc html``, and reads as a
  general patch tool rather than a U-Boot-specific one.

0.0.8 - 2026-06-08
------------------

Added
~~~~~
- ``patman review -c/--context`` passes extra notes to the review agent
  for a single run -- either a literal string or ``@path`` to read the
  text from a file.
- ``patman series changes <text>`` records a ``Series-changes`` (or,
  with ``-c``, ``Cover-changes``) bullet on the HEAD commit and amends
  it, instead of hand-editing the commit message.
- Documentation is now published at https://patman.readthedocs.io/.

Changed
~~~~~~~
- Follow-up reviews respect the "say it in v1" convention: later
  versions confirm whether earlier feedback was addressed rather than
  piling on fresh nits.
- A partial patch apply now warns and continues with whatever applied,
  instead of aborting and discarding the work.
- Patman is now distributed as the self-contained ``patch-manager``
  package, with ``u_boot_pylib`` vendored in, installable from PyPI
  with ``pip install patch-manager``.

Earlier releases
----------------

Releases 0.0.7 and earlier predate this changelog.
