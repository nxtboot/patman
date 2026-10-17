# SPDX-License-Identifier: GPL-2.0+

# Copyright 2025 Simon Glass <sjg@chromium.org>
#
"""Tests for sending patches, including through a web relay"""

import os
import types
from unittest import mock

from u_boot_pylib import terminal
from patman import cmdline
from patman import control
from patman.test_common import CseriesTester

#pylint: disable=protected-access


class TestSend(CseriesTester):
    """Tests for 'patman send' and sending through a web relay"""
    def test_send_series_upstream(self):
        """Plain 'send' uses the settings of the series' upstream"""
        cser = self.get_cser()
        cser.db.upstream_add('ci', 'https://ci.git', series_to='concept',
                             no_maintainers=True, no_tags=True,
                             identity='work')
        cser.db.commit()
        with terminal.capture():
            cser.add('first', 'my description', allow_unmarked=True)
        cser.db.series_set_upstream(cser.db.series_find_by_name('first'), 'ci')
        cser.db.commit()
        self.db_close()

        args = cmdline.parse_args(['send', '-b', 'first'], config_fname=False)
        with terminal.capture() as (out, _):
            control._use_series_upstream(args, self.tmpdir)
        self.assertIn("Using the send settings of upstream 'ci'", out.getvalue())
        self.assertEqual(('concept', False, False, 'work'),
                         (args.series_to, args.add_maintainers,
                          args.process_tags, args.identity))

        # A branch which is not a tracked series is sent as it is
        args = cmdline.parse_args(['send', '-b', 'second'], config_fname=False)
        control._use_series_upstream(args, self.tmpdir)
        self.assertEqual((None, True), (getattr(args, 'series_to', None),
                                        args.add_maintainers))

        # With no database there is nothing to do, and none is created
        os.unlink(os.path.join(self.tmpdir, '.patman.db'))
        args = cmdline.parse_args(['send', '-b', 'first'], config_fname=False)
        control._use_series_upstream(args, self.tmpdir)
        self.assertTrue(args.add_maintainers)
        self.assertFalse(os.path.exists(os.path.join(self.tmpdir,
                                                     '.patman.db')))

    def _relay_submit(self, response_bytes, reflect=False):
        """Call relay.submit with urlopen mocked to return response_bytes

        Returns (result, captured_request_body).
        """
        import json
        from patman import relay

        class Resp:
            def read(self):
                return response_bytes

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        captured = {}

        def fake_open(req, timeout=None):
            captured['url'] = req.full_url
            captured['body'] = json.loads(req.data)
            return Resp()

        with mock.patch('patman.relay.urllib.request.urlopen', fake_open):
            with terminal.capture():
                result = relay.submit('https://relay.example/submit',
                                      ['m1', 'm2'], reflect=reflect)
        return result, captured

    def test_relay_submit(self):
        """Test the relay submit request and success handling"""
        n, cap = self._relay_submit(b'{"result": "success"}')
        self.assertEqual(2, n)
        self.assertEqual('receive', cap['body']['action'])
        self.assertEqual(['m1', 'm2'], cap['body']['messages'])

        # --reflect uses a different action
        _, cap = self._relay_submit(b'{"result": "success"}', reflect=True)
        self.assertEqual('reflect', cap['body']['action'])

    def test_relay_submit_error(self):
        """Test an error result from the endpoint is raised with its message"""
        with self.assertRaises(ValueError) as cm:
            self._relay_submit(b'{"result": "error", "message": "no key"}')
        self.assertIn('no key', str(cm.exception))

    def test_relay_submit_bad_json(self):
        """Test a non-JSON response is reported clearly"""
        with self.assertRaises(ValueError) as cm:
            self._relay_submit(b'<html>nope</html>')
        self.assertIn('Unexpected response', str(cm.exception))

    def test_relay_sign_message(self):
        """Test sign_message delegates to patatt"""
        from patman import relay
        fake = mock.MagicMock()
        fake.rfc2822_sign.return_value = b'signed body'
        with mock.patch.dict('sys.modules', {'patatt': fake}):
            out = relay.sign_message(b'raw body')
        self.assertEqual(b'signed body', out)
        fake.rfc2822_sign.assert_called_once_with(b'raw body')

    def test_relay_sign_no_key(self):
        """Test a missing patatt key gives a helpful message, not a traceback"""
        from patman import relay
        fake = types.ModuleType('patatt')

        class NoKeyError(Exception):
            pass

        fake.NoKeyError = NoKeyError
        fake.SigningError = type('SigningError', (Exception,), {})

        def rfc2822_sign(data):
            raise NoKeyError('patatt.signingkey is not set')

        fake.rfc2822_sign = rfc2822_sign
        with mock.patch.dict('sys.modules', {'patatt': fake}):
            with self.assertRaises(ValueError) as cm:
                relay.sign_message(b'x')
        self.assertIn('patatt.signingkey', str(cm.exception))

    def test_relay_auth_new(self):
        """Test auth_new posts the registration request"""
        import json
        from patman import relay

        class Resp:
            def read(self):
                return b'{"result": "success"}'

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        captured = {}

        def fake_open(req, timeout=None):
            captured['body'] = json.loads(req.data)
            return Resp()

        with mock.patch.object(relay, '_auth_config',
                               return_value=('Me', 'me@x', 'sel', 'PUBKEY')), \
                mock.patch('patman.relay.urllib.request.urlopen', fake_open):
            with terminal.capture():
                relay.auth_new('https://relay/x')
        body = captured['body']
        self.assertEqual('auth-new', body['action'])
        self.assertEqual('Me', body['name'])
        self.assertEqual('me@x', body['identity'])
        self.assertEqual('sel', body['selector'])
        self.assertEqual('PUBKEY', body['pubkey'])

    def test_relay_auth_verify(self):
        """Test auth_verify signs the challenge and posts it"""
        import email as email_mod
        import json
        from patman import relay

        class Resp:
            def read(self):
                return b'{"result": "success"}'

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        captured = {}

        def fake_open(req, timeout=None):
            captured['body'] = json.loads(req.data)
            return Resp()

        with mock.patch.object(relay, '_auth_config',
                               return_value=('Me', 'me@x', 'sel', 'PUB')), \
                mock.patch.object(relay, 'sign_message',
                                  side_effect=lambda data: b'SIGNED:' + data), \
                mock.patch('patman.relay.urllib.request.urlopen', fake_open):
            with terminal.capture():
                relay.auth_verify('https://relay/x', 'CHAL')
        body = captured['body']
        self.assertEqual('auth-verify', body['action'])
        self.assertTrue(body['msg'].startswith('SIGNED:'))
        # The signed message is a MIME message carrying the challenge
        inner = email_mod.message_from_string(body['msg'][len('SIGNED:'):])
        self.assertEqual('me@x', inner['From'])
        self.assertEqual('b4-send-verify', inner['Subject'])
        self.assertEqual(b'verify:CHAL\n', inner.get_payload(decode=True))

    def test_send_via_relay_threading(self):
        """Test relay threading: patches reply to the cover, root to -r"""
        from patman import send as send_mod
        from patman import relay
        import email as email_mod

        d = self.tmpdir
        # cover and patch 'a' have Message-Ids; patch 'b' has none (generated)
        specs = [('0000-cover.patch', 'cover', 'Message-Id: <cover@x>\n'),
                 ('0001-a.patch', 'a', 'Message-Id: <a@x>\n'),
                 ('0002-b.patch', 'b', '')]
        for name, subj, mid in specs:
            with open(os.path.join(d, name), 'w') as fd:
                fd.write(f'From: Me <me@example.org>\nSubject: {subj}\n'
                         f'{mid}\nbody\n')
        ccf = os.path.join(d, 'cc')
        with open(ccf, 'w') as fd:
            for name, _, _ in specs:
                fd.write(f'{name} \n')
        series = types.SimpleNamespace(get=lambda key, dflt=None: dflt)

        sent = {}

        def fake_submit(endpoint, messages, reflect=False):
            sent['messages'] = messages
            return len(messages)

        with mock.patch.object(relay, 'sign_message',
                               side_effect=lambda data: data), \
                mock.patch.object(relay, 'submit', side_effect=fake_submit):
            with terminal.capture():
                send_mod.send_via_relay(
                    series, '0000-cover.patch',
                    ['0001-a.patch', '0002-b.patch'], ccf, 'https://r/x',
                    reflect=False, dry_run=False, thread=True,
                    in_reply_to='<prev@x>', cwd=d)

        cover = email_mod.message_from_string(sent['messages'][0])
        a = email_mod.message_from_string(sent['messages'][1])
        b = email_mod.message_from_string(sent['messages'][2])
        # The root replies to the --in-reply-to message
        self.assertEqual('<prev@x>', cover['In-Reply-To'])
        # Patches reply to the cover (shallow threading)
        self.assertEqual('<cover@x>', a['In-Reply-To'])
        self.assertEqual('<cover@x>', b['In-Reply-To'])
        self.assertIn('<cover@x>', a['References'])
        self.assertIn('<prev@x>', a['References'])
        # The patch with no Message-Id got one generated
        self.assertTrue(b['Message-ID'])

    def test_send_web_auth_requires_endpoint(self):
        """Test a web-auth action without an endpoint errors clearly"""
        from patman import send as send_mod
        args = types.SimpleNamespace(send_endpoint_web=None,
                                     web_auth_new=True, web_auth_verify=None)
        with self.assertRaises(ValueError) as cm:
            send_mod.send(args)
        self.assertIn('No web endpoint', str(cm.exception))

    def test_send_endpoint_no_relay(self):
        """Test --no-relay forces git send-email over a configured relay"""
        from patman import send as send_mod
        args = types.SimpleNamespace(send_endpoint_web='https://relay/x',
                                     no_relay=False)
        self.assertEqual('https://relay/x', send_mod._send_endpoint(args))
        args.no_relay = True
        self.assertIsNone(send_mod._send_endpoint(args))
        # No relay configured -> None regardless
        args = types.SimpleNamespace(send_endpoint_web=None, no_relay=False)
        self.assertIsNone(send_mod._send_endpoint(args))

    def test_send_identity_arg(self):
        """'send -I' sets the git send-email identity; absent leaves None"""
        args = cmdline.parse_args(['send', '-I', 'chromium'],
                                  config_fname=False)
        self.assertEqual('chromium', args.identity)
        args = cmdline.parse_args(['send', '--identity', 'x'],
                                  config_fname=False)
        self.assertEqual('x', args.identity)
        # 'series send' shares the same argument
        args = cmdline.parse_args(['series', 'send', '-I', 'y'],
                                  config_fname=False)
        self.assertEqual('y', args.identity)
        # Absent -> None, so an upstream-configured identity still applies
        args = cmdline.parse_args(['send'], config_fname=False)
        self.assertIsNone(args.identity)

    def test_send_parse_cc_file(self):
        """Test parsing the MakeCcFile output, incl. names with spaces"""
        from patman import send as send_mod
        path = os.path.join(self.tmpdir, 'cc')
        with open(path, 'w', encoding='utf-8') as fd:
            # '<fname> <cc1>\0<cc2>' -- cc addresses may contain spaces
            fd.write('0001-a.patch a@x\0Bob B <bob@x>\n')
            fd.write('0002-b.patch \n')  # no Cc
        cc_map = send_mod._parse_cc_file(path)
        self.assertEqual(['a@x', 'Bob B <bob@x>'], cc_map['0001-a.patch'])
        self.assertEqual([], cc_map['0002-b.patch'])

    def test_send_via_relay(self):
        """Test the relay send builds signed messages with To/Cc and posts"""
        from patman import send as send_mod
        from patman import relay
        import email as email_mod

        d = self.tmpdir
        with open(os.path.join(d, '0000-cover.patch'), 'w') as fd:
            fd.write('From: Me <me@x>\nSubject: cover\n'
                     'Message-Id: <0@x>\n\ncover body\n')
        with open(os.path.join(d, '0001-first.patch'), 'w') as fd:
            fd.write('From: Me <me@x>\nSubject: first\n'
                     'Message-Id: <1@x>\n\npatch body\n')
        ccf = os.path.join(d, 'cc')
        with open(ccf, 'w') as fd:
            fd.write('0000-cover.patch a@x\0Bob B <bob@x>\n')
            fd.write('0001-first.patch b@x\n')

        series = types.SimpleNamespace(
            get=lambda key, dflt=None: ['to@list'] if key == 'to' else dflt)

        sent = {}

        def fake_submit(endpoint, messages, reflect=False):
            sent.update(endpoint=endpoint, messages=messages, reflect=reflect)
            return len(messages)

        with mock.patch.object(relay, 'sign_message',
                               side_effect=lambda data: data), \
                mock.patch.object(relay, 'submit', side_effect=fake_submit):
            with terminal.capture():
                n = send_mod.send_via_relay(
                    series, '0000-cover.patch', ['0001-first.patch'], ccf,
                    'https://relay/x', reflect=False, dry_run=False, cwd=d)

        self.assertEqual(2, n)
        self.assertEqual('https://relay/x', sent['endpoint'])
        cover = email_mod.message_from_string(sent['messages'][0])
        self.assertEqual('to@list', cover['To'])
        self.assertIn('a@x', cover['Cc'])
        self.assertIn('Bob B <bob@x>', cover['Cc'])
        patch = email_mod.message_from_string(sent['messages'][1])
        self.assertEqual('b@x', patch['Cc'])
        self.assertEqual('patman', patch['X-Mailer'])

        # Dry run posts nothing
        sent.clear()
        with mock.patch.object(relay, 'sign_message',
                               side_effect=lambda data: data), \
                mock.patch.object(relay, 'submit',
                                  side_effect=fake_submit) as mock_submit:
            with terminal.capture():
                n = send_mod.send_via_relay(
                    series, None, ['0001-first.patch'], ccf,
                    'https://relay/x', reflect=True, dry_run=True, cwd=d)
        self.assertEqual(0, n)
        mock_submit.assert_not_called()
