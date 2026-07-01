from abc import ABC, abstractmethod
from typing import AsyncIterator

class VoiceConverter(ABC):
    """
    Abstract base class for all pluggable voice conversion engines.
    """

    @abstractmethod
    async def convert_stream(self, in_audio: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        """
        Takes an asynchronous iterator of raw 16-bit mono 16kHz PCM audio bytes,
        performs voice conversion, and yields the converted raw PCM audio bytes.
        """
        pass
