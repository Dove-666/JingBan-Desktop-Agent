"""LAN readiness without a WAN relay, using synthetic endpoints only.

The default tests open no sockets and read no real settings, identity or ticket.
An explicitly enabled SDK smoke uses fresh in-memory identities and minimal
endpoints, never a saved device or API configuration.
"""

import asyncio
from contextlib import ExitStack
import importlib.util
import json
import os
import secrets
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from desktop_pet.remote.addresses import physical_interface_ips, usable_direct_addresses
from desktop_pet.remote.protocol import ALPN, read_frame, write_frame
from desktop_pet.remote.transport import DEFAULT_LISTEN_PORT, IrohTransport, _bind_endpoint


class DirectAddressTests(unittest.TestCase):
    def test_invalid_proxy_and_unrouteable_addresses_are_not_advertised(self):
        values = [
            '10.2.3.4:43123', '198.18.0.1:43123', '198.19.255.254:43123',
            '127.0.0.1:43123', '0.0.0.0:43123', '169.254.1.2:43123',
            '224.0.0.1:43123', '255.255.255.255:43123',
            '[::1]:43123', '[::]:43123', '[fe80::1%12]:43123',
            '[ff02::1]:43123', '[::ffff:198.18.0.1]:43123',
            '10.2.3.4:0', '10.2.3.4:65536', '10.2.3.4:not-a-port',
            'not-an-ip:43123', '', None, '10.2.3.4:43123',
            '10.2.3.5:0043123', '[fd12:3456::1]:43123',
        ]
        self.assertEqual(usable_direct_addresses(values), [
            '10.2.3.4:43123', '10.2.3.5:43123', '[fd12:3456::1]:43123',
        ])

    def test_physical_interface_filter_excludes_vm_but_keeps_public_reflexive_candidate(self):
        values = ['172.26.0.1:43123', '192.168.214.1:43123', '10.2.3.4:43123',
                  '198.18.0.1:43123', '[fd12::1]:43123', '8.8.8.8:43123']
        self.assertEqual(usable_direct_addresses(values, {'10.2.3.4', 'fd12::1', '198.18.0.1'}),
                         ['10.2.3.4:43123', '[fd12::1]:43123', '8.8.8.8:43123'])
        self.assertEqual(usable_direct_addresses(values[:3], set()), [])

    def test_ipv6_candidates_are_canonical_and_keep_sdk_order(self):
        self.assertEqual(usable_direct_addresses([
            '[2606:4700:4700:0:0:0:0:1111]:43123',
            '[2606:4700:4700::1111]:43123', '10.2.3.4:1234',
        ]), ['[2606:4700:4700::1111]:43123', '10.2.3.4:1234'])

    def test_local_interface_enumeration_ignores_down_loopback_proxy_and_vm_interfaces(self):
        from PySide6.QtNetwork import QNetworkInterface
        flag = QNetworkInterface.InterfaceFlag
        up = flag.IsUp | flag.IsRunning

        def interface(name, ip, flags=up):
            return SimpleNamespace(name=lambda:name, humanReadableName=lambda:name,
                flags=lambda:flags, addressEntries=lambda:[SimpleNamespace(
                    ip=lambda:SimpleNamespace(toString=lambda:ip))])

        rows = [interface('WLAN', '10.2.3.4'), interface('Ethernet', 'fd12::1'),
                interface('Mihomo', '198.18.0.1'), interface('VMware VMnet8', '192.168.214.1'),
                interface('vEthernet (WSL)', '172.26.0.1'),
                interface('Ethernet 2', '192.168.1.2', flag.IsUp),
                interface('Loopback', '127.0.0.1', up | flag.IsLoopBack)]
        with patch.object(QNetworkInterface, 'allInterfaces', return_value=rows):
            self.assertEqual(physical_interface_ips(), {'10.2.3.4', 'fd12::1'})
        with patch.object(QNetworkInterface, 'allInterfaces', side_effect=OSError('private failure')):
            self.assertIsNone(physical_interface_ips())


class BindPortTests(unittest.TestCase):
    def test_fixed_service_port_does_not_change_ephemeral_peer_bind_default(self):
        builders = []

        class Builder:
            def __init__(self):
                self.bind_addresses = []
                self.alpn = None
                builders.append(self)
            def apply_n0(self): pass
            def secret_key(self, value): pass
            def alpns(self, value): self.alpn = value
            def bind_addr(self, value): self.bind_addresses.append(value)
            async def bind(self): return object()

        sdk = SimpleNamespace(EndpointBuilder=Builder)
        asyncio.run(_bind_endpoint(sdk, b'x'*32, DEFAULT_LISTEN_PORT))
        asyncio.run(_bind_endpoint(sdk, b'x'*32))
        self.assertEqual(DEFAULT_LISTEN_PORT, 43123)
        self.assertIn('0.0.0.0:43123', builders[0].bind_addresses)
        self.assertFalse(any(not address.endswith(':0') for address in builders[1].bind_addresses))
        self.assertEqual(builders[0].alpn, [ALPN])

    def test_invalid_listen_ports_fail_before_starting_any_worker(self):
        from desktop_pet.remote.transport import TransportError
        for value in [True, -1, 65536, '43123', None]:
            with self.subTest(value=value), self.assertRaises(TransportError):
                IrohTransport('a'*64, lambda *_:{}, lambda *_:None, listen_port=value)


class _Address:
    def __init__(self, endpoint_id, relay, addresses):
        self.endpoint_id = endpoint_id
        self.relay = relay
        self.addresses = list(addresses)
    def id(self): return self.endpoint_id
    def relay_url(self): return self.relay
    def direct_addresses(self): return list(self.addresses)


class _Endpoint:
    def __init__(self, addresses, *, relay='https://unavailable.iroh.link', online_error=False):
        self.addresses = list(addresses)
        self.relay = relay
        self.online_error = online_error
        self.relay_gate = threading.Event()
        self.online_entered = threading.Event()
        self.online_cancelled = threading.Event()
        self.closed = threading.Event()
    def id(self): return 'synthetic-endpoint-id'
    def addr(self): return _Address(self.id(), self.relay, self.addresses)
    async def online(self):
        self.online_entered.set()
        try:
            if self.online_error:
                raise OSError('private-network-error-sentinel')
            while not self.relay_gate.is_set():
                await asyncio.sleep(.01)
        except asyncio.CancelledError:
            self.online_cancelled.set()
            raise
    async def accept_next(self):
        await asyncio.Event().wait()
    async def close(self):
        self.closed.set()


class _Sdk:
    EndpointAddr = _Address
    def __init__(self):
        self.ticket_addresses = []
        self.EndpointTicket = SimpleNamespace(from_addr=self.ticket)
    def ticket(self, addr):
        self.ticket_addresses.append(addr)
        return json.dumps({'addresses':addr.addresses,'relay':addr.relay},sort_keys=True)


class LanReadinessTests(unittest.TestCase):
    def start_fake(self, endpoint, *, timeout=None, port=DEFAULT_LISTEN_PORT):
        stack = ExitStack()
        self.addCleanup(stack.close)
        sdk = _Sdk()
        bound_ports = []
        async def bind(_sdk, _secret, listen_port=0):
            bound_ports.append(listen_port)
            return endpoint
        stack.enter_context(patch('desktop_pet.remote.transport._load_iroh', return_value=sdk))
        stack.enter_context(patch('desktop_pet.remote.transport._bind_endpoint', side_effect=bind))
        stack.enter_context(patch('desktop_pet.remote.transport.physical_interface_ips', return_value=None))
        if timeout is not None:
            stack.enter_context(patch('desktop_pet.remote.transport.REQUEST_TIMEOUT', timeout))
        states = []
        transport = IrohTransport('a'*64, lambda *_:{}, states.append, listen_port=port)
        self.addCleanup(self.stop_fake, transport)
        transport.start()
        return transport, states, sdk, bound_ports

    def stop_fake(self, transport):
        transport.stop()
        self.assertTrue(transport.wait_stopped(2), 'Synthetic remote transport failed to stop')

    def wait_state(self, states, predicate, timeout=2):
        deadline = time.monotonic()+timeout
        while time.monotonic()<deadline:
            for state in list(states):
                if predicate(state): return state
            time.sleep(.005)
        self.fail('Expected transport state did not arrive: '+repr(states))

    def test_lan_listens_while_home_relay_waits_and_does_not_publish_unusable_relay(self):
        endpoint = _Endpoint(['198.18.0.1:43123', '10.2.3.4:43123'])
        started = time.monotonic()
        transport, states, sdk, ports = self.start_fake(endpoint)
        ready = self.wait_state(states, lambda state:state['state']=='listening')
        self.assertLess(time.monotonic()-started, 1)
        self.assertTrue(ready['local_available'])
        self.assertFalse(ready['relay_available'])
        self.assertEqual(ports, [43123])
        self.assertEqual(sdk.ticket_addresses[-1].addresses, ['10.2.3.4:43123'])
        self.assertIsNone(sdk.ticket_addresses[-1].relay)
        self.assertTrue(endpoint.online_entered.wait(1))
        self.assertFalse(any(state['state']=='error' for state in states))
        self.stop_fake(transport)
        self.assertTrue(endpoint.online_cancelled.is_set())
        self.assertTrue(endpoint.closed.is_set())

    def test_unreachable_relay_exception_does_not_take_healthy_lan_offline(self):
        endpoint = _Endpoint(['10.2.3.4:43123'], online_error=True)
        _, states, _, _ = self.start_fake(endpoint)
        self.wait_state(states, lambda state:state['state']=='listening')
        self.assertTrue(endpoint.online_entered.wait(1))
        time.sleep(.05)
        self.assertFalse(any(state['state']=='error' for state in states))
        self.assertNotIn('private-network-error-sentinel', repr(states))
        self.assertNotIn('a'*64, repr(states))

    def test_only_fake_addresses_fail_with_explicit_network_code(self):
        endpoint = _Endpoint(['198.18.0.1:43123', '[fe80::1%2]:43123'])
        transport, states, _, _ = self.start_fake(endpoint, timeout=.05)
        failure = self.wait_state(states, lambda state:state['state']=='error')
        self.assertEqual(failure['error_code'], 'no_usable_address')
        self.assertFalse(failure['local_available'])
        self.assertEqual(failure['ticket'], '')
        self.assertFalse(any(state['state']=='listening' for state in states))
        self.assertTrue(transport.wait_stopped(2))
        self.assertTrue(endpoint.closed.is_set())

    def test_stop_interrupts_waiting_for_a_real_local_interface(self):
        endpoint = _Endpoint([])
        transport, states, _, _ = self.start_fake(endpoint)
        self.assertTrue(endpoint.online_entered.wait(1))
        started = time.monotonic()
        self.stop_fake(transport)
        self.assertLess(time.monotonic()-started, 1)
        self.assertEqual(states[-1]['state'], 'stopped')
        self.assertFalse(any(state['state']=='listening' for state in states))

    def test_isolated_server_can_explicitly_request_ephemeral_port(self):
        endpoint = _Endpoint(['10.2.3.4:12345'])
        _, states, _, ports = self.start_fake(endpoint, port=0)
        self.wait_state(states, lambda state:state['state']=='listening')
        self.assertEqual(ports, [0])

    def test_late_relay_online_updates_ticket_without_rebinding_the_endpoint(self):
        endpoint = _Endpoint(['10.2.3.4:43123'])
        transport, states, sdk, ports = self.start_fake(endpoint)
        self.wait_state(states, lambda state:state['state']=='listening')
        endpoint.relay_gate.set()
        online = self.wait_state(states, lambda state:state['state']=='listening' and state['relay_available'], timeout=6)
        self.assertTrue(online['local_available'])
        self.assertEqual(sdk.ticket_addresses[-1].relay, endpoint.relay)
        self.assertEqual(ports, [43123])
        self.assertEqual(online['endpoint_id'], 'synthetic-endpoint-id')
        self.assertFalse(endpoint.closed.is_set())
        self.stop_fake(transport)


@unittest.skipUnless(os.environ.get('AURA_REMOTE_NETWORK_TESTS')=='1' and
                     importlib.util.find_spec('iroh') is not None,
                     'Opt-in fresh minimal SDK endpoint smoke')
class RealLanOnlySdkTests(unittest.TestCase):
    def test_minimal_sdk_peer_reaches_lan_service_with_blocked_online_future(self):
        """No n0 discovery/relay on either fresh endpoint, same authenticated RPC."""
        import iroh
        import desktop_pet.remote.transport as module
        states=[]
        done=threading.Event()

        async def bind_minimal(_sdk, secret, listen_port=0):
            builder=iroh.EndpointBuilder()
            builder.apply_minimal()
            builder.secret_key(secret)
            builder.alpns([ALPN])
            if listen_port:builder.bind_addr(f'0.0.0.0:{listen_port}')
            return await builder.bind()

        def status(value):
            states.append(value)
            if value['state'] in {'listening','error'}:done.set()

        server=IrohTransport(secrets.token_hex(32),
            lambda _peer, request:{'id':request['id'],'ok':True,'result':{'local':True}},
            status,listen_port=0)
        with patch.object(module,'_bind_endpoint',side_effect=bind_minimal):
            server.start()
            try:
                self.assertTrue(done.wait(5))
                ready=next((state for state in reversed(states) if state['state']=='listening'),None)
                self.assertIsNotNone(ready, 'Fresh LAN service did not start')
                self.assertTrue(ready['local_available'])
                self.assertFalse(ready['relay_available'])

                async def exchange():
                    endpoint=await bind_minimal(iroh,secrets.token_bytes(32))
                    connection=None
                    try:
                        addr=iroh.EndpointTicket.from_string(ready['ticket']).endpoint_addr()
                        self.assertIsNone(addr.relay_url())
                        self.assertTrue(addr.direct_addresses())
                        connection=await asyncio.wait_for(endpoint.connect(addr,ALPN),5)
                        self.assertEqual(str(connection.remote_id()),ready['endpoint_id'])
                        stream=await connection.open_bi()
                        message={'id':str(uuid4()),'method':'status','params':{}}
                        await write_frame(stream.send(),message)
                        response=await asyncio.wait_for(read_frame(stream.recv()),5)
                        self.assertTrue(response['ok'])
                        self.assertTrue(response['result']['local'])
                        self.assertEqual(response['id'],message['id'])
                    finally:
                        if connection is not None:
                            connection.close(0,b'test done')
                        await endpoint.close()
                asyncio.run(exchange())
            finally:
                server.stop()
                self.assertTrue(server.wait_stopped(5))


if __name__=='__main__':
    unittest.main()
