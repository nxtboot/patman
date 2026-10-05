# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2025 Canonical Ltd.
# Written by Simon Glass <simon.glass@canonical.com>
#

"""Common Claude Agent SDK utilities.

Provides shared functions for running Claude agents across tools that need
AI assistance (e.g. pickman, patman review).
"""

from u_boot_pylib import tout

# Maximum buffer size for agent responses
MAX_BUFFER_SIZE = 10 * 1024 * 1024  # 10MB

# Check if claude_agent_sdk is available
try:
    from claude_agent_sdk import query, ClaudeAgentOptions
    AGENT_AVAILABLE = True
except ImportError:
    AGENT_AVAILABLE = False


def check_available():
    """Check if the Claude Agent SDK is available

    Returns:
        bool: True if available, False otherwise
    """
    if not AGENT_AVAILABLE:
        tout.error('Claude Agent SDK not available')
        tout.error('Install with: pip install claude-agent-sdk')
        return False
    return True


async def run_agent_collect(prompt, options):
    """Run a Claude agent and collect its conversation log

    Sends the prompt to a Claude agent, streams output to stdout and
    collects all text blocks into a conversation log.

    Args:
        prompt (str): The prompt to send to the agent
        options (ClaudeAgentOptions): Agent configuration

    Returns:
        tuple: (success, conversation_log) where success is bool and
            conversation_log is the agent's output text
    """
    import os
    debug = os.environ.get('PATMAN_DEBUG_AGENT')
    conversation_log = []
    try:
        async for message in query(prompt=prompt, options=options):
            if debug:
                tout.error(f'AGENT MSG: {type(message).__name__}: '
                           f'{message!r}')
            if hasattr(message, 'content'):
                for block in message.content:
                    if debug:
                        tout.error(f'  BLOCK: {type(block).__name__}: '
                                   f'{block!r}')
                    if hasattr(block, 'text'):
                        print(block.text)
                        conversation_log.append(block.text)
        return True, '\n\n'.join(conversation_log)
    except (RuntimeError, ValueError, OSError) as exc:
        tout.error(f'Agent failed: {exc}')
        return False, '\n\n'.join(conversation_log)
    except Exception as exc:
        if 'API Error' in str(exc) or 'exit code' in str(exc):
            tout.error(f'Agent failed: {exc}')
            return False, '\n\n'.join(conversation_log)
        raise
