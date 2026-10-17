.. SPDX-License-Identifier: GPL-2.0+

Series management
=================

Sometimes you might have several series in flight at the same time. Each of
these receives comments and you want to create a new version of each series with
those comments addressed.

Patman provides a few subcommands which are helpful for managing series.

Series and branches
-------------------

'patman series' works with the concept of a series. It maintains a local
database (.patman.db in your top-level git tree) and uses that to keep track of
series and patches.

Each series goes through muliple versions. Patman requires that the first
version of your series is in a branch without a numeric suffix. Branch names
like 'serial' and 'video' are OK, but 'part3' is not. This is because Patman
uses the number at the end of the branch name to indicate the version.

If your series name is 'video', then you can have a 'video' branch for version
1 of the series, 'video2' for version 2 and 'video3' for version 3. All three
branches are for the same series. Patman keeps track of these different
versions. It handles the branch naming automatically, but you need to be aware
of what it is doing.

You will have an easier time if the branch names you use with 'patman series'
are short, no more than 15 characters. This is the amount of columnar space in
listings. You can add a longer description as the series description. If you
are used to having very descriptive branch names, remember that patman lets you
add metadata into commit which is automatically removed before sending.

This documentation uses the term 'series' to mean all the versions of a series
and 'series/version' to mean a particular version of a series.

Updating commits
----------------

Since Patman provides quite a bit of automation, it updates your commits in
some cases, effectively doing a rebase of a branch in order to change the tags
in the commits. It never makes code changes.

In extremis you can use 'git reflog' to revert something that Patman did.


Series subcommands
------------------

'patman series' has subcommands to add a series, send it, link it to
patchwork, gather its tags, start the next version and archive it, along with
others to list, show and tidy up series. 'patman series -h' lists them, and
:doc:`the command reference </reference/series>` describes each one with its
options. 'patman series' can be abbreviated to 'patman s'.

The workflow below shows the main ones in the order you use them.

Patman series workflow
----------------------

These are the commands for the life of a series, in order.
:doc:`Tracking a series with the database </guide/series-workflow>` goes
through them with a real U-Boot series and the output of each.

1. Tell patman the patchwork project for the upstream remote, once for each
   clone. The remote defaults to the one the current branch tracks, or the
   only remote::

       patman patchwork set-project U-Boot

2. On the branch for version 1, e.g. 'video', add the series. Its
   description comes from the cover letter. Use ``-m`` to add ``Change-Id:``
   tags to the commits, so patches can be followed even if their subjects
   change, or ``-M`` to match them by subject::

       patman series add -M

3. Send it. patman then waits for the series to appear on patchwork and
   records its link. If it does not appear in time, run
   ``patman series autolink`` later::

       patman series send

4. Follow the reviews. ``gather`` records the state of each patch and adds
   new review tags to the commits; ``status`` just shows them. Use ``-c`` and
   ``-C`` to see the comments on the patches and the cover letter::

       patman series gather
       patman series status -cC

5. See where all your series stand::

       patman series progress

6. Start the next version. ``inc`` creates the 'video2' branch, sets
   ``Series-version: 2`` and adds v2 to the database. After changing the
   commits, use ``scan`` to update the database from the branch, then send
   as before::

       patman series inc
       patman series scan -M
       patman series send

   Alternatively, create 'video2' yourself and run ``patman series add``, or
   collect the tags into it with ``patman status -d video2``.

7. Once the series is merged, archive it. This keeps each version's branch
   as a tag and deletes the branches::

       patman series archive

Here is a sample 'progress' view:

.. image:: pics/patman.jpg
  :width: 800
  :alt: Patman showing the progress view

Review notes
------------

The ``handle-reviews`` workflow produces a ``review-notes.txt`` file
describing how feedback was addressed. Store it in the database so
future versions can reference it::

    patman series save-notes [notes-file]

The default filename is ``review-notes.txt``. Display notes from all
previous versions::

    patman series show-notes

Following up
------------

patman keeps a record of what happens to each series and reminds you to come
back to it. Sending a series with ``patman series send``, or reviewing one
with ``patman review``, records when that happened, and marks the series to
come back to a week later.

``patman workflow todo`` marks a series to come back to after a number of
days (14 if you do not give one), and ``--clear`` removes the mark::

    $ patman workflow todo -s align-fix 7
    Series 'align-fix' marked for todo on 2026-10-24 13:46:14

``patman workflow todo-list`` lists the series which are due, or all those
marked with ``--all``::

    $ patman workflow todo-list --all
    Series                        Due  Description
    -----------------  --------------  ------------------------------
    align-fix                   in 6d  Repair boards broken by the 6.1 kbuild bump

``patman workflow list`` shows the record for each series, such as when it
was sent and when it is next due::

    $ patman workflow list
    Type    Series             Ver        When  Description
    ------  -----------------  ---  ----------  ------------------------------
    todo    align-fix                    in 7d  Repair boards broken by the 6.1 kbuild bump

The web page shows the series which are due in its Todo tab (see
:doc:`the Web server chapter </serve>`).

Multiple upstreams
------------------

If you send patches to more than one upstream tree (e.g. U-Boot mainline and
the U-Boot concept tree), you can configure patman with multiple upstreams so
that each series automatically uses the correct send settings for its
destination. The trees may use different mailing lists, patchwork servers and
SMTP credentials.

For a worked example with real output, see
:doc:`Sending to more than one upstream </guide/upstreams>`.

The key concepts are:

**Upstream**
    A remote git repository that you send patches to. Each upstream has a name
    (e.g. ``us``, ``ci``) and a URL. Upstreams can also carry send settings:
    a patchwork URL, a sendemail identity, a To address and flags controlling
    ``get_maintainer.pl`` and subject-tag processing. Use ``patman upstream``
    commands to manage these.

**Patchwork project**
    The patchwork server tracks patches for a project (e.g. ``U-Boot``).
    Each upstream can point to a different patchwork server, and patman needs
    to know the project name on that server so it can look up series links.
    Use ``patman patchwork set-project`` to configure this per upstream.

**Sendemail identity**
    Git supports multiple SMTP configurations via ``[sendemail "<name>"]``
    sections in ``.gitconfig``. An upstream can reference one of these
    identities so that patman passes ``--identity`` to ``git send-email``
    automatically. To pick one for a single run, or when you have no
    upstream configured, pass ``patman send -I <identity>``; it overrides
    the upstream's identity.

**Series upstream**
    Each series can be associated with an upstream. When you send the series,
    patman looks up the upstream's settings and applies them. This means the
    correct identity, To address and patchwork server are used without any
    extra flags on the command line.

Adding upstreams
~~~~~~~~~~~~~~~~

Add each upstream with its git remote name, its URL and, optionally, its
patchwork project, along with its settings::

    patman upstream add us https://git.u-boot-project.org/u-boot/u-boot \
        U-Boot -p https://patchwork.ozlabs.org -t u-boot

    patman upstream add ci https://concept.u-boot.org/u-boot/u-boot.git \
        U-Boot -p https://patchwork.deinde.dev -t concept -m --no-tags

The options are:

``-p`` / ``--patchwork-url``
    URL of the patchwork server for this upstream

``-t`` / ``--series-to``
    Patman alias for the To address (from your ``~/.patman`` alias file)

``-I`` / ``--identity``
    Git sendemail identity (selects ``[sendemail "<identity>"]`` from
    ``.gitconfig``)

``-m`` / ``--no-maintainers``
    Skip running ``get_maintainer.pl``

``--no-tags``
    Skip subject-tag alias processing

Giving the project name looks it up on the patchwork server; otherwise set
it later with ``patman patchwork set-project U-Boot ci``. Each upstream has
its own project entry, since each patchwork server has its own project IDs.

Add the To aliases to the ``[alias]`` section of ``~/.patman``::

    [alias]
    concept: U-Boot Concept <concept@u-boot.org>

Use ``patman upstream ls`` to list the upstreams, ``patman upstream default
us`` to choose the default, and ``patman upstream set`` to change one, with
the same options as ``upstream add`` plus ``--maintainers`` and ``--tags`` to
turn those back on::

    patman upstream set ci -I concept

Sendemail identities
~~~~~~~~~~~~~~~~~~~~

If the upstreams use different SMTP servers or credentials, add a git
sendemail identity for each in ``.gitconfig``. The ``[sendemail]`` settings
are the default, and a ``[sendemail "<name>"]`` section overrides them for
that identity (the servers here are just examples)::

    [sendemail]
        smtpserver = smtp.example.com
        smtpserverport = 587
        smtpencryption = tls

    [sendemail "concept"]
        smtpserver = smtp.gmail.com
        smtpserverport = 587
        smtpencryption = tls
        smtpuser = user@gmail.com

An upstream with ``-I concept`` makes patman pass ``--identity=concept`` to
``git send-email``.

The upstream of a series
~~~~~~~~~~~~~~~~~~~~~~~~

When a series is added, it uses the upstream which its branch tracks, if
that remote is one of the upstreams. Otherwise give it with ``-S``, or set it
later::

    patman series add -S us
    patman series -s video set-upstream us

A series with no upstream is sent with the default settings, so set one for
every series which is not for the default tree.

Sending
~~~~~~~

``patman series send`` applies the upstream's settings to the series:

- passes ``--identity`` to ``git send-email`` if configured
- sets the To address from ``--series-to`` if no ``Series-to:`` tag is present
- skips ``get_maintainer.pl`` if ``--no-maintainers`` is set
- skips tag processing if ``--no-tags`` is set

If the series has a ``Series-to:`` tag that does not match the upstream's
To address, patman refuses to send it, which catches a series about to go
to the wrong mailing list. Plain ``patman send`` uses the same settings when
the branch belongs to a series which patman tracks, and says so.

The upstream's patchwork server is also used for the series' links, status
and tags.
