.. SPDX-License-Identifier: GPL-2.0+

Web server
==========

``patman serve`` runs a small web server in your git tree, so that you can
check on your series and work on reviews from a phone or another computer. The
page has these tabs:

- **Series**: your series with the state of each patch in the latest version.
  Tap a series to see each version's patches, then tap a patch or the cover
  letter to read its comments, which are fetched from patchwork
- **Reviews**: series you have reviewed with ``patman review``, showing the
  review of each patch and whether it has been drafted, sent or replied to
- **Inbox**: recent series on patchwork, showing which you have reviewed
- **Jobs**: reviews and other commands started from the page, and the syncs,
  with their output
- **Todo**: series marked with ``patman workflow todo``, due ones first

The page uses the JSON API described in
:doc:`The patman serve protocol </serve-protocol>`, which other tools can use
too.

Starting the server
-------------------

The server only listens on the local machine by default::

    patman serve

then browse to http://localhost:8040/ on the same machine. To use it from a
phone, either forward the port over ssh (``ssh -L 8040:localhost:8040 <host>``)
or use a VPN such as Tailscale and listen on that address with a token::

    patman serve --host 100.64.1.2 --token <secret>

Open the URL that patman prints (which includes the token) once on the phone;
the page remembers the token after that. With Tailscale, ``tailscale serve
8040`` provides HTTPS, which lets the browser install the page as an app.

The options are:

- ``--host ADDR``: address to listen on (default 127.0.0.1)
- ``--port PORT``: port to listen on (default 8040)
- ``-t``, ``--token TOKEN``: token which clients must give; use this when
  listening on any address other than a loopback one
- ``-s``, ``--sync [MINUTES]``: sync with patchwork every MINUTES minutes
  (default 60); see below
- ``-r``, ``--review-sync``: also run ``patman review --sync`` in each sync
- ``--review-db-dir DIR``: read reviews from a separate database (the
  ``review_db_dir`` setting)

Keeping the status up to date
-----------------------------

Since the status of each patch only changes in the database when patman asks
patchwork, the server can do this periodically::

    patman serve --sync        # every hour
    patman serve --sync 20     # every 20 minutes
    patman serve --sync -r     # also run 'patman review --sync'

Each sync runs ``patman series gather-all -G`` in a separate process. The
``-G`` flag means that review tags are not gathered into your branches, so
nothing changes in your git tree while you are working in it. Use ``patman
series gather`` yourself when you are ready to pick up the tags. The ``-r``
flag also runs ``patman review --sync`` to track sent review drafts and
replies; this needs Gmail access (see
:doc:`the Assisted patch review chapter </review>`).

Each series is read from the patchwork server of its upstream, so a server
which is down only affects its own series.

Before gathering, the sync runs ``git fetch`` for the default upstream and the
upstreams of reviewed series, where these are git remotes, so that reviews
apply patches to up-to-date branches. This only updates the remote branches,
not your own.

Tap the sync indicator at the top of the page to follow the output of a sync as
it runs, to start one or to cancel one.

Reading patches
---------------

Each patch and cover letter can be read on the page: tap 'Read' on a series, a
patch name in a review, or a patch in the inbox to see the commit message, the
diff (with a summary of the files changed) and the comments on patchwork.

To see the code around a chunk of a diff, tap '5 more' above or below it, as
often as needed, or 'All' to fill the gap. Tap a file's name, or 'Whole file',
to see all of the file, with line numbers, at that point. The code is read from
the git repo where the server runs, using the blobs named in the diff's 'index'
line, so it is only available if the repo has them: the file as it is before
the patch is there if the series is based on a commit the repo has, and the
file with the patch applied is there once the series has been reviewed.

Reviewing from the page
-----------------------

On the Reviews tab, 'Review a series' takes a patchwork series ID, the URL of a
series or patch, or words from a cover-letter title, and runs ``patman review``
(with ``-d`` to create Gmail drafts, if selected). 'Scan for new versions' runs
``patman review --scan``. Each reviewed series has buttons to recreate its
Gmail drafts, either all of them or one patch's, and to review it again.

The Inbox tab lists the most recent series on patchwork for each upstream.
Series which you have reviewed, or which are your own, are marked, as are new
versions of series you reviewed earlier, so it is easy to see what needs a
review. Tap a series to see its patches and start a review.

Each review has 'Show the patch' (or 'Show the cover letter'), which shows the
commit message and diff just below the review, so the two can be read together.

A review which has not been sent can be edited on the page. Saving it can also
update its Gmail draft, so that the draft matches. Whether the review approves
the patch follows from whether the text includes a ``Reviewed-by:`` line.

To change just one comment, tap 'Edit' under it. The comment opens for editing
in place, between the lines it quotes and the rest of the review. Saving it
with no text drops the comment along with the lines it quotes.

To have the agent change a review which has not been sent, tap 'Revise' under
it and say what should change, or tap 'Revise this' under one comment to talk
about just that part. 'Revise all' at the top applies a note to every unsent
review of the series, e.g. 'drop the comments about style'. These run ``patman
review --revise``, updating the Gmail drafts if selected.

'Send' under a review sends its Gmail draft, and 'Send N drafts' at the top
sends them all; each needs a second tap to confirm.

These run as jobs (see `Jobs`_), but the page stays where it is, so you can
carry on reading the series. Each review a job acts on shows how the job is
getting on, with a link to its output. When the job finishes, the review
shows its new text, and 'Show changes' shows the text from before.

Replies to reviews
------------------

When someone replies to one of your reviews (found by ``review --sync``, so use
``serve -r``), the reply is shown below the review, along with any response the
agent suggests. The Reviews tab has a 'Needs reply' filter. You can edit the
response, or write one, then create a Gmail draft which is threaded as a reply,
or send it. You can also tap 'Suggest a response' to have the agent suggest one
(again), or note that no reply is needed.

Jobs
----

Commands started from the page, such as reviews, run as jobs, one at a time in
the order they are started, along with the syncs. The Jobs tab shows each job
with its output as it runs, and lets you cancel it. While jobs are waiting or
running, the tab shows how many there are.

Each tab remembers the screen you last looked at in it, so tapping Reviews
goes back to the series you were reviewing; tapping it again goes to the list
of reviews. Each screen also keeps its scroll position, so going back to it
finds it where you left it.

Jobs which are waiting or running are recorded in ``.patman-serve-jobs.json``
next to the database. If the server stops before a job finishes, for example
because the service is restarted, the job is run again from the start when the
server next starts. A job is resumed at most three times.

Running it as a service
-----------------------

To keep the server running, for example on a machine which you reach over a
VPN, run it as a systemd user service. Put the token in a file which only you
can read, such as ``~/.config/patman-serve.env``::

    PATMAN_TOKEN=<secret>

then create ``~/.config/systemd/user/patman-serve.service``, changing the
directory, the path to patman and the address to suit::

    [Unit]
    Description=patman web server
    After=network-online.target

    [Service]
    WorkingDirectory=%h/src/u-boot
    EnvironmentFile=%h/.config/patman-serve.env
    ExecStart=%h/.local/bin/patman serve --host 100.64.1.2 \
        -t ${PATMAN_TOKEN} -s -r
    Restart=on-failure
    RestartSec=30

    [Install]
    WantedBy=default.target

and start it with::

    systemctl --user daemon-reload
    systemctl --user enable --now patman-serve

Use ``journalctl --user -u patman-serve`` to see its output. Run ``loginctl
enable-linger`` once if the server should keep running while you are not logged
in.
