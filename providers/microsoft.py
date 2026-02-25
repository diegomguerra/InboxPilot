import os
from typing import Optional

from providers.base import EmailProvider, EmailMessage, DebugStatus
from utils.text import html_to_text
from graph import graph_get, graph_post, graph_patch, graph_delete
from store import get_item, set_item
from llm import draft_reply


class MicrosoftProvider(EmailProvider):
    provider_name = "microsoft"

    def __init__(self, get_token_func):
        self.get_token = get_token_func

    def debug_status(self) -> DebugStatus:
        token = self.get_token()
        
        user = graph_get(token, "/me", params={"$select": "mail,displayName"})
        email = user.get("mail", user.get("userPrincipalName", "unknown"))

        folders_data = graph_get(token, "/me/mailFolders", params={"$top": "50"})
        folders = [f.get("displayName", "") for f in folders_data.get("value", [])]

        inbox_data = graph_get(
            token,
            "/me/mailFolders/inbox",
            params={"$select": "totalItemCount,unreadItemCount"}
        )

        junk_data = {"totalItemCount": 0, "unreadItemCount": 0}
        try:
            junk_data = graph_get(
                token,
                "/me/mailFolders/junkemail",
                params={"$select": "totalItemCount,unreadItemCount"}
            )
        except:
            pass

        return DebugStatus(
            connection="OK",
            email=email,
            folders=folders,
            inbox_total=inbox_data.get("totalItemCount", 0),
            inbox_unseen=inbox_data.get("unreadItemCount", 0),
            junk_total=junk_data.get("totalItemCount", 0),
            junk_unseen=junk_data.get("unreadItemCount", 0),
        )

    def queue_next(self, folder: str = "inbox") -> Optional[EmailMessage]:
        token = self.get_token()
        folder_path = "junkemail" if folder.lower() in ["spam", "junk"] else "inbox"

        data = graph_get(
            token,
            f"/me/mailFolders/{folder_path}/messages",
            params={
                "$top": "1",
                "$orderby": "receivedDateTime asc",
                "$filter": "isRead eq false",
                "$select": "id,subject,from,receivedDateTime,body",
            },
        )

        items = data.get("value", [])
        if not items:
            return None

        m = items[0]
        body_content = (m.get("body") or {}).get("content", "")
        body_type = (m.get("body") or {}).get("contentType", "text")

        if body_type.lower() == "html":
            body_content = html_to_text(body_content)

        return EmailMessage(
            id=m["id"],
            provider=self.provider_name,
            from_addr=(m.get("from") or {}).get("emailAddress", {}).get("address", ""),
            subject=m.get("subject", ""),
            body=body_content[:4000] if body_content else "",
            date=m.get("receivedDateTime", ""),
        )

    def get_message(self, message_id: str) -> Optional[EmailMessage]:
        token = self.get_token()

        try:
            m = graph_get(
                token,
                f"/me/messages/{message_id}",
                params={"$select": "id,subject,from,receivedDateTime,body"}
            )
        except:
            return None

        body_content = (m.get("body") or {}).get("content", "")
        body_type = (m.get("body") or {}).get("contentType", "text")

        if body_type.lower() == "html":
            body_content = html_to_text(body_content)

        return EmailMessage(
            id=m["id"],
            provider=self.provider_name,
            from_addr=(m.get("from") or {}).get("emailAddress", {}).get("address", ""),
            subject=m.get("subject", ""),
            body=body_content[:4000] if body_content else "",
            date=m.get("receivedDateTime", ""),
        )

    def suggest_reply(self, message_id: str) -> dict:
        email_msg = self.get_message(message_id)
        if not email_msg:
            raise Exception(f"Email {message_id} not found")

        suggestion = draft_reply(
            email_msg.from_addr,
            email_msg.subject,
            email_msg.body,
        )

        set_item(f"draft:{message_id}", suggestion["raw"])

        return {
            "id": message_id,
            "from": email_msg.from_addr,
            "subject": email_msg.subject,
            "suggested_reply": suggestion.get("reply", suggestion["raw"]),
            "raw": suggestion["raw"],
        }

    def send(self, message_id: str) -> dict:
        token = self.get_token()
        raw = get_item(f"draft:{message_id}")
        if not raw:
            raise Exception("No draft found")

        email_msg = self.get_message(message_id)
        if not email_msg:
            raise Exception(f"Original email {message_id} not found")

        payload = {
            "message": {
                "subject": f"RE: {email_msg.subject}",
                "body": {"contentType": "Text", "content": raw},
                "toRecipients": [{"emailAddress": {"address": email_msg.from_addr}}],
            },
            "saveToSentItems": True,
        }

        graph_post(token, "/me/sendMail", payload)
        return {"ok": True, "id": message_id}

    def mark_read(self, message_id: str) -> dict:
        token = self.get_token()
        graph_patch(
            token,
            f"/me/messages/{message_id}",
            {"isRead": True}
        )
        return {"ok": True, "id": message_id}

    def delete(self, message_id: str) -> dict:
        token = self.get_token()
        graph_delete(token, f"/me/messages/{message_id}")
        return {"ok": True, "id": message_id}
