import asyncio
from typing import Callable, Dict, List, Optional, Set, Text, Tuple, Union, cast

from .exceptions import (
    SnmpErrorAuthorizationError,
    SnmpErrorBadValue,
    SnmpErrorCommitFailed,
    SnmpErrorGenErr,
    SnmpErrorInconsistentName,
    SnmpErrorInconsistentValue,
    SnmpErrorNoAccess,
    SnmpErrorNoCreation,
    SnmpErrorNoSuchName,
    SnmpErrorNotWritable,
    SnmpErrorReadOnly,
    SnmpErrorResourceUnavailable,
    SnmpErrorTooBig,
    SnmpErrorUndoFailed,
    SnmpErrorWrongEncoding,
    SnmpErrorWrongLength,
    SnmpErrorWrongType,
    SnmpErrorWrongValue,
    SnmpTimeoutError,
)
from .message import PDU, SnmpMessage, SnmpResponse, SnmpV2TrapMessage, SnmpVarbind

_ERROR_STATUS_TO_EXCEPTION = {
    1: SnmpErrorTooBig,
    2: SnmpErrorNoSuchName,
    3: SnmpErrorBadValue,
    4: SnmpErrorReadOnly,
    5: SnmpErrorGenErr,
    6: SnmpErrorNoAccess,
    7: SnmpErrorWrongType,
    8: SnmpErrorWrongLength,
    9: SnmpErrorWrongEncoding,
    10: SnmpErrorWrongValue,
    11: SnmpErrorNoCreation,
    12: SnmpErrorInconsistentValue,
    13: SnmpErrorResourceUnavailable,
    14: SnmpErrorCommitFailed,
    15: SnmpErrorUndoFailed,
    16: SnmpErrorAuthorizationError,
    17: SnmpErrorNotWritable,
    18: SnmpErrorInconsistentName,
}

Address = Union[Tuple[str, int], Tuple[str, int, int, int], str, bytes, bytearray]
SocketKey = Union[Tuple[str, int], str, bytes, bytearray]


class SnmpTrapProtocol(asyncio.DatagramProtocol):
    __slots__ = ("loop", "transport", "communities", "handler")

    def __init__(self, communities: Optional[Set[str]], handler: Callable) -> None:
        self.loop: asyncio.AbstractEventLoop = asyncio.get_event_loop()
        self.communities: Optional[Set[str]] = communities
        self.handler: Callable = handler

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = cast(asyncio.DatagramTransport, transport)

    def datagram_received(self, data: Union[bytes, Text], addr: Address) -> None:
        if isinstance(data, Text):
            return

        message = SnmpV2TrapMessage.decode(data)
        if not message or (
            self.communities and message.community not in self.communities
        ):
            return

        host, port = addr[:2] if isinstance(addr, tuple) else (addr, -1)
        if isinstance(host, (bytes, bytearray)):
            host = host.decode()
        asyncio.ensure_future(self.handler(host, port, message))


class SnmpProtocol(asyncio.DatagramProtocol):
    __slots__ = ("loop", "transport", "requests", "timeout", "retries")

    def __init__(self, timeout: float, retries: int) -> None:
        self.loop: asyncio.AbstractEventLoop = asyncio.get_event_loop()
        self.requests: Dict[Tuple[SocketKey, int], asyncio.Future] = {}
        self.timeout: float = timeout
        self.retries: int = retries

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = cast(asyncio.DatagramTransport, transport)

    def datagram_received(self, data: Union[bytes, Text], addr: Address) -> None:
        socket_key: SocketKey = addr[:2] if isinstance(addr, tuple) else addr
        if isinstance(data, Text):
            raise RuntimeError("data should be bytes.")
        message = SnmpResponse.decode(data)

        key = (socket_key, message.data.request_id)
        if key in self.requests:
            exception: Optional[Exception] = None
            if isinstance(message.data, PDU) and message.data.error_status != 0:
                index: int = message.data.error_index
                exception = _ERROR_STATUS_TO_EXCEPTION[message.data.error_status](
                    index, message.data.varbinds[index - 1].oid
                )
            try:
                if exception:
                    self.requests[key].set_exception(exception)
                else:
                    self.requests[key].set_result(message.data.varbinds)
            except asyncio.InvalidStateError:
                del self.requests[key]

    async def _send(
        self, message: SnmpMessage, socket_key: SocketKey
    ) -> List[SnmpVarbind]:
        key = (socket_key, message.data.request_id)
        fut: asyncio.Future = self.loop.create_future()
        fut.add_done_callback(
            lambda fn: self.requests.pop(key) if key in self.requests else None
        )
        self.requests[key] = fut
        for i in range(self.retries):
            self.transport.sendto(message.encode())
            done, _ = await asyncio.wait(
                {fut}, timeout=self.timeout, return_when=asyncio.ALL_COMPLETED
            )
            if not done:
                continue
            r: List[SnmpVarbind] = fut.result()
            return r
        fut.cancel()
        raise SnmpTimeoutError
