.. SPDX-License-Identifier: GPL-2.0+

Common problems
===============

These are messages you may see when using patman, what causes them and how
to deal with them. The output is from patman, run on branches of the U-Boot
tree and on series on patchwork.

The upstream branch is not known
--------------------------------

::

    $ patman send -n
    patman: ValueError: Failed to determine upstream: fatal: no upstream configured for branch 'nouser'

patman counts the commits in the series from the upstream branch which the
branch tracks. Set it with::

    git branch --set-upstream-to us/master

or give the number of commits with ``-c``, e.g. ``patman send -c 3 -n``.
Branches created by ``patman status -d`` do not track an upstream branch, so
set one before sending from them.

Checkpatch problems stop the series being sent
----------------------------------------------

::

    $ patman send -n
    README:2378: error: trailing whitespace
    1 errors, 1 warnings, 0 checks for 0004-doc-Add-a-line-with-trailing-space.patch:
    checkpatch.pl found 1 error(s), 1 warning(s), 0 checks(s)
    Not sending emails due to errors/warnings

Fix the commit, e.g. with ``git rebase -i`` and 'edit', and run the dry run
again until it is clean. The patch number in the file name, ``0004`` here,
says which commit it is. Use ``-i`` to send in spite of the problems, or
``--no-check`` to skip checkpatch.

There is no one to send the series to
-------------------------------------

::

    $ patman send -n
    No recipient.
    Please add something like this to a commit
    Series-to: Fred Bloggs <f.blogs@napier.co.nz>
    Or do something like this
    git config sendemail.to u-boot@lists.u-boot-project.org

Add a ``Series-to:`` tag to one of the commits, e.g. ``Series-to: u-boot``
(see :doc:`/tags`). If you use ``patman series``, the upstream can give the
address instead (see :doc:`upstreams`).

An alias is not found
---------------------

::

    $ patman send -n
    Alias 'doc' not found

patman treats the tags at the start of each subject, such as ``doc:`` here,
as aliases to copy the patch to, and warns when there is no such alias. This
is harmless, but you can add the alias to the ``[alias]`` section of
``~/.patman``, use ``-t`` to stop the warnings, or ``--no-tags`` to stop
using subject tags as aliases. For an upstream which does not use them, add
it with ``--no-tags`` (see :doc:`upstreams`).

The commits are unmarked
------------------------

::

    $ patman series add
    patman: ValueError: 3 commit(s) are unmarked; please use -m or -M

``patman series`` can follow each patch by a ``Change-Id:`` tag. Use ``-m``
to add these to the commits, or ``-M`` to leave the commits as they are and
match patches by subject (see :doc:`series-workflow`).

The branch has no patchwork link
--------------------------------

::

    $ patman status
    patman: ValueError: Branch has no Series-links value

``patman status`` needs the ID of the series on patchwork. Add a
``Series-links:`` tag to the top commit (see :doc:`status-and-tags`), or, if
you use ``patman series``, let ``patman series autolink`` find it.

The patchwork project is not set
--------------------------------

::

    $ patman series -s align-fix autolink
    patman: ValueError: Patchwork project not configured; use 'patman patchwork set-project'

Commands which search patchwork for a series need its project. Set it once
for each remote, e.g. ``patman patchwork set-project U-Boot us`` (see
:doc:`/patchwork`).

The Series-to tag does not match the upstream
---------------------------------------------

::

    $ patman series -n send
    patman: ValueError: Series-to tag ['u-boot'] does not match expected 'concept' from upstream settings

The series is about to go to a different list from the one its upstream
sends to. Fix the ``Series-to:`` tag, drop it and let the upstream give the
address, or check that the series has the right upstream (see
:doc:`upstreams`).

The review agent is not available
---------------------------------

::

    $ patman review -s 155 -U ci
    Claude Agent SDK not available
    Install with: pip install claude-agent-sdk
    ...
    patman: ValueError: The review agent is not available; install the 'review' extra (pip install patch-manager[review])

``patman review`` needs the Claude Agent SDK, which comes with the review
extra: ``pip install patch-manager[review]``. The agent also needs Claude Code
to be logged in (see :doc:`reviewing`).

Patchwork cannot be reached
---------------------------

::

    $ patman review -s 508846 -U us
    ...
    Downloading mbox for series 508846 from https://patchwork.ozlabs.org
    patman: ClientConnectorError: Cannot connect to host patchwork.ozlabs.org:443 ssl:default [Network is unreachable]

The patchwork server did not answer. Check that you can reach it, e.g. by
opening it in a browser; if it is down, try again later. A server which is
just slow is retried a few times, and ``patchwork_timeout`` in ``~/.patman``
sets how long to wait (see :doc:`/configuration`).

There is nothing to review
--------------------------

::

    $ patman review -s 150 -U ci
    ...
    Nothing to review for 'pw-150-review': every patch is already applied upstream

The series has been merged, so applying it to the upstream branch adds
nothing. To review it anyway, give a branch from before it was merged with
``-b``, as :doc:`reviewing` does.
