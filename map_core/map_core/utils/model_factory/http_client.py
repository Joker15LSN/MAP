"""Custom Client for LLM and Embedding Models based on ollama.Client."""

import json
import logging
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Generic, Literal, TypeVar, cast, overload

import httpx

T = TypeVar("T")


# Exception Classes
class ClientError(Exception):
    """Base exception for client errors."""


class StreamParseError(ClientError):
    """Raised when streaming data cannot be parsed."""


class APIError(ClientError):
    """Raised when API returns an error response."""


class NetworkError(ClientError):
    """Raised when network request fails."""


# Configuration Management
@dataclass
class ClientConfig:
    """Configuration for HTTP clients."""

    connect_timeout: float = 10.0
    read_timeout: float = 45.0
    write_timeout: float = 30.0
    total_timeout: float = 60.0
    retry_attempts: int = 3
    retry_delay: float = 1.0
    default_headers: dict[str, str] | None = None

    @property
    def httpx_timeout(self) -> httpx.Timeout:
        return httpx.Timeout(
            connect=self.connect_timeout,
            read=self.read_timeout,
            write=self.write_timeout,
            timeout=self.total_timeout,
        )


# Stream Parser Utility
class StreamParser:
    """Utility class for parsing streaming responses."""

    @staticmethod
    def parse_sse_line(line: bytes) -> list[dict]:
        """Parse Server-Sent Events line into list of data objects."""
        data_list = line.decode().strip().split("data:")
        result = []
        for data in data_list:
            if not data or data.strip() == "[DONE]":
                continue
            try:
                parsed = json.loads(data)
                result.append(parsed)
            except json.JSONDecodeError as e:
                raise StreamParseError(f"Failed to parse JSON: {data}") from e
        return result

    @staticmethod
    def validate_response_part(part: dict) -> None:
        """Validate response part for errors."""
        if err := part.get("error"):
            raise APIError(f"API returned error: {err}")


# Logging Mixin
class ClientMixin:
    """Mixin for providing logging capabilities."""

    @property
    def logger(self) -> logging.Logger:
        if not hasattr(self, "_logger"):
            self._logger = logging.getLogger(self.__class__.__name__)
        return self._logger


# HTTP Client
class HTTPClient(ClientMixin):
    """HTTP client with both sync and async functionality."""

    def __init__(self, config: ClientConfig | None = None):
        self.config = config or ClientConfig()

    # Async methods
    async def arequest_raw(self, **kwargs) -> dict:
        """Non-streaming async request."""
        try:
            async with httpx.AsyncClient(timeout=self.config.httpx_timeout) as client:
                response = await client.request(**kwargs)
                response.raise_for_status()
                return response.json()
        except httpx.HTTPStatusError as e:
            error_msg = e.response.text
            self.logger.error(
                f"HTTP error: {e.response.status_code}, response: {error_msg}"
            )
            raise NetworkError(
                f"HTTP error {e.response.status_code}: {error_msg}"
            ) from e
        except httpx.RequestError as e:
            self.logger.error(f"Async request failed: {e}")
            raise NetworkError(f"Async request failed: {e}") from e
        except Exception as e:
            self.logger.error(f"Unexpected error in async request: {e}")
            raise ClientError(f"Unexpected error: {e}") from e

    async def astream_request(self, cls: type[T] | None, **kwargs) -> AsyncIterator[T]:
        """Streaming async request."""
        if cls is None:
            cls = cast(type[T], dict)
        try:
            async with httpx.AsyncClient(timeout=self.config.httpx_timeout) as client:
                async with client.stream(**kwargs) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        try:
                            parts = StreamParser.parse_sse_line(line.encode())
                            for part in parts:
                                StreamParser.validate_response_part(part)
                                yield cls(**part)
                        except ClientError:
                            raise
                        except Exception as e:
                            self.logger.error(
                                f"Error processing stream line: {line!r}, error: {e}"
                            )
                            raise StreamParseError(
                                f"Stream processing error: {e}"
                            ) from e
        except httpx.HTTPStatusError as e:
            error_msg = (await e.response.aread()).decode()
            self.logger.error(
                f"HTTP stream error: {e.response.status_code}, response: {error_msg}"
            )
            raise NetworkError(
                f"HTTP stream error {e.response.status_code}: {error_msg}"
            ) from e
        except httpx.RequestError as e:
            self.logger.error(f"Async stream request failed: {e}")
            raise NetworkError(f"Async stream request failed: {e}") from e

    async def arequest(
        self, cls: type[T] | None = None, stream: bool = False, **kwargs
    ) -> T | AsyncIterator[T]:
        """Generic async request method supporting both streaming and non-streaming."""
        if cls is None:
            cls = cast(type[T], dict)
        if stream:
            return self.astream_request(cls, **kwargs)
        else:
            response_dict = await self.arequest_raw(**kwargs)
            return cls(**response_dict)

    # Sync methods
    def request_raw(self, **kwargs) -> httpx.Response:
        """Non-streaming sync request."""
        try:
            response = httpx.request(timeout=self.config.httpx_timeout, **kwargs)
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as e:
            error_msg = e.response.text
            self.logger.error(
                f"HTTP error: {e.response.status_code}, response: {error_msg}"
            )
            raise NetworkError(
                f"HTTP error {e.response.status_code}: {error_msg}"
            ) from e
        except httpx.RequestError as e:
            self.logger.error(f"Request error: {e}")
            raise NetworkError(f"Request error: {e}") from e

    def stream_request(self, cls: type[T] | None, **kwargs) -> Iterator[T]:
        """Streaming sync request."""
        if cls is None:
            cls = cast(type[T], dict)
        try:
            with httpx.stream(timeout=self.config.httpx_timeout, **kwargs) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    try:
                        parts = StreamParser.parse_sse_line(line.encode())
                        for part in parts:
                            StreamParser.validate_response_part(part)
                            yield cls(**part)
                    except ClientError:
                        raise
                    except Exception as e:
                        self.logger.error(
                            f"Error processing stream line: {line!r}, error: {e}"
                        )
                        raise StreamParseError(f"Stream processing error: {e}") from e
        except httpx.HTTPStatusError as e:
            error_msg = e.response.read().decode()
            self.logger.error(
                f"HTTP stream error: {e.response.status_code}, response: {error_msg}"
            )
            raise NetworkError(
                f"HTTP stream error {e.response.status_code}: {error_msg}"
            ) from e
        except httpx.RequestError as e:
            self.logger.error(f"Stream request error: {e}")
            raise NetworkError(f"Stream request error: {e}") from e

    def request(
        self, cls: type[T] | None = None, stream: bool = False, **kwargs
    ) -> T | Iterator[T]:
        """Generic sync request method supporting both streaming and non-streaming."""
        if cls is None:
            cls = cast(type[T], dict)
        if stream:
            return self.stream_request(cls, **kwargs)
        else:
            response = self.request_raw(**kwargs)
            return cls(**response.json())


# Client Classes
ChatResponseType = TypeVar("ChatResponseType")
EmbeddingResponseType = TypeVar("EmbeddingResponseType")


@dataclass
class ChatClient(ClientMixin, Generic[ChatResponseType]):
    """Chat client with both sync and async functionality."""

    response_cls: type[ChatResponseType] | None
    http_client: HTTPClient

    def __init__(
        self,
        response_cls: type[ChatResponseType] | None = None,
        config: ClientConfig | None = None,
    ):
        self.response_cls = cast(
            type[ChatResponseType],
            response_cls if response_cls is not None else dict,
        )
        self.http_client = HTTPClient(config)

    # Async methods
    @overload
    async def achat(
        self,
        data: dict,
        url: str = "",
        headers: dict | None = None,
        stream: Literal[False] = False,
    ) -> ChatResponseType: ...

    @overload
    async def achat(
        self,
        data: dict,
        url: str = "",
        headers: dict | None = None,
        stream: Literal[True] = True,
    ) -> AsyncIterator[ChatResponseType]: ...

    @overload
    async def achat(
        self,
        data: dict,
        url: str = "",
        headers: dict | None = None,
        stream: bool = True,
    ) -> ChatResponseType | AsyncIterator[ChatResponseType]: ...

    async def achat(
        self,
        data: dict,
        url: str = "",
        headers: dict | None = None,
        stream: bool = True,
    ) -> ChatResponseType | AsyncIterator[ChatResponseType]:
        """Create a chat response in ASYNC mode. If `stream==True`, return a ChatResponse AsyncIterator."""
        if headers is None:
            headers = {"Content-Type": "application/json"}

        return await self.http_client.arequest(
            self.response_cls,
            method="POST",
            url=url,
            headers=headers,
            data=json.dumps(data),
            stream=stream,
        )

    # Sync methods
    @overload
    def chat(
        self,
        data: dict,
        url: str = "",
        headers: dict | None = None,
        stream: Literal[False] = False,
    ) -> ChatResponseType: ...

    @overload
    def chat(
        self,
        data: dict,
        url: str = "",
        headers: dict | None = None,
        stream: Literal[True] = True,
    ) -> Iterator[ChatResponseType]: ...

    @overload
    def chat(
        self,
        data: dict,
        url: str = "",
        headers: dict | None = None,
        stream: bool = True,
    ) -> ChatResponseType | Iterator[ChatResponseType]: ...

    def chat(
        self,
        data: dict,
        url: str = "",
        headers: dict | None = None,
        stream: bool = True,
    ) -> ChatResponseType | Iterator[ChatResponseType]:
        """Create a chat response. If `stream==True`, return a ChatResponse Iterator."""
        if headers is None:
            headers = {"Content-Type": "application/json"}

        return self.http_client.request(
            self.response_cls,
            method="POST",
            url=url,
            headers=headers,
            data=json.dumps(data),
            stream=stream,
        )


# Legacy alias for backward compatibility
AsyncChatClient = ChatClient


@dataclass
class EmbedClient(ClientMixin, Generic[EmbeddingResponseType]):
    """Embedding client with both sync and async functionality."""

    response_cls: type[EmbeddingResponseType] | None
    http_client: HTTPClient

    def __init__(
        self,
        response_cls: type[EmbeddingResponseType] | None = None,
        config: ClientConfig | None = None,
    ):
        self.response_cls = cast(
            type[EmbeddingResponseType],
            response_cls if response_cls is not None else dict,
        )
        self.http_client = HTTPClient(config)

    # Async methods
    @overload
    async def aembed(
        self,
        data: dict,
        url: str,
        headers: dict | None = None,
        stream: Literal[False] = False,
    ) -> EmbeddingResponseType: ...

    @overload
    async def aembed(
        self,
        data: dict,
        url: str,
        headers: dict | None = None,
        stream: Literal[True] = True,
    ) -> AsyncIterator[EmbeddingResponseType]: ...

    @overload
    async def aembed(
        self,
        data: dict,
        url: str,
        headers: dict | None = None,
        stream: bool = True,
    ) -> EmbeddingResponseType | AsyncIterator[EmbeddingResponseType]: ...

    async def aembed(
        self,
        data: dict,
        url: str,
        headers: dict | None = None,
        stream: bool = True,
    ) -> EmbeddingResponseType | AsyncIterator[EmbeddingResponseType]:
        """Embed given data in ASYNC mode."""
        if headers is None:
            headers = {"Content-Type": "application/json"}

        return await self.http_client.arequest(
            self.response_cls,
            method="POST",
            url=url,
            headers=headers,
            data=json.dumps(data),
            stream=stream,
        )

    # Sync methods
    @overload
    def embed(
        self,
        data: dict,
        url: str,
        headers: dict | None = None,
        stream: Literal[False] = False,
    ) -> EmbeddingResponseType: ...

    @overload
    def embed(
        self,
        data: dict,
        url: str,
        headers: dict | None = None,
        stream: Literal[True] = True,
    ) -> Iterator[EmbeddingResponseType]: ...

    @overload
    def embed(
        self,
        data: dict,
        url: str,
        headers: dict | None = None,
        stream: bool = True,
    ) -> EmbeddingResponseType | Iterator[EmbeddingResponseType]: ...

    def embed(
        self,
        data: dict,
        url: str,
        headers: dict | None = None,
        stream: bool = True,
    ) -> EmbeddingResponseType | Iterator[EmbeddingResponseType]:
        """Embed given data."""
        if headers is None:
            headers = {"Content-Type": "application/json"}

        return self.http_client.request(
            self.response_cls,
            method="POST",
            url=url,
            headers=headers,
            data=json.dumps(data),
            stream=stream,
        )


# Legacy alias for backward compatibility
AsyncEmbedClient = EmbedClient
