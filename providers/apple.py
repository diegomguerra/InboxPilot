import os
import imaplib
import smtplib
import email as email_lib
import html
from email.mime.text import MIMEText
from email.header import decode_header
from typing import Optional, List
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from providers.base import EmailProvider, EmailMessage, DebugStatus
from utils.text import html_to_text, parse_email_address
from store import get_item, set_item
from llm import draft_reply
from apple_imap import AppleIMAPClient
from time_filters import period_to_range


IMAP_SERVER = "imap.mail.me.com"
SMTP_SERVER = "smtp.mail.me.com"
SMTP_PORT = 587

OTP_SUBJECT_PATTERNS = ["código", "codigo", "confirm", "verification", "otp", "one-time", "2fa", "two-factor", "security code"]
OTP_FROM_PATTERNS = ["no-reply", "noreply", "donotreply", "do-not-reply"]


class AppleMailProvider(EmailProvider):
    provider_name = "apple"

    def __init__(self):
        self.email = os.environ.get("APPLE_EMAIL")
        self.password = os.environ.get("APPLE_APP_PASSWORD")

    def _require_creds(self):
        if not self.email or not self.password:
            raise Exception("APPLE_EMAIL/APPLE_APP_PASSWORD not configured in Secrets.")

    def _connect(self, folder: str = "INBOX", readonly: bool = True):
        self._require_creds()
        mail = imaplib.IMAP4_SSL(IMAP_SERVER)
        mail.login(self.email, self.password)
        mail.select(folder, readonly=readonly)
        return mail

    def _decode_payload(self, part) -> str:
        payload = part.get_payload(decode=True)
        if payload is None:
            return ""
        charset = part.get_content_charset() or "utf-8"
        try:
            text = payload.decode(charset, errors="replace")
        except (LookupError, UnicodeDecodeError):
            try:
                text = payload.decode("utf-8", errors="replace")
            except:
                text = payload.decode("latin-1", errors="replace")
        return html.unescape(text)

    def _extract_body(self, msg) -> str:
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                ctype = part.get_content_type()
                disp = str(part.get("Content-Disposition", ""))
                if ctype == "text/plain" and "attachment" not in disp.lower():
                    body = self._decode_payload(part)
                    break
            if not body:
                for part in msg.walk():
                    ctype = part.get_content_type()
                    disp = str(part.get("Content-Disposition", ""))
                    if ctype == "text/html" and "attachment" not in disp.lower():
                        html_content = self._decode_payload(part)
                        body = html_to_text(html_content)
                        break
        else:
            body = self._decode_payload(msg)

        if body and ("<html" in body.lower() or "<body" in body.lower() or "<div" in body.lower()):
            body = html_to_text(body)

        return body[:4000] if body else ""

    def _fetch_by_uid(self, mail, uid: str):
        status, msg_data = mail.uid("fetch", uid, "(BODY.PEEK[])")
        if status != "OK" or not msg_data:
            return None

        raw_email = None
        for response_part in msg_data:
            if isinstance(response_part, tuple) and len(response_part) > 1:
                raw_email = response_part[1]
                break

        if raw_email is None:
            return None

        if isinstance(raw_email, str):
            raw_email = raw_email.encode()

        return email_lib.message_from_bytes(raw_email)

    def _parse_message(self, msg, uid: str) -> EmailMessage:
        subject_raw = msg.get("Subject", "")
        subject_parts = decode_header(subject_raw)
        subject = ""
        for part, enc in subject_parts:
            if isinstance(part, bytes):
                subject += part.decode(enc or "utf-8", errors="replace")
            else:
                subject += str(part)
        subject = html.unescape(subject)

        sender = msg.get("From", "")
        from_addr = parse_email_address(sender)
        date = msg.get("Date", "")
        body = self._extract_body(msg)

        return EmailMessage(
            id=uid,
            provider=self.provider_name,
            from_addr=from_addr,
            subject=subject,
            body=body,
            date=date,
        )

    def _is_otp_email(self, from_addr: str, subject: str) -> tuple:
        from_lower = from_addr.lower()
        subject_lower = subject.lower()

        for pattern in OTP_FROM_PATTERNS:
            if pattern in from_lower:
                return True, f"From address contains '{pattern}'"

        for pattern in OTP_SUBJECT_PATTERNS:
            if pattern in subject_lower:
                return True, f"Subject contains '{pattern}'"

        return False, ""

    def debug_status(self) -> DebugStatus:
        self._require_creds()
        mail = imaplib.IMAP4_SSL(IMAP_SERVER)
        mail.login(self.email, self.password)

        status, folders_raw = mail.list()
        folders = []
        if status == "OK":
            for f in folders_raw:
                if isinstance(f, bytes):
                    folders.append(f.decode(errors="ignore"))

        mail.select("INBOX", readonly=True)
        _, all_msgs = mail.uid("search", None, "ALL")
        _, unseen_msgs = mail.uid("search", None, "UNSEEN")
        inbox_total = len(all_msgs[0].split()) if all_msgs[0] else 0
        inbox_unseen = len(unseen_msgs[0].split()) if unseen_msgs[0] else 0

        junk_total = 0
        junk_unseen = 0
        try:
            mail.select("Junk", readonly=True)
            _, all_junk = mail.uid("search", None, "ALL")
            _, unseen_junk = mail.uid("search", None, "UNSEEN")
            junk_total = len(all_junk[0].split()) if all_junk[0] else 0
            junk_unseen = len(unseen_junk[0].split()) if unseen_junk[0] else 0
        except:
            pass

        mail.logout()

        return DebugStatus(
            connection="OK",
            email=self.email,
            folders=folders,
            inbox_total=inbox_total,
            inbox_unseen=inbox_unseen,
            junk_total=junk_total,
            junk_unseen=junk_unseen,
        )

    def queue_next(self, folder: str = "inbox") -> Optional[EmailMessage]:
        mailbox = "Junk" if folder.lower() == "spam" else "INBOX"
        mail = self._connect(mailbox, readonly=True)

        status, uids = mail.uid("search", None, "UNSEEN")

        if status != "OK" or not uids or not uids[0]:
            mail.logout()
            return None

        uid_list = uids[0].split()
        if not uid_list:
            mail.logout()
            return None

        latest_uid = uid_list[-1].decode() if isinstance(uid_list[-1], bytes) else str(uid_list[-1])

        msg = self._fetch_by_uid(mail, latest_uid)
        mail.logout()

        if not msg:
            return None

        return self._parse_message(msg, latest_uid)

    def _resolve_folder(self, folder: str) -> str:
        folder_lower = folder.lower()
        folder_map = {
            "inbox": "INBOX",
            "spam": "Junk",
            "junk": "Junk",
            "sent": "Sent Messages",
            "drafts": "Drafts",
            "trash": "Deleted Messages",
            "lixeira": "Lixeira",
            "archive": "Archive",
            "notes": "Notes",
        }
        return folder_map.get(folder_lower, folder)

    def list_emails(
        self, 
        folder: str = "inbox", 
        limit: int = 50, 
        date_start: datetime = None, 
        date_end: datetime = None,
        unread_only: bool = False
    ) -> List[EmailMessage]:
        """
        Robust email listing using UID-based pagination with INTERNALDATE filtering.
        Uses AppleIMAPClient for reliable iCloud sync with unseen boost.
        """
        self._require_creds()
        mailbox = self._resolve_folder(folder)
        
        client = AppleIMAPClient(
            host=IMAP_SERVER,
            username=self.email,
            password=self.password
        )
        
        try:
            client.connect()
            
            messages = client.fetch_messages(
                folder=mailbox,
                start_utc=date_start,
                end_utc=date_end,
                limit=limit,
                buffer_days=2,
                unseen_boost=True,
                batch_size=150
            )
            
            emails = []
            for msg_data in messages:
                uid = msg_data.get("id", "")
                
                if unread_only and not msg_data.get("unseen", False):
                    continue
                
                email_msg = EmailMessage(
                    id=uid,
                    provider=self.provider_name,
                    from_addr=msg_data.get("from", ""),
                    subject=msg_data.get("subject", ""),
                    body=msg_data.get("preview", ""),
                    date=msg_data.get("date", msg_data.get("date_header", "")),
                )
                email_msg.folder = folder
                email_msg.unread = msg_data.get("unseen", False)
                emails.append(email_msg)
            
            return emails
            
        finally:
            client.close()

    def get_folder_stats(self, folder: str = "inbox") -> dict:
        self._require_creds()
        mailbox = self._resolve_folder(folder)
        client = AppleIMAPClient(
            host=IMAP_SERVER,
            username=self.email,
            password=self.password
        )
        try:
            client.connect()
            return client.get_folder_stats(mailbox)
        finally:
            client.close()

    def get_message(self, message_id: str) -> Optional[EmailMessage]:
        mail = self._connect("INBOX", readonly=True)
        msg = self._fetch_by_uid(mail, message_id)
        mail.logout()

        if not msg:
            return None

        return self._parse_message(msg, message_id)

    def suggest_reply(self, message_id: str) -> dict:
        email_msg = self.get_message(message_id)
        if not email_msg:
            raise Exception(f"Email UID {message_id} not found")

        skip, reason = self._is_otp_email(email_msg.from_addr, email_msg.subject)
        if skip:
            return {"skip": True, "reason": reason, "uid": message_id}

        suggestion = draft_reply(
            email_msg.from_addr,
            email_msg.subject,
            email_msg.body,
        )

        set_item(f"apple_draft:{message_id}", suggestion["raw"])

        return {
            "uid": message_id,
            "from": email_msg.from_addr,
            "subject": email_msg.subject,
            "suggested_reply": suggestion.get("reply", suggestion["raw"]),
            "raw": suggestion["raw"],
        }

    def send(self, message_id: str) -> dict:
        draft = get_item(f"apple_draft:{message_id}")
        if not draft:
            raise Exception(f"No draft found for UID {message_id}")

        email_msg = self.get_message(message_id)
        if not email_msg:
            raise Exception(f"Original email UID {message_id} not found")

        self._require_creds()
        msg = MIMEText(draft, "plain", "utf-8")
        msg["From"] = self.email
        msg["To"] = email_msg.from_addr
        msg["Subject"] = "Re: " + (email_msg.subject or "")

        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(self.email, self.password)
        server.send_message(msg)
        server.quit()

        try:
            self.mark_read(message_id)
        except:
            pass

        return {"ok": True, "uid": message_id}

    def mark_read(self, message_id: str) -> dict:
        mail = self._connect("INBOX", readonly=False)
        status, _ = mail.uid("store", message_id, "+FLAGS", "(\\Seen)")
        mail.logout()

        if status != "OK":
            raise Exception(f"Failed to mark UID {message_id} as read")

        return {"ok": True, "uid": message_id}

    def mark_unread(self, message_id: str) -> dict:
        mail = self._connect("INBOX", readonly=False)
        status, _ = mail.uid("store", message_id, "-FLAGS", "(\\Seen)")
        mail.logout()

        if status != "OK":
            raise Exception(f"Failed to mark UID {message_id} as unread")

        return {"ok": True, "uid": message_id}

    def delete(self, message_id: str) -> dict:
        import logging
        logging.info(f"[APPLE DELETE] Starting delete for UID: {message_id}")
        
        mail = self._connect("INBOX", readonly=False)
        
        # iCloud uses Trash folder - try to move there first
        trash_folders = ["Trash", "Deleted Messages", "INBOX.Trash", "[Gmail]/Trash"]
        trash_folder = None
        
        # Find the trash folder
        try:
            status, folders_raw = mail.list()
            if status == "OK":
                for f in folders_raw:
                    if isinstance(f, bytes):
                        decoded = f.decode(errors="ignore").lower()
                        if "trash" in decoded or "deleted" in decoded or "lixeira" in decoded:
                            # Extract folder name
                            parts = f.decode(errors="ignore").split('"')
                            if len(parts) >= 2:
                                trash_folder = parts[-2]
                                logging.info(f"[APPLE DELETE] Found trash folder: {trash_folder}")
                                break
        except Exception as e:
            logging.warning(f"[APPLE DELETE] Could not list folders: {e}")
        
        # Method 1: Try to COPY to Trash then delete from INBOX
        if trash_folder:
            try:
                copy_status, copy_result = mail.uid("copy", message_id, trash_folder)
                logging.info(f"[APPLE DELETE] Copy to trash status: {copy_status}")
                if copy_status == "OK":
                    # Mark as deleted in INBOX
                    mail.uid("store", message_id, "+FLAGS", "(\\Deleted)")
                    mail.expunge()
                    mail.logout()
                    logging.info(f"[APPLE DELETE] Successfully moved to trash")
                    return {"ok": True, "uid": message_id, "method": "move_to_trash"}
            except Exception as e:
                logging.warning(f"[APPLE DELETE] Copy to trash failed: {e}")
        
        # Method 2: Direct delete with expunge
        status, result = mail.uid("store", message_id, "+FLAGS", "(\\Deleted)")
        logging.info(f"[APPLE DELETE] Store status: {status}, result: {result}")
        
        if status != "OK":
            mail.logout()
            raise Exception(f"Failed to mark UID {message_id} as deleted: {result}")
        
        exp_status, exp_result = mail.expunge()
        logging.info(f"[APPLE DELETE] Expunge status: {exp_status}, result: {exp_result}")
        
        mail.logout()
        
        return {"ok": True, "uid": message_id, "method": "direct_delete"}

    def list_folders(self) -> List[str]:
        self._require_creds()
        mail = imaplib.IMAP4_SSL(IMAP_SERVER)
        mail.login(self.email, self.password)
        status, folders_raw = mail.list()
        folders = []
        if status == "OK":
            for f in folders_raw:
                if isinstance(f, bytes):
                    decoded = f.decode(errors="ignore")
                    if '"' in decoded:
                        parts = decoded.split('"')
                        if len(parts) >= 2:
                            folders.append(parts[-2])
        mail.logout()
        return folders

    def send_reply(self, message_id: str, body: str) -> dict:
        email_msg = self.get_message(message_id)
        if not email_msg:
            raise Exception(f"Original email UID {message_id} not found")

        self._require_creds()
        msg = MIMEText(body, "plain", "utf-8")
        msg["From"] = self.email
        msg["To"] = email_msg.from_addr
        msg["Subject"] = "Re: " + (email_msg.subject or "")
        msg["Date"] = email_lib.utils.formatdate(localtime=True)
        msg["Message-ID"] = email_lib.utils.make_msgid()

        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(self.email, self.password)
        server.send_message(msg)
        server.quit()

        self._save_to_sent(msg)

        try:
            self.mark_read(message_id)
        except:
            pass

        return {"ok": True, "uid": message_id}

    def _save_to_sent(self, msg):
        """Save a copy of the sent message to the Sent folder via IMAP"""
        try:
            imap = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
            imap.login(self.email, self.password)
            imap.select('"Sent Messages"')
            imap.append('"Sent Messages"', "\\Seen", None, msg.as_bytes())
            imap.logout()
        except Exception as e:
            print(f"Warning: Could not save to Sent folder: {e}")

    def compose_email(self, to: str, subject: str, body: str) -> dict:
        """Send a new email (not a reply)"""
        self._require_creds()
        
        msg = MIMEText(body, "plain", "utf-8")
        msg["From"] = self.email
        msg["To"] = to
        msg["Subject"] = subject
        msg["Date"] = email_lib.utils.formatdate(localtime=True)
        msg["Message-ID"] = email_lib.utils.make_msgid()

        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(self.email, self.password)
        server.send_message(msg)
        server.quit()

        self._save_to_sent(msg)

        return {"ok": True, "to": to, "subject": subject}
