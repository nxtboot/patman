.. SPDX-License-Identifier: GPL-2.0+

Sending to more than one upstream
=================================

Some developers send patches to more than one tree. For U-Boot, that may be
mainline and the U-Boot concept tree, which have their own mailing lists and
patchwork servers. Patches for the concept tree should also not be copied to
the mainline maintainers, so it is set up here not to use
``get_maintainer.pl``.

patman handles this with upstreams. Each upstream is a git remote, along with
the settings for sending to it: its patchwork server, the address the series
goes to, whether to copy the maintainers, and which SMTP settings to use.
Each series in the ``patman series`` database (see :doc:`series-workflow`)
belongs to an upstream, and ``patman series send`` uses its settings.

This uses ``binj-us`` for mainline, as in the earlier pages, and a small
series of two doc fixes, ``typos``, for the concept tree. The ``typos`` series
was made for this page and not sent, so it is shown only with dry runs. All
the output is from running patman on these branches, starting with an empty
database.

The remotes
-----------

Each upstream is a git remote. Here ``us`` is mainline and ``ci`` is the
concept tree::

    git remote add ci https://concept.u-boot.org/u-boot/u-boot.git
    git fetch ci

Start each series from the tree it is for, so that it tracks that tree::

    git checkout -b typos ci/master

Adding the upstreams
--------------------

Add each upstream to the database, with its remote, URL and patchwork
project, and its settings::

    $ patman upstream add us https://git.u-boot-project.org/u-boot/u-boot \
        U-Boot -p https://patchwork.ozlabs.org -t u-boot
    Added upstream 'us' (https://git.u-boot-project.org/u-boot/u-boot) patchwork 'https://patchwork.ozlabs.org' to 'u-boot' project 'U-Boot'

    $ patman upstream add ci https://concept.u-boot.org/u-boot/u-boot.git \
        U-Boot -p https://patchwork.deinde.dev -t concept -m --no-tags
    Added upstream 'ci' (https://concept.u-boot.org/u-boot/u-boot.git) patchwork 'https://patchwork.deinde.dev' to 'concept' no-maintainers no-tags project 'U-Boot'

The settings are:

``-p``
    The patchwork server, used to find the series and its reviews. Giving
    the project name, ``U-Boot`` here, looks the project up on that server,
    as ``patman patchwork set-project`` does

``-t``
    The alias for the address the series goes to, used when the series has
    no ``Series-to:`` tag and checked against it when it has one

``-m``
    Do not copy the maintainers found by ``get_maintainer.pl``

``--no-tags``
    Do not treat the tags at the start of a subject, such as ``doc:``, as
    aliases to copy

Set the one most series go to as the default::

    $ patman upstream default us
    Set default upstream to 'us'

and check the result::

    $ patman upstream ls
    Name   Def Project    URL                                          Options
    ------ --- ---------- -------------------------------------------- --------------------
    us     *   U-Boot     https://git.u-boot-project.org/u-boot/u-boot pw:https://patchwork.ozlabs.org to:u-boot
    ci         U-Boot     https://concept.u-boot.org/u-boot/u-boot.git pw:https://patchwork.deinde.dev id:concept to:concept no-maintainers no-tags

(``id:concept`` is the identity added in `Using different SMTP settings`_.)
Each upstream has its own project, since the two patchwork servers give the
project different IDs::

    $ patman patchwork ls
    Project                 ID  Link name        Remotes
    --------------------  ----  ---------------  ---------------
    U-Boot                   1  uboot            ci
    U-Boot                  18  uboot            us
    --------------------  ----  ---------------  ---------------

``u-boot`` is an alias from U-Boot's ``doc/git-mailrc`` (see
:doc:`getting-started`). Add ``concept`` to the ``[alias]`` section of
``~/.patman``::

    [alias]
    concept: U-Boot Concept <concept@u-boot.org>

Adding the series
-----------------

When a series is added, it takes the upstream which its branch tracks, as
long as that remote is one of the upstreams::

    $ git checkout typos
    $ patman series add -M
    Using upstream 'ci', which branch 'typos' tracks
    Added series 'typos' v1 (2 commits)

Use ``-S`` to give the upstream instead, e.g. for a branch which tracks
another local branch::

    $ git checkout binj-us
    $ patman series add -M -S us
    Added series 'binj-us' v1 (3 commits)

    $ patman series ls
    Name             Description                               Accepted  Us  Versions
    ---------------  ----------------------------------------  --------  --  ---------------
    binj-us          binman: Tidy up some missing bintool docs      -/3  us  1
    typos            doc: Fix a few typos in the developer doc      -/2  ci  1
    ---------------  ----------------------------------------  --------  --  ---------------

The ``Us`` column shows each series' upstream. If a series has none, for
example because its branch tracks a remote which is not an upstream, set it
afterwards with::

    $ patman series set-upstream ci
    Set upstream for series 'typos' to 'ci'

Do not leave it unset, since the series is then sent with the default
settings, which suit mainline. A dry run of ``typos`` with no upstream
copies the mainline list and maintainers, and complains about the ``doc:``
subject tag::

    $ patman series -n send
    Alias 'doc' not found
    Alias 'doc' not found
    ...
       0001-doc-Drop-doubled-words-in-two-developer-docs.patch
          Cc:  Simon Glass <sjg@chromium.org>
          Cc:  Tom Rini <trini@konsulko.com>
          Cc:  u-boot@lists.u-boot-project.org
    ...

Sending
-------

With the upstream set, the same dry run sends the series only to the
concept list::

    $ patman series -n send
    Cleaned 2 patches
    Cc processing complete
    Dry run, so not doing much. But I would do this:

    Send a total of 2 patches with a cover letter.
       0001-doc-Drop-doubled-words-in-two-developer-docs.patch
       0002-doc-Correct-the-spelling-of-successful-and-occurred.patch
    To:       U-Boot Concept <concept@u-boot.org>
    Version:  None
    Prefix:   None
    Postfix:          None
    Cover: 3 lines
    Git command: git send-email --annotate --to "U-Boot Concept <concept@u-boot.org>" ...

``binj-us``, with the ``us`` upstream, is sent to the U-Boot list and copied
to the maintainers, as in :doc:`first-series`.

Plain ``patman send`` uses these settings too, when the branch belongs to a
series in the database, and prints a line saying which upstream's settings
it is using. A branch which is not in the database is sent with the default
settings.

Checking the To address
-----------------------

A series for the concept tree can leave out ``Series-to:``, since the
upstream gives the address. If it has one, it must match the upstream, which
catches a series about to go to the wrong list. With ``Series-to: u-boot``
in ``typos``::

    $ patman series -n send
    ...
    patman: ValueError: Series-to tag ['u-boot'] does not match expected 'concept' from upstream settings

Using different SMTP settings
-----------------------------

If the trees need different SMTP settings, for example a different account,
add a git sendemail identity for each to ``~/.gitconfig``. The ``[sendemail]``
settings are the default, and a ``[sendemail "<name>"]`` section overrides
them for that identity::

    [sendemail "concept"]
        smtpserver = smtp.example.com
        smtpserverport = 587
        smtpencryption = tls
        smtpuser = you@example.com

Then give the identity to the upstream::

    $ patman upstream set ci -I concept
    Updated upstream 'ci': identity='concept'

    $ patman series -n send
    ...
    Git command: git send-email --annotate --identity=concept --to "U-Boot Concept <concept@u-boot.org>" ...

To use an identity for one send only, give it with ``patman series send -I``.

Patchwork
---------

Each upstream's patchwork server is used for its series, so ``autolink``,
``status`` and ``gather`` look for ``typos`` on patchwork.deinde.dev and for
``binj-us`` on patchwork.ozlabs.org, with no options needed.

'Multiple upstreams' in :doc:`/series` describes the ``patman upstream``
commands in full.
