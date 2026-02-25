import os
import base64
import json
import time
import logging
from email.mime.text import MIMEText
from typing import Optional, List
from datetime import datetime, timedelta

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

from providers.base import EmailProvider, EmailMessage, DebugStatus
from utils.text import html_to_text, parse_email_address, normalize_email_text
from store import get_item, set_item, get_gmail_token, set_gmail_token
from llm import draft_reply

logger = logging.getLogger(__name__)

SCOPES = ['https://www.googleapis.com/auth/gmail.modify', 'https://www.googleapis.com/auth/gmail.send']


class GmailProvider(EmailProvider):
    provider_name = "gmail"

    def __init__(self, base_url: str):
        self.base_url = base_url
        self.client_id = os.environ.get("GMAIL_CLIENT_ID") or os.environ.get("CLIENT_ID")
        self.client_secret = os.environ.get("GMAIL_CLIENT_SECRET") or os.environ.get("CLIENT_SECRET")

    def _require_creds(self):
        if not self.client_id or not self.client_secret:
            raise Exception("GMAIL_CLIENT_ID/GMAIL_CLIENT_SECRET not configured in Secrets.")

    def _get_flow(self) -> Flow:
        self._require_creds()
        client_config = {
            "web": {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [f"{self.base_url}/gmail/auth/callback"],
            }
        }
        return Flow.from_client_config(
            client_config,
            scopes=SCOPES,
            redirect_uri=f"{self.base_url}/gmail/auth/callback"
        )

    def get_auth_url(self) -> str:
        flow = self._get_flow()
        
        auth_params = {
            'access_type': 'offline',
            'prompt': 'consent',
            'include_granted_scopes': 'true',
        }
        
        auth_url, state = flow.authorization_url(**auth_params)
        set_item("gmail_oauth_state", state)
        return auth_url

    def handle_callback(self, code: str, state: str = None) -> dict:
        stored_state = get_item("gmail_oauth_state")
        if not stored_state:
            raise Exception("OAuth state not found. Please start login flow again at /gmail/login")
        
        if state and state != stored_state:
            raise Exception("Invalid OAuth state. Possible CSRF attack. Please start login flow again.")
        
        set_item("gmail_oauth_state", "")
        
        flow = self._get_flow()
        flow.fetch_token(code=code)
        creds = flow.credentials

        expiry_ts = int(time.time()) + 3600
        if creds.expiry:
            expiry_ts = int(creds.expiry.timestamp())

        scope_str = ' '.join(list(creds.scopes)) if creds.scopes else ' '.join(SCOPES)
        
        set_gmail_token(
            access_token=creds.token,
            refresh_token=creds.refresh_token if creds.refresh_token else None,
            scope=scope_str,
            expiry_ts=expiry_ts,
            client_id=creds.client_id,
            client_secret=creds.client_secret,
            needs_reauth=False,
            preserve_refresh_token=True
        )
        
        saved_token = get_gmail_token()
        has_refresh = bool(saved_token and saved_token.get("refresh_token"))
        
        if not has_refresh:
            logger.warning("Gmail OAuth: No refresh_token. User may need to revoke and re-authorize.")
        
        logger.info(f"Gmail OAuth: Token saved to SQLite. Has refresh_token: {has_refresh}")
        return {"ok": True, "message": "Gmail authenticated successfully.", "has_refresh_token": has_refresh}

    def _get_service(self):
        token_data = get_gmail_token()
        if not token_data:
            raise Exception("Not authenticated. Go to /gmail/login")
        
        if token_data.get("needs_reauth"):
            raise Exception("Re-authentication required. Go to /gmail/login")
        
        refresh_token = token_data.get("refresh_token")
        if not refresh_token:
            set_gmail_token(needs_reauth=True, last_refresh_error="No refresh_token stored")
            raise Exception("No refresh token available. Go to /gmail/login to re-authenticate.")

        scopes = token_data.get("scope", "").split() if token_data.get("scope") else SCOPES
        
        creds = Credentials(
            token=token_data.get("access_token"),
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=token_data.get("client_id") or self.client_id,
            client_secret=token_data.get("client_secret") or self.client_secret,
            scopes=scopes,
        )

        now = int(time.time())
        expiry_ts = token_data.get("expiry_ts") or 0
        token_expired = (not creds.token) or (now >= expiry_ts - 60)
        
        if token_expired or creds.expired:
            logger.info("Gmail: Access token expired, attempting refresh...")
            try:
                creds.refresh(Request())
                
                new_expiry = int(time.time()) + 3600
                if creds.expiry:
                    new_expiry = int(creds.expiry.timestamp())
                
                set_gmail_token(
                    access_token=creds.token,
                    expiry_ts=new_expiry,
                    refresh_token=creds.refresh_token if creds.refresh_token else None,
                    needs_reauth=False,
                    preserve_refresh_token=True
                )
                logger.info("Gmail: Token refreshed successfully")
            except Exception as e:
                error_str = str(e).lower()
                if "invalid_grant" in error_str or "token has been expired or revoked" in error_str:
                    set_gmail_token(needs_reauth=True, last_refresh_error=str(e))
                    raise Exception("Gmail token revoked or expired. Go to /gmail/login to re-authenticate.")
                else:
                    set_gmail_token(last_refresh_error=str(e))
                    logger.error(f"Gmail: Token refresh failed: {e}")
                    raise Exception(f"Token refresh failed: {e}. Try /gmail/login")

        return build('gmail', 'v1', credentials=creds)

    def _parse_message(self, message: dict) -> EmailMessage:
        payload = message.get('payload', {})
        headers = payload.get('headers', [])

        subject = next((h['value'] for h in headers if h['name'].lower() == 'subject'), '')
        from_raw = next((h['value'] for h in headers if h['name'].lower() == 'from'), '')
        date = next((h['value'] for h in headers if h['name'].lower() == 'date'), '')

        from_addr = parse_email_address(from_raw)

        body = ''
        parts = payload.get('parts', [])
        if parts:
            for part in parts:
                if part.get('mimeType') == 'text/plain':
                    data = part.get('body', {}).get('data', '')
                    if data:
                        body = base64.urlsafe_b64decode(data).decode('utf-8', errors='replace')
                        break
            if not body:
                for part in parts:
                    if part.get('mimeType') == 'text/html':
                        data = part.get('body', {}).get('data', '')
                        if data:
                            html_content = base64.urlsafe_b64decode(data).decode('utf-8', errors='replace')
                            body = html_to_text(html_content)
                            break
        else:
            data = payload.get('body', {}).get('data', '')
            if data:
                body = base64.urlsafe_b64decode(data).decode('utf-8', errors='replace')
                if '<html' in body.lower() or '<body' in body.lower():
                    body = html_to_text(body)

        clean_body = normalize_email_text(body)
        clean_subject = normalize_email_text(subject)

        return EmailMessage(
            id=message['id'],
            provider=self.provider_name,
            from_addr=from_addr,
            subject=clean_subject,
            body=clean_body[:4000] if clean_body else '',
            date=date,
        )

    def debug_status(self) -> DebugStatus:
        from datetime import datetime as dt
        
        token_data = get_gmail_token()
        
        if not token_data:
            return DebugStatus(
                connection="NOT_AUTHENTICATED",
                email="",
                folders=[],
                inbox_total=0,
                inbox_unseen=0,
                junk_total=0,
                junk_unseen=0,
                extra={
                    "connected": False,
                    "needs_reauth": True,
                    "has_refresh_token": False,
                    "access_token_expired": True,
                    "last_token_update": None,
                    "last_refresh_error": None,
                    "scopes": None,
                    "storage": "sqlite",
                    "message": "Not authenticated. Go to /gmail/login"
                }
            )
        
        has_refresh_token = bool(token_data.get("refresh_token"))
        needs_reauth = token_data.get("needs_reauth", False)
        expiry_ts = token_data.get("expiry_ts") or 0
        now = int(time.time())
        access_token_expired = (not token_data.get("access_token")) or (now >= expiry_ts - 60)
        last_update_ts = token_data.get("updated_at")
        last_update_iso = dt.fromtimestamp(last_update_ts).isoformat() if last_update_ts else None
        last_refresh_error = token_data.get("last_refresh_error")
        scopes = token_data.get("scope")
        
        if needs_reauth or not has_refresh_token:
            return DebugStatus(
                connection="NEEDS_REAUTH",
                email="",
                folders=[],
                inbox_total=0,
                inbox_unseen=0,
                junk_total=0,
                junk_unseen=0,
                extra={
                    "connected": False,
                    "needs_reauth": True,
                    "has_refresh_token": has_refresh_token,
                    "access_token_expired": access_token_expired,
                    "last_token_update": last_update_iso,
                    "last_refresh_error": last_refresh_error,
                    "scopes": scopes,
                    "storage": "sqlite",
                    "message": "Re-authentication required. Go to /gmail/login"
                }
            )
        
        try:
            service = self._get_service()
            
            profile = service.users().getProfile(userId='me').execute()
            email = profile.get('emailAddress', 'unknown')

            labels_result = service.users().labels().list(userId='me').execute()
            labels = [l.get('name', '') for l in labels_result.get('labels', [])]

            inbox_info = service.users().labels().get(userId='me', id='INBOX').execute()
            inbox_total = inbox_info.get('messagesTotal', 0)
            inbox_unseen = inbox_info.get('messagesUnread', 0)

            spam_total = 0
            spam_unseen = 0
            try:
                spam_info = service.users().labels().get(userId='me', id='SPAM').execute()
                spam_total = spam_info.get('messagesTotal', 0)
                spam_unseen = spam_info.get('messagesUnread', 0)
            except:
                pass

            token_data_fresh = get_gmail_token()
            last_ts = token_data_fresh.get("updated_at") if token_data_fresh else None
            
            return DebugStatus(
                connection="OK",
                email=email,
                folders=labels,
                inbox_total=inbox_total,
                inbox_unseen=inbox_unseen,
                junk_total=spam_total,
                junk_unseen=spam_unseen,
                extra={
                    "connected": True,
                    "needs_reauth": False,
                    "has_refresh_token": True,
                    "access_token_expired": False,
                    "last_token_update": dt.fromtimestamp(last_ts).isoformat() if last_ts else None,
                    "last_refresh_error": None,
                    "scopes": token_data_fresh.get("scope") if token_data_fresh else None,
                    "storage": "sqlite"
                }
            )
        except Exception as e:
            error_msg = str(e)
            return DebugStatus(
                connection="ERROR",
                email="",
                folders=[],
                inbox_total=0,
                inbox_unseen=0,
                junk_total=0,
                junk_unseen=0,
                extra={
                    "connected": False,
                    "needs_reauth": "login" in error_msg.lower() or "authenticate" in error_msg.lower(),
                    "has_refresh_token": has_refresh_token,
                    "access_token_expired": access_token_expired,
                    "last_token_update": last_update_iso,
                    "last_refresh_error": last_refresh_error,
                    "scopes": scopes,
                    "storage": "sqlite",
                    "error": error_msg
                }
            )

    def queue_next(self, folder: str = "inbox") -> Optional[EmailMessage]:
        service = self._get_service()

        if folder.lower() in ['spam', 'junk']:
            query = 'is:unread in:spam -in:trash'
        else:
            query = 'in:inbox is:unread -in:trash'

        results = service.users().messages().list(
            userId='me',
            q=query,
            maxResults=1
        ).execute()

        messages = results.get('messages', [])
        if not messages:
            return None

        msg_id = messages[0]['id']
        full_message = service.users().messages().get(
            userId='me',
            id=msg_id,
            format='full'
        ).execute()

        return self._parse_message(full_message)

    def list_emails(
        self, 
        folder: str = "inbox", 
        limit: int = 50, 
        date_start: datetime = None, 
        date_end: datetime = None,
        unread_only: bool = False
    ) -> List[EmailMessage]:
        service = self._get_service()
        
        query_parts = []
        if unread_only:
            query_parts.append('is:unread')
        
        if folder.lower() in ['spam', 'junk']:
            query_parts.append('in:spam')
        else:
            query_parts.append('in:inbox')
        
        if date_start:
            # Gmail 'after' is exclusive, so subtract 1 day to include the start date
            adjusted_start = date_start - timedelta(days=1)
            query_parts.append(f'after:{adjusted_start.strftime("%Y/%m/%d")}')
        if date_end:
            # Gmail 'before' is exclusive, so add 1 day to include the end date
            adjusted_end = date_end + timedelta(days=1)
            query_parts.append(f'before:{adjusted_end.strftime("%Y/%m/%d")}')
        
        query = ' '.join(query_parts)
        
        results = service.users().messages().list(
            userId='me',
            q=query,
            maxResults=limit
        ).execute()
        
        messages = results.get('messages', [])
        if not messages:
            return []
        
        emails = []
        fetched = {}

        def _on_msg(request_id, response, exception):
            if exception is not None:
                logging.warning(f"Gmail batch item {request_id} error: {exception}")
            elif response:
                fetched[request_id] = response

        try:
            batch = service.new_batch_http_request(callback=_on_msg)
            for msg_ref in messages:
                batch.add(
                    service.users().messages().get(
                        userId='me',
                        id=msg_ref['id'],
                        format='full'
                    ),
                    request_id=msg_ref['id']
                )
            batch.execute()
            logging.info(f"Gmail batch: {len(fetched)}/{len(messages)} fetched")
        except Exception as e:
            logging.warning(f"Gmail batch failed ({e}), falling back to individual fetch")
            fetched = {}
            for msg_ref in messages:
                try:
                    full_message = service.users().messages().get(
                        userId='me',
                        id=msg_ref['id'],
                        format='full'
                    ).execute()
                    if full_message:
                        fetched[msg_ref['id']] = full_message
                except:
                    continue

        for msg_ref in messages:
            full_message = fetched.get(msg_ref['id'])
            if not full_message:
                continue
            try:
                email_msg = self._parse_message(full_message)
                if email_msg:
                    email_msg.folder = folder
                    emails.append(email_msg)
            except:
                continue
        
        return emails

    def get_message(self, message_id: str) -> Optional[EmailMessage]:
        service = self._get_service()

        try:
            message = service.users().messages().get(
                userId='me',
                id=message_id,
                format='full'
            ).execute()
            return self._parse_message(message)
        except:
            return None

    def suggest_reply(self, message_id: str) -> dict:
        email_msg = self.get_message(message_id)
        if not email_msg:
            raise Exception(f"Email {message_id} not found")

        suggestion = draft_reply(
            email_msg.from_addr,
            email_msg.subject,
            email_msg.body,
        )

        set_item(f"gmail_draft:{message_id}", suggestion["raw"])

        return {
            "id": message_id,
            "from": email_msg.from_addr,
            "subject": email_msg.subject,
            "suggested_reply": suggestion.get("reply", suggestion["raw"]),
            "raw": suggestion["raw"],
        }

    def send(self, message_id: str) -> dict:
        service = self._get_service()

        draft = get_item(f"gmail_draft:{message_id}")
        if not draft:
            raise Exception(f"No draft found for message {message_id}")

        email_msg = self.get_message(message_id)
        if not email_msg:
            raise Exception(f"Original email {message_id} not found")

        message = MIMEText(draft, 'plain', 'utf-8')
        message['to'] = email_msg.from_addr
        message['subject'] = f"Re: {email_msg.subject}"

        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        body = {'raw': raw}

        service.users().messages().send(userId='me', body=body).execute()

        try:
            self.mark_read(message_id)
        except:
            pass

        return {"ok": True, "id": message_id}

    def mark_read(self, message_id: str) -> dict:
        service = self._get_service()

        service.users().messages().modify(
            userId='me',
            id=message_id,
            body={'removeLabelIds': ['UNREAD']}
        ).execute()

        return {"ok": True, "id": message_id}

    def delete(self, message_id: str) -> dict:
        """Move message to Trash (not permanent delete)"""
        logger.info(f"[GMAIL][DELETE] request id={message_id}")
        service = self._get_service()
        
        try:
            result = service.users().messages().trash(
                userId='me',
                id=message_id
            ).execute()
            
            result_labels = result.get('labelIds', [])
            logger.info(f"[GMAIL][DELETE] result: trashed, labels={result_labels}")
            
            return {
                "ok": True, 
                "id": message_id,
                "gmail_result": "trashed",
                "labels_after": result_labels
            }
        except Exception as e:
            logger.error(f"[GMAIL][DELETE] error: {e}")
            raise

    def mark_unread(self, message_id: str) -> dict:
        service = self._get_service()
        service.users().messages().modify(
            userId='me',
            id=message_id,
            body={'addLabelIds': ['UNREAD']}
        ).execute()
        return {"ok": True, "id": message_id}

    def get_message_labels(self, message_id: str) -> dict:
        """Get labels for a specific message (for debug purposes)"""
        service = self._get_service()
        
        try:
            message = service.users().messages().get(
                userId='me',
                id=message_id,
                format='minimal'
            ).execute()
            
            return {
                "ok": True,
                "id": message_id,
                "labelIds": message.get('labelIds', []),
                "threadId": message.get('threadId'),
                "snippet": message.get('snippet', '')[:100]
            }
        except Exception as e:
            return {
                "ok": False,
                "id": message_id,
                "error": str(e)
            }

    def get_folder_stats(self, folder: str = "inbox") -> dict:
        service = self._get_service()
        label_id = "INBOX"
        if folder.lower() in ["spam", "junk"]:
            label_id = "SPAM"
        try:
            info = service.users().labels().get(userId='me', id=label_id).execute()
            total = info.get('messagesTotal', 0)
            unseen = info.get('messagesUnread', 0)
            return {"total": total, "unseen": unseen, "read": total - unseen}
        except Exception as e:
            logger.error(f"Gmail get_folder_stats error: {e}")
            return {"total": 0, "unseen": 0, "read": 0}

    def compose_email(self, to: str, subject: str, body: str) -> dict:
        """Send a new email (not a reply)"""
        service = self._get_service()
        
        message = MIMEText(body, 'plain', 'utf-8')
        message['to'] = to
        message['subject'] = subject
        
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        send_body = {'raw': raw}
        
        result = service.users().messages().send(userId='me', body=send_body).execute()
        
        return {"ok": True, "to": to, "subject": subject, "id": result.get('id')}
