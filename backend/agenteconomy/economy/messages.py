"""Messages between agents. Off-chain and untrusted: nothing here can move coins or make an agent do anything.

Posting a message schedules nothing. An agent sees it only if it chooses to read its inbox on its own turn, and then
only as data.
"""

import re
from dataclasses import asdict, dataclass
from typing import Any

MAX_MESSAGE_CHARS = 280
INBOX_SIZE = 5

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202f\u2060-\u206f\ufeff]")


def clean_text(value: Any, limit: int) -> str:
    """Short plain text: no control or invisible characters, single spaces."""
    if not isinstance(value, str):
        return ""
    return " ".join(_CONTROL.sub(" ", value).split())[:limit]


@dataclass(frozen=True)
class Message:
    id: int
    day: int
    time: int
    sender: str
    to: str
    text: str
    attack_id: str | None = None  # set when this is from the fixed attack set, for measuring


class MessageBoard:
    def __init__(self):
        self.messages: list[Message] = []
        self._read: set[int] = set()

    def post(self, *, day: int, time: int, sender: str, to: str, text: str, attack_id: str | None = None) -> Message:
        message = Message(len(self.messages), day, time, sender, to, clean_text(text, MAX_MESSAGE_CHARS), attack_id)
        self.messages.append(message)
        return message

    def unread_for(self, address: str) -> list[Message]:
        return [message for message in self.messages if message.to == address and message.id not in self._read]

    def inbox(self, address: str) -> tuple[Message, ...]:
        """The newest unread messages, so a flood can't grow a prompt or bury what's recent."""
        return tuple(self.unread_for(address)[-INBOX_SIZE:])

    def mark_read(self, message_ids: list[int]) -> None:
        self._read.update(message_ids)

    def was_read(self, message_id: int) -> bool:
        return message_id in self._read

    def to_dict(self) -> dict[str, Any]:
        return {"messages": [asdict(message) for message in self.messages], "read": sorted(self._read)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MessageBoard:
        board = cls()
        board.messages = [Message(**message) for message in data["messages"]]
        board._read = set(data["read"])
        return board
