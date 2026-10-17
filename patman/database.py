# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2025 Simon Glass <sjg@chromium.org>
#
# pylint: disable=C0302
"""Handles the patman database

This uses sqlite3 with a local file.

To adjsut the schema, increment LATEST, create a migrate_to_v<x>() function
and write some code in migrate_to() to call it.
"""

from collections import namedtuple, OrderedDict
import fcntl
import os
import sqlite3

from u_boot_pylib import tools
from u_boot_pylib import tout
from patman.series import Series

# Schema version (version 0 means there is no database yet)
LATEST = 12

# Information about a review record
# Review status values
REVIEW_NEW = 'new'          # AI review generated, no draft created
REVIEW_DRAFT = 'draft'      # Gmail draft created
REVIEW_SENT = 'sent'        # Draft was sent
REVIEW_DELETED = 'deleted'  # Draft was deleted without sending
REVIEW_REPLIED = 'replied'  # Author has replied to our review

Review = namedtuple(
    'REVIEW',
    'idnum,svid,seq,body,approved,timestamp,draft_id,status,'
    'gmail_msg_id,gmail_thread_id')

# Status values for a reply to one of our reviews
REPLY_NEW = 'new'              # Reply received, no response suggested yet
REPLY_SUGGESTED = 'suggested'  # A response is ready, with no draft
REPLY_SKIP = 'skip'            # No response is needed
REPLY_DRAFT = 'draft'          # Gmail draft created for the response
REPLY_SENT = 'sent'            # Response was sent

# Statuses of a reply which still needs attention
REPLY_WAITING = (REPLY_NEW, REPLY_SUGGESTED)

# Information about a reply to one of our reviews
#   idnum: ID of the reply record
#   review_id: ID of the review which was replied to
#   gmail_msg_id: Gmail message ID of the reply
#   msgid: Message-ID header of the reply, for threading the response
#   refs: References header of the reply
#   sender: Who sent the reply ('Name <email>')
#   to_addrs: To header of the reply
#   cc_addrs: Cc header of the reply
#   subject: Subject of the reply
#   date: Date header of the reply
#   body: Text of the reply
#   response: Text of our response, or None if there is none yet
#   status: REPLY_... value
#   draft_id: Gmail draft ID of the response, or None
#   timestamp: When the reply was recorded
Reply = namedtuple(
    'REPLY',
    'idnum,review_id,gmail_msg_id,msgid,refs,sender,to_addrs,cc_addrs,'
    'subject,date,body,response,status,draft_id,timestamp')

REPLY_COLS = ('id, review_id, gmail_msg_id, msgid, refs, sender, to_addrs, '
              'cc_addrs, subject, date, body, response, status, draft_id, '
              'timestamp')

# Information about a series/version record
SerVer = namedtuple(
    'SER_VER',
    'idnum,series_id,version,link,cover_id,cover_num_comments,name,'
    'archive_tag,desc,notes')

# Record from the pcommit table:
# idnum (int): record ID
# seq (int): Patch sequence in series (0 is first)
# subject (str): patch subject
# svid (int): ID of series/version record in ser_ver table
# change_id (str): Change-ID value
# state (str): Current status in patchwork
# patch_id (int): Patchwork's patch ID for this patch
# num_comments (int): Number of comments attached to the commit
Pcommit = namedtuple(
    'PCOMMIT',
    'idnum,seq,subject,svid,change_id,state,patch_id,num_comments')


class Database:  # pylint:disable=R0904
    """Database of information used by patman"""

    # dict of databases:
    #   key: filename
    #   value: Database object
    instances = {}

    def __init__(self, db_path):
        """Set up a new database object

        Args:
            db_path (str): Path to the database
        """
        if db_path in Database.instances:
            # Two connections to the database can cause:
            # sqlite3.OperationalError: database is locked
            raise ValueError(f"There is already a database for '{db_path}'")
        self.con = None
        self.cur = None
        self.db_path = db_path
        self.is_open = False
        Database.instances[db_path] = self

    @staticmethod
    def get_instance(db_path):
        """Get the database instance for a path

        This is provides to ensure that different callers can obtain the
        same database object when accessing the same database file.

        Args:
            db_path (str): Path to the database

        Return:
            Database: Database instance, which is created if necessary
        """
        db = Database.instances.get(db_path)
        if db:
            return db, False
        return Database(db_path), True

    def start(self):
        """Open the database ready for use, migrate to latest schema

        Return:
            int or None: Schema version before migration, or None if no
                migration was needed
        """
        self.open_it()
        old_version = self.get_schema_version()
        if old_version > LATEST:
            self.close()
            tout.fatal(
                f'Database version {old_version} is too new (max'
                f' {LATEST}); please update patman')
        self.migrate_to(LATEST)
        if old_version == LATEST:
            return None
        return old_version

    def open_it(self):
        """Open the database, creating it if necessary"""
        if self.is_open:
            raise ValueError('Already open')
        if not os.path.exists(self.db_path):
            tout.warning(f'Creating new database {self.db_path}')
        self.con = sqlite3.connect(self.db_path, timeout=30)
        self.cur = self.con.cursor()
        # WAL lets readers and a writer coexist across processes; the busy
        # timeout above rides out brief contention before raising 'database
        # is locked'
        self.cur.execute('PRAGMA journal_mode=WAL')
        self.cur.execute('PRAGMA synchronous=NORMAL')
        self.is_open = True

    def close(self):
        """Close the database"""
        if not self.is_open:
            raise ValueError('Already closed')
        self.con.close()
        self.cur = None
        self.con = None
        self.is_open = False

    def create_v1(self):
        """Create a database with the v1 schema"""
        self.cur.execute(
            'CREATE TABLE series (id INTEGER PRIMARY KEY AUTOINCREMENT,'
            'name UNIQUE, desc, archived BIT)')

        # Provides a series_id/version pair, which is used to refer to a
        # particular series version sent to patchwork. This stores the link
        # to patchwork
        self.cur.execute(
            'CREATE TABLE ser_ver (id INTEGER PRIMARY KEY AUTOINCREMENT,'
            'series_id INTEGER, version INTEGER, link,'
            'FOREIGN KEY (series_id) REFERENCES series (id))')

        self.cur.execute(
            'CREATE TABLE upstream (name UNIQUE, url, is_default BIT)')

        # change_id is the Change-Id
        # patch_id is the ID of the patch on the patchwork server
        self.cur.execute(
            'CREATE TABLE pcommit (id INTEGER PRIMARY KEY AUTOINCREMENT,'
            'svid INTEGER, seq INTEGER, subject, patch_id INTEGER, '
            'change_id, state, num_comments INTEGER, '
            'FOREIGN KEY (svid) REFERENCES ser_ver (id))')

        self.cur.execute(
            'CREATE TABLE settings (name UNIQUE, proj_id INT, link_name)')

    def _migrate_to_v2(self):
        """Add a schema_version table"""
        self.cur.execute('CREATE TABLE schema_version (version INTEGER)')

    def _migrate_to_v3(self):
        """Store the number of cover-letter comments in the schema"""
        self.cur.execute('ALTER TABLE ser_ver ADD COLUMN cover_id')
        self.cur.execute('ALTER TABLE ser_ver ADD COLUMN cover_num_comments '
                         'INTEGER')
        self.cur.execute('ALTER TABLE ser_ver ADD COLUMN name')

    def _migrate_to_v4(self):
        """Add an archive tag for each ser_ver"""
        self.cur.execute('ALTER TABLE ser_ver ADD COLUMN archive_tag')

    def _migrate_to_v5(self):
        """Add upstream support to series, patchwork and upstream tables

        - Add upstream column to series table
        - Rename and recreate patchwork table (formerly 'settings') without
          UNIQUE constraint on name, adding an upstream column (since the
          same project can have multiple remotes)
        - Add patchwork_url, identity, series_to, no_maintainers and
          no_tags columns to upstream table
        - Add desc column to ser_ver table
        """
        self.cur.execute('ALTER TABLE series ADD COLUMN upstream')

        self.cur.execute(
            'CREATE TABLE patchwork_new '
            '(name, proj_id INT, link_name, upstream)')
        self.cur.execute(
            'INSERT INTO patchwork_new SELECT name, proj_id, link_name, NULL '
            'FROM settings')
        self.cur.execute('DROP TABLE settings')
        self.cur.execute('ALTER TABLE patchwork_new RENAME TO patchwork')
        default_ups = self.upstream_get_default()
        if default_ups:
            self.cur.execute(
                'UPDATE patchwork SET upstream = ?', (default_ups,))

        self.cur.execute('ALTER TABLE upstream ADD COLUMN patchwork_url')
        self.cur.execute('ALTER TABLE upstream ADD COLUMN identity')
        self.cur.execute('ALTER TABLE upstream ADD COLUMN series_to')
        self.cur.execute(
            'ALTER TABLE upstream ADD COLUMN no_maintainers BIT')
        self.cur.execute('ALTER TABLE upstream ADD COLUMN no_tags BIT')
        self.cur.execute('ALTER TABLE ser_ver ADD COLUMN desc')

    def _migrate_to_v6(self):
        """Add workflow table for tracking todos and other workflow items

        Fields:
            id: Auto-increment primary key
            type: Workflow-entry type, e.g. 'todo' or 'sent'
            series_id: Foreign key referencing series.id
            timestamp: Due/event time as 'YYYY-MM-DD HH:MM:SS'
            archived: 0 for active entries, 1 for archived (soft-delete)
        """
        self.cur.execute(
            'CREATE TABLE workflow (id INTEGER PRIMARY KEY AUTOINCREMENT,'
            'type, series_id INTEGER, timestamp, archived BIT,'
            'FOREIGN KEY (series_id) REFERENCES series (id))')

    def _migrate_to_v7(self):
        """Add ser_ver_id to workflow table for tracking which version was sent

        Fields:
            ser_ver_id: Foreign key referencing ser_ver.id, or NULL for
                entries not tied to a specific version (e.g. todo)
        """
        self.cur.execute(
            'ALTER TABLE workflow ADD COLUMN ser_ver_id INTEGER')

    def _migrate_to_v8(self):
        """Add review tracking and series source type

        - Add source column to series table (NULL for user's own series,
          'review' for series being reviewed)
        - Add review table for storing AI review results per patch
        """
        self.cur.execute('ALTER TABLE series ADD COLUMN source')
        self.cur.execute(
            'CREATE TABLE review (id INTEGER PRIMARY KEY AUTOINCREMENT,'
            'svid INTEGER, seq INTEGER, body TEXT, approved BIT, '
            'timestamp TEXT, '
            'FOREIGN KEY (svid) REFERENCES ser_ver (id))')

    def _migrate_to_v9(self):
        """Add draft tracking, status, and Gmail IDs to review table"""
        self.cur.execute('ALTER TABLE review ADD COLUMN draft_id')
        self.cur.execute('ALTER TABLE review ADD COLUMN status')
        self.cur.execute('ALTER TABLE review ADD COLUMN gmail_msg_id')
        self.cur.execute('ALTER TABLE review ADD COLUMN gmail_thread_id')
        self.cur.execute("UPDATE review SET status = 'new'")

    def _migrate_to_v10(self):
        """Add review notes column to ser_ver table"""
        self.cur.execute('ALTER TABLE ser_ver ADD COLUMN notes')

    def _migrate_to_v11(self):
        """Add a reply table for replies to our reviews"""
        self.cur.execute(
            'CREATE TABLE reply (id INTEGER PRIMARY KEY AUTOINCREMENT,'
            'review_id INTEGER, gmail_msg_id UNIQUE, msgid, refs, sender, '
            'to_addrs, cc_addrs, subject, date, body TEXT, response TEXT, '
            'status, draft_id, timestamp, '
            'FOREIGN KEY (review_id) REFERENCES review (id))')

    def _migrate_to_v12(self):
        """Add the agent session ID to the review table"""
        self.cur.execute('ALTER TABLE review ADD COLUMN session_id')

    # pylint: disable=R0912
    def migrate_to(self, dest_version):
        """Migrate the database to the selected version

        Args:
            dest_version (int): Version to migrate to
        """
        # Serialise migrations across processes via an advisory lock on a
        # sentinel file beside the DB. Without this, two patman processes
        # starting against the same out-of-date DB can both decide they
        # need to run the next migration step, and the second one crashes
        # on a duplicate-column ALTER TABLE
        with open(f'{self.db_path}.lock', 'w', encoding='utf-8') as lock_fd:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            self._migrate_locked(dest_version)

    def _migrate_locked(self, dest_version):
        """Run the migration loop; caller holds the migration lock"""
        while True:
            # Re-read each iteration: a peer process may have advanced the
            # version while we were waiting for the lock
            version = self.get_schema_version()
            if version == dest_version:
                break

            self.close()
            tools.write_file(f'{self.db_path}old.v{version}',
                             tools.read_file(self.db_path))

            version += 1
            tout.notice(f'Update database to v{version}')
            self.open_it()
            if version == 1:
                self.create_v1()
            elif version == 2:
                self._migrate_to_v2()
            elif version == 3:
                self._migrate_to_v3()
            elif version == 4:
                self._migrate_to_v4()
            elif version == 5:
                self._migrate_to_v5()
            elif version == 6:
                self._migrate_to_v6()
            elif version == 7:
                self._migrate_to_v7()
            elif version == 8:
                self._migrate_to_v8()
            elif version == 9:
                self._migrate_to_v9()
            elif version == 10:
                self._migrate_to_v10()
            elif version == 11:
                self._migrate_to_v11()
            elif version == 12:
                self._migrate_to_v12()

            # Save the new version if we have a schema_version table
            if version > 1:
                self.cur.execute('DELETE FROM schema_version')
                self.cur.execute(
                    'INSERT INTO schema_version (version) VALUES (?)',
                    (version,))
            self.commit()

    def get_schema_version(self):
        """Get the version of the database's schema

        Return:
            int: Database version, 0 means there is no data; anything less than
                LATEST means the schema is out of date and must be updated
        """
        # If there is no database at all, assume v0
        version = 0
        try:
            self.cur.execute('SELECT name FROM series')
        except sqlite3.OperationalError:
            return 0

        # If there is no schema, assume v1
        try:
            self.cur.execute('SELECT version FROM schema_version')
            version = self.cur.fetchone()[0]
        except sqlite3.OperationalError:
            return 1
        return version

    def execute(self, query, parameters=()):
        """Execute a database query

        Args:
            query (str): Query string
            parameters (list of values): Parameters to pass

        Return:

        """
        return self.cur.execute(query, parameters)

    def commit(self):
        """Commit changes to the database"""
        self.con.commit()

    def rollback(self):
        """Roll back changes to the database"""
        self.con.rollback()

    def lastrowid(self):
        """Get the last row-ID reported by the database

        Return:
            int: Value for lastrowid
        """
        return self.cur.lastrowid

    def rowcount(self):
        """Get the row-count reported by the database

        Return:
            int: Value for rowcount
        """
        return self.cur.rowcount

    def _get_series_list(self, include_archived, include_reviews=False,
                         reviews_only=False):
        """Get a list of Series objects from the database

        Args:
            include_archived (bool): True to include archives series
            include_reviews (bool): True to include review series
            reviews_only (bool): True to show only review series

        Return:
            list of Series
        """
        conditions = []
        if not include_archived:
            conditions.append('archived = 0')
        if reviews_only:
            conditions.append("source = 'review'")
        elif not include_reviews:
            conditions.append("(source IS NULL OR source != 'review')")
        where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
        res = self.execute(
            f'SELECT id, name, desc, upstream FROM series {where}')
        return [Series.from_fields(idnum=idnum, name=name, desc=desc, ups=ups)
                for idnum, name, desc, ups in res.fetchall()]

    # series functions

    def series_get_dict_by_id(self, include_archived=False):
        """Get a dict of Series objects from the database

        Args:
            include_archived (bool): True to include archives series

        Return:
            OrderedDict:
                key: series ID
                value: Series with idnum, name and desc filled out
        """
        sdict = OrderedDict()
        for ser in self._get_series_list(include_archived):
            sdict[ser.idnum] = ser
        return sdict

    def series_find_by_name(self, name, include_archived=False):
        """Find a series and return its details

        Args:
            name (str): Name to search for
            include_archived (bool): True to include archives series

        Returns:
            idnum, or None if not found
        """
        res = self.execute(
            'SELECT id FROM series WHERE name = ?' +
            ('AND archived = 0' if not include_archived else ''), (name,))
        recs = res.fetchall()

        # This shouldn't happen
        assert len(recs) <= 1, 'Expected one match, but multiple found'

        if len(recs) != 1:
            return None
        return recs[0][0]

    def series_get_info(self, idnum):
        """Get information for a series from the database

        Args:
            idnum (int): Series ID to look up

        Return: tuple:
            str: Series name
            str: Series description
            str or None: Upstream name

        Raises:
            ValueError: Series is not found
        """
        res = self.execute(
            'SELECT name, desc, upstream FROM series WHERE id = ?',
            (idnum,))
        recs = res.fetchall()
        if len(recs) != 1:
            raise ValueError(f'No series found (id {idnum} len {len(recs)})')
        return recs[0]

    def series_get_dict(self, include_archived=False,
                        include_reviews=False, reviews_only=False):
        """Get a dict of Series objects from the database

        Args:
            include_archived (bool): True to include archives series
            include_reviews (bool): True to include review series
            reviews_only (bool): True to show only review series

        Return:
            OrderedDict:
                key: series name
                value: Series with idnum, name and desc filled out
        """
        sdict = OrderedDict()
        for ser in self._get_series_list(include_archived, include_reviews,
                                         reviews_only):
            sdict[ser.name] = ser
        return sdict

    def series_get_version_list(self, series_idnum):
        """Get a list of the versions available for a series

        Args:
            series_idnum (int): ID of series to look up

        Return:
            str: List of versions, which may be empty if the series is in the
                process of being added
        """
        res = self.execute('SELECT version FROM ser_ver WHERE series_id = ?',
                           (series_idnum,))
        return [x[0] for x in res.fetchall()]

    def series_get_max_version(self, series_idnum):
        """Get the highest version number available for a series

        Args:
            series_idnum (int): ID of series to look up

        Return:
            int: Maximum version number, or 0 if the series has no versions
                recorded yet
        """
        res = self.execute(
            'SELECT MAX(version) FROM ser_ver WHERE series_id = ?',
            (series_idnum,))
        # MAX() over no rows is SQL NULL; report that as 0, not None, so
        # callers can compare and do arithmetic without a type error
        return res.fetchall()[0][0] or 0

    def series_get_all_max_versions(self):
        """Find the latest version of all series

        Return: list of:
            int: ser_ver ID
            int: series ID
            int: Maximum version
        """
        res = self.execute(
            'SELECT id, series_id, MAX(version) FROM ser_ver '
            'GROUP BY series_id')
        return res.fetchall()

    def series_add(self, name, desc, ups=None):
        """Add a new series record

        The new record is set to not archived

        Args:
            name (str): Series name
            desc (str): Series description
            ups (str or None): Name of the upstream for this series

        Return:
            int: ID num of the new series record
        """
        self.execute(
            'INSERT INTO series (name, desc, archived, upstream) '
            'VALUES (?, ?, 0, ?)', (name, desc, ups))
        return self.lastrowid()

    def series_remove(self, idnum):
        """Remove a series from the database

        The series must exist

        Args:
            idnum (int): ID num of series to remove
        """
        self.execute('DELETE FROM series WHERE id = ?', (idnum,))
        assert self.rowcount() == 1

    def series_remove_by_name(self, name):
        """Remove a series from the database

        Args:
            name (str): Name of series to remove

        Raises:
            ValueError: Series does not exist (database is rolled back)
        """
        self.execute('DELETE FROM series WHERE name = ?', (name,))
        if self.rowcount() != 1:
            self.rollback()
            raise ValueError(f"No such series '{name}'")

    def series_set_archived(self, series_idnum, archived):
        """Update archive flag for a series

        Args:
            series_idnum (int): ID num of the series
            archived (bool): Whether to mark the series as archived or
                unarchived
        """
        self.execute(
            'UPDATE series SET archived = ? WHERE id = ?',
            (archived, series_idnum))

    def series_set_name(self, series_idnum, name):
        """Update name for a series

        Args:
            series_idnum (int): ID num of the series
            name (str): new name to use
        """
        self.execute(
            'UPDATE series SET name = ? WHERE id = ?', (name, series_idnum))

    def series_set_desc(self, series_idnum, desc):
        """Update description for a series

        Args:
            series_idnum (int): ID num of the series
            desc (str): New description
        """
        self.execute(
            'UPDATE series SET desc = ? WHERE id = ?',
            (desc, series_idnum))

    def series_set_upstream(self, series_idnum, ups):
        """Update upstream for a series

        Args:
            series_idnum (int): ID num of the series
            ups (str or None): Name of the upstream, or None to clear
        """
        self.execute(
            'UPDATE series SET upstream = ? WHERE id = ?',
            (ups, series_idnum))

    def series_set_source(self, series_idnum, source):
        """Update the source field for a series

        Args:
            series_idnum (int): ID num of the series
            source (str): Source value, e.g. 'review'
        """
        self.execute(
            'UPDATE series SET source = ? WHERE id = ?',
            (source, series_idnum))

    def series_get_null_upstream(self):
        """Get a list of series names that have no upstream set

        Return:
            list of str: Series names with NULL upstream
        """
        res = self.execute(
            'SELECT name FROM series WHERE upstream IS NULL')
        return [row[0] for row in res.fetchall()]

    # ser_ver functions

    def ser_ver_get_link(self, series_idnum, version):
        """Get the link for a series version

        Args:
            series_idnum (int): ID num of the series
            version (int): Version number to search for

        Return:
            str: Patchwork link as a string, e.g. '12325', or None if none

        Raises:
            ValueError: Multiple matches are found
        """
        res = self.execute(
            'SELECT link FROM ser_ver WHERE '
            f"series_id = {series_idnum} AND version = '{version}'")
        recs = res.fetchall()
        if not recs:
            return None
        if len(recs) > 1:
            raise ValueError('Expected one match, but multiple matches found')
        return recs[0][0]

    def ser_ver_set_link(self, series_idnum, version, link):
        """Set the link for a series version

        Args:
            series_idnum (int): ID num of the series
            version (int): Version number to search for
            link (str): Patchwork link for the ser_ver

        Return:
            bool: True if the record was found and updated, else False
        """
        if link is None:
            link = ''
        self.execute(
            'UPDATE ser_ver SET link = ? WHERE series_id = ? AND version = ?',
            (str(link), series_idnum, version))
        return self.rowcount() != 0

    def ser_ver_set_info(self, info):
        """Set the info for a series version

        Args:
            info (SER_VER): Info to set. Only two options are supported:
                1: svid,cover_id,cover_num_comments,name
                2: svid,name

        Return:
            bool: True if the record was found and updated, else False
        """
        assert info.idnum is not None
        if info.cover_id:
            assert info.series_id is None
            self.execute(
                'UPDATE ser_ver SET cover_id = ?, cover_num_comments = ?, '
                'name = ? WHERE id = ?',
                (info.cover_id, info.cover_num_comments, info.name,
                 info.idnum))
        else:
            assert not info.cover_id
            assert not info.cover_num_comments
            assert not info.series_id
            assert not info.version
            assert not info.link
            self.execute('UPDATE ser_ver SET name = ? WHERE id = ?',
                         (info.name, info.idnum))

        return self.rowcount() != 0

    def ser_ver_set_version(self, svid, version):
        """Sets the version for a ser_ver record

        Args:
            svid (int): Record ID to update
            version (int): Version number to add

        Raises:
            ValueError: svid was not found
        """
        self.execute(
            'UPDATE ser_ver SET version = ? WHERE id = ?', (version, svid))
        if self.rowcount() != 1:
            raise ValueError(f'No ser_ver updated (svid {svid})')

    def ser_ver_set_archive_tag(self, svid, tag):
        """Sets the archive tag for a ser_ver record

        Args:
            svid (int): Record ID to update
            tag (tag): Tag to add

        Raises:
            ValueError: svid was not found
        """
        self.execute(
            'UPDATE ser_ver SET archive_tag = ? WHERE id = ?', (tag, svid))
        if self.rowcount() != 1:
            raise ValueError(f'No ser_ver updated (svid {svid})')

    def ser_ver_set_desc(self, svid, desc):
        """Update the description for a series version

        Args:
            svid (int): ser_ver ID num
            desc (str): Description text
        """
        self.execute('UPDATE ser_ver SET desc = ? WHERE id = ?', (desc, svid))

    def ser_ver_set_notes(self, svid, notes):
        """Store review-handling notes for a series version

        Args:
            svid (int): ser_ver ID num
            notes (str): Notes text from review-notes.txt
        """
        self.execute(
            'UPDATE ser_ver SET notes = ? WHERE id = ?', (notes, svid))

    def ser_ver_get_all_notes(self, series_id):
        """Get review notes from all versions of a series

        Args:
            series_id (int): Series ID

        Return:
            list of tuple: (version, notes) for versions that have notes,
                ordered by version
        """
        res = self.execute(
            'SELECT version, notes FROM ser_ver '
            'WHERE series_id = ? AND notes IS NOT NULL '
            'ORDER BY version', (series_id,))
        return [(v, n) for v, n in res.fetchall() if n]

    def ser_ver_add(self, series_idnum, version, link=None, desc=None):
        """Add a new ser_ver record

        Args:
            series_idnum (int): ID num of the series which is getting a new
                version
            version (int): Version number to add
            link (str): Patchwork link, or None if not known
            desc (str or None): Series description for this version

        Return:
            int: ID num of the new ser_ver record
        """
        self.execute(
            'INSERT INTO ser_ver (series_id, version, link, desc) '
            'VALUES (?, ?, ?, ?)',
            (series_idnum, version, link, desc))
        return self.lastrowid()

    def ser_ver_get_for_series(self, series_idnum, version=None):
        """Get a list of ser_ver records for a given series ID

        Args:
            series_idnum (int): ID num of the series to search
            version (int): Version number to search for, or None for all

        Return:
            SER_VER: Requested information

        Raises:
            ValueError: There is no matching idnum/version
        """
        base = ('SELECT id, series_id, version, link, cover_id, '
                'cover_num_comments, name, archive_tag, desc, notes '
                'FROM ser_ver WHERE series_id = ?')
        if version:
            res = self.execute(base + ' AND version = ?',
                               (series_idnum, version))
        else:
            res = self.execute(base, (series_idnum,))
        recs = res.fetchall()
        if not recs:
            raise ValueError(
                f'No matching series for id {series_idnum} version {version}')
        if version:
            return SerVer(*recs[0])
        return [SerVer(*x) for x in recs]

    def ser_ver_get_by_id(self, svid):
        """Get a ser_ver record by its ID

        Args:
            svid (int): ser_ver ID num

        Return:
            SER_VER: Requested information

        Raises:
            ValueError: There is no such record
        """
        res = self.execute(
            'SELECT id, series_id, version, link, cover_id, '
            'cover_num_comments, name, archive_tag, desc, notes '
            'FROM ser_ver WHERE id = ?', (svid,))
        rec = res.fetchone()
        if not rec:
            raise ValueError(f'No series version with id {svid}')
        return SerVer(*rec)

    def ser_ver_get_svids(self, series_idnum):
        """Get all ser_ver IDs for a series

        Args:
            series_idnum (int): Series ID

        Return:
            list of int: svids of every version of the series
        """
        res = self.execute('SELECT id FROM ser_ver WHERE series_id = ?',
                           (series_idnum,))
        return [row[0] for row in res.fetchall()]

    def ser_ver_set_series(self, svid, series_idnum):
        """Move a ser_ver record to a different series

        Args:
            svid (int): ser_ver ID to move
            series_idnum (int): Series ID to attach it to
        """
        self.execute('UPDATE ser_ver SET series_id = ? WHERE id = ?',
                     (series_idnum, svid))

    def ser_ver_get_ids_for_series(self, series_idnum, version=None):
        """Get a list of ser_ver records for a given series ID

        Args:
            series_idnum (int): ID num of the series to search
            version (int): Version number to search for, or None for all

        Return:
            list of int: List of svids for the matching records
        """
        if version:
            res = self.execute(
                'SELECT id FROM ser_ver WHERE series_id = ? AND version = ?',
                (series_idnum, version))
        else:
            res = self.execute(
                'SELECT id FROM ser_ver WHERE series_id = ?', (series_idnum,))
        return list(res.fetchall()[0])

    def ser_ver_get_list(self):
        """Get a list of patchwork entries from the database

        Return:
            list of SER_VER
        """
        res = self.execute(
            'SELECT id, series_id, version, link, cover_id, '
            'cover_num_comments, name, archive_tag, desc, notes '
            'FROM ser_ver')
        items = res.fetchall()
        return [SerVer(*x) for x in items]

    def ser_ver_remove(self, series_idnum, version=None, remove_pcommits=True,
                       remove_series=True):
        """Delete a ser_ver record

        Removes the record which has the given series ID num and version

        Args:
            series_idnum (int): ID num of the series
            version (int): Version number, or None to remove all versions
            remove_pcommits (bool): True to remove associated pcommits too
            remove_series (bool): True to remove the series if versions is None
        """
        if remove_pcommits:
            # Figure out svids to delete
            svids = self.ser_ver_get_ids_for_series(series_idnum, version)

            self.pcommit_delete_list(svids)

        if version:
            self.execute(
                'DELETE FROM ser_ver WHERE series_id = ? AND version = ?',
                (series_idnum, version))
        else:
            self.execute(
                'DELETE FROM ser_ver WHERE series_id = ?',
                (series_idnum,))
        if not version and remove_series:
            self.series_remove(series_idnum)

    # pcommit functions

    def pcommit_get_list(self, find_svid=None):
        """Get a dict of pcommits entries from the database

        Args:
            find_svid (int): If not None, finds the records associated with a
                particular series and version; otherwise returns all records

        Return:
            list of PCOMMIT: pcommit records
        """
        query = ('SELECT id, seq, subject, svid, change_id, state, patch_id, '
                 'num_comments FROM pcommit')
        if find_svid is not None:
            query += f' WHERE svid = {find_svid}'
        res = self.execute(query)
        return [Pcommit(*rec) for rec in res.fetchall()]

    def pcommit_add_list(self, svid, pcommits):
        """Add records to the pcommit table

        Args:
            svid (int): ser_ver ID num
            pcommits (list of PCOMMIT): Only seq, subject, change_id are
                uses; svid comes from the argument passed in and the others
                are assumed to be obtained from patchwork later
        """
        for pcm in pcommits:
            self.execute(
                'INSERT INTO pcommit (svid, seq, subject, change_id) VALUES '
                '(?, ?, ?, ?)', (svid, pcm.seq, pcm.subject, pcm.change_id))

    def pcommit_delete(self, svid):
        """Delete pcommit records for a given ser_ver ID

        Args_:
            svid (int): ser_ver ID num of records to delete
        """
        self.execute('DELETE FROM pcommit WHERE svid = ?', (svid,))

    def pcommit_delete_list(self, svid_list):
        """Delete pcommit records for a given set of ser_ver IDs

        Args_:
            svid (list int): ser_ver ID nums of records to delete
        """
        vals = ', '.join([str(x) for x in svid_list])
        self.execute('DELETE FROM pcommit WHERE svid IN (?)', (vals,))

    def pcommit_update(self, pcm):
        """Update a pcommit record

        Args:
            pcm (PCOMMIT): Information to write; only the idnum, state,
                patch_id and num_comments are used

        Return:
            True if the data was written
        """
        self.execute(
            'UPDATE pcommit SET '
            'patch_id = ?, state = ?, num_comments = ? WHERE id = ?',
            (pcm.patch_id, pcm.state, pcm.num_comments, pcm.idnum))
        return self.rowcount() > 0

    # upstream functions

    # pylint: disable=R0913,R0917
    def upstream_add(self, name, url, patchwork_url=None, identity=None,
                     series_to=None, no_maintainers=False, no_tags=False):
        """Add a new upstream record

        Args:
            name (str): Name of the tree
            url (str): URL for the tree
            patchwork_url (str or None): URL of the patchwork server
            identity (str or None): Git sendemail identity to use
            series_to (str or None): Patman alias for the To address
            no_maintainers (bool): True to skip get_maintainer.pl
            no_tags (bool): True to skip subject-tag alias processing

        Raises:
            ValueError if the name already exists in the database
        """
        try:
            self.execute(
                'INSERT INTO upstream (name, url, patchwork_url, identity,'
                ' series_to, no_maintainers, no_tags) '
                'VALUES (?, ?, ?, ?, ?, ?, ?)',
                (name, url, patchwork_url, identity, series_to,
                 no_maintainers, no_tags))
        except sqlite3.IntegrityError as exc:
            if 'UNIQUE constraint failed: upstream.name' in str(exc):
                raise ValueError(f"Upstream '{name}' already exists") from exc

    def upstream_set_default(self, name):
        """Mark (only) the given upstream as the default

        Args:
            name (str): Name of the upstream remote to set as default, or None

        Raises:
            ValueError if more than one name matches (should not happen);
                database is rolled back
        """
        self.execute("UPDATE upstream SET is_default = 0")
        if name is not None:
            self.execute(
                'UPDATE upstream SET is_default = 1 WHERE name = ?', (name,))
            if self.rowcount() != 1:
                self.rollback()
                raise ValueError(f"No such upstream '{name}'")

    def upstream_get_default(self):
        """Get the name of the default upstream

        Return:
            str: Default-upstream name, or None if there is no default
        """
        res = self.execute(
            "SELECT name FROM upstream WHERE is_default = 1")
        recs = res.fetchall()
        if len(recs) != 1:
            return None
        return recs[0][0]

    def upstream_delete(self, name):
        """Delete an upstream target

        Args:
            name (str): Name of the upstream remote to delete

        Raises:
            ValueError: Upstream does not exist (database is rolled back)
        """
        self.execute(f"DELETE FROM upstream WHERE name = '{name}'")
        if self.rowcount() != 1:
            self.rollback()
            raise ValueError(f"No such upstream '{name}'")

    def upstream_set(self, name, **kwargs):
        """Update fields on an existing upstream

        Args:
            name (str): Name of the upstream remote to update
            kwargs: Fields to update, each being one of:
                patchwork_url (str): URL of the patchwork server, e.g.
                    'patchwork.ozlabs.org'
                identity (str): Git sendemail identity to use when
                    sending, corresponding to a [sendemail "<identity>"]
                    section in .gitconfig
                series_to (str): Mailing-list address to use as the
                    default To: for this upstream
                no_maintainers (bool): True to skip
                    get_maintainer.pl when sending
                no_tags (bool): True to skip processing of subject
                    tags (e.g. 'dm:') when sending

        Raises:
            ValueError: Upstream does not exist or invalid field
        """
        valid = {'patchwork_url', 'identity', 'series_to',
                 'no_maintainers', 'no_tags'}
        invalid = set(kwargs) - valid
        if invalid:
            raise ValueError(f"Invalid upstream field(s): {invalid}")
        for field, value in kwargs.items():
            self.execute(
                f'UPDATE upstream SET {field} = ? WHERE name = ?',
                (value, name))
            if self.rowcount() != 1:
                self.rollback()
                raise ValueError(f"No such upstream '{name}'")

    def upstream_get_patchwork_url(self, name):
        """Get the patchwork URL for an upstream

        Args:
            name (str): Upstream name

        Return:
            str or None: Patchwork URL, or None if not set
        """
        res = self.execute(
            'SELECT patchwork_url FROM upstream WHERE name = ?', (name,))
        rec = res.fetchone()
        if rec:
            return rec[0]
        return None

    def upstream_get_identity(self, name):
        """Get the sendemail identity for an upstream

        Args:
            name (str): Upstream name

        Return:
            str or None: Identity name, or None if not set
        """
        res = self.execute(
            'SELECT identity FROM upstream WHERE name = ?', (name,))
        rec = res.fetchone()
        if rec:
            return rec[0]
        return None

    def upstream_get_send_settings(self, name):
        """Get the send settings for an upstream

        Args:
            name (str): Upstream name

        Return:
            tuple or None:
                str or None: identity
                str or None: series_to
                bool: no_maintainers
                bool: no_tags
        """
        res = self.execute(
            'SELECT identity, series_to, no_maintainers, no_tags '
            'FROM upstream WHERE name = ?', (name,))
        rec = res.fetchone()
        if rec:
            return rec
        return None

    def upstream_get_dict(self):
        """Get a list of upstream entries from the database

        Return:
            OrderedDict:
                key (str): upstream name
                value: tuple:
                    str: url
                    bool: is_default
                    str or None: patchwork_url
                    str or None: identity
                    str or None: series_to
                    bool: no_maintainers
                    bool: no_tags
        """
        res = self.execute(
            'SELECT name, url, is_default, patchwork_url, identity,'
            ' series_to, no_maintainers, no_tags FROM upstream')
        udict = OrderedDict()
        for rec in res.fetchall():
            udict[rec[0]] = rec[1:]
        return udict

    # patchwork functions

    def patchwork_update(self, name, proj_id, link_name, ups=None):
        """Set the patchwork project details for an upstream

        Args:
            name (str): Name of the project to use in patchwork
            proj_id (int): Project ID for the project
            link_name (str): Link name for the project
            ups (str or None): Upstream name to associate with, or None
        """
        self.execute(
            'DELETE FROM patchwork WHERE upstream IS ?', (ups,))
        self.execute(
            'INSERT INTO patchwork (name, proj_id, link_name, upstream) '
            'VALUES (?, ?, ?, ?)', (name, proj_id, link_name, ups))

    def patchwork_delete(self, ups):
        """Delete a patchwork project configuration

        Args:
            ups (str or None): Upstream name to delete, or None for the
                entry with no upstream

        Raises:
            ValueError: if no matching entry exists
        """
        if ups is not None:
            self.execute(
                'DELETE FROM patchwork WHERE upstream = ?', (ups,))
        else:
            self.execute(
                'DELETE FROM patchwork WHERE upstream IS NULL')
        if not self.rowcount():
            raise ValueError(
                f"No patchwork project found for upstream '{ups}'")

    def patchwork_get_list(self):
        """Get all patchwork project configurations

        Returns:
            list of tuple:
                name (str): Project name, e.g. 'U-Boot'
                proj_id (int): Patchworks project ID for this project
                link_name (str): Patchwork's link-name for the project
                upstream (str or None): Upstream name
        """
        res = self.execute(
            'SELECT name, proj_id, link_name, upstream FROM patchwork '
            'ORDER BY name, upstream')
        return res.fetchall()

    def patchwork_get(self, ups=None):
        """Get the patchwork project details for an upstream

        Args:
            ups (str or None): Upstream name to look up, or None for any

        Returns:
            tuple or None if there is no project set:
                name (str): Project name, e.g. 'U-Boot'
                proj_id (int): Patchworks project ID for this project
                link_name (str): Patchwork's link-name for the project
        """
        if ups is not None:
            res = self.execute(
                'SELECT name, proj_id, link_name FROM patchwork '
                'WHERE upstream = ?', (ups,))
        else:
            res = self.execute(
                'SELECT name, proj_id, link_name FROM patchwork')
        recs = res.fetchall()
        if not recs:
            return None
        return recs[0]

    # workflow functions

    def workflow_add(self, wtype, series_id, timestamp, ser_ver_id=None):
        """Add a workflow entry

        Args:
            wtype (str): Workflow type, e.g. 'todo'
            series_id (int): ID of the series
            timestamp (str): Timestamp string, e.g. '2025-01-15 10:30:00'
            ser_ver_id (int or None): ID of the ser_ver record, if applicable
        """
        self.execute(
            'INSERT INTO workflow '
            '(type, series_id, timestamp, archived, ser_ver_id) '
            'VALUES (?, ?, ?, 0, ?)',
            (wtype, series_id, timestamp, ser_ver_id))

    def workflow_archive(self, wtype, series_id):
        """Archive active workflow entries for a given type and series

        Args:
            wtype (str): Workflow type, e.g. 'todo'
            series_id (int): ID of the series
        """
        self.execute(
            'UPDATE workflow SET archived = 1 '
            'WHERE type = ? AND series_id = ? AND archived = 0',
            (wtype, series_id))

    def workflow_get(self, wtype, series_id):
        """Get the active workflow entry for a given type and series

        Args:
            wtype (str): Workflow type, e.g. 'todo'
            series_id (int): ID of the series

        Return:
            str or None: Timestamp string if found, else None
        """
        res = self.execute(
            'SELECT timestamp FROM workflow '
            'WHERE type = ? AND series_id = ? AND archived = 0',
            (wtype, series_id))
        rec = res.fetchone()
        if rec:
            return rec[0]
        return None

    def workflow_get_by_type(self, wtype, before=None):
        """Get active workflow entries for a given type, joined with series

        Args:
            wtype (str): Workflow type, e.g. 'todo'
            before (str or None): If set, only return entries where
                timestamp <= this value

        Return:
            list of tuple:
                int: series ID
                str: series name
                str: series description
                str: timestamp
        """
        query = ('SELECT s.id, s.name, s.desc, w.timestamp '
                 'FROM workflow w '
                 'JOIN series s ON w.series_id = s.id '
                 'WHERE w.type = ? AND w.archived = 0 AND s.archived = 0')
        params = [wtype]
        if before is not None:
            query += ' AND w.timestamp <= ?'
            params.append(before)
        query += ' ORDER BY w.timestamp'
        res = self.execute(query, params)
        return res.fetchall()

    def workflow_list(self, include_archived=False):
        """Get workflow entries joined with series info

        Args:
            include_archived (bool): True to include archived entries

        Return:
            list of tuple:
                str: workflow type
                str: series name
                str: series description
                str: timestamp
                int: archived flag (0 or 1)
                int or None: version number from ser_ver, if applicable
        """
        query = ('SELECT w.type, s.name, s.desc, w.timestamp, w.archived,'
                 'sv.version '
                 'FROM workflow w '
                 'JOIN series s ON w.series_id = s.id '
                 'LEFT JOIN ser_ver sv ON w.ser_ver_id = sv.id')
        if not include_archived:
            query += ' WHERE w.archived = 0'
        query += ' ORDER BY w.timestamp'
        res = self.execute(query)
        return res.fetchall()

    # pylint: disable=R0913,R0917
    def review_add(self, svid, seq, body, approved, timestamp):
        """Add a review record

        Args:
            svid (int): ser_ver ID num
            seq (int): Patch sequence (0 for cover, 1..N for patches)
            body (str): Review email body text
            approved (bool): True if Reviewed-by was given
            timestamp (str): ISO datetime string

        Return:
            int: ID num of the new review record
        """
        self.execute(
            'INSERT INTO review (svid, seq, body, approved, timestamp, '
            'status) VALUES (?, ?, ?, ?, ?, ?)',
            (svid, seq, body, 1 if approved else 0, timestamp,
             REVIEW_NEW))
        return self.lastrowid()

    def review_set_session(self, review_id, session_id):
        """Set the ID of the agent session which wrote a review

        Args:
            review_id (int): Review record ID
            session_id (str or None): Agent session ID
        """
        self.execute('UPDATE review SET session_id = ? WHERE id = ?',
                     (session_id, review_id))

    def review_get_session(self, review_id):
        """Get the ID of the agent session which wrote a review

        Args:
            review_id (int): Review record ID

        Return:
            str or None: Agent session ID, or None if not known
        """
        rec = self.execute('SELECT session_id FROM review WHERE id = ?',
                           (review_id,)).fetchone()
        return rec[0] if rec else None

    def review_set_draft_id(self, review_id, draft_id):
        """Set the Gmail draft ID for a review record

        Args:
            review_id (int): Review record ID
            draft_id (str or None): Gmail draft ID
        """
        status = REVIEW_DRAFT if draft_id else None
        self.execute(
            'UPDATE review SET draft_id = ?, status = ? WHERE id = ?',
            (draft_id, status, review_id))

    def review_set_sent(self, review_id, body, gmail_msg_id=None,
                        gmail_thread_id=None):
        """Mark a review as sent and update its body

        Args:
            review_id (int): Review record ID
            body (str): Sent email body text
            gmail_msg_id (str or None): Gmail message ID of sent email
            gmail_thread_id (str or None): Gmail thread ID
        """
        self.execute(
            'UPDATE review SET body = ?, draft_id = NULL, status = ?, '
            'gmail_msg_id = ?, gmail_thread_id = ? WHERE id = ?',
            (body, REVIEW_SENT, gmail_msg_id, gmail_thread_id,
             review_id))

    def review_set_body(self, review_id, body, approved):
        """Change the text of a review which has not been sent

        Args:
            review_id (int): Review record ID
            body (str): New review email body text
            approved (bool): True if Reviewed-by is given
        """
        self.execute(
            'UPDATE review SET body = ?, approved = ? WHERE id = ?',
            (body, 1 if approved else 0, review_id))

    def review_set_thread(self, review_id, thread_id):
        """Note the Gmail thread holding the draft of a review

        Args:
            review_id (int): Review record ID
            thread_id (str): Gmail thread ID
        """
        self.execute('UPDATE review SET gmail_thread_id = ? WHERE id = ?',
                     (thread_id, review_id))

    def review_set_replied(self, review_id):
        """Mark a review as having received a reply

        Args:
            review_id (int): Review record ID
        """
        self.execute(
            'UPDATE review SET status = ? WHERE id = ?',
            (REVIEW_REPLIED, review_id))

    def review_set_deleted(self, review_id):
        """Mark a review draft as deleted (not sent)

        Args:
            review_id (int): Review record ID
        """
        self.execute(
            'UPDATE review SET draft_id = NULL, status = ? '
            'WHERE id = ?', (REVIEW_DELETED, review_id))

    def review_delete_for_version(self, svid):
        """Delete all review records for a given series version

        Args:
            svid (int): ser_ver ID num
        """
        self.execute('DELETE FROM reply WHERE review_id IN '
                     '(SELECT id FROM review WHERE svid = ?)', (svid,))
        self.execute('DELETE FROM review WHERE svid = ?', (svid,))

    def review_get_for_version(self, svid):
        """Get review records for a given series version

        Args:
            svid (int): ser_ver ID num

        Return:
            list of Review: Review records ordered by sequence
        """
        res = self.execute(
            'SELECT id, svid, seq, body, approved, timestamp, draft_id, '
            'status, gmail_msg_id, gmail_thread_id '
            'FROM review WHERE svid = ? ORDER BY seq', (svid,))
        return [Review(*row) for row in res.fetchall()]

    def review_get(self, review_id):
        """Get a review record by its ID

        Args:
            review_id (int): Review record ID

        Return:
            Review: Review record

        Raises:
            ValueError: There is no such review
        """
        res = self.execute(
            'SELECT id, svid, seq, body, approved, timestamp, draft_id, '
            'status, gmail_msg_id, gmail_thread_id '
            'FROM review WHERE id = ?', (review_id,))
        rec = res.fetchone()
        if not rec:
            raise ValueError(f'No review with id {review_id}')
        return Review(*rec)

    def review_get_by_status(self, status, need_thread=False):
        """Get review records with a given status

        Args:
            status (str): Status to filter by (e.g. 'draft', 'sent')
            need_thread (bool): If True, only return records with a
                gmail_thread_id

        Return:
            list of Review: Matching review records
        """
        sql = ('SELECT id, svid, seq, body, approved, timestamp, '
               'draft_id, status, gmail_msg_id, gmail_thread_id '
               'FROM review WHERE status = ?')
        if need_thread:
            sql += ' AND gmail_thread_id IS NOT NULL'
        res = self.execute(sql, (status,))
        return [Review(*row) for row in res.fetchall()]

    def review_get_previous(self, series_id, version):
        """Get reviews from the most recent earlier version of a series

        Returns the reviews of the highest version below the current one
        that actually has reviews, so they can be provided as context when
        reviewing a new version. Skipping over versions with no reviews
        means a gap (e.g. an intermediate version that was never reviewed)
        still yields the earlier feedback rather than nothing.

        Args:
            series_id (int): Series ID
            version (int): Current version being reviewed

        Return:
            list of Review: Reviews from the latest earlier reviewed
                version, or empty list
        """
        res = self.execute(
            'SELECT sv.id FROM ser_ver sv '
            'JOIN review r ON r.svid = sv.id '
            'WHERE sv.series_id = ? AND sv.version < ? '
            'ORDER BY sv.version DESC LIMIT 1',
            (series_id, version))
        row = res.fetchone()
        if not row:
            return []
        return self.review_get_for_version(row[0])

    # pylint: disable=R0913,R0917
    def reply_add(self, review_id, gmail_msg_id, msgid, refs, sender,
                  to_addrs, cc_addrs, subject, date, body, timestamp):
        """Record a reply to one of our reviews, if not already recorded

        Args:
            review_id (int): ID of the review which was replied to
            gmail_msg_id (str): Gmail message ID of the reply
            msgid (str): Message-ID header of the reply
            refs (str): References header of the reply
            sender (str): From header of the reply
            to_addrs (str): To header of the reply
            cc_addrs (str): Cc header of the reply
            subject (str): Subject of the reply
            date (str): Date header of the reply
            body (str): Text of the reply
            timestamp (str): When the reply was recorded

        Return:
            int or None: ID of the new reply record, or None if the reply was
                already recorded
        """
        if self.execute('SELECT id FROM reply WHERE gmail_msg_id = ?',
                        (gmail_msg_id,)).fetchone():
            return None
        self.execute(
            'INSERT INTO reply (review_id, gmail_msg_id, msgid, refs, sender, '
            'to_addrs, cc_addrs, subject, date, body, status, timestamp) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (review_id, gmail_msg_id, msgid, refs, sender, to_addrs,
             cc_addrs, subject, date, body, REPLY_NEW, timestamp))
        return self.lastrowid()

    def reply_get(self, reply_id):
        """Get a reply record by its ID

        Args:
            reply_id (int): Reply record ID

        Return:
            Reply: Reply record

        Raises:
            ValueError: There is no such reply
        """
        rec = self.execute(f'SELECT {REPLY_COLS} FROM reply WHERE id = ?',
                           (reply_id,)).fetchone()
        if not rec:
            raise ValueError(f'No reply with id {reply_id}')
        return Reply(*rec)

    def reply_get_for_review(self, review_id):
        """Get the replies to a review, oldest first

        Args:
            review_id (int): Review record ID

        Return:
            list of Reply: Replies
        """
        res = self.execute(f'SELECT {REPLY_COLS} FROM reply '
                           'WHERE review_id = ? ORDER BY id', (review_id,))
        return [Reply(*rec) for rec in res.fetchall()]

    def reply_get_by_status(self, statuses):
        """Get the replies with any of the given statuses, oldest first

        Args:
            statuses (list of str): Statuses to look for

        Return:
            list of Reply: Replies
        """
        marks = ', '.join(['?'] * len(statuses))
        res = self.execute(f'SELECT {REPLY_COLS} FROM reply '
                           f'WHERE status IN ({marks}) ORDER BY id',
                           tuple(statuses))
        return [Reply(*rec) for rec in res.fetchall()]

    def reply_count_waiting(self):
        """Count the replies which still need attention, for each series

        Return:
            dict: key (int): series ID, value (int): number of replies
        """
        marks = ', '.join(['?'] * len(REPLY_WAITING))
        res = self.execute(
            'SELECT sv.series_id, COUNT(*) FROM reply rp '
            'JOIN review r ON r.id = rp.review_id '
            'JOIN ser_ver sv ON sv.id = r.svid '
            f'WHERE rp.status IN ({marks}) GROUP BY sv.series_id',
            REPLY_WAITING)
        return dict(res.fetchall())

    def reply_set_response(self, reply_id, response, status):
        """Set our response to a reply

        Args:
            reply_id (int): Reply record ID
            response (str or None): Text of the response
            status (str): New status, e.g. REPLY_SUGGESTED
        """
        self.execute('UPDATE reply SET response = ?, status = ? WHERE id = ?',
                     (response, status, reply_id))

    def reply_set_status(self, reply_id, status, draft_id=None):
        """Set the status of a reply, and the draft of our response

        Args:
            reply_id (int): Reply record ID
            status (str): New status
            draft_id (str or None): Gmail draft ID of the response, if any
        """
        self.execute('UPDATE reply SET status = ?, draft_id = ? WHERE id = ?',
                     (status, draft_id, reply_id))

    def series_find_by_link(self, link):
        """Find a series by its patchwork link

        Args:
            link (str): Patchwork series link/ID

        Return:
            tuple or None: (series_id, name, version, svid) if found
        """
        res = self.execute(
            'SELECT s.id, s.name, sv.version, sv.id '
            'FROM ser_ver sv '
            'JOIN series s ON sv.series_id = s.id '
            'WHERE sv.link = ?', (str(link),))
        return res.fetchone()

    def series_search(self, query, include_archived=False):
        """Search for series by subject fragment

        Matches series against the given query string in three places:
         - Series description (cover letter title)
         - ser_ver description (per-version title)
         - Patch subjects (pcommit.subject)

        Args:
            query (str): Text to search for (case-insensitive substring)
            include_archived (bool): True to include archived series

        Return:
            list of tuple: (series_id, name, desc, version, link, match_type,
                match_text) where match_type is 'series', 'version' or
                'patch', ordered by series name and version
        """
        pat = f'%{query}%'
        cond = '' if include_archived else ' AND s.archived = 0'

        sql = (
            "SELECT s.id, s.name, s.desc, sv.version, sv.link, "
            "'series' AS mtype, s.desc AS mtext "
            'FROM series s JOIN ser_ver sv ON sv.series_id = s.id '
            f'WHERE s.desc LIKE ?{cond} '
            'UNION '
            "SELECT s.id, s.name, s.desc, sv.version, sv.link, "
            "'version' AS mtype, sv.desc AS mtext "
            'FROM series s JOIN ser_ver sv ON sv.series_id = s.id '
            f'WHERE sv.desc LIKE ?{cond} '
            'UNION '
            "SELECT s.id, s.name, s.desc, sv.version, sv.link, "
            "'patch' AS mtype, pc.subject AS mtext "
            'FROM series s JOIN ser_ver sv ON sv.series_id = s.id '
            'JOIN pcommit pc ON pc.svid = sv.id '
            f'WHERE pc.subject LIKE ?{cond} '
            'ORDER BY 2, 4')
        res = self.execute(sql, (pat, pat, pat))
        return res.fetchall()

    def series_find_review_by_name(self, name):
        """Find a review series by its description

        Looks for series with source='review' whose description matches
        the given name, so that new versions of a previously reviewed
        series can be added under the same series record.

        Args:
            name (str): Series description (cover letter title)

        Return:
            tuple or None: (series_id, name, max_version) if found
        """
        res = self.execute(
            'SELECT s.id, s.name, MAX(sv.version) '
            'FROM series s '
            'JOIN ser_ver sv ON sv.series_id = s.id '
            "WHERE s.source = 'review' AND s.desc = ? "
            'GROUP BY s.id', (name,))
        return res.fetchone()
