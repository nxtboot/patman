# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2025 Canonical Ltd.
# Written by Simon Glass <simon.glass@canonical.com>
#
# pylint: disable=C0415,R0913,R0914,R0917,W0718

"""Gmail API integration for creating draft review emails.

Handles OAuth2 authentication and creation of draft emails via the
Gmail API. Tokens are stored in ~/.config/patman/ for reuse across
sessions.
"""

import base64
from collections import namedtuple
import os
from email.header import Header, decode_header
from email.mime.text import MIMEText
from email.utils import formataddr, getaddresses

from u_boot_pylib import tout

# Common parameters for draft creation
DraftParams = namedtuple('DraftParams',
                         'service fallback_addr list_email '
                         'dry_run sender cover_msgid')

# Config paths - use patman.d to avoid conflict with existing ~/.config/patman
CONFIG_DIR = os.path.expanduser('~/.config/patman.d')
CLIENT_SECRET_PATH = os.path.join(CONFIG_DIR, 'client_secret.json')

# Gmail API scopes - compose drafts and read messages (for thread lookup)
SCOPES = ['https://www.googleapis.com/auth/gmail.compose',
          'https://www.googleapis.com/auth/gmail.readonly']


def _token_path(account=None):
    """Get the token path for a given account

    Args:
        account (str or None): Gmail account email, or None for default

    Returns:
        str: Path to the token file
    """
    if account:
        safe = account.replace('@', '_at_').replace('.', '_')
        return os.path.join(CONFIG_DIR, f'gmail_token_{safe}.json')
    return os.path.join(CONFIG_DIR, 'gmail_token.json')


def check_available():
    """Check if Gmail API dependencies are installed

    Returns:
        bool: True if google-api-python-client and google-auth-oauthlib
              are available
    """
    try:
        from googleapiclient import discovery  # pylint: disable=W0611
        from google_auth_oauthlib import flow  # pylint: disable=W0611
        from google.auth.transport import requests  # pylint: disable=W0611
    except ImportError:
        tout.error('Gmail API dependencies not available')
        tout.error('Install with: pip install google-api-python-client '
                   'google-auth-httplib2 google-auth-oauthlib')
        return False

    if not os.path.exists(CLIENT_SECRET_PATH):
        tout.error(f'Gmail client secret not found at {CLIENT_SECRET_PATH}')
        tout.error('Download OAuth client credentials from Google Cloud '
                   'Console and save them there')
        return False
    return True


def _get_credentials(account=None):
    """Get or refresh OAuth2 credentials

    Loads existing token if available and valid. If expired, refreshes
    using the refresh token. If no valid token exists, initiates the
    OAuth2 consent flow.

    Args:
        account (str or None): Gmail account email (e.g.
            'user@gmail.com'), or None for default token

    Returns:
        google.oauth2.credentials.Credentials: Valid credentials

    Raises:
        FileNotFoundError: if client_secret.json is missing
    """
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    token_path = _token_path(account)
    creds = None

    # Load existing token
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)

    # Refresh or get new credentials
    if not creds or not creds.valid:
        if creds and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CLIENT_SECRET_PATH):
                raise FileNotFoundError(
                    f'Gmail client secret not found at {CLIENT_SECRET_PATH}')
            app_flow = InstalledAppFlow.from_client_secrets_file(
                CLIENT_SECRET_PATH, SCOPES)
            if account:
                tout.notice(f'Please authenticate as {account}')
            creds = app_flow.run_local_server(port=0)

        # Save the token for next time
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(token_path, 'w', encoding='utf-8') as fh:
            import json
            token_data = {
                'token': creds.token,
                'refresh_token': creds.refresh_token,
                'token_uri': creds.token_uri,
                'client_id': creds.client_id,
                'client_secret': creds.client_secret,
                'scopes': list(creds.scopes or []),
            }
            json.dump(token_data, fh)

    return creds


def get_service(account=None):
    """Get an authenticated Gmail API service object

    Args:
        account (str or None): Gmail account email, or None for default

    Returns:
        googleapiclient.discovery.Resource: Gmail API service
    """
    from googleapiclient.discovery import build

    creds = _get_credentials(account)
    return build('gmail', 'v1', credentials=creds)


def fetch_sent_reviews(service, list_email, user_email,
                       max_results=20):
    """Fetch sent emails that are reviews of other people's patches

    Paginates through Gmail search results to collect review emails.
    Filters out replies to the user's own patches by checking whether
    the first message in the thread was sent by someone else.

    Args:
        service (googleapiclient.discovery.Resource): Gmail API service
        list_email (str): Mailing list address to filter by
        user_email (str): The reviewer's own email, used to skip
            threads initiated by the reviewer
        max_results (int): Maximum number of emails to collect

    Returns:
        list of str: Email body texts
    """
    query = f'from:me to:{list_email} subject:(Re: PATCH)'
    bodies = []
    page_token = None
    # Cache thread originator lookups
    thread_from_cache = {}

    while len(bodies) < max_results:
        kwargs = {'userId': 'me', 'q': query, 'maxResults': 100}
        if page_token:
            kwargs['pageToken'] = page_token
        results = service.users().messages().list(**kwargs).execute()
        messages = results.get('messages', [])
        if not messages:
            break

        for msg_info in messages:
            if len(bodies) >= max_results:
                break
            tout.progress(
                f'Fetched {len(bodies)}/{max_results} review emails')

            msg = service.users().messages().get(
                userId='me', id=msg_info['id'], format='full').execute()

            # Check if the thread was started by someone else
            thread_id = msg.get('threadId')
            if thread_id not in thread_from_cache:
                thread = service.users().threads().get(
                    userId='me', id=thread_id,
                    format='metadata',
                    metadataHeaders=['From']).execute()
                first_msg = thread.get('messages', [{}])[0]
                hdrs = first_msg.get('payload', {}).get('headers', [])
                from_val = ''
                for h in hdrs:
                    if h['name'] == 'From':
                        from_val = h['value']
                        break
                thread_from_cache[thread_id] = from_val

            # Skip if this thread was started by us
            if user_email in thread_from_cache.get(thread_id, ''):
                continue

            payload = msg.get('payload', {})
            body = _extract_body(payload)
            if body and '>' in body:
                bodies.append(body)

        page_token = results.get('nextPageToken')
        if not page_token:
            break

    tout.clear_progress()
    return bodies


def _extract_body(payload):
    """Extract plain text body from a Gmail message payload

    Args:
        payload (dict): Gmail message payload

    Returns:
        str or None: Decoded body text
    """
    if payload.get('mimeType') == 'text/plain':
        data = payload.get('body', {}).get('data', '')
        if data:
            return base64.urlsafe_b64decode(data).decode('utf-8', 'replace')

    for part in payload.get('parts', []):
        result = _extract_body(part)
        if result:
            return result
    return None


def _find_thread(service, msgid):
    """Find the Gmail thread containing a message with the given Message-ID

    Args:
        service (googleapiclient.discovery.Resource): Gmail API service
        msgid (str): Message-ID header value (e.g. '<abc@example.com>')

    Returns:
        str or None: Gmail thread ID if found
    """
    # Strip angle brackets — Gmail search expects bare message ID
    bare_id = msgid.strip('<>')
    try:
        results = service.users().messages().list(
            userId='me', q=f'rfc822msgid:{bare_id}').execute()
        messages = results.get('messages', [])
        if messages:
            return messages[0].get('threadId')
    except Exception as exc:
        tout.warning(f'Failed to search Gmail for thread: {exc}')
    return None


def create_draft(service, to, subject, body,
                 in_reply_to=None, references=None, cc=None,
                 sender=None):
    """Create a Gmail draft email

    If in_reply_to is set, searches for the original message in Gmail
    and attaches the draft to that thread so it appears as a reply.

    If sender is provided and differs from the Gmail account's address,
    a From header is set so the email is sent on behalf of the right
    identity.

    Args:
        service (googleapiclient.discovery.Resource): Gmail API service
        to (str): Recipient email address
        subject (str): Email subject line
        body (str): Plain text email body
        in_reply_to (str or None): Message-ID to reply to
        references (str or None): References header value
        cc (str or None): CC addresses
        sender (str or None): Sender identity, e.g. 'Name <email>'.
            If set, added as the From header.

    Returns:
        dict: Gmail API response with draft ID and message info

    Raises:
        googleapiclient.errors.HttpError: on API failure
    """
    msg = MIMEText(body)
    if sender:
        msg['from'] = sender
    msg['to'] = to
    msg['subject'] = (subject if subject.isascii() else
                      Header(subject, 'utf-8'))
    if cc:
        msg['cc'] = cc
    if in_reply_to:
        msg['In-Reply-To'] = in_reply_to
        msg['References'] = references or in_reply_to

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    draft_body = {'message': {'raw': raw}}

    # Find the existing thread in Gmail so the draft appears as a reply
    if in_reply_to:
        thread_id = _find_thread(service, in_reply_to)
        if thread_id:
            draft_body['message']['threadId'] = thread_id
            tout.notice(f'  Found thread: {thread_id}')
        else:
            tout.notice(f'  Thread not found for {in_reply_to}'
                        ' - draft will be standalone')

    draft = service.users().drafts().create(
        userId='me', body=draft_body).execute()
    return draft


def delete_draft(service, draft_id):
    """Delete a Gmail draft

    A draft that no longer exists (already sent or deleted by hand) is
    ignored rather than raising.

    Args:
        service (googleapiclient.discovery.Resource): Gmail API service
        draft_id (str): ID of the draft to delete
    """
    from googleapiclient.errors import HttpError
    try:
        service.users().drafts().delete(userId='me', id=draft_id).execute()
    except HttpError as exc:
        if exc.resp.status != 404:
            raise


def _decode_hdr(val):
    """Decode and unfold an email header value

    Headers from patchwork may be folded and may contain RFC 2047 encoded
    words, sometimes with the bogus charset 'unknown-8bit' (produced when
    the original email has raw 8-bit characters in a header). Such values
    are rejected by Gmail, so decode them to plain text, treating
    'unknown-8bit' as UTF-8

    Args:
        val (str or list of str): Header value, or list of values if the
            header appears more than once

    Returns:
        str: Decoded, unfolded header value
    """
    if isinstance(val, list):
        return ', '.join(_decode_hdr(v) for v in val)
    val = ' '.join(val.split())
    out = []
    for part, charset in decode_header(val):
        if isinstance(part, bytes):
            if not charset or charset == 'unknown-8bit':
                charset = 'utf-8'
            part = part.decode(charset, errors='replace')
        out.append(part)
    return ''.join(out)


def _format_addrs(vals):
    """Parse and re-encode a list of address-header values

    Args:
        vals (list of str): Header values, each with one or more addresses

    Returns:
        str: Comma-separated addresses, deduplicated, with any non-ASCII
            names encoded suitably for an email header
    """
    seen = set()
    addrs = []
    for name, addr in getaddresses([_decode_hdr(v) for v in vals if v]):
        if not addr or addr.lower() in seen:
            continue
        seen.add(addr.lower())
        addrs.append(formataddr((name, addr), charset='utf-8'))
    return ', '.join(addrs)


def _build_cc(headers, list_email):
    """Build a CC list from the original patch headers

    Combines the original To, Cc headers and mailing list address,
    deduplicating entries.

    Args:
        headers (dict): Email headers from patchwork get_patch()
        list_email (str or None): Mailing list email address

    Returns:
        str: Comma-separated CC addresses
    """
    vals = [headers.get(field, '') for field in ('To', 'Cc')]
    if list_email:
        vals.append(list_email)
    return _format_addrs(vals)


def _get_msgid(patch_data, hdrs):
    """Get the Message-ID from patch data or headers

    Args:
        patch_data (dict): Patch or cover letter data from patchwork
        hdrs (dict): Email headers from patchwork get_patch()

    Returns:
        str or None: Message-ID
    """
    return (patch_data.get('msgid') or
            hdrs.get('Message-Id') or
            hdrs.get('Message-ID'))


def _make_draft(params, patch_data, body, hdrs, refs=None):
    """Create a Gmail draft for a review reply

    Args:
        params (DraftParams): Common draft parameters
        patch_data (dict): Patch or cover letter data from patchwork
        body (str): Review email body
        hdrs (dict): Email headers
        refs (str or None): References header value. If None, uses
            the Message-ID as the reference (for cover letters).

    Returns:
        str or None: Gmail draft ID, or None for dry run
    """
    subject = _decode_hdr(hdrs.get('Subject', patch_data.get('name', '')))
    if not subject.startswith('Re: '):
        subject = f"Re: {subject}"
    msgid = _get_msgid(patch_data, hdrs)
    if refs is None:
        refs = msgid
    to_addr = _format_addrs([hdrs.get('Reply-To') or params.fallback_addr])
    cc = _build_cc(hdrs, params.list_email)
    if params.dry_run:
        tout.notice(f"Would create draft: {subject}")
        tout.notice(f"  To: {to_addr}, Cc: {cc}")
        return None
    draft = create_draft(params.service, to_addr, subject,
                         body, in_reply_to=msgid,
                         references=refs, cc=cc,
                         sender=params.sender)
    tout.notice(f"Created draft: {subject}")
    return draft['id']


def create_review_drafts(series_data, review_bodies, patch_headers=None,
                         dry_run=False, account=None, sender=None):
    """Create Gmail drafts for a reviewed series

    Creates one draft per patch (and optionally the cover letter),
    each threaded as a reply to the original email.

    Args:
        series_data (dict): Series data from patchwork
            get_series(), containing 'patches', 'cover_letter',
            'submitter', 'project'
        review_bodies (dict): Map of patch index to review body
            text. Key 0 is the cover letter, 1..N are patches.
        patch_headers (dict or None): Map of patch index to
            headers dict from patchwork get_patch()
        dry_run (bool): If True, print what would be created
            without calling the API
        account (str or None): Gmail account email to use
        sender (str or None): Reviewer identity, e.g.
            'Name <email>'. If this differs from the Gmail
            account, it is set as the From header.

    Returns:
        dict: Map of patch index to Gmail draft ID (empty for
            dry run)
    """
    if patch_headers is None:
        patch_headers = {}
    submitter = series_data.get('submitter', {})
    fallback_addr = submitter.get('email', '')
    project = series_data.get('project', {})
    list_email = project.get('list_email')
    cover = series_data.get('cover_letter')
    patches = series_data.get('patches', [])

    cover_hdrs = patch_headers.get(0, {})
    cover_msgid = _get_msgid(cover, cover_hdrs) if cover else None

    service = None
    if not dry_run:
        if not check_available():
            return {}
        service = get_service(account)

    params = DraftParams(service, fallback_addr, list_email,
                         dry_run, sender, cover_msgid)

    return _make_all_drafts(params, cover, patches,
                            review_bodies, patch_headers)


def _make_all_drafts(params, cover, patches, review_bodies,
                     patch_headers):
    """Create drafts for the cover letter and each patch

    Args:
        params (DraftParams): Common draft parameters
        cover (dict or None): Cover letter data from patchwork
        patches (list of dict): Patch data from patchwork
        review_bodies (dict): Map of index to review body text
        patch_headers (dict): Map of index to email headers

    Returns:
        dict: Map of patch index to Gmail draft ID
    """
    draft_ids = {}

    if cover and 0 in review_bodies:
        hdrs = patch_headers.get(0, {})
        did = _make_draft(params, cover, review_bodies[0],
                          hdrs)
        if did:
            draft_ids[0] = did

    for i, patch in enumerate(patches):
        idx = i + 1
        if idx not in review_bodies:
            continue
        hdrs = patch_headers.get(idx, {})
        msgid = _get_msgid(patch, hdrs)
        refs = (f'{params.cover_msgid} {msgid}'
                if params.cover_msgid else msgid)
        did = _make_draft(params, patch, review_bodies[idx], hdrs, refs)
        if did:
            draft_ids[idx] = did

    return draft_ids


def fetch_thread_replies(service, thread_id, after_msg_id):
    """Fetch replies in a thread that appeared after a given message

    Args:
        service (googleapiclient.discovery.Resource): Gmail API service
        thread_id (str): Gmail thread ID
        after_msg_id (str): Gmail message ID of our sent review

    Returns:
        list of dict: Replies, each with 'from', 'date', 'body'
    """
    try:
        thread = service.users().threads().get(
            userId='me', id=thread_id, format='full').execute()
    except Exception:
        return []

    messages = thread.get('messages', [])

    # Find our message index
    our_idx = -1
    for i, msg in enumerate(messages):
        if msg['id'] == after_msg_id:
            our_idx = i
            break

    if our_idx < 0:
        return []

    # Collect messages after ours
    replies = []
    for msg in messages[our_idx + 1:]:
        payload = msg.get('payload', {})
        headers = {h['name']: h['value']
                   for h in payload.get('headers', [])}
        body = _extract_body(payload)
        if body:
            replies.append({
                'from': headers.get('From', ''),
                'date': headers.get('Date', ''),
                'body': body,
                'msg_id': msg['id'],
                'thread_id': thread_id,
            })
    return replies


def sync_drafts(service, reviews):
    """Check if review drafts have been sent or deleted

    For each review that has a draft_id, checks if the draft still
    exists. If gone, checks the sent folder for a matching message.

    Args:
        service (googleapiclient.discovery.Resource): Gmail API service
        reviews (list of Review): Review records with draft_id set

    Returns:
        tuple: (sent, deleted) where:
            sent (dict): Map of review ID to (body, msg_id, thread_id)
            deleted (list): List of review IDs whose drafts were
                deleted without sending
    """
    sent = {}
    deleted = []

    for rev in reviews:
        if not rev.draft_id:
            continue

        # Check if the draft still exists
        try:
            service.users().drafts().get(
                userId='me', id=rev.draft_id).execute()
            # Draft still exists — not sent yet
            continue
        except Exception:
            pass

        # Draft is gone — check if it was sent by looking for a
        # sent message with matching subject and timestamp
        found = False
        try:
            results = service.users().messages().list(
                userId='me', q='in:sent is:sent',
                maxResults=50).execute()
            for msg_info in results.get('messages', []):
                msg = service.users().messages().get(
                    userId='me', id=msg_info['id'],
                    format='full').execute()
                payload = msg.get('payload', {})
                body = _extract_body(payload)
                if body and rev.body[:50] in body:
                    sent[rev.idnum] = (body, msg_info['id'],
                                       msg.get('threadId'))
                    found = True
                    break
        except Exception:
            pass

        if not found:
            deleted.append(rev.idnum)

    return sent, deleted
