# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2025 Canonical Ltd.
#

"""Tests for the Claude Agent SDK utilities module."""

import asyncio
import unittest
from unittest.mock import MagicMock

from u_boot_pylib import claude
from u_boot_pylib import terminal


class TestClaude(unittest.TestCase):
    """Tests for u_boot_pylib.claude"""

    def test_check_available_when_sdk_missing(self):
        """check_available() returns False when SDK is not installed"""
        if not claude.AGENT_AVAILABLE:
            with terminal.capture():
                self.assertFalse(claude.check_available())

    def test_check_available_when_sdk_present(self):
        """check_available() returns True when SDK is installed"""
        old = claude.AGENT_AVAILABLE
        try:
            claude.AGENT_AVAILABLE = True
            self.assertTrue(claude.check_available())
        finally:
            claude.AGENT_AVAILABLE = old

    def test_max_buffer_size(self):
        """MAX_BUFFER_SIZE is defined and reasonable"""
        self.assertEqual(claude.MAX_BUFFER_SIZE, 10 * 1024 * 1024)

    def _setup_claude_with_mock_query(self, mock_query):
        """Inject a mock query function into the claude module"""
        claude.query = mock_query

    def test_run_agent_collect_success(self):
        """run_agent_collect() collects text from agent messages"""
        block1 = MagicMock()
        block1.text = 'Hello'
        msg1 = MagicMock()
        msg1.content = [block1]

        block2 = MagicMock()
        block2.text = 'World'
        msg2 = MagicMock()
        msg2.content = [block2]

        # pylint: disable=W0613
        async def mock_query(**kwargs):
            for msg in [msg1, msg2]:
                yield msg

        self._setup_claude_with_mock_query(mock_query)
        loop = asyncio.new_event_loop()
        with terminal.capture():
            success, log = loop.run_until_complete(
                claude.run_agent_collect('test prompt', MagicMock()))
        loop.close()

        self.assertTrue(success)
        self.assertIn('Hello', log)
        self.assertIn('World', log)

    def test_run_agent_collect_handles_error(self):
        """run_agent_collect() returns False on agent failure"""
        # pylint: disable=W0613
        async def mock_query(**kwargs):
            raise RuntimeError('Agent crashed')
            yield  # pylint: disable=W0101

        self._setup_claude_with_mock_query(mock_query)
        loop = asyncio.new_event_loop()
        with terminal.capture():
            success, _ = loop.run_until_complete(
                claude.run_agent_collect('test prompt', MagicMock()))
        loop.close()

        self.assertFalse(success)

    def test_run_agent_collect_handles_api_error(self):
        """run_agent_collect() catches SDK API errors"""
        # pylint: disable=W0613,W0719
        async def mock_query(**kwargs):
            raise Exception(
                'Command failed with exit code 1 (exit code: 1)')
            yield  # pylint: disable=W0101

        self._setup_claude_with_mock_query(mock_query)
        loop = asyncio.new_event_loop()
        with terminal.capture():
            success, _ = loop.run_until_complete(
                claude.run_agent_collect('test prompt', MagicMock()))
        loop.close()

        self.assertFalse(success)

    def test_run_agent_collect_reraises_unknown(self):
        """run_agent_collect() re-raises unexpected exceptions"""
        # pylint: disable=W0613
        async def mock_query(**kwargs):
            raise TypeError('unexpected bug')
            yield  # pylint: disable=W0101

        self._setup_claude_with_mock_query(mock_query)
        loop = asyncio.new_event_loop()
        with self.assertRaises(TypeError):
            loop.run_until_complete(
                claude.run_agent_collect('test prompt', MagicMock()))
        loop.close()

    def test_run_agent_collect_skips_non_text_blocks(self):
        """run_agent_collect() ignores blocks without text attribute"""
        text_block = MagicMock()
        text_block.text = 'Real text'
        tool_block = MagicMock(spec=[])  # No text attribute

        msg = MagicMock()
        msg.content = [tool_block, text_block]

        # pylint: disable=W0613
        async def mock_query(**kwargs):
            yield msg

        self._setup_claude_with_mock_query(mock_query)
        loop = asyncio.new_event_loop()
        with terminal.capture():
            success, log = loop.run_until_complete(
                claude.run_agent_collect('test prompt', MagicMock()))
        loop.close()

        self.assertTrue(success)
        self.assertIn('Real text', log)


if __name__ == '__main__':
    unittest.main()
