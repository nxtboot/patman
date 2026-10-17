# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2025 Google LLC
#
"""Handles the 'series' subcommand
"""

import asyncio
from collections import OrderedDict, defaultdict
import os
import re

import aiohttp
import pygit2

from u_boot_pylib import command
from u_boot_pylib import cros_subprocess
from u_boot_pylib import gitutil
from u_boot_pylib import terminal
from u_boot_pylib.terminal import tprint
from u_boot_pylib import tools
from u_boot_pylib import tout

from patman import patchstream
from patman.patchwork import Patchwork
from patman import cser_helper
from patman.cser_helper import AUTOLINK, oid
from patman import send
from patman import status
from patman import workflow


_TRAILER_RE = re.compile(r'^[A-Za-z][A-Za-z0-9-]*:\s')


def _add_change_tag(message, version, text, cover=False):
    """Insert or extend a Series-changes / Cover-changes block

    The block is placed before the trailer (Signed-off-by) block so that
    'git commit --amend -s' still recognises the existing signoff and
    does not duplicate it. The required blank line between the block
    and the trailers is preserved.

    Args:
        message (str): Existing commit message
        version (int): Series version number
        text (str): Bullet text (with or without leading '- ')
        cover (bool): True for Cover-changes, False for Series-changes

    Return:
        str: New commit message ending with a single newline
    """
    tag = 'Cover-changes' if cover else 'Series-changes'
    bullet = text.lstrip()
    if not bullet.startswith('-'):
        bullet = f'- {bullet}'
    bullet = bullet.rstrip()

    lines = message.rstrip('\n').split('\n')

    # Identify the trailer block at the end (consecutive 'Word: value' lines,
    # ignoring blank separators between them)
    trailer_start = len(lines)
    for i in range(len(lines) - 1, -1, -1):
        if not lines[i].strip():
            continue
        if _TRAILER_RE.match(lines[i]):
            trailer_start = i
        else:
            break

    # Reuse an existing block of the same kind/version if present
    block_re = re.compile(rf'^{re.escape(tag)}:\s*{version}\s*$')
    for i in range(trailer_start):
        if block_re.match(lines[i]):
            j = i + 1
            while j < len(lines) and lines[j].lstrip().startswith('-'):
                j += 1
            return '\n'.join(lines[:j] + [bullet] + lines[j:]) + '\n'

    # No existing block: insert a new one just before the trailer block
    body_end = trailer_start
    while body_end > 0 and not lines[body_end - 1].strip():
        body_end -= 1
    body = lines[:body_end]
    trailers = lines[trailer_start:]
    new_block = [f'{tag}: {version}', bullet]
    out = body + [''] + new_block
    if trailers:
        out += [''] + trailers
    return '\n'.join(out) + '\n'


def _describe_error(exc):
    """Describe an error from reading patchwork

    Args:
        exc (Exception): Error raised

    Return:
        str: Description, e.g. 'ValueError: Could not read URL'
    """
    if isinstance(exc, asyncio.TimeoutError):
        return 'Timed out'
    return f'{type(exc).__name__}: {exc}'


class Cseries(cser_helper.CseriesHelper):
    """Database with information about series

    This class handles database read/write as well as operations in a git
    directory to update series information.
    """
    def __init__(self, topdir=None, colour=terminal.COLOR_IF_TERMINAL,
                 db_dir=None):
        """Set up a new Cseries

        Args:
            topdir (str): Top-level directory of the repo
            colour (terminal.enum): Whether to enable ANSI colour or not
            db_dir (str or None): Directory holding the database, or None to
                use PATMAN_DB_DIR or else topdir
        """
        super().__init__(topdir, colour, db_dir)

    def add(self, branch_name, desc=None, mark=False, allow_unmarked=False,
            end=None, use_commit=False, force_version=False, ups=None,
            dry_run=False):
        """Add a series (or new version of a series) to the database

        Args:
            branch_name (str): Name of branch to sync, or None for current one
            desc (str): Description to use, or None to use the series subject
            mark (str): True to mark each commit with a change ID
            allow_unmarked (str): True to not require each commit to be marked
            end (str): Add only commits up to but excluding this commit
            use_commit (bool): True to use the first commit's subject as the
                series description, if none is available in the series or
                provided in 'desc'
            force_version (bool): True if ignore a Series-version tag that
                doesn't match its branch name
            ups (str or None): Name of the upstream for this series, or None
                to use the upstream which the branch tracks, if it is one
            dry_run (bool): True to do a dry run
        """
        name, ser, version, msg = self.prep_series(branch_name, end)
        tout.info(f"Adding series '{ser.name}' v{version}: mark {mark} "
                  f'allow_unmarked {allow_unmarked}')
        if msg:
            tout.info(msg)
        if desc is None:
            if not ser.cover:
                if use_commit and ser.commits:
                    desc = ser.commits[0].subject
                    tout.info(f"Using description from first commit: '{desc}'")
                else:
                    raise ValueError(f"Branch '{name}' has no cover letter - "
                                    'please provide description')
            if not desc:
                desc = ser.cover[0]  # pylint: disable=E1136

        ser = self._handle_mark(name, ser, version, mark, allow_unmarked,
                                force_version, dry_run)
        link = ser.get_link_for_version(version)

        msg = 'Added'
        added = False
        series_id = self.db.series_find_by_name(ser.name)
        if not series_id:
            if not ups:
                ups = self._branch_upstream(name)
                if ups:
                    tout.notice(f"Using upstream '{ups}', which branch "
                                f"'{name}' tracks")
            series_id = self.db.series_add(ser.name, desc, ups=ups)
            added = True
            msg += f" series '{ser.name}'"
        else:
            if ups:
                self.db.series_set_upstream(series_id, ups)
            self.db.series_set_desc(series_id, desc)

        if version not in self._get_version_list(series_id):
            svid = self.db.ser_ver_add(series_id, version, link, desc)
            msg += f" v{version}"
            if not added:
                msg += f" to existing series '{ser.name}'"
            added = True

            self._add_series_commits(ser, svid)
            count = len(ser.commits)
            msg += f" ({count} commit{'s' if count > 1 else ''})"
        if not added:
            tout.notice(f"Series '{ser.name}' v{version} already exists")
            msg = None
        elif not dry_run:
            self.commit()
        else:
            self.rollback()
            series_id = None
        ser.desc = desc
        ser.idnum = series_id

        if msg:
            tout.notice(msg)
        if dry_run:
            tout.info('Dry run completed')

    def decrement(self, series, dry_run=False):
        """Decrement a series to the previous version and delete the branch

        Args:
            series (str): Name of series to use, or None to use current branch
            dry_run (bool): True to do a dry run
        """
        ser = self._parse_series(series)
        self._ensure_in_db(ser)

        max_vers = self._series_max_version(ser.idnum)
        if max_vers < 2:
            raise ValueError(f"Series '{ser.name}' only has one version")

        tout.info(f"Removing series '{ser.name}' v{max_vers}")

        new_max = max_vers - 1

        repo = pygit2.Repository(self.gitdir)
        if not dry_run:
            name = self._get_branch_name(ser.name, new_max)
            branch = repo.lookup_branch(name)
            try:
                repo.checkout(branch)
            except pygit2.errors.GitError:
                tout.warning(f"Failed to checkout branch {name}")
                raise

            del_name = f'{ser.name}{max_vers}'
            del_branch = repo.lookup_branch(del_name)
            branch_oid = del_branch.peel(pygit2.enums.ObjectType.COMMIT).id
            del_branch.delete()
            tout.info(f"Deleted branch '{del_name}' {oid(branch_oid)}")

        self.db.ser_ver_remove(ser.idnum, max_vers)
        if not dry_run:
            self.commit()
        else:
            self.rollback()

        tout.notice(f"Decremented series '{ser.name}' to v{new_max}")

    def increment(self, series_name, dry_run=False):
        """Increment a series to the next version and create a new branch

        Args:
            series_name (str): Name of series to use, or None to use current
                branch
            dry_run (bool): True to do a dry run
        """
        ser = self._parse_series(series_name)
        self._ensure_in_db(ser)

        max_vers = self._series_max_version(ser.idnum)

        branch_name = self._get_branch_name(ser.name, max_vers)
        on_branch = gitutil.get_branch(self.gitdir) == branch_name
        svid = self.get_series_svid(ser.idnum, max_vers)
        pwc = self.get_pcommit_dict(svid)
        count = len(pwc.values())
        series = patchstream.get_metadata(branch_name, 0, count,
                                          git_dir=self.gitdir)
        tout.info(f"Increment '{ser.name}' v{max_vers}: {count} patches")

        # Create a new branch
        vers = max_vers + 1
        new_name = self._join_name_version(ser.name, vers)

        self.update_series(branch_name, series, max_vers, new_name, dry_run,
                            add_vers=vers, switch=on_branch)

        old_svid = self.get_series_svid(ser.idnum, max_vers)
        pcd = self.get_pcommit_dict(old_svid)

        # Set the per-version description from the cover letter or
        # first commit subject, so autolink can find the series on
        # patchwork even if the patch order changes
        meta_name = new_name if not dry_run else branch_name
        new_series = patchstream.get_metadata(meta_name, 0, count,
                                              git_dir=self.gitdir)
        if new_series.get('cover'):
            sv_desc = new_series.cover[0]  # pylint: disable=E1136
        elif new_series.commits:
            sv_desc = new_series.commits[0].subject
        else:
            sv_desc = None
        svid = self.db.ser_ver_add(ser.idnum, vers, desc=sv_desc)
        self.db.pcommit_add_list(svid, pcd.values())
        if not dry_run:
            self.commit()
        else:
            self.rollback()

        # repo.head.set_target(amended)
        tout.notice(f"Incremented series '{ser.name}' to v{vers}")
        if dry_run:
            tout.info('Dry run completed')

    def link_set(self, series_name, version, link, update_commit):
        """Add / update a series-links link for a series

        Args:
            series_name (str): Name of series to use, or None to use current
                branch
            version (int): Version number, or None to detect from name
            link (str): Patchwork link-string for the series
            update_commit (bool): True to update the current commit with the
                link
        """
        ser, version = self._parse_series_and_version(series_name, version)
        self._ensure_in_db(ser)
        self._ensure_version(ser, version)

        self._set_link(ser.idnum, ser.name, version, link, update_commit)
        self.commit()
        tout.notice(
            f"Setting link for series '{ser.name}' v{version} to {link}")

    def link_get(self, series, version):
        """Get the patchwork link for a version of a series

        Args:
            series (str): Name of series to use, or None to use current branch
            version (int): Version number or None for current

        Return:
            str: Patchwork link as a string, e.g. '12325'
        """
        ser, version = self._parse_series_and_version(series, version)
        self._ensure_in_db(ser)
        self._ensure_version(ser, version)
        return self.db.ser_ver_get_link(ser.idnum, version)

    def link_search(self, pwork, series, version):
        """Search patch for the link for a series

        Returns either the single match, or None, in which case the second part
        of the tuple is filled in

        Args:
            pwork (Patchwork): Patchwork object to use
            series (str): Series name to search for, or None for current series
                that is checked out
            version (int): Version to search for, or None for current version
                detected from branch name

        Returns:
            tuple:
                int: ID of the series found, or None
                list of possible matches, or None, each a dict:
                    'id': series ID
                    'name': series name
                str: series name
                int: series version
                str: series description
        """
        _, ser, version, _, _, _, _, _ = self._get_patches(series, version)
        svinfo = self.get_ser_ver(ser.idnum, version)

        # Prefer the live cover-letter title from the branch: it is what
        # patchwork indexes the series by, and it reflects a rename made
        # after the version was created (the stored description would be
        # stale in that case). Fall back to the stored per-version
        # description for series with no cover letter, where it is more
        # stable than the first commit subject (e.g. if patches are
        # reordered).
        desc = ser.cover[0] if ser.cover else None
        desc = desc or svinfo.desc or ser.desc
        if not desc:
            raise ValueError(f"Series '{ser.name}' has an empty description")
        ser.desc = desc

        pws, options = self.loop.run_until_complete(pwork.find_series(
            ser, version))
        return pws, options, ser.name, version, desc

    def link_auto(self, pwork, series, version, update_commit, wait_s=0):
        """Automatically find a series link by looking in patchwork

        Args:
            pwork (Patchwork): Patchwork object to use
            series (str): Series name to search for, or None for current series
                that is checked out
            version (int): Version to search for, or None for current version
                detected from branch name
            update_commit (bool): True to update the current commit with the
                link
            wait_s (int): Number of seconds to wait for the autolink to succeed
        """
        start = self.get_time()
        stop = start + wait_s
        sleep_time = 5
        last_options = None
        first = True
        while True:
            pws, options, name, version, desc = self.link_search(
                pwork, series, version)
            if first:
                tout.debug(f"Autolinking series '{name}' v{version}"
                           f" (timeout {wait_s}s)")
                first = False
            tout.debug(f"Searching {pwork.url} project {pwork.proj_id}"
                       f" for '{desc}'")
            if pws:
                tout.clear_progress()
                tout.debug(f'Found link: {pws}')
                if wait_s:
                    tout.notice('Link completed after '
                                f'{self.get_time() - start} seconds')
                break

            if not wait_s or self.get_time() > stop:
                tout.clear_progress()
                if options != last_options:
                    self._show_autolink_matches(name, version, desc,
                                                options)
                delay = f' after {wait_s} seconds' if wait_s else ''
                raise ValueError(
                    f"Cannot find series '{desc}'{delay}; "
                    'to try again later:\n'
                    f'  patman series -s {name} -V {version} autolink')

            if options != last_options:
                tout.clear_progress()
                self._show_autolink_matches(name, version, desc, options)
                last_options = options

            elapsed = int(self.get_time() - start)
            tout.progress(
                f'Waiting for series on patchwork ({elapsed}s)')
            self.sleep(sleep_time)
            sleep_time = min(sleep_time + 5, 30)

        self.link_set(name, version, pws, update_commit)

    def _show_autolink_matches(self, name, version, desc, options):
        """Show possible autolink matches

        Args:
            name (str): Series name
            version (int): Series version
            desc (str): Series description
            options (list of dict): Possible matches from patchwork
        """
        print(f"Possible matches for '{name}' v{version} desc '{desc}':")
        if options:
            print('  Link  Version  Description')
            for opt in options:
                print(f"{opt['id']:6}  {opt['version']:7}  {opt['name']}")
        else:
            print('  (none)')

    def link_auto_all(self, pwork, update_commit, link_all_versions,
                      replace_existing, dry_run, show_summary=True):
        """Automatically find a series link by looking in patchwork

        Args:
            pwork (Patchwork): Patchwork object to use
            update_commit (bool): True to update the current commit with the
                link
            link_all_versions (bool): True to sync all versions of a series,
                False to sync only the latest version
            replace_existing (bool): True to sync a series even if it already
                has a link
            dry_run (bool): True to do a dry run
            show_summary (bool): True to show a summary of how things went

        Return:
            OrderedDict of summary info:
                key (int): ser_ver ID
                value (AUTOLINK): result of autolinking on this ser_ver
        """
        sdict = self.db.series_get_dict_by_id()
        all_ser_vers = self._get_autolink_dict(sdict, link_all_versions)

        # Get rid of things without a description
        valid = {}
        state = {}
        no_desc = 0
        not_found = 0
        updated = 0
        failed = 0
        already = 0
        for svid, (ser_id, name, version, link, desc) in all_ser_vers.items():
            if link and not replace_existing:
                state[svid] = f'already:{link}'
                already += 1
            elif desc:
                valid[svid] = ser_id, version, link, desc
            else:
                no_desc += 1
                state[svid] = 'missing description'

        results, requests = self.loop.run_until_complete(
            pwork.find_series_list(valid))

        for svid, ser_id, link, _ in results:
            if link:
                version = all_ser_vers[svid][2]
                if self._set_link(ser_id, sdict[ser_id].name, version,
                                  link, update_commit, dry_run=dry_run):
                    updated += 1
                    state[svid] = f'linked:{link}'
                else:
                    failed += 1
                    state[svid] = 'failed'
            else:
                not_found += 1
                state[svid] = 'not found'

        # Create a summary sorted by name and version
        summary = OrderedDict()
        for svid in sorted(all_ser_vers, key=lambda k: all_ser_vers[k][1:2]):
            _, name, version, link, ser = all_ser_vers[svid]
            summary[svid] = AUTOLINK(name, version, link, ser.desc,
                                     state[svid])

        if show_summary:
            msg = f'{updated} series linked'
            if already:
                msg += f', {already} already linked'
            if not_found:
                msg += f', {not_found} not found'
            if no_desc:
                msg += f', {no_desc} missing description'
            if failed:
                msg += f', {failed} updated failed'
            tout.notice(msg + f' ({requests} requests)')

            tout.info('')
            tout.info(f"{'Name':15}  Version  {'Description':40}  Result")
            border = f"{'-' * 15}  -------  {'-' * 40}  {'-' * 15}"
            tout.info(border)
            for name, version, link, desc, state in summary.values():
                bright = True
                if state.startswith('already'):
                    col = self.col.GREEN
                    bright = False
                elif state.startswith('linked'):
                    col = self.col.MAGENTA
                else:
                    col = self.col.RED
                col_state = self.col.build(col, state, bright)
                tout.info(f"{name:16.16} {version:7}  {desc or '':40.40}  "
                          f'{col_state}')
            tout.info(border)
        if dry_run:
            tout.info('Dry run completed')

        return summary

    def series_list(self, include_archived=False, reviews_only=False):
        """List all series

        Lines all series along with their description, number of patches
        accepted and the available versions

        Args:
            include_archived (bool): True to include archived series also
            reviews_only (bool): True to show only review series
        """
        sdict = self.db.series_get_dict(include_archived,
                                        include_reviews=reviews_only,
                                        reviews_only=reviews_only)
        print(f"{'Name':15}  {'Description':40}  Accepted  Us  Versions")
        border = f"{'-' * 15}  {'-' * 40}  --------  --  {'-' * 15}"
        print(border)
        for name in sorted(sdict):
            ser = sdict[name]
            versions = self._get_version_list(ser.idnum)
            stat = self._series_get_version_stats(
                ser.idnum, self._series_max_version(ser.idnum))[0]
            ups = (ser.upstream or '')[:2]

            vlist = ' '.join([str(ver) for ver in sorted(versions)])

            print(f'{name:16.16} {ser.desc:41.41} {stat.rjust(8)}  '
                  f'{ups:2}  {vlist}')
        print(border)

    def series_find(self, query, include_archived=False):
        """Search for series by subject fragment

        Args:
            query (str): Text to search for in series/version/patch
                subjects
            include_archived (bool): True to include archived series
        """
        col = self.col
        rows = self.db.series_search(query, include_archived)
        if not rows:
            tout.notice(f"No series match '{query}'")
            return

        # Deduplicate: for each (series_id, version), keep the best match
        # priority: series > version > patch
        priority = {'series': 0, 'version': 1, 'patch': 2}
        best = {}
        for sid, name, desc, version, link, mtype, mtext in rows:
            key = (sid, version)
            prev = best.get(key)
            if prev is None or priority[mtype] < priority[prev[5]]:
                best[key] = (sid, name, desc, version, link, mtype, mtext)

        with terminal.pager():
            terminal.tprint(f"{len(best)} match(es) for '{query}':",
                            colour=col.WHITE, col=col)
            last_sid = None
            for key in sorted(best,
                              key=lambda k: (best[k][1], best[k][3])):
                sid, name, desc, version, link, mtype, mtext = best[key]
                if sid != last_sid:
                    terminal.tprint('')
                    terminal.tprint(f'{name}', colour=col.YELLOW,
                                    col=col)
                    terminal.tprint(f'  {desc or "(no description)"}',
                                    col=col)
                    last_sid = sid
                link_str = link or '(no link)'
                terminal.tprint(f'  v{version} [{link_str}]',
                                colour=col.BLUE, col=col, newline=False)
                terminal.tprint(f' {mtype}: {mtext}', col=col)

    def list_patches(self, series, version, show_commit=False,
                     show_patch=False):
        """List patches in a series

        Args:
            series (str): Name of series to use, or None to use current branch
            version (int): Version number, or None to detect from name
            show_commit (bool): True to show the commit and diffstate
            show_patch (bool): True to show the patch
        """
        branch, series, version, pwc, name, _, cover_id, num_comments = (
            self._get_patches(series, version))
        with terminal.pager():
            state_totals = defaultdict(int)
            self._list_patches(branch, pwc, series, name, cover_id,
                               num_comments, show_commit, show_patch, True,
                               state_totals)

    def mark(self, in_name, allow_marked=False, dry_run=False):
        """Add Change-Id tags to a series

        Args:
            in_name (str): Name of the series to unmark
            allow_marked (bool): Allow commits to be (already) marked
            dry_run (bool): True to do a dry run, restoring the original tree
                afterwards

        Return:
            pygit.oid: oid of the new branch
        """
        name, ser, _, _ = self.prep_series(in_name)
        tout.info(f"Marking series '{name}': allow_marked {allow_marked}")

        if not allow_marked:
            bad = []
            for cmt in ser.commits:
                if cmt.change_id:
                    bad.append(cmt)
            if bad:
                print(f'{len(bad)} commit(s) already have marks')
                for cmt in bad:
                    print(f' - {oid(cmt.hash)} {cmt.subject}')
                raise ValueError(
                    f'Marked commits {len(bad)}/{len(ser.commits)}')
        new_oid = self._mark_series(in_name, ser, dry_run=dry_run)

        count = len(ser.commits)
        tout.notice(f"Marked {count} commit{self.plural(count)}"
                    f" in series '{name}'")
        if dry_run:
            tout.info('Dry run completed')
        return new_oid

    def unmark(self, name, allow_unmarked=False, dry_run=False):
        """Remove Change-Id tags from a series

        Args:
            name (str): Name of the series to unmark
            allow_unmarked (bool): Allow commits to be (already) unmarked
            dry_run (bool): True to do a dry run, restoring the original tree
                afterwards

        Return:
            pygit.oid: oid of the new branch
        """
        name, ser, _, _ = self.prep_series(name)
        tout.info(
            f"Unmarking series '{name}': allow_unmarked {allow_unmarked}")

        if not allow_unmarked:
            bad = []
            for cmt in ser.commits:
                if not cmt.change_id:
                    bad.append(cmt)
            if bad:
                print(f'{len(bad)} commit(s) are missing marks')
                for cmt in bad:
                    print(f' - {oid(cmt.hash)} {cmt.subject}')
                raise ValueError(
                    f'Unmarked commits {len(bad)}/{len(ser.commits)}')
        vals = None
        for vals in self.process_series(name, ser, dry_run=dry_run):
            if cser_helper.CHANGE_ID_TAG in vals.msg:
                lines = vals.msg.splitlines()
                updated = [line for line in lines
                           if not line.startswith(cser_helper.CHANGE_ID_TAG)]
                vals.msg = '\n'.join(updated)

                tout.detail("   - removing mark")
                vals.info = 'unmarked'
            else:
                vals.info = 'no mark'

        count = len(ser.commits)
        tout.notice(f"Unmarked {count} commit{self.plural(count)}"
                    f" in series '{name}'")
        if dry_run:
            tout.info('Dry run completed')
        return vals.oid

    def open(self, pwork, name, version):
        """Open the patchwork page for a series

        Args:
            pwork (Patchwork): Patchwork object to use
            name (str): Name of series to open
            version (str): Version number to open
        """
        ser, version = self._parse_series_and_version(name, version)
        link = self.link_get(ser.name, version)
        url = self.loop.run_until_complete(pwork.get_series_url(link))
        print(f'Opening {url}')

        # With Firefox, GTK produces lots of warnings, so suppress them
        # Gtk-Message: 06:48:20.692: Failed to load module "xapp-gtk3-module"
        # Gtk-Message: 06:48:20.692: Not loading module "atk-bridge": The
        # functionality is provided by GTK natively. Please try to not load it.
        # Gtk-Message: 06:48:20.692: Failed to load module "appmenu-gtk-module"
        # Gtk-Message: 06:48:20.692: Failed to load module "appmenu-gtk-module"
        # [262145, Main Thread] WARNING: GTK+ module /snap/firefox/5987/
        #  gnome-platform/usr/lib/gtk-2.0/modules/libcanberra-gtk-module.so
        #  cannot be loaded.
        # GTK+ 2.x symbols detected. Using GTK+ 2.x and GTK+ 3 in the same
        #  process #  is not supported.: 'glib warning', file /build/firefox/
        #  parts/firefox/build/toolkit/xre/nsSigHandlers.cpp:201
        #
        # (firefox_firefox:262145): Gtk-WARNING **: 06:48:20.728: GTK+ module
        #  /snap/firefox/5987/gnome-platform/usr/lib/gtk-2.0/modules/
        #  libcanberra-gtk-module.so cannot be loaded.
        # GTK+ 2.x symbols detected. Using GTK+ 2.x and GTK+ 3 in the same
        #  process is not supported.
        # Gtk-Message: 06:48:20.728: Failed to load module
        #  "canberra-gtk-module"
        # [262145, Main Thread] WARNING: GTK+ module /snap/firefox/5987/
        #  gnome-platform/usr/lib/gtk-2.0/modules/libcanberra-gtk-module.so
        #  cannot be loaded.
        # GTK+ 2.x symbols detected. Using GTK+ 2.x and GTK+ 3 in the same
        #  process is not supported.: 'glib warning', file /build/firefox/
        #  parts/firefox/build/toolkit/xre/nsSigHandlers.cpp:201
        #
        # (firefox_firefox:262145): Gtk-WARNING **: 06:48:20.729: GTK+ module
        #   /snap/firefox/5987/gnome-platform/usr/lib/gtk-2.0/modules/
        #   libcanberra-gtk-module.so cannot be loaded.
        # GTK+ 2.x symbols detected. Using GTK+ 2.x and GTK+ 3 in the same
        #  process is not supported.
        # Gtk-Message: 06:48:20.729: Failed to load module
        #  "canberra-gtk-module"
        # ATTENTION: default value of option mesa_glthread overridden by
        # environment.
        cros_subprocess.Popen(['xdg-open', url])

    def progress(self, series, show_all_versions, list_patches,
                 include_archived):
        """Show progress information for all versions in a series

        Args:
            series (str): Name of series to use, or None to show progress for
                all series
            show_all_versions (bool): True to show all versions of a series,
                False to show only the final version
            list_patches (bool): True to list all patches for each series,
                False to just show the series summary on a single line
            include_archived (bool): True to include archived series also
        """
        with terminal.pager():
            state_totals = defaultdict(int)
            if series is not None:
                _, _, need_scan = self._progress_one(
                    self._parse_series(series), show_all_versions,
                    list_patches, state_totals)
                if need_scan:
                    tout.warning(
                        'Inconsistent commit-subject: Please use '
                        "'patman series -s <branch> scan' to resolve this")
                return

            total_patches = 0
            total_series = 0
            sdict = self.db.series_get_dict(include_archived)
            border = None
            total_need_scan = 0
            if not list_patches:
                print(self.col.build(
                    self.col.MAGENTA,
                    f"{'Name':16} {'Description':41} Count  {'Status'}"))
                border = f"{'-' * 15}  {'-' * 40}  -----  {'-' * 15}"
                print(border)
            for name in sorted(sdict):
                ser = sdict[name]
                num_series, num_patches, need_scan = self._progress_one(
                    ser, show_all_versions, list_patches, state_totals,
                    not include_archived)
                total_need_scan += need_scan
                if list_patches:
                    print()
                total_series += num_series
                total_patches += num_patches
            if not list_patches:
                print(border)
                total = f'{total_series} series'
                out = ''
                for state, freq in state_totals.items():
                    out += ' ' + self._build_col(state, f'{freq}:')[0]
                if total_need_scan:
                    out = '*' + out[1:]

                print(f"{total:15}  {'':40}  {total_patches:5} {out}")
                if total_need_scan:
                    tout.info(
                        f'Series marked * ({total_need_scan}) have commit '
                        'subjects which mismatch their patches and need to be '
                        'scanned')

    def project_set(self, pwork, name, ups=None, quiet=False):
        """Set the name of the project for an upstream

        Args:
            pwork (Patchwork): Patchwork object to use
            name (str): Name of the project to use in patchwork
            ups (str or None): Upstream name to associate with
            quiet (bool): True to skip writing the message
        """
        tout.detail(f"Patchwork URL '{pwork.url}': finding name '{name}'")
        res = self.loop.run_until_complete(pwork.get_projects())
        proj_id = None
        link_name = None
        for proj in res:
            pid, pname = proj['id'], proj['name']
            ok = pname.strip() == name
            tout.detail(f"{pid:3} '{pname}'")
            if ok:
                proj_id = pid
                link_name = proj['link_name']
                tout.detail(f'Name match: ID {proj_id}')
        if not proj_id:
            raise ValueError(f"Unknown project name '{name}'")
        self.db.patchwork_update(name, proj_id, link_name, ups)
        self.commit()
        if not quiet:
            msg = f"Project '{name}' patchwork-ID {proj_id} "
            msg += f"link-name '{link_name}'"
            if ups:
                msg += f" remote '{ups}'"
            tout.notice(msg)

    def project_get(self, ups=None):
        """Get the details of the project for an upstream

        Args:
            ups (str or None): Upstream name to look up, or None for any

        Returns:
            tuple or None if there are no settings:
                name (str): Project name, e.g. 'U-Boot'
                proj_id (int): Patchworks project ID for this project
                link_name (str): Patchwork's link-name for the project
        """
        return self.db.patchwork_get(ups)

    def project_list(self):
        """List all patchwork project configurations"""
        settings = self.db.patchwork_get_list()
        if not settings:
            print('No patchwork projects configured')
            return
        print(f"{'Project':20}  {'ID':>4}  {'Link name':15}  Remotes")
        border = f"{'-' * 20}  {'-' * 4}  {'-' * 15}  {'-' * 15}"
        print(border)

        # Group remotes by project
        projects = OrderedDict()
        for name, proj_id, link_name, ups in settings:
            key = (name, proj_id, link_name)
            projects.setdefault(key, [])
            if ups:
                projects[key].append(ups)
        for (name, proj_id, link_name), remotes in projects.items():
            rlist = ' '.join(sorted(remotes))
            print(f'{name:20}  {proj_id:4}  {link_name:15}  {rlist}')
        print(border)

    def remove(self, name, dry_run=False):
        """Remove a series from the database

        Args:
            name (str): Name of series to remove, or None to use current one
            dry_run (bool): True to do a dry run
        """
        ser = self._parse_series(name)
        name = ser.name
        self._ensure_in_db(ser)

        self.db.ser_ver_remove(ser.idnum, None)
        if not dry_run:
            self.commit()
            # The review worktree (if any) has no value once the db row
            # is gone; v1 review series share their branch name with the
            # series name, so review_worktree_path() resolves it directly
            gitutil.remove_worktree(self.topdir,
                cser_helper.review_worktree_path(self.topdir, name))
        else:
            self.rollback()

        self.commit()
        tout.notice(f"Removed series '{name}'")
        if dry_run:
            tout.info('Dry run completed')

    def rename(self, series, name, dry_run=False):
        """Rename a series

        Renames a series and changes the name of any branches which match
        versions present in the database

        Args:
            series (str): Name of series to use, or None to use current branch
            name (str): new name to use (must not include version number)
            dry_run (bool): True to do a dry run
        """
        old_ser, _ = self._parse_series_and_version(series, None)
        self._ensure_in_db(old_ser)
        if old_ser.name != series:
            raise ValueError(f"Invalid series name '{series}': "
                             'did you use the branch name?')
        chk, _ = patchstream.split_name_version(name)
        if chk != name:
            raise ValueError(
                f"Invalid series name '{name}': did you use the branch name?")
        if chk == old_ser.name:
            raise ValueError(
                f"Cannot rename series '{old_ser.name}' to itself")
        if self.get_series_by_name(name):
            raise ValueError(f"Cannot rename: series '{name}' already exists")

        versions = self._get_version_list(old_ser.idnum)
        missing = []
        exists = []
        todo = {}
        for ver in versions:
            ok = True
            old_branch = self._get_branch_name(old_ser.name, ver)
            if not gitutil.check_branch(old_branch, self.gitdir):
                missing.append(old_branch)
                ok = False

            branch = self._get_branch_name(name, ver)
            if gitutil.check_branch(branch, self.gitdir):
                exists.append(branch)
                ok = False

            if ok:
                todo[ver] = [old_branch, branch]

        if missing or exists:
            msg = 'Cannot rename'
            if missing:
                msg += f": branches missing: {', '.join(missing)}"
            if exists:
                msg += f": branches exist: {', '.join(exists)}"
            raise ValueError(msg)

        for old_branch, branch in todo.values():
            tout.info(f"Renaming branch '{old_branch}' to '{branch}'")
            if not dry_run:
                gitutil.rename_branch(old_branch, branch, self.gitdir)

        # Change the series name; nothing needs to change in ser_ver
        self.db.series_set_name(old_ser.idnum, name)

        if not dry_run:
            self.commit()
        else:
            self.rollback()

        tout.notice(f"Renamed series '{series}' to '{name}'")
        if dry_run:
            tout.info('Dry run completed')

    def add_change(self, series, version, text, cover=False, dry_run=False):
        """Add a Series-changes / Cover-changes bullet to the HEAD commit

        Reads HEAD's commit message, inserts the bullet into an existing
        block for this version (or creates one), then runs
        'git commit --amend' to fold both the message change and any
        staged code changes in a single step. Typical use:

            git add <files>
            patman s changes 'fix the offset calculation'

        Args:
            series (str): Series name, or None for current branch
            version (int or None): Version for the tag (defaults to the
                current series version)
            text (str): Bullet text
            cover (bool): True for Cover-changes (cover-letter only),
                False for Series-changes
            dry_run (bool): True to print the resulting message instead
                of amending
        """
        _, ver = self._parse_series_and_version(series, version)
        old_msg = command.output(
            'git', '-C', self.topdir, 'log', '-1', '--format=%B').rstrip()
        new_msg = _add_change_tag(old_msg, ver, text, cover=cover)

        if dry_run:
            print(new_msg)
            return

        command.output(
            'git', '-C', self.topdir, 'commit', '--amend', '-F', '-',
            stdin_data=new_msg)
        tag = 'Cover-changes' if cover else 'Series-changes'
        tout.notice(f"Added '{tag}: {ver}' bullet to HEAD")

    def save_notes(self, series, notes_file='review-notes.txt'):
        """Save review-handling notes for the current series version

        Args:
            series (str): Series name, or None for current branch
            notes_file (str): Path to the notes file
        """
        if not os.path.exists(notes_file):
            raise FileNotFoundError(f"Notes file not found: {notes_file}")

        notes = tools.read_file(notes_file, binary=False).strip()
        ser, version = self._parse_series_and_version(series, None)
        self._ensure_in_db(ser)
        svid = self.get_series_svid(ser.idnum, version)
        self.db.ser_ver_set_notes(svid, notes)
        self.commit()
        tout.notice(f"Saved notes for '{ser.name}' v{version}")

    def show_notes(self, series):
        """Show review-handling notes from all versions of a series

        Args:
            series (str): Series name, or None for current branch
        """
        ser, _ = self._parse_series_and_version(series, None)
        self._ensure_in_db(ser)
        all_notes = self.db.ser_ver_get_all_notes(ser.idnum)
        if not all_notes:
            tout.notice(f"No review notes for '{ser.name}'")
            return
        for version, notes in all_notes:
            tprint(f'\n--- v{version} ---',
                            colour=terminal.Color.YELLOW)
            print(notes)
            print()

    def show_info(self, series, show_reviews=None):
        """Show detailed information about a series and all its versions

        Args:
            series (str): Series name, or None for current branch
            show_reviews (list of int or None): If not None, show review
                text. An empty list means all patches; otherwise only
                the listed patch numbers (1-based).
        """
        ser, _ = self._parse_series_and_version(series, None)
        self._ensure_in_db(ser)

        with terminal.pager():
            self._show_info(ser, show_reviews)

    def _show_info(self, ser, show_reviews):
        """Show series info (called within a pager context)

        Args:
            ser (Series): Series object with idnum set
            show_reviews (list of int or None): Patch numbers to show
                reviews for, or empty list for all, or None for none
        """
        col = self.col
        tprint('Series: ', newline=False, colour=col.BLUE, col=col)
        tprint(ser.name, colour=col.WHITE, col=col)
        tprint('  Description: ', newline=False, colour=col.BLUE, col=col)
        tprint(ser.desc, col=col)
        tprint('  Upstream: ', newline=False, colour=col.BLUE, col=col)
        tprint(ser.upstream or '(none)', col=col)

        versions = self.db.ser_ver_get_for_series(ser.idnum)
        if not isinstance(versions, list):
            versions = [versions]

        for sv in versions:
            link_str = sv.link or '(none)'
            tprint(f'\n  Version {sv.version}:', colour=col.YELLOW, col=col)
            tprint('    Link: ', newline=False, colour=col.BLUE, col=col)
            tprint(link_str, col=col)
            tprint('    Description: ', newline=False, colour=col.BLUE, col=col)
            tprint(sv.desc or '(none)', col=col)
            if sv.name:
                tprint('    Cover: ', newline=False, colour=col.BLUE, col=col)
                tprint(sv.name, col=col)
            if sv.archive_tag:
                tprint('    Archive tag: ', newline=False, colour=col.BLUE,
                       col=col)
                tprint(sv.archive_tag, col=col)

            self._show_version_info(sv, show_reviews)

    def _show_version_info(self, sv, show_reviews):
        """Show patches, reviews and notes for one series version

        Args:
            sv (SerVer): Series-version record to display
            show_reviews (list of int or None): Patch numbers to show
                reviews for, or empty list for all, or None for none
        """
        col = self.col

        # Show patches
        pclist = self.db.pcommit_get_list(sv.idnum)
        tprint('    Patches: ', newline=False, colour=col.BLUE, col=col)
        tprint(str(len(pclist)), col=col)
        for pc in pclist:
            state = f' [{pc.state}]' if pc.state else ''
            colour = col.GREEN if pc.state == 'accepted' else None
            tprint(f'      {pc.seq + 1}: {pc.subject}{state}', colour=colour,
                   col=col)

        # Show reviews if requested
        if show_reviews is not None:
            reviews = self.db.review_get_for_version(sv.idnum)
            if reviews:
                shown = [r for r in reviews if not show_reviews
                         or r.seq in show_reviews]
                tprint('    Reviews: ', newline=False, colour=col.BLUE, col=col)
                tprint(f'{len(shown)}/{len(reviews)}', col=col)
                for rev in shown:
                    colour = col.GREEN if rev.approved else col.YELLOW
                    stat = 'approved' if rev.approved else 'comments'
                    tprint(f'      Patch {rev.seq}: [{stat}]', colour=colour,
                           col=col)
                    for line in rev.body.splitlines():
                        if line.startswith('> '):
                            tprint(f'        {line}', colour=col.MAGENTA,
                                   col=col)
                        else:
                            tprint(f'        {line}', colour=col.WHITE, col=col)

        # Show notes if any
        if sv.notes:
            lines = sv.notes.strip().splitlines()
            tprint('    Notes: ', newline=False, colour=col.BLUE, col=col)
            tprint(lines[0], colour=col.CYAN, col=col)
            for line in lines[1:3]:
                tprint(f'           {line}', colour=col.CYAN, col=col)
            if len(lines) > 3:
                tprint(f'           ... ({len(lines)} lines)', col=col)

    def set_upstream(self, series, ups, dry_run=False):
        """Set the upstream for a series

        Args:
            series (str): Name of series to use, or None to use current branch
            ups (str): Name of the upstream to set
            dry_run (bool): True to do a dry run
        """
        if not ups:
            raise ValueError('Please specify the upstream name')
        ser, _ = self._parse_series_and_version(series, None)
        self._ensure_in_db(ser)

        self.db.series_set_upstream(ser.idnum, ups)

        if not dry_run:
            self.commit()
        else:
            self.rollback()

        tout.notice(f"Set upstream for series '{ser.name}' to '{ups}'")
        if dry_run:
            tout.info('Dry run completed')

    def scan(self, branch_name, mark=False, allow_unmarked=False, end=None,
             dry_run=False):
        """Scan a branch and make updates to the database if it has changed

        Args:
            branch_name (str): Name of branch to sync, or None for current one
            mark (str): True to mark each commit with a change ID
            allow_unmarked (str): True to not require each commit to be marked
            end (str): Add only commits up to but exclu
            dry_run (bool): True to do a dry run
        """
        def _show_item(oper, seq, subject):
            col = None
            if oper == '+':
                col = self.col.GREEN
            elif oper == '-':
                col = self.col.RED
            out = self.col.build(col, subject) if col else subject
            tout.info(f'{oper} {seq:3} {out}')

        name, ser, version, msg = self.prep_series(branch_name, end)
        self._ensure_in_db(ser)
        svid = self.get_ser_ver(ser.idnum, version).idnum
        pcdict = self.get_pcommit_dict(svid)

        tout.info(
            f"Syncing series '{name}' v{version}: mark {mark} "
            f'allow_unmarked {allow_unmarked}')
        if msg:
            tout.info(msg)

        ser = self._handle_mark(name, ser, version, mark, allow_unmarked,
                                False, dry_run)

        # First check for new patches that are not in the database
        to_add = dict(enumerate(ser.commits))
        for pcm in pcdict.values():
            tout.debug(f'pcm {pcm.subject}')
            i = self._find_matched_commit(to_add, pcm)
            if i is not None:
                del to_add[i]

        # Now check for patches in the database that are not in the branch
        to_remove = dict(enumerate(pcdict.values()))
        for cmt in ser.commits:
            tout.debug(f'cmt {cmt.subject}')
            i = self._find_matched_patch(to_remove, cmt)
            if i is not None:
                del to_remove[i]

        removed = 0
        added = 0
        for seq, cmt in enumerate(ser.commits):
            if seq in to_remove:
                _show_item('-', seq, to_remove[seq].subject)
                del to_remove[seq]
                removed += 1
            if seq in to_add:
                _show_item('+', seq, to_add[seq].subject)
                del to_add[seq]
                added += 1
            else:
                _show_item(' ', seq, cmt.subject)
        seq = len(ser.commits)
        for cmt in to_add.items():
            _show_item('+', seq, cmt.subject)
            seq += 1
        for seq, pcm in to_remove.items():
            _show_item('+', seq, pcm.subject)

        self.db.pcommit_delete(svid)
        self._add_series_commits(ser, svid)

        # Update series description if the cover letter has changed
        branch_desc = ser.cover[0] if ser.cover else None  # pylint: disable=E1136
        if branch_desc and branch_desc != ser.desc:
            self.db.series_set_desc(ser.idnum, branch_desc)
            tout.notice(f"Updated description to '{branch_desc}'")

        # Update per-version description from cover letter or first
        # commit, so autolink uses the right search term
        if ser.cover:
            sv_desc = ser.cover[0]  # pylint: disable=E1136
        elif ser.commits:
            sv_desc = ser.commits[0].subject
        else:
            sv_desc = None
        if sv_desc:
            self.db.ser_ver_set_desc(svid, sv_desc)

        if not dry_run:
            self.commit()
            seq = len(ser.commits)
            msg = ''
            if added:
                msg += f'{added} added'
            if removed:
                if msg:
                    msg += ', '
                msg += f'{removed} removed'
            if msg:
                msg = f' ({msg})'
            tout.notice(f'Scanned {seq} commit{self.plural(seq)}{msg}')
        else:
            self.rollback()
            tout.info('Dry run completed')

    def send(self, pwork, name, autolink, autolink_wait, args):
        """Send out a series

        Args:
            pwork (Patchwork): Patchwork object to use
            name (str): Series name to search for, or None for current series
                that is checked out
            autolink (bool): True to auto-link the series after sending
            args (argparse.Namespace): 'send' arguments provided
            autolink_wait (int): Number of seconds to wait for the autolink to
                succeed
        """
        ser, version = self._parse_series_and_version(name, None)
        self._ensure_in_db(ser)

        self.apply_send_settings(args, self.get_series_upstream(name))

        args.branch = self._get_branch_name(ser.name, version)
        likely_sent = send.send(args, git_dir=self.gitdir, cwd=self.topdir)

        if likely_sent:
            svid = self.get_series_svid(ser.idnum, version)
            workflow.sent(self, ser.idnum, ser_ver_id=svid)

        if likely_sent and autolink:
            tout.notice(f'Autolinking with Patchwork ({autolink_wait} seconds)')
            self.link_auto(pwork, name, version, True, wait_s=autolink_wait)

    def archive(self, series):
        """Archive a series

        Args:
            series (str): Name of series to use, or None to use current branch
        """
        ser = self._parse_series(series, include_archived=True)
        self._ensure_in_db(ser)

        svlist = self.db.ser_ver_get_for_series(ser.idnum)

        # Figure out the tags we will create
        tag_info = {}
        now = self.get_now()
        now_str = now.strftime('%d%b%y').lower()
        for svi in svlist:
            name = self._get_branch_name(ser.name, svi.version)
            if not gitutil.check_branch(name, git_dir=self.gitdir):
                raise ValueError(f"No branch named '{name}'")
            tag_info[svi.version] = [svi.idnum, name, f'{name}-{now_str}']

        # Create the tags
        repo = pygit2.Repository(self.gitdir)
        for _, (idnum, name, tag_name) in tag_info.items():
            commit = repo.revparse_single(name)
            repo.create_tag(tag_name, str(commit.id),
                            pygit2.enums.ObjectType.COMMIT,
                            commit.author, commit.message)

        # Update the database
        for idnum, name, tag_name in tag_info.values():
            self.db.ser_ver_set_archive_tag(idnum, tag_name)

        # Delete the branches
        for idnum, name, tag_name in tag_info.values():
            # Drop any review worktree first; a checked-out branch
            # cannot be deleted
            gitutil.remove_worktree(self.topdir,
                cser_helper.review_worktree_path(self.topdir, name))

            # Detach HEAD from the branch if pointing to this branch
            commit = repo.revparse_single(name)
            if repo.head.target == commit.id:
                repo.set_head(commit.id)

            repo.branches.delete(name)

        self.db.series_set_archived(ser.idnum, True)
        self.commit()
        count = len(tag_info)
        tout.notice(f"Archived series '{ser.name}'"
                    f" ({count} version{self.plural(count)})")

    def unarchive(self, series):
        """Unarchive a series

        Args:
            series (str): Name of series to use, or None to use current branch
        """
        ser = self._parse_series(series, include_archived=True)
        self._ensure_in_db(ser)
        self.db.series_set_archived(ser.idnum, False)

        svlist = self.db.ser_ver_get_for_series(ser.idnum)

        # Collect the tags
        repo = pygit2.Repository(self.gitdir)
        tag_info = {}
        for svi in svlist:
            name = self._get_branch_name(ser.name, svi.version)
            target = repo.revparse_single(svi.archive_tag)
            tag_info[svi.idnum] = name, svi.archive_tag, target

        # Make sure the branches don't exist
        for name, tag_name, tag in tag_info.values():
            if name in repo.branches:
                raise ValueError(
                    f"Cannot restore branch '{name}': already exists")

        # Recreate the branches
        for name, tag_name, tag in tag_info.values():
            target = repo.get(tag.target)
            repo.branches.create(name, target)

        # Delete the tags
        for name, tag_name, tag in tag_info.values():
            repo.references.delete(f'refs/tags/{tag_name}')

        # Update the database
        for idnum, (name, tag_name, tag) in tag_info.items():
            self.db.ser_ver_set_archive_tag(idnum, None)

        self.commit()
        count = len(tag_info)
        tout.notice(f"Unarchived series '{ser.name}'"
                    f" ({count} version{self.plural(count)})")

    def status(self, pwork, series, version, show_comments,
               show_cover_comments=False):
        """Show the series status from patchwork

        Args:
            pwork (Patchwork): Patchwork object to use
            series (str): Name of series to use, or None to use current branch
            version (int): Version number, or None to detect from name
            show_comments (bool): Show all comments on each patch
            show_cover_comments (bool): Show all comments on the cover letter
        """
        branch, series, version, _, _, link, _, _ = self._get_patches(
            series, version, include_archived=True)
        if not link:
            raise ValueError(
                f"Series '{series.name}' v{version} has no patchwork link: "
                f"Try 'patman series -s {branch} autolink'")
        status.check_and_show_status(
            series, link, branch, None, False, show_comments,
            show_cover_comments, pwork, self.gitdir)

    def summary(self, series):
        """Show summary information for all series

        Args:
            series (str): Name of series to use
        """
        print(f"{'Name':17}  Status  Description")
        print(f"{'-' * 17}  {'-' * 6}  {'-' * 30}")
        if series is not None:
            self._summary_one(self._parse_series(series))
            return

        sdict = self.db.series_get_dict()
        for ser in sdict.values():
            self._summary_one(ser)

    def gather(self, pwork, series, version, show_comments,
               show_cover_comments, gather_tags, dry_run=False):
        """Gather any new tags from Patchwork, optionally showing comments

        Args:
            pwork (Patchwork): Patchwork object to use
            series (str): Name of series to use, or None to use current branch
            version (int): Version number, or None to detect from name
            show_comments (bool): True to show the comments on each patch
            show_cover_comments (bool): True to show the comments on the cover
                letter
            gather_tags (bool): True to gather review/test tags
            dry_run (bool): True to do a dry run (database is not updated)
        """
        ser, version = self._parse_series_and_version(
            series, version, include_archived=True)
        self._ensure_version(ser, version)
        svid, link = self._get_series_svid_link(ser.idnum, version)
        if not link:
            raise ValueError(
                "No patchwork link is available: use 'patman series autolink'")
        tout.info(
            f"Updating series '{ser.name}' version {version} "
            f"from link '{link}'")

        loop = asyncio.get_event_loop()
        with pwork.collect_stats() as stats:
            cover, patches = loop.run_until_complete(self._gather(
                pwork, link, show_cover_comments))

        with terminal.pager():
            updated, updated_cover = self._sync_one(
                svid, ser.name, version, show_comments, show_cover_comments,
                gather_tags, cover, patches, dry_run)
            tout.notice(f"{updated} patch{'es' if updated != 1 else ''}"
                        f"{' and cover letter' if updated_cover else ''} "
                        f'updated ({stats.request_count} requests)')

            if not dry_run:
                self.commit()
                self.check_applied(svid, ser.name, version)
            else:
                self.rollback()
                tout.info('Dry run completed')

    def review(self, series, version, force=False, spelling='British',
               context=None):
        """AI-review the series and store the result in the database

        Reviews the commits on the series branch with the review agent and
        stores the findings, keyed by version, so they can be viewed later
        with 'patman series info -r'. The series must already have been
        added with 'patman series add'.

        Args:
            series (str): Name of series, or None to use the current branch
            version (int): Version number, or None to detect from the branch
            force (bool): Re-review even if reviews are already stored
            spelling (str): Spelling convention for review comments
            context (str or None): Extra context for the review agent, or
                '@path' to read it from a file
        """
        from patman import review

        ser, version = self._parse_series_and_version(series, version)
        self._ensure_in_db(ser)
        self._ensure_version(ser, version)
        svid = self.get_ser_ver(ser.idnum, version).idnum

        existing = self.db.review_get_for_version(svid)
        if existing and not force:
            raise ValueError(
                f"Series '{ser.name}' v{version} already has "
                f"{len(existing)} stored review(s); view with 'patman "
                "series info -r' or use -f to re-review")
        if existing:
            self.db.review_delete_for_version(svid)

        name = self._get_branch_name(ser.name, version)
        if not gitutil.check_branch(name, git_dir=self.gitdir):
            raise ValueError(f"No branch named '{name}'")
        count = gitutil.count_commits_to_branch(name, self.gitdir)
        series_obj = patchstream.get_metadata(name, 0, count,
                                              git_dir=self.gitdir)

        review.review_series(self, ser.idnum, svid, version, name,
                             series_obj, spelling=spelling, context=context)

    def gather_all(self, pwork, show_comments, show_cover_comments,
                   sync_all_versions, gather_tags, dry_run=False):
        to_fetch, missing = self._get_fetch_dict(sync_all_versions)
        groups = self._group_by_patchwork(pwork, to_fetch)
        for pwk, fetch in groups:
            tout.info(f'Reading {len(fetch)} series from {pwk.url}')

        loop = asyncio.get_event_loop()
        outcomes = loop.run_until_complete(self._sync_groups(groups))

        # Collect the results, skipping any series which could not be read
        results = {}
        requests = 0
        failed = 0
        for (pwk, fetch), (result, count) in zip(groups, outcomes):
            requests += count
            errors = []
            for (svid, sync), item in zip(fetch.items(), result):
                if not isinstance(item, BaseException):
                    results[svid] = item
                elif isinstance(item, (aiohttp.ClientError, OSError,
                                       asyncio.TimeoutError, ValueError)):
                    errors.append((sync, item))
                else:
                    raise item
            if len(fetch) > 1 and len(errors) == len(fetch):
                tout.error(f'Cannot read any of {len(fetch)} series from '
                           f'{pwk.url}: {_describe_error(errors[0][1])}')
            else:
                for sync, exc in errors:
                    tout.error(f"Cannot read '{sync.series_name}' "
                               f'v{sync.version} from {pwk.url}: '
                               f'{_describe_error(exc)}')
            failed += len(errors)

        with terminal.pager():
            tot_updated = 0
            tot_cover = 0
            add_newline = False
            for svid, sync in to_fetch.items():
                if svid not in results:
                    continue
                cover, patches = results[svid]
                if add_newline:
                    tout.info('')
                tout.info(f"Syncing '{sync.series_name}' v{sync.version}")
                updated, updated_cover = self._sync_one(
                    svid, sync.series_name, sync.version, show_comments,
                    show_cover_comments, gather_tags, cover, patches, dry_run)
                tot_updated += updated
                tot_cover += updated_cover
                add_newline = gather_tags

            tout.info('')
            tout.notice(
                f"{tot_updated} patch{'es' if tot_updated != 1 else ''} and "
                f"{tot_cover} cover letter{'s' if tot_cover != 1 else ''} "
                f'updated, {missing} missing '
                f"link{'s' if missing != 1 else ''} ({requests} requests)")
            applied = []
            if not dry_run:
                self.commit()
                for svid, sync in to_fetch.items():
                    if svid not in results:
                        continue
                    if self.check_applied(svid, sync.series_name, sync.version):
                        applied.append(sync.series_name)
                if applied:
                    tout.notice(f'{len(applied)} series applied upstream')
            else:
                self.rollback()
                tout.info('Dry run completed')
        if failed:
            raise ValueError(f'Cannot read {failed} series from patchwork')

    def upstream_add(self, name, url, project=None, pwork=None,
                     patchwork_url=None, identity=None, series_to=None,
                     no_maintainers=False, no_tags=False):
        """Add a new upstream tree

        Args:
            name (str): Name of the tree
            url (str): URL for the tree
            project (str or None): Patchwork project name to associate
            pwork (Patchwork or None): Patchwork object for looking up
                the project
            patchwork_url (str or None): URL of the patchwork server for
                this upstream
            identity (str or None): Git sendemail identity to use
            series_to (str or None): Patman alias for the To address
            no_maintainers (bool): True to skip get_maintainer.pl
            no_tags (bool): True to skip subject-tag alias processing
        """
        self.db.upstream_add(name, url, patchwork_url, identity=identity,
                             series_to=series_to,
                             no_maintainers=no_maintainers,
                             no_tags=no_tags)
        if project:
            if not pwork:
                if not patchwork_url:
                    raise ValueError(
                        'Patchwork URL is required when setting a project')
                pwork = Patchwork(patchwork_url)
            self.project_set(pwork, project, ups=name, quiet=True)
        self.commit()
        msg = f"Added upstream '{name}' ({url})"
        if patchwork_url:
            msg += f" patchwork '{patchwork_url}'"
        if identity:
            msg += f" identity '{identity}'"
        if series_to:
            msg += f" to '{series_to}'"
        if no_maintainers:
            msg += ' no-maintainers'
        if no_tags:
            msg += ' no-tags'
        if project:
            msg += f" project '{project}'"
        tout.notice(msg)

    def upstream_list(self):
        """List the upstream repos

        Shows a list of the repos, obtained from the database, along with
        any associated patchwork project
        """
        udict = self.get_upstream_dict()

        print(f"{'Name':6} {'Def':3} {'Project':10} {'URL':44} Options")
        border = (f"{'-' * 6} {'-' * 3} {'-' * 10} {'-' * 44} "
                  f"{'-' * 20}")
        print(border)
        for name, items in udict.items():
            (url, is_default, patchwork_url, identity, series_to,
             no_maintainers, no_tags) = items
            default = '*' if is_default else ''
            proj = self.db.patchwork_get(name)
            proj_name = proj[0] if proj else ''
            opts = []
            if patchwork_url:
                opts.append(f'pw:{patchwork_url}')
            if identity:
                opts.append(f'id:{identity}')
            if series_to:
                opts.append(f'to:{series_to}')
            if no_maintainers:
                opts.append('no-maintainers')
            if no_tags:
                opts.append('no-tags')
            print(f'{name:6} {default:3} {proj_name:10} {url:44} '
                  f'{" ".join(opts)}')

    def upstream_set(self, name, **kwargs):
        """Update settings on an existing upstream

        See Database.upstream_set() for permitted kwargs.

        Args:
            name (str): Name of the upstream remote to update
            kwargs: Fields to update
        """
        self.db.upstream_set(name, **kwargs)
        self.commit()
        parts = [f'{k}={v!r}' for k, v in kwargs.items()]
        tout.notice(f"Updated upstream '{name}': {', '.join(parts)}")

    def upstream_set_default(self, name):
        """Set the default upstream target

        Args:
            name (str): Name of the upstream remote to set as default, or None
                for none
        """
        self.db.upstream_set_default(name)
        self.commit()
        if name:
            tout.notice(f"Set default upstream to '{name}'")

    def upstream_get_default(self):
        """Get the default upstream target

        Return:
            str: Name of the upstream remote to set as default, or None if none
        """
        return self.db.upstream_get_default()

    def upstream_delete(self, name):
        """Delete an upstream target

        Args:
            name (str): Name of the upstream remote to delete
        """
        self.db.upstream_delete(name)
        self.commit()
        tout.notice(f"Deleted upstream '{name}'")

    def version_remove(self, name, version, dry_run=False):
        """Remove a version of a series from the database

        Args:
            name (str): Name of series to remove, or None to use current one
            version (int): Version number to remove
            dry_run (bool): True to do a dry run
        """
        ser, version = self._parse_series_and_version(name, version)
        name = ser.name

        versions = self._ensure_version(ser, version)

        if versions == [version]:
            raise ValueError(
                f"Series '{ser.name}' only has one version: remove the series")

        self.db.ser_ver_remove(ser.idnum, version)
        if not dry_run:
            self.commit()
        else:
            self.rollback()

        tout.notice(f"Removed version {version} from series '{name}'")
        if dry_run:
            tout.info('Dry run completed')

    def version_change(self, name, version, new_version, dry_run=False):
        """Change a version of a series to be a different version

        Args:
            name (str): Name of series to remove, or None to use current one
            version (int): Version number to change
            new_version (int): New version
            dry_run (bool): True to do a dry run
        """
        ser, version = self._parse_series_and_version(name, version)
        name = ser.name

        versions = self._ensure_version(ser, version)
        vstr = list(map(str, versions))
        if version not in versions:
            raise ValueError(
                f"Series '{ser.name}' does not have v{version}: "
                f"{' '.join(vstr)}")

        if not new_version:
            raise ValueError('Please provide a new version number')

        if new_version in versions:
            raise ValueError(
                f"Series '{ser.name}' already has a v{new_version}: "
                f"{' '.join(vstr)}")

        new_name = self._join_name_version(ser.name, new_version)

        svid = self.get_series_svid(ser.idnum, version)
        pwc = self.get_pcommit_dict(svid)
        count = len(pwc.values())
        series = patchstream.get_metadata(name, 0, count, git_dir=self.gitdir)

        self.update_series(name, series, version, new_name, dry_run,
                            add_vers=new_version, switch=True)
        self.db.ser_ver_set_version(svid, new_version)

        if not dry_run:
            self.commit()
        else:
            self.rollback()

        tout.notice(f"Changed version {version} in series '{ser.name}' "
                    f"to {new_version} named '{new_name}'")
        if dry_run:
            tout.info('Dry run completed')
