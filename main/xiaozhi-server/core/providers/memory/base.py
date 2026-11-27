from config.log_safety import safe_diagnostic
from abc import ABC, abstractmethod
from config.logger import setup_logging

TAG = __name__
logger = setup_logging()


class MemoryProviderBase(ABC):
    managed = False

    def __init__(self, config):
        self.config = config
        self.role_id = None

    def set_llm(self, llm):
        self.llm = llm

    @abstractmethod
    async def save_memory(self, msgs, session_id=None):
        """Save a new memory for specific role and return memory ID"""
        safe_diagnostic()

    @abstractmethod
    async def query_memory(self, query: str) -> str:
        """Query memories for specific role based on similarity"""
        return "please implement query method"

    def init_memory(self, role_id, llm, **kwargs):
        self.role_id = role_id
        self.llm = llm

    async def ingest_turn(self, event):
        """Optional durable per-turn ingest; legacy providers retain save_memory."""
        return None

    async def control(self, text, event):
        """Optional application-owned memory operations, never arbitrary tools."""
        return None
