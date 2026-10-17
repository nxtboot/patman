.. SPDX-License-Identifier: GPL-2.0+

The patman database
===================

Patman stores series tracking, review and workflow state in a SQLite
database (``.patman.db``) in the top-level git directory. The schema is
versioned and migrated automatically when patman starts; it is currently at
v12. The :doc:`db` chapter of the manual covers where the database is
kept, the separate review database and sharing it between patman runs.

The database allows patman to track a patch series across multiple
versions, recording which patches belong to each version and how they
map to patchwork entries. This means patman can detect when a new
version of a series arrives, carry forward review tags and change logs
from earlier versions, and show upstream progress without re-querying
patchwork each time.

For assisted review of other people's patches, the database stores
the generated review text, Gmail draft IDs and thread state so that
patman can resume where it left off  - showing stored reviews, creating
drafts for reviews that were not yet sent, detecting when drafts have
been sent or deleted, and providing previous review context when a new
version of the series is posted. Review-handling notes from the
``handle-reviews`` workflow are also stored per-version so the agent has
context about what was changed and why.

Entity relationships
--------------------

::

    upstream 1---* patchwork        (patchwork.upstream references
                                     upstream.name)

    series   1---* ser_ver          (ser_ver.series_id -> series.id)

    ser_ver  1---* pcommit          (pcommit.svid -> ser_ver.id)

    ser_ver  1---* review           (review.svid -> ser_ver.id)

    series   1---* workflow          (workflow.series_id -> series.id)

A **series** represents a named patch series across all its versions.
Each **ser_ver** is one version of that series (e.g. v1, v2), linked to
patchwork by a series link. Each ser_ver has one **pcommit** per patch
and zero or more **review** records (one per drafted review).

Tables
------

series
~~~~~~

Patman tracks series that you are working on so it can keep an eye on
any feedback from other people (from patchwork). Series that are being
reviewed (other people's patches) are also stored here with
``source='review'``.

==========  ========  ==========================================
Column      Type      Description
==========  ========  ==========================================
id          INTEGER   Primary key (auto-increment)
name        TEXT      Series name (unique)
desc        TEXT      Series description / cover-letter title
archived    BIT       True if series is archived
upstream    TEXT      Upstream name (added v5)
source      TEXT      'review' for reviewed series (added v8)
==========  ========  ==========================================

ser_ver
~~~~~~~

Each time you send a new version of a series, patman creates a ser_ver
record linking it to patchwork. This lets patman track review tags,
change logs and upstream progress separately for each version. For
reviewed series, review-handling notes are stored here so the next
version has context about what was changed.

==================  ========  ==========================================
Column              Type      Description
==================  ========  ==========================================
id                  INTEGER   Primary key (auto-increment)
series_id           INTEGER   FK -> series.id
version             INTEGER   Version number (1, 2, ...)
link                TEXT      Patchwork series link/ID
cover_id            INTEGER   Patchwork cover letter ID (added v3)
cover_num_comments  INTEGER   Number of comments on cover (added v3)
name                TEXT      Cover letter name (added v3)
archive_tag         TEXT      Git tag for archived version (added v4)
desc                TEXT      Version description (added v6)
notes               TEXT      Review-handling notes (added v10)
==================  ========  ==========================================

pcommit
~~~~~~~

Each patch in a series version gets a pcommit record. Patman uses the
Change-Id to match patches across versions (so it knows which patch in
v2 corresponds to which patch in v1) and tracks the patchwork state and
comment count so ``patman series progress`` can show upstream status.

==============  ========  ==========================================
Column          Type      Description
==============  ========  ==========================================
id              INTEGER   Primary key (auto-increment)
svid            INTEGER   FK -> ser_ver.id
seq             INTEGER   Patch sequence (0-based)
subject         TEXT      Patch subject line
patch_id        INTEGER   Patchwork patch ID
change_id       TEXT      Change-Id tag value
state           TEXT      Patchwork state (e.g. 'new', 'accepted')
num_comments    INTEGER   Number of patchwork comments
==============  ========  ==========================================

review
~~~~~~

When patman reviews someone else's patches, it stores the generated
review email text here  - one record per patch plus optionally the cover
letter. This allows patman to show the review again later, create Gmail
drafts from stored reviews, detect when drafts have been sent or
deleted, and provide the previous review as context when a new version
of the series arrives. When ``--sync`` detects that a draft was sent,
the body is updated to the final text the user actually sent (after any
manual edits), so the stored review reflects what was really posted to
the mailing list.

================  ========  ==========================================
Column            Type      Description
================  ========  ==========================================
id                INTEGER   Primary key (auto-increment)
svid              INTEGER   FK -> ser_ver.id
seq               INTEGER   Patch sequence (0=cover, 1..N=patches)
body              TEXT      Review email body text
approved          BIT       True if Reviewed-by was given
timestamp         TEXT      ISO datetime of review creation
draft_id          TEXT      Gmail draft ID (added v9)
status            TEXT      'new', 'draft', 'sent', 'deleted',
                            'replied' (added v9)
gmail_msg_id      TEXT      Gmail message ID after sending (added v9)
gmail_thread_id   TEXT      Gmail thread ID for replies (added v9)
session_id        TEXT      ID of the agent session which wrote the
                            review, so that a revision can carry on
                            from it (added v12)
================  ========  ==========================================

reply
~~~~~

When someone replies to one of your sent reviews, ``patman review --sync``
records the reply here (added v11), along with your response to it. The
response can be suggested by the agent, edited and then turned into a Gmail
draft which is threaded as a reply.

================  ========  ==========================================
Column            Type      Description
================  ========  ==========================================
id                INTEGER   Primary key (auto-increment)
review_id         INTEGER   FK -> review.id
gmail_msg_id      TEXT      Gmail message ID of the reply (unique)
msgid             TEXT      Message-ID header, for threading a response
refs              TEXT      References header
sender            TEXT      From header
to_addrs          TEXT      To header
cc_addrs          TEXT      Cc header
subject           TEXT      Subject header
date              TEXT      Date header
body              TEXT      Text of the reply
response          TEXT      Text of your response, if any
status            TEXT      'new', 'suggested', 'skip', 'draft', 'sent'
draft_id          TEXT      Gmail draft ID of the response
timestamp         TEXT      When the reply was recorded
================  ========  ==========================================

upstream
~~~~~~~~

Patman needs to know about the upstream repositories you send patches
to. Each upstream has a git remote name, URL and optional patchwork
settings. One upstream can be marked as the default so you do not need
to specify ``-U`` on every command.

================  ========  ==========================================
Column            Type      Description
================  ========  ==========================================
name              TEXT      Git remote name (unique, e.g. 'us')
url               TEXT      Repository URL
is_default        BIT       True if this is the default upstream
patchwork_url     TEXT      Patchwork server URL (added v6)
identity          TEXT      Git sendemail identity (added v6)
series_to         TEXT      Default To: alias for series (added v6)
no_maintainers    BIT       Skip get_maintainer.pl (added v6)
no_tags           BIT       Skip subject-tag processing (added v6)
================  ========  ==========================================

patchwork
~~~~~~~~~

Each upstream can have a patchwork project associated with it. This
stores the project name, ID and link name so patman can query patchwork
for series status, review tags and comments. Multiple upstreams can
point to different patchwork projects (e.g. U-Boot mainline vs a
custodian tree).

==============  ========  ==========================================
Column          Type      Description
==============  ========  ==========================================
name            TEXT      Project name (e.g. 'U-Boot')
proj_id         INTEGER   Patchwork project ID
link_name       TEXT      Patchwork link name (e.g. 'uboot')
upstream        TEXT      Upstream name, or NULL for default
==============  ========  ==========================================

workflow
~~~~~~~~

Patman records workflow events such as when a series was sent, when
review feedback arrived and what needs attention. The ``patman workflow
todo`` command uses this to show outstanding tasks across all your
series.

==============  ========  ==========================================
Column          Type      Description
==============  ========  ==========================================
id              INTEGER   Primary key (auto-increment)
series_id       INTEGER   FK -> series.id
action          TEXT      Workflow action (e.g. 'sent', 'todo')
timestamp       TEXT      ISO datetime
data            TEXT      JSON payload
ser_ver_id      INTEGER   FK -> ser_ver.id (added v7b)
==============  ========  ==========================================

schema_version
~~~~~~~~~~~~~~

A single-row table that records which schema version the database is at.
Patman checks this on startup and runs any needed migrations
automatically.

==============  ========  ==========================================
Column          Type      Description
==============  ========  ==========================================
version         INTEGER   Current schema version (currently 11)
==============  ========  ==========================================
