# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2026 Simon Glass <sjg@chromium.org>
#
"""Copy and move series between patman databases

Series which you wrote and series which you reviewed are kept apart: a
reviewed series has its source set to 'review'. Copying never mixes the two,
so if a series of one kind has the same name as a series of the other kind
in the destination, it is left alone and reported.

When a series already exists in the destination (with the same kind), its
new versions are added. A version which exists in both is left alone, since
it cannot be merged safely. When moving, a series is only copied, and then
removed from the source, if all of its versions can be copied; otherwise it is
left alone in the source, so nothing is lost and no series ends up split
between the two databases.
"""

import sqlite3

from u_boot_pylib import tout

# Kinds of series to copy
ALL, OWN, REVIEWS = 'all', 'own', 'reviews'

# Result of copying a series
COPIED = 'copied'      # Every version was copied
PARTIAL = 'partial'    # Some versions were already in the destination
CONFLICT = 'conflict'  # A series of the other kind has the same name


class Stats:
    """Counts of what was copied

    Properties:
        series (int): Number of series added
        versions (int): Number of versions added
        patches (int): Number of patches added
        reviews (int): Number of reviews added
        replies (int): Number of replies to reviews added
        todos (int): Number of workflow entries (todos) added
        upstreams (int): Number of upstreams added
        dup_versions (list of str): Versions already in the destination, as
            'name vN'
        conflicts (list of str): Names of series kept apart from a series of
            the other kind
        moved (int): Number of series removed from the source
        left (list of str): Names of series which could not be moved, since
            they were not copied completely
    """
    def __init__(self):
        self.series = 0
        self.versions = 0
        self.patches = 0
        self.reviews = 0
        self.replies = 0
        self.todos = 0
        self.upstreams = 0
        self.dup_versions = []
        self.conflicts = []
        self.moved = 0
        self.left = []

    def report(self, move):
        """Show a summary of what was done

        Args:
            move (bool): True if series were moved, False if copied
        """
        verb = 'Moved' if move else 'Copied'
        count = self.moved if move else self.series
        tout.notice(
            f'{verb} {count} series: {self.versions} versions, '
            f'{self.patches} patches, {self.reviews} reviews, '
            f'{self.todos} todos')
        if self.replies:
            tout.notice(f'Including {self.replies} replies to reviews')
        if self.upstreams:
            tout.notice(f'Added {self.upstreams} upstreams')
        for item in self.dup_versions:
            tout.warning(f'Already present, not copied: {item}')
        for name in self.conflicts:
            tout.warning(f"Kept apart: '{name}' is one of your series in one "
                         'database and a review in the other')


def is_review(source):
    """Check whether a series is one being reviewed

    Args:
        source (str or None): Value of the series' source field

    Return:
        bool: True if it is a review, False if it is your own series
    """
    return source == 'review'


def backup(db):
    """Back up a database, including any changes not yet checkpointed

    The backup is written next to the database, with '.bak' added

    Args:
        db (Database): Open database

    Return:
        str: Filename of the backup
    """
    fname = f'{db.db_path}.bak'
    dest = sqlite3.connect(fname)
    try:
        db.con.backup(dest)
    finally:
        dest.close()
    tout.notice(f'Backed up {db.db_path} to {fname}')
    return fname


def _wanted(source, kind):
    """Check whether a series is of the kind being copied"""
    if kind == OWN:
        return not is_review(source)
    if kind == REVIEWS:
        return is_review(source)
    return True


def _copy_rows(src, dst, table, cols, where_col, old_id, new_id):
    """Copy rows which belong to a record, pointing them at its copy

    Args:
        src (Database): Database to copy from
        dst (Database): Database to copy to
        table (str): Table name
        cols (list of str): Columns to copy, not including where_col
        where_col (str): Column which refers to the parent record
        old_id (int): Parent record's ID in src
        new_id (int): Parent record's ID in dst

    Return:
        int: Number of rows copied
    """
    names = ', '.join(cols)
    rows = src.execute(f'SELECT {names} FROM {table} WHERE {where_col} = ?',
                       (old_id,)).fetchall()
    marks = ', '.join(['?'] * (len(cols) + 1))
    for row in rows:
        dst.execute(f'INSERT INTO {table} ({where_col}, {names}) '
                    f'VALUES ({marks})', (new_id, *row))
    return len(rows)


SER_VER_COLS = ['version', 'link', 'cover_id', 'cover_num_comments', 'name',
                'archive_tag', 'desc', 'notes']
PCOMMIT_COLS = ['seq', 'subject', 'patch_id', 'change_id', 'state',
                'num_comments']
REVIEW_COLS = ['seq', 'body', 'approved', 'timestamp', 'draft_id', 'status',
               'gmail_msg_id', 'gmail_thread_id', 'session_id']
REPLY_COLS = ['gmail_msg_id', 'msgid', 'refs', 'sender', 'to_addrs',
              'cc_addrs', 'subject', 'date', 'body', 'response', 'status',
              'draft_id', 'timestamp']


def _copy_reviews(src, dst, svid, new_svid, stats):
    """Copy the reviews of a version, along with the replies to them

    Args:
        src (Database): Database to copy from
        dst (Database): Database to copy to
        svid (int): Version ID in src
        new_svid (int): Version ID in dst
        stats (Stats): Counts to update
    """
    cols = ', '.join(REVIEW_COLS)
    marks = ', '.join(['?'] * (len(REVIEW_COLS) + 1))
    for rid, *fields in src.execute(
            f'SELECT id, {cols} FROM review WHERE svid = ?',
            (svid,)).fetchall():
        dst.execute(f'INSERT INTO review (svid, {cols}) VALUES ({marks})',
                    (new_svid, *fields))
        stats.reviews += 1
        stats.replies += _copy_rows(src, dst, 'reply', REPLY_COLS,
                                    'review_id', rid, dst.lastrowid())


def _copy_one(src, dst, sid, name, fields, stats, move):
    """Copy one series and everything which belongs to it

    Args:
        src (Database): Database to copy from
        dst (Database): Database to copy to
        sid (int): Series ID in src
        name (str): Series name
        fields (tuple): desc, archived, upstream, source
        stats (Stats): Counts to update
        move (bool): True if the series is being moved, so its todos must
            be kept

    Return:
        str: COPIED, PARTIAL or CONFLICT
    """
    source = fields[3]
    existing = dst.execute('SELECT id, source FROM series WHERE name = ?',
                           (name,)).fetchone()
    if existing:
        did, dsource = existing
        if is_review(dsource) != is_review(source):
            stats.conflicts.append(name)
            return CONFLICT

    # When moving, leave the series alone unless it can all be copied
    if move:
        vers = [row[0] for row in src.execute(
            'SELECT version FROM ser_ver WHERE series_id = ?', (sid,))]
        have = set()
        if existing:
            have = {row[0] for row in dst.execute(
                'SELECT version FROM ser_ver WHERE series_id = ?', (did,))}
        dups = sorted({ver for ver in vers
                       if ver in have or vers.count(ver) > 1})
        if dups:
            stats.dup_versions += [f'{name} v{ver}' for ver in dups]
            return PARTIAL

    if not existing:
        dst.execute('INSERT INTO series (name, desc, archived, upstream, '
                    'source) VALUES (?, ?, ?, ?, ?)', (name, *fields))
        did = dst.lastrowid()
        stats.series += 1

    result = COPIED
    svmap = {}
    cols = ', '.join(SER_VER_COLS)
    for svid, *svfields in src.execute(
            f'SELECT id, {cols} FROM ser_ver WHERE series_id = ?',
            (sid,)).fetchall():
        version = svfields[0]
        if dst.execute('SELECT id FROM ser_ver WHERE series_id = ? AND '
                       'version = ?', (did, version)).fetchone():
            stats.dup_versions.append(f'{name} v{version}')
            result = PARTIAL
            continue
        marks = ', '.join(['?'] * (len(SER_VER_COLS) + 1))
        dst.execute(f'INSERT INTO ser_ver (series_id, {cols}) '
                    f'VALUES ({marks})', (did, *svfields))
        new_svid = dst.lastrowid()
        svmap[svid] = new_svid
        stats.versions += 1
        stats.patches += _copy_rows(src, dst, 'pcommit', PCOMMIT_COLS, 'svid',
                                    svid, new_svid)
        _copy_reviews(src, dst, svid, new_svid, stats)

    # Todos are only copied with a new series, to avoid duplicating them,
    # unless they would otherwise be lost
    if not existing or move:
        for wtype, timestamp, archived, svid in src.execute(
                'SELECT type, timestamp, archived, ser_ver_id FROM workflow '
                'WHERE series_id = ?', (sid,)).fetchall():
            dst.execute('INSERT INTO workflow (type, series_id, timestamp, '
                        'archived, ser_ver_id) VALUES (?, ?, ?, ?, ?)',
                        (wtype, did, timestamp, archived, svmap.get(svid)))
            stats.todos += 1
    return result


def copy_upstreams(src, dst, stats):
    """Copy upstreams, and their patchwork projects, missing from dst

    The default upstream in dst is not changed.

    Args:
        src (Database): Database to copy from
        dst (Database): Database to copy to
        stats (Stats): Counts to update
    """
    have = {row[0] for row in dst.execute('SELECT name FROM upstream')}
    for row in src.execute(
            'SELECT name, url, patchwork_url, identity, series_to, '
            'no_maintainers, no_tags FROM upstream').fetchall():
        if row[0] in have:
            continue
        dst.execute('INSERT INTO upstream (name, url, is_default, '
                    'patchwork_url, identity, series_to, no_maintainers, '
                    'no_tags) VALUES (?, ?, 0, ?, ?, ?, ?, ?)', row)
        stats.upstreams += 1
    have = {row[0] for row in dst.execute('SELECT upstream FROM patchwork')}
    for row in src.execute(
            'SELECT name, proj_id, link_name, upstream FROM patchwork'
            ).fetchall():
        if row[3] not in have:
            dst.execute('INSERT INTO patchwork (name, proj_id, link_name, '
                        'upstream) VALUES (?, ?, ?, ?)', row)


def _remove_series(db, sid):
    """Remove a series and everything which belongs to it

    Args:
        db (Database): Database to update
        sid (int): Series ID
    """
    svids = '(SELECT id FROM ser_ver WHERE series_id = ?)'
    db.execute(f'DELETE FROM reply WHERE review_id IN (SELECT id FROM review '
               f'WHERE svid IN {svids})', (sid,))
    db.execute(f'DELETE FROM review WHERE svid IN {svids}', (sid,))
    db.execute(f'DELETE FROM pcommit WHERE svid IN {svids}', (sid,))
    db.execute('DELETE FROM workflow WHERE series_id = ?', (sid,))
    db.execute('DELETE FROM ser_ver WHERE series_id = ?', (sid,))
    db.execute('DELETE FROM series WHERE id = ?', (sid,))


def copy_series(src, dst, kind=ALL, move=False):
    """Copy (or move) series from one database to another

    Changes are not committed; the caller should commit or roll back both
    databases.

    Args:
        src (Database): Database to copy from
        dst (Database): Database to copy to
        kind (str): ALL, OWN or REVIEWS
        move (bool): True to remove each series from src once all of its
            versions are copied

    Return:
        Stats: Counts of what was done
    """
    stats = Stats()
    for sid, name, *fields in src.execute(
            'SELECT id, name, desc, archived, upstream, source FROM series'
            ).fetchall():
        if not _wanted(fields[3], kind):
            continue
        result = _copy_one(src, dst, sid, name, fields, stats, move)
        if move:
            if result == COPIED:
                _remove_series(src, sid)
                stats.moved += 1
            else:
                stats.left.append(name)
    return stats
