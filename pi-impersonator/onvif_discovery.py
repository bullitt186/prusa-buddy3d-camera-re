"""Asyncio transport adapter for the pure WS-Discovery facade."""

import asyncio
import logging
import socket

from onvif_facade import discovery_response


log = logging.getLogger('prusa-cam.onvif.discovery')
GROUP = '239.255.255.250'
PORT = 3702


class WsDiscoveryProtocol(asyncio.DatagramProtocol):
    def __init__(self, context):
        self.context = context
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        try:
            response = discovery_response(self.context, data)
        except Exception as exc:
            log.warning(f'WS-Discovery request failed safely: {exc}')
            return
        if response and self.transport is not None:
            self.transport.sendto(response, addr)

    def error_received(self, exc):
        log.warning(f'WS-Discovery transport error: {exc}')


async def start_ws_discovery(context):
    """Join the ONVIF multicast group and return its asyncio transport."""
    if not context.ipv4:
        log.warning('WS-Discovery disabled: camera has no LAN IPv4 address')
        return None
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(('', PORT))
        membership = socket.inet_aton(GROUP) + socket.inet_aton(context.ipv4)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
        sock.setblocking(False)
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            lambda: WsDiscoveryProtocol(context), sock=sock
        )
    except BaseException:
        sock.close()
        raise
    log.info(f'ONVIF WS-Discovery listening on {GROUP}:{PORT} via {context.ipv4}')
    return transport
