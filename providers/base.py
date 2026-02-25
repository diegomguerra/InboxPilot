from abc import ABC, abstractmethod
from typing import Optional, List, Dict, Any
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class EmailMessage:
    id: str
    provider: str
    from_addr: str
    subject: str
    body: str
    date: str
    folder: str = "inbox"

    def to_dict(self, include_snippet: bool = True) -> dict:
        result = {
            "provider": self.provider,
            "id": self.id,
            "from": self.from_addr,
            "subject": self.subject,
            "body": self.body,
            "date": self.date,
            "folder": self.folder,
        }
        if include_snippet:
            result["snippet"] = self.body[:160].strip() if self.body else ""
        return result


@dataclass
class DebugStatus:
    connection: str
    email: str
    folders: list
    inbox_total: int
    inbox_unseen: int
    junk_total: int = 0
    junk_unseen: int = 0
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        result = {
            "connection": self.connection,
            "email": self.email,
            "folders": self.folders,
            "inbox_total": self.inbox_total,
            "inbox_unseen": self.inbox_unseen,
            "junk_total": self.junk_total,
            "junk_unseen": self.junk_unseen,
        }
        if self.extra:
            result.update(self.extra)
        return result


class EmailProvider(ABC):
    provider_name: str = "base"

    @abstractmethod
    def debug_status(self) -> DebugStatus:
        pass

    @abstractmethod
    def queue_next(self, folder: str = "inbox") -> Optional[EmailMessage]:
        pass

    @abstractmethod
    def get_message(self, message_id: str) -> Optional[EmailMessage]:
        pass

    @abstractmethod
    def suggest_reply(self, message_id: str) -> dict:
        pass

    @abstractmethod
    def send(self, message_id: str) -> dict:
        pass

    @abstractmethod
    def mark_read(self, message_id: str) -> dict:
        pass

    @abstractmethod
    def delete(self, message_id: str) -> dict:
        pass

    def list_emails(
        self, 
        folder: str = "inbox", 
        limit: int = 50, 
        date_start: datetime = None, 
        date_end: datetime = None,
        unread_only: bool = False
    ) -> List[EmailMessage]:
        return []
