import asyncio
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import time

from app.graph import Turn


class ConversationCapacityError(Exception):
    pass


class ConversationBusyError(Exception):
    pass


@dataclass
class Conversation:
    turns: deque[Turn]
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0
    pending_email: dict | None = None


class ConversationStore:
    """Bounded RAM-only turns and locks for one app event loop, not user identity."""

    def __init__(self, max_conversations=64, max_turns=6, *, clock=time.time):
        if not 1 <= max_conversations <= 128 or not 1 <= max_turns <= 12:
            raise ValueError("Memory limits must be 1..128 conversations and 1..12 complete turns.")
        self.max_conversations, self.max_turns = max_conversations, max_turns
        self.entries: dict[str, Conversation] = {}
        self.clock = clock

    @asynccontextmanager
    async def use(self, conversation_id):
        # Reservation has no await: active and queued users pin the same entry.
        entry = self.entries.get(conversation_id)
        if entry is None:
            if len(self.entries) >= self.max_conversations:
                raise ConversationCapacityError
            entry = Conversation(deque(maxlen=self.max_turns))
            self.entries[conversation_id] = entry
        entry.users += 1
        try:
            async with entry.lock:
                if entry.pending_email and self.clock() >= entry.pending_email["expires_at"]:
                    entry.pending_email = None
                yield entry
        finally:
            entry.users -= 1
            # A failed new conversation must not permanently consume capacity.
            if not entry.users and not entry.turns:
                del self.entries[conversation_id]

    def reset(self, conversation_id) -> bool:
        entry = self.entries.get(conversation_id)
        if entry is None:
            return False
        if entry.users:
            raise ConversationBusyError
        del self.entries[conversation_id]
        return True
