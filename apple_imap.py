# apple_imap.py
import imaplib
import email
from email.header import decode_header, make_header
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional
import html
import ssl
import re

try:
    from zoneinfo import ZoneInfo
    SAO_PAULO_TZ = ZoneInfo("America/Sao_Paulo")
except ImportError:
    SAO_PAULO_TZ = timezone(timedelta(hours=-3))

HEADER_FIELDS = (
    "FROM SUBJECT DATE MESSAGE-ID TO CC REPLY-TO LIST-ID "
    "AUTO-SUBMITTED PRECEDENCE X-AUTO-RESPONSE-SUPPRESS"
)

def _decode_mime(s: Optional[str]) -> str:
    if not s:
        return ""
    try:
        return str(make_header(decode_header(s)))
    except Exception:
        return s

def _clean_text(s: str) -> str:
    if not s:
        return ""
    s = html.unescape(s)
    s = re.sub(r"\s+\n", "\n", s)
    s = re.sub(r"[ \t]+", " ", s)
    return s.strip()

def _parse_internaldate(internaldate: str) -> datetime:
    try:
        tup = imaplib.Internaldate2tuple(internaldate)
        if tup:
            return datetime(*tup[:6], tzinfo=timezone.utc).astimezone(timezone.utc)
    except Exception:
        pass
    try:
        import email.utils
        dt = email.utils.parsedate_to_datetime(internaldate)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)

def _classify_email(from_addr: str, subj: str, headers: Dict[str, str]) -> str:
    f = (from_addr or "").lower()
    s = (subj or "").lower()

    otp_keywords = ["código", "codigo", "verification", "verify", "otp", "confirmação", "confirmacao"]
    if any(k in s for k in otp_keywords) or "one-time" in s:
        return "otp"

    auto = headers.get("auto-submitted", "").lower()
    precedence = headers.get("precedence", "").lower()
    if "no-reply" in f or "noreply" in f or auto or precedence in ("bulk", "junk", "list"):
        return "automated"

    if headers.get("list-id") or "unsubscribe" in s:
        return "newsletter"

    return "human"

class AppleIMAPClient:
    """
    Robust iCloud IMAP fetch:
    - Uses UID list + descending batches
    - Fetches INTERNALDATE (more reliable than header Date on iCloud)
    - Applies local time filters (server date filters are flaky for "today")
    """

    def __init__(self, host: str, username: str, password: str, port: int = 993):
        self.host = host
        self.username = username
        self.password = password
        self.port = port
        self.conn: Optional[imaplib.IMAP4_SSL] = None

    def connect(self):
        context = ssl.create_default_context()
        self.conn = imaplib.IMAP4_SSL(self.host, self.port, ssl_context=context)
        self.conn.login(self.username, self.password)

    def close(self):
        try:
            if self.conn:
                self.conn.logout()
        except Exception:
            pass
        self.conn = None

    def select_folder(self, folder: str):
        assert self.conn
        typ, data = self.conn.select(folder, readonly=False)
        if typ != "OK":
            raise RuntimeError(f"Could not select folder {folder}: {data}")

    def _uid_search_all(self) -> List[int]:
        assert self.conn
        typ, data = self.conn.uid("SEARCH", None, "ALL")
        if typ != "OK":
            return []
        raw = data[0].decode("utf-8", errors="ignore").strip()
        if not raw:
            return []
        return [int(x) for x in raw.split() if x.isdigit()]

    def _uid_search_unseen(self) -> List[int]:
        assert self.conn
        typ, data = self.conn.uid("SEARCH", None, "UNSEEN")
        if typ != "OK":
            return []
        raw = data[0].decode("utf-8", errors="ignore").strip()
        if not raw:
            return []
        return [int(x) for x in raw.split() if x.isdigit()]

    def get_folder_stats(self, folder: str = "INBOX") -> Dict:
        assert self.conn
        self.select_folder(folder)
        all_uids = self._uid_search_all()
        unseen_uids = self._uid_search_unseen()
        total = len(all_uids)
        unseen = len(unseen_uids)
        return {
            "total": total,
            "unseen": unseen,
            "read": total - unseen,
        }

    def fetch_messages(
        self,
        folder: str = "INBOX",
        start_utc: Optional[datetime] = None,
        end_utc: Optional[datetime] = None,
        limit: int = 200,
        buffer_days: int = 2,
        unseen_boost: bool = True,
        batch_size: int = 150,
    ) -> List[Dict]:
        """
        Returns messages in range [start_utc, end_utc], filtered locally,
        plus optional unseen boost (ensures "unread today" doesn't vanish).
        Unseen emails within range are always prioritized.
        """
        assert self.conn
        self.select_folder(folder)

        all_uids = self._uid_search_all()
        if not all_uids:
            return []

        all_uids.sort(reverse=True)

        unseen_uids = set(self._uid_search_unseen()) if unseen_boost else set()

        now_utc = datetime.now(timezone.utc)
        if end_utc is None:
            end_utc = now_utc
        elif end_utc.tzinfo is None:
            # Interpret naive datetimes as America/Sao_Paulo and convert to UTC
            end_utc = end_utc.replace(tzinfo=SAO_PAULO_TZ).astimezone(timezone.utc)
        
        if start_utc is None:
            start_utc = end_utc - timedelta(days=7)
        elif start_utc.tzinfo is None:
            # Interpret naive datetimes as America/Sao_Paulo and convert to UTC
            start_utc = start_utc.replace(tzinfo=SAO_PAULO_TZ).astimezone(timezone.utc)

        cutoff_utc = start_utc - timedelta(days=buffer_days)

        results: List[Dict] = []
        seen_keys = set()

        passed_cutoff_batches = 0

        for i in range(0, len(all_uids), batch_size):
            if len(results) >= limit and passed_cutoff_batches >= 2:
                break

            batch = all_uids[i:i + batch_size]
            if not batch:
                break

            uid_set = ",".join(str(u) for u in batch)

            fetch_items = f"(UID INTERNALDATE FLAGS RFC822.SIZE BODY.PEEK[HEADER.FIELDS ({HEADER_FIELDS})])"
            typ, data = self.conn.uid("FETCH", uid_set, fetch_items)
            if typ != "OK" or not data:
                continue

            batch_old = False

            for item in data:
                if not isinstance(item, tuple) or len(item) < 2:
                    continue

                meta = item[0].decode("utf-8", errors="ignore")
                raw_headers = item[1]

                m = re.search(r"UID\s+(\d+)", meta)
                if not m:
                    continue
                uid = int(m.group(1))
                key = f"apple:{uid}"
                if key in seen_keys:
                    continue

                im = re.search(r'INTERNALDATE\s+"([^"]+)"', meta)
                internaldate = im.group(1) if im else ""
                dt_utc = _parse_internaldate(internaldate)

                flags = []
                fm = re.search(r"FLAGS\s+\(([^)]*)\)", meta)
                if fm:
                    flags = [f.strip() for f in fm.group(1).split() if f.strip()]

                msg = email.message_from_bytes(raw_headers)
                from_addr = _decode_mime(msg.get("From", ""))
                subject = _decode_mime(msg.get("Subject", ""))
                date_hdr = _decode_mime(msg.get("Date", ""))

                headers_norm = {
                    "auto-submitted": (msg.get("Auto-Submitted", "") or ""),
                    "precedence": (msg.get("Precedence", "") or ""),
                    "list-id": (msg.get("List-Id", "") or ""),
                    "x-auto-response-suppress": (msg.get("X-Auto-Response-Suppress", "") or ""),
                }

                classification = _classify_email(from_addr, subject, headers_norm)

                in_range = (start_utc <= dt_utc <= end_utc)

                if in_range:
                    seen_keys.add(key)
                    results.append({
                        "provider": "apple",
                        "id": str(uid),
                        "key": key,
                        "from": _clean_text(from_addr),
                        "subject": _clean_text(subject),
                        "date": dt_utc.isoformat(),
                        "dt_utc": dt_utc,  # Store parsed datetime for priority sorting
                        "date_header": date_hdr,
                        "flags": flags,
                        "unseen": (uid in unseen_uids) or ("\\Seen" not in flags),
                        "class": classification,
                        "preview": "",
                    })

                if dt_utc < cutoff_utc:
                    batch_old = True

            if batch_old:
                passed_cutoff_batches += 1

        in_range_unseen = []
        in_range_read = []
        
        for r in results:
            is_unseen = r.get("unseen", False)
            if is_unseen:
                in_range_unseen.append(r)
            else:
                in_range_read.append(r)
        
        in_range_unseen.sort(key=lambda x: x["date"], reverse=True)
        in_range_read.sort(key=lambda x: x["date"], reverse=True)
        
        final = in_range_unseen + in_range_read
        return final[:limit]

    def fetch_preview(self, uid: int, max_bytes: int = 4000) -> str:
        """
        Fetch a lightweight text preview by getting the first text/plain or text/html MIME part.
        Properly decodes quoted-printable and base64 content.
        """
        assert self.conn
        try:
            typ, data = self.conn.uid("FETCH", str(uid), "(BODY.PEEK[]<0.8000>)")
            if typ != "OK" or not data:
                return ""
            raw_bytes = b""
            for item in data:
                if isinstance(item, tuple) and len(item) >= 2:
                    raw_bytes = item[1]
                    if isinstance(raw_bytes, bytes):
                        break
            if not raw_bytes:
                return ""

            import quopri, base64
            msg = email.message_from_bytes(raw_bytes)

            for part in (msg.walk() if msg.is_multipart() else [msg]):
                ctype = part.get_content_type()
                if ctype not in ("text/plain", "text/html"):
                    continue
                try:
                    payload = part.get_payload(decode=True)
                    if not payload:
                        continue
                    charset = part.get_content_charset() or "utf-8"
                    text = payload.decode(charset, errors="ignore")
                    if ctype == "text/html":
                        text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
                        text = re.sub(r"<[^>]+>", " ", text)
                        text = html.unescape(text)
                    return _clean_text(text)[:300]
                except Exception:
                    continue
            return ""
        except Exception:
            return ""

    def fetch_body(self, uid: int) -> str:
        """
        Fetch full body only on demand (keeps list fast).
        """
        assert self.conn
        typ, data = self.conn.uid("FETCH", str(uid), "(BODY.PEEK[])")
        if typ != "OK" or not data:
            return ""
        raw = None
        for item in data:
            if isinstance(item, tuple) and len(item) >= 2:
                raw = item[1]
                break
        if not raw:
            return ""
        msg = email.message_from_bytes(raw)

        def extract_text(m: email.message.Message) -> str:
            if m.is_multipart():
                for part in m.walk():
                    ctype = part.get_content_type()
                    disp = str(part.get("Content-Disposition", "")).lower()
                    if ctype == "text/plain" and "attachment" not in disp:
                        try:
                            return part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", errors="ignore")
                        except Exception:
                            pass
                for part in m.walk():
                    ctype = part.get_content_type()
                    disp = str(part.get("Content-Disposition", "")).lower()
                    if ctype == "text/html" and "attachment" not in disp:
                        try:
                            html_txt = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", errors="ignore")
                            return re.sub(r"<[^>]+>", " ", html_txt)
                        except Exception:
                            pass
                return ""
            else:
                ctype = m.get_content_type()
                payload = m.get_payload(decode=True) or b""
                charset = m.get_content_charset() or "utf-8"
                try:
                    txt = payload.decode(charset, errors="ignore")
                except Exception:
                    txt = payload.decode("utf-8", errors="ignore")
                if ctype == "text/html":
                    txt = re.sub(r"<[^>]+>", " ", txt)
                return txt

        return _clean_text(extract_text(msg))
