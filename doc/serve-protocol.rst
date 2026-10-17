.. SPDX-License-Identifier: GPL-2.0+

The patman serve protocol
=========================

``patman serve`` runs a web server in a git tree which provides a JSON API over
HTTP, covering the patman database and, where needed, the patchwork servers of
the upstreams. This document describes that API, for anyone working on the
server or writing a client. See :doc:`the Web server chapter </serve>` of the
manual for how to run the server. The page which the server provides is one
such client; see `The page`_.

Overview
--------

The API is under ``/api/``. Each response is a JSON value, encoded in UTF-8,
and each request which takes a body expects a JSON object. An empty body is the
same as ``{}``.

Your own series are read from the patman database in the git tree. Reviews of
other people's series are read from the review database if there is one
(``--review-db-dir`` or the ``review_db_dir`` setting), otherwise from the same
database; upstreams always come from the main database. Comments on patches and
cover letters are not stored, so these are read from patchwork when asked for.

Series IDs are those of the patman database: ``/api/series/<id>`` takes the ID
of one of your series and ``/api/reviews/<id>`` the ID of a reviewed series, in
the review database. ``/api/review-items/<id>`` takes the ID of a review, and
``/api/replies/<id>`` the ID of a reply, as given in the reviews. IDs from
patchwork, such as a patch or cover-letter ID, are named as such.

Times are local, in the form ``YYYY-MM-DD HH:MM:SS``, except where they come
from patchwork or email, which give their own form.

Access
------

The server listens on ``127.0.0.1`` port 8040 by default (``--host`` and
``--port``). When it is given a token with ``--token``, each request to a path
under ``/api/`` must provide it, either in an ``Authorization: Bearer <token>``
header or as a ``token`` query parameter. A request which does not is refused
with status 401 and ``{"error": "Unauthorised"}``. The server warns if it
listens on an address other than a loopback one without a token.

Errors
------

An error is reported as a JSON object with an ``error`` string, using these
status codes:

- 401: the token is missing or wrong
- 400: the request is not valid, e.g. a missing field or a bad value
- 404: the thing asked for does not exist, or patchwork does not have it
- 409: the request conflicts with the current state, e.g. starting a sync while
  one is running, or changing a review while a job is acting on it
- 502: patchwork could not be reached or did not respond in time; the message
  starts with ``Patchwork:``

The sync and job endpoints give their usual response with status 409, rather
than an error object.

A path which does not exist, or the wrong method, gets aiohttp's own 404 or 405
response, which is plain text.

Common objects
--------------

These objects appear in several responses.

Patch
~~~~~

- ``seq`` (int): position in the series, starting at 1
- ``subject`` (str): subject of the commit
- ``state`` (str or null): patchwork state, e.g. ``accepted``
- ``patch_id`` (int or null): patchwork patch ID
- ``num_comments`` (int): number of comments on patchwork

Version
~~~~~~~

A version of a series:

- ``svid`` (int): ID of the version in the database
- ``version`` (int): version number
- ``link`` (str or null): patchwork series ID
- ``desc`` (str or null): description of the version
- ``cover_id`` (int or null): patchwork cover-letter ID
- ``cover_name`` (str or null): name of the cover letter
- ``cover_num_comments`` (int): number of comments on the cover letter
- ``archive_tag`` (str or null): tag holding the archived branch

A ``states`` object maps each patchwork state to the number of patches in it,
using ``unknown`` for patches with no state.

Comment
~~~~~~~

A comment on patchwork; any field may be null:

- ``date`` (str): when it was sent
- ``name`` (str): name of the sender
- ``email`` (str): email address of the sender
- ``subject`` (str): subject
- ``content`` (str): text of the comment
- ``web_url`` (str): URL of the comment on patchwork

Review
~~~~~~

A review of one patch, or of the cover letter:

- ``id`` (int): ID of the review
- ``seq`` (int): patch number, or 0 for the cover letter
- ``body`` (str): text of the review
- ``approved`` (bool): true if the review gives a Reviewed-by tag
- ``timestamp`` (str): when it was written
- ``status`` (str or null): ``new``, ``draft``, ``sent``, ``replied`` or
  ``deleted``
- ``replies`` (list of Reply): replies to the review, oldest first; only given
  by ``GET /api/reviews/<id>``
- ``job`` (int or null): number of the job which is waiting or running to act
  on the review, e.g. to revise or send it, or null if there is none; only
  given by ``GET /api/reviews/<id>``. While there is one, requests which would
  change the review, or start another job on it, get status 409

Reply
~~~~~

A reply to a review which was sent:

- ``id`` (int): ID of the reply
- ``sender`` (str): sender, e.g. ``Name <email>``
- ``subject`` (str): subject
- ``date`` (str): date from the email
- ``body`` (str): text of the reply
- ``response`` (str or null): our response
- ``status`` (str): ``new``, ``suggested`` (there is a response), ``skip`` (no
  response is needed), ``draft`` (the response is in Gmail) or ``sent``
- ``timestamp`` (str): when the reply was found

Job
~~~

A job runs one or more commands in the git tree, such as a sync or a review:

- ``id`` (int): job number, starting at 1 when the server starts
- ``kind`` (str): ``sync`` or ``review``
- ``title`` (str): description, e.g. ``Review series 123``
- ``queued`` (bool): true while waiting for other jobs to finish
- ``running`` (bool): true while running
- ``cancelled`` (bool): true if it was cancelled
- ``started`` (str or null): when it started
- ``finished`` (str or null): when it finished; null while it is queued or
  running
- ``results`` (list): one object for each command which has started:

  - ``cmd`` (str): the command, for display, e.g. ``patman series gather-all
    -G``
  - ``returncode`` (int or null): exit code, null while running; negative if it
    was stopped by a signal, e.g. when cancelled
  - ``output`` (str): output so far, with stderr mixed in, keeping only the
    last 20000 characters

- ``resumed`` (int): number of times the job has been run again after the
  server stopped before it finished; see `Resuming jobs`_
- ``target`` (object or null): the reviews which the job acts on, for jobs
  started on a reviewed series, with the series ``series`` (int), its
  ``version`` (int) and the patches ``seqs`` (list of int, empty for all the
  unsent reviews); null for other jobs

Your series
-----------

``GET /api/series``
~~~~~~~~~~~~~~~~~~~

Your series which are not archived, sorted by name. Each is an object with:

- ``id`` (int): series ID
- ``name`` (str): name of the series (its branch)
- ``desc`` (str): description
- ``upstream`` (str or null): upstream the series is for
- ``version`` (int or null): latest version
- ``link`` (str or null): patchwork series ID of the latest version
- ``count`` (int): number of patches in the latest version
- ``states`` (object): states of those patches
- ``num_comments`` (int): number of comments on those patches
- ``cover_num_comments`` (int): number of comments on the cover letter
- ``sent`` (str or null): when the series was last sent

``GET /api/series/<id>``
~~~~~~~~~~~~~~~~~~~~~~~~

A series with all its versions:

- ``id``, ``name``, ``desc``, ``upstream``: as above
- ``patchwork_url`` (str or null): URL of the patchwork server for the
  upstream, falling back to the ``-P`` setting
- ``versions`` (list of Version): newest first, each with ``patches`` (list of
  Patch, in order) and ``states``

Patchwork
---------

These read from patchwork each time. Those which take an optional ``ups`` query
parameter use the patchwork server of that upstream, falling back to the ``-P``
setting. Requests to patchwork are retried if they fail in a way which may not
happen again, using the ``--patchwork-timeout`` setting for each attempt.

``GET /api/comments/<kind>/<id>``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The comments on a patch (``kind`` is ``patch``) or cover letter (``cover``),
given its patchwork ID: a list of Comment.

``GET /api/patch/<id>``
~~~~~~~~~~~~~~~~~~~~~~~

A patch, given its patchwork ID:

- ``id`` (int): patchwork ID
- ``name`` (str): subject
- ``date`` (str): when it was sent
- ``submitter`` (str): name or email address of the sender
- ``state`` (str or null): patchwork state; null for a cover letter
- ``content`` (str): commit message, or the text of a cover letter
- ``diff`` (str): the diff; empty for a cover letter
- ``web_url`` (str): URL of the patch on patchwork
- ``series`` (list): the series it is in, each with ``id``, ``name`` and
  ``version``

``GET /api/cover/<id>``
~~~~~~~~~~~~~~~~~~~~~~~

A cover letter, given its patchwork ID, in the same form as a patch.

``GET /api/upstreams``
~~~~~~~~~~~~~~~~~~~~~~

The upstreams which have a patchwork server, each an object with ``name``,
``default`` (true for the default upstream) and ``patchwork_url``.

``GET /api/inbox``
~~~~~~~~~~~~~~~~~~

The most recent series on the patchwork server of an upstream, for choosing
what to review. The ``ups`` query parameter gives the upstream, defaulting to
the default upstream, which must have a patchwork project. The ``page`` query
parameter gives the page, starting at 1, with 50 series on each. The response
has ``ups``, ``page``, ``per_page`` and ``series``, a list of objects with:

- ``id`` (int): patchwork series ID
- ``name`` (str): name of the series
- ``version`` (int): version
- ``date`` (str): when it was sent
- ``submitter`` (str): name or email address of the sender
- ``total`` (int): number of patches
- ``received_all`` (bool): true if patchwork has all of them
- ``web_url`` (str): URL of the series on patchwork
- ``cover_id`` (int or null): patchwork cover-letter ID
- ``patches`` (list): each with ``id`` (patchwork ID) and ``name``
- ``reviewed`` (int or null): ID of the reviewed series, if this one has been
  reviewed
- ``earlier`` (int or null): if not, the ID of a reviewed series with the same
  title, i.e. an earlier version
- ``mine`` (bool): true if this is one of your own series

Files
-----

``GET /api/blob``
~~~~~~~~~~~~~~~~~

A file from the git repo, given the hash of its blob in the ``hash`` query
parameter, as in the ``index`` line of a diff. The parameter can be given more
than once, in which case the first blob which the repo has is used, since the
repo may have the file before a patch or after it, but not both. The response
has ``hash``, the hash which was found, and ``text``, the contents of the file.
A hash which is not 7 to 40 hex digits, a file which is not text or is larger
than 4MB, and a file which is not in the repo each give an error.

Reviews
-------

``GET /api/reviews``
~~~~~~~~~~~~~~~~~~~~

Series reviewed with ``patman review`` which are not archived, most recently
reviewed first. Each is an object with:

- ``id``, ``name``, ``desc``, ``upstream``, ``version``, ``link``: as for ``GET
  /api/series``
- ``num_reviews`` (int): number of reviews of the latest version
- ``approved`` (int): how many of them approve the patch
- ``statuses`` (object): number of reviews with each status, using ``new`` for
  those with none
- ``timestamp`` (str or null): when the latest review was written
- ``replies_waiting`` (int): number of replies which need a response

``GET /api/reviews/<id>``
~~~~~~~~~~~~~~~~~~~~~~~~~

A reviewed series with all its versions:

- ``id``, ``name``, ``desc``: as above
- ``upstream`` (str or null): upstream of the series, or the default upstream
  if it has none, since ``patman review`` uses that
- ``patchwork_url`` (str or null): as for ``GET /api/series/<id>``
- ``project`` (str or null): patchwork project of the upstream
- ``versions`` (list of Version): newest first, each with ``notes`` (str or
  null; notes from handling reviews of the series), ``reviews`` (list of
  Review, in order) and ``patches`` (list of Patch, in order)

Series added for review before cover letters were recorded have no
``cover_id``. For versions with a review of the cover letter, the server looks
up the series on patchwork and records its cover letter, so this only happens
once.

Acting on reviews
-----------------

These start a job which runs ``patman review``, responding with status 202 and
the Job, except where noted.

``POST /api/review``
~~~~~~~~~~~~~~~~~~~~

Review a series, or part of one. The body has one of ``target``, ``patch`` and
``title``, with other fields as needed:

- ``target`` (str): a patchwork series ID, or the URL of a series or patch
- ``patch`` (int): a patchwork patch ID, to review that patch
- ``title`` (str): words from the cover-letter title, to search for
- ``version`` (int): with ``title``, the version to review
- ``whole`` (bool): with a patch, review its whole series
- ``index`` (str): review only these patches, e.g. ``1,3-5``
- ``ups`` (str): upstream whose patchwork server to use
- ``drafts`` (bool): create Gmail drafts of the reviews
- ``force`` (bool): review the series again

``POST /api/review/scan``
~~~~~~~~~~~~~~~~~~~~~~~~~

Look for new versions of reviewed series and review them (``patman review
--scan``). The body may have ``drafts`` (bool) to create Gmail drafts.

``POST /api/reviews/<id>/redraft``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Create the Gmail drafts of a reviewed series again. The body may have
``version`` (int), defaulting to the latest, and ``seqs`` (list of int), the
patches to act on, using 0 for the cover letter; the default is all of them.

``POST /api/reviews/<id>/send``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Send the Gmail drafts of a reviewed series. The body is as for ``redraft``.

``POST /api/reviews/<id>/revise``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Have the agent change the reviews of a series which have not been sent,
following a note. The body has ``note`` (str), saying what to change, and may
have ``part`` (str), the part of a review which the note is about, and
``drafts`` (bool) to update the Gmail drafts, along with ``version`` and
``seqs`` as for ``redraft``.

``POST /api/review-items/<id>``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Change a review which has not been sent, given its ID. The body may have:

- ``body`` (str): new text of the review, which must not be empty
- ``approved`` (bool): whether the review approves the patch; by default, true
  if the text has a ``Reviewed-by:`` line
- ``redraft`` (bool): update the Gmail draft too

The response is the Review, or with ``redraft``, status 202 and the Job which
updates the draft. A review which has been sent cannot be changed, giving
status 409.

Replies
-------

``POST /api/replies/<id>``
~~~~~~~~~~~~~~~~~~~~~~~~~~

Change the response to a reply, given its ID. The body may have:

- ``response`` (str): new response; an empty one removes it
- ``skip`` (bool): note that no response is needed
- ``draft`` (bool): create a Gmail draft of the response, threaded as a reply

The response is the Reply, or with ``draft``, status 202 and the Job which
creates the draft. Once the response has been sent, it cannot be changed,
giving status 409.

``POST /api/replies/<id>/suggest``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Have the agent suggest a response to a reply, giving status 202 and the Job.

``POST /api/replies/<id>/send``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Send the response to a reply, creating or updating its Gmail draft first if
needed, giving status 202 and the Job. A response which has been sent gives
status 409, and there must be a response to send.

Todo
----

``GET /api/todo``
~~~~~~~~~~~~~~~~~

Series marked with ``patman workflow todo``, earliest first, each an object
with ``series_id``, ``name``, ``desc``, ``timestamp`` (when it is due) and
``due`` (true if that time has passed).

Sync
----

A sync updates the database from patchwork. With ``--sync``, one runs when the
server starts and then every so many minutes; one can also be started through
the API. It runs these commands in turn:

- ``git fetch`` of the default upstream and the upstreams of reviewed series,
  where these are git remotes
- ``patman series gather-all -G``
- ``patman review --sync``, if the server is given ``-r``, passing on any
  ``--review-db-dir``

``GET /api/sync``
~~~~~~~~~~~~~~~~~

A sync runs as a job, one at a time with the other jobs. Its state is the Job
of the most recent sync, along with:

- ``interval`` (int): minutes between syncs, or 0 if they are only run when
  asked
- ``review_sync`` (bool): true if the sync runs ``patman review --sync``

Before the first sync there is no Job, so only ``queued``, ``running``,
``cancelled``, ``started``, ``finished`` and ``results`` are given, along with
these.

``POST /api/sync``
~~~~~~~~~~~~~~~~~~

Start a sync, giving status 202 and the sync state, or 409 and the sync state
if a sync is already queued or running.

``POST /api/sync/cancel``
~~~~~~~~~~~~~~~~~~~~~~~~~

Cancel the sync which is queued or running, stopping its command, giving status
202 and the sync state, or 409 if there is none.

Jobs
----

Jobs run one at a time, in the order they are started, since they may all write
to the database. Each command runs in the git tree, in a process group of its
own, with no input. The server remembers the 20 most recent jobs, along with
any which have not finished.

``GET /api/jobs``
~~~~~~~~~~~~~~~~~

The jobs, newest first, as a list of Job.

``GET /api/jobs/<id>``
~~~~~~~~~~~~~~~~~~~~~~

A job.

``POST /api/jobs/<id>/cancel``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Cancel a job which is queued or running, stopping its command and any processes
it started, giving status 202 and the Job, or 409 if it has finished.

Resuming jobs
~~~~~~~~~~~~~

Jobs which have not finished, other than syncs, are recorded in
``.patman-serve-jobs.json`` next to the database, as a list of objects with
``kind``, ``title``, ``cmds`` (the commands, each a list of arguments) and
``resumed``. The file is replaced in one step each time a job is added or
finishes. A job counts as unfinished if it is queued or running, or if a
command was stopped by the server stopping, rather than by cancelling the job.
When the server starts, it runs each of these again as a new job, from the
start, unless it has already been resumed three times.

The page
--------

``GET /`` gives the page, a single HTML file which needs no build step and
loads nothing from other sites, and ``/static/<file>`` gives the files it uses,
such as its web-app manifest and icon. Neither needs the token.

The page takes the token from a ``token`` query parameter in its own URL, as
printed by ``patman serve``, keeps it in the browser's local storage and
removes it from the URL. It sends the token in an ``Authorization`` header and
asks for it if a request is refused with status 401.

While a sync or job is running, the page asks for its state every few seconds
to show its progress, and otherwise checks the sync state once a minute.
