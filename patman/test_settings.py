# SPDX-License-Identifier: GPL-2.0+
#
# Copyright (c) 2022 Maxim Cournoyer <maxim.cournoyer@savoirfairelinux.com>
#

import contextlib
import os
import sys
import tempfile
import unittest
from unittest import mock

from patman import cmdline
from patman import settings
from u_boot_pylib import tools


@contextlib.contextmanager
def empty_git_repository():
    orig = os.getcwd()
    with tempfile.TemporaryDirectory() as tmpdir:
        os.chdir(tmpdir)
        try:
            tools.run('git', 'init', raise_on_error=True)
            yield tmpdir
        finally:
            os.chdir(orig)


@contextlib.contextmanager
def cleared_command_line_args():
    old_value = sys.argv[:]
    sys.argv = [sys.argv[0]]
    try:
        yield
    finally:
        sys.argv = old_value


def _make_parser():
    """Build a parser with a project option and a send subcommand"""
    parser = cmdline.ErrorCatchingArgumentParser()
    parser.add_argument('-p', '--project', default='unknown')
    subparsers = parser.add_subparsers(dest='cmd')
    send = subparsers.add_parser('send')
    send.add_argument('--no-check', action='store_false', dest='check_patch',
                      default=True)
    send.add_argument('--smtp-server', dest='smtp_server', default=None)
    return parser, send


class TestSettings(unittest.TestCase):
    def test_git_local_config(self):
        # Clearing the command line arguments is required, otherwise
        # arguments passed to the test running such as in 'pytest -k
        # filter' would be processed by _UpdateDefaults and fail.
        with cleared_command_line_args():
            with empty_git_repository():
                with tempfile.NamedTemporaryFile() as global_config:
                    global_config.write(b'[settings]\n'
                                        b'project=u-boot\n')
                    global_config.flush()
                    parser, send = _make_parser()

                    # Test "global" config is used.
                    settings.Setup(parser, 'unknown', [], global_config.name)
                    args, _ = parser.parse_known_args([])
                    self.assertEqual('u-boot', args.project)
                    send_args, _ = send.parse_known_args([])
                    self.assertTrue(send_args.check_patch)

                    # Test local config can shadow it.
                    with open('.patman', 'w', buffering=1) as f:
                        f.write('[settings]\n'
                                'project: guix-patches\n'
                                'check_patch: False\n')
                    settings.Setup(parser, 'unknown', [], global_config.name)
                    args, _ = parser.parse_known_args([])
                    self.assertEqual('guix-patches', args.project)
                    send_args, _ = send.parse_known_args([])
                    self.assertFalse(send_args.check_patch)

    def test_patman_defaults_layering(self):
        # .patman-defaults is the lowest-priority layer: global and local
        # both override it per-key, while a key set only in it falls through
        with cleared_command_line_args():
            with empty_git_repository():
                with tempfile.NamedTemporaryFile() as global_config:
                    global_config.write(b'[settings]\nproject=global-proj\n')
                    global_config.flush()

                    with open('.patman-defaults', 'w', buffering=1) as f:
                        f.write('[settings]\n'
                                'project: base-proj\n'
                                'smtp_server: base-smtp\n')

                    parser, send = _make_parser()

                    # global overrides base for 'project'; the base-only
                    # 'smtp_server' falls through
                    settings.Setup(parser, 'unknown', [], global_config.name)
                    args, _ = parser.parse_known_args([])
                    self.assertEqual('global-proj', args.project)
                    send_args, _ = send.parse_known_args([])
                    self.assertEqual('base-smtp', send_args.smtp_server)

                    # the user's local .patman overrides base and global
                    with open('.patman', 'w', buffering=1) as f:
                        f.write('[settings]\nproject: local-proj\n')
                    settings.Setup(parser, 'unknown', [], global_config.name)
                    args, _ = parser.parse_known_args([])
                    self.assertEqual('local-proj', args.project)
                    send_args, _ = send.parse_known_args([])
                    self.assertEqual('base-smtp', send_args.smtp_server)

                    # a command-line argument overrides every config layer
                    args, _ = parser.parse_known_args(['-p', 'cli-proj'])
                    self.assertEqual('cli-proj', args.project)

    def test_patman_defaults_disabled(self):
        # config_fname=False disables all file reads, the base file
        # included, so a .patman-defaults present is not applied
        with cleared_command_line_args():
            with empty_git_repository():
                with open('.patman-defaults', 'w', buffering=1) as f:
                    f.write('[settings]\nproject: base-proj\n')
                parser, _ = _make_parser()
                settings.Setup(parser, 'unknown', [], False)
                args, _ = parser.parse_known_args([])
                self.assertEqual('unknown', args.project)

    def test_patman_defaults_only(self):
        # With only .patman-defaults present, its values are used, but it
        # does not prevent the user's own ~/.patman being created
        with cleared_command_line_args():
            with empty_git_repository() as tmpdir:
                home = os.path.join(tmpdir, 'home')
                os.makedirs(home)
                with mock.patch.dict(os.environ, {'HOME': home}), \
                        mock.patch.object(
                            settings, 'CreatePatmanConfigFile') as create:
                    with open('.patman-defaults', 'w', buffering=1) as f:
                        f.write('[settings]\nproject: base-proj\n')

                    parser, _ = _make_parser()
                    settings.Setup(parser, 'unknown', [])
                    args, _ = parser.parse_known_args([])
                    self.assertEqual('base-proj', args.project)
                    create.assert_called_once()


if __name__ == '__main__':
    unittest.main()
