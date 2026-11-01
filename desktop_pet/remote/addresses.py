"""Usable direct QUIC candidates, excluding proxy/VM and unrouteable addresses."""
from __future__ import annotations

import ipaddress


_FAKE_IP = ipaddress.ip_network('198.18.0.0/15')
_VIRTUAL_NAMES = ('mihomo', 'clash', 'vmware', 'vmnet', 'vethernet', 'hyper-v',
                  'virtualbox', 'vboxnet', 'wsl', 'loopback', 'docker', 'tun', 'tap')


def physical_interface_ips() -> set[str] | None:
    """Read interface metadata locally; None means enumeration is unavailable.

    An empty set is intentional: no usable physical interface is up yet. This
    lets startup wait/retry after boot instead of advertising a VM-only address.
    """
    try:
        from PySide6.QtNetwork import QNetworkInterface
        addresses = set()
        for interface in QNetworkInterface.allInterfaces():
            flags = interface.flags()
            flag = QNetworkInterface.InterfaceFlag
            name = (interface.name()+' '+interface.humanReadableName()).lower()
            if (not flags & flag.IsUp or not flags & flag.IsRunning
                    or flags & flag.IsLoopBack or any(word in name for word in _VIRTUAL_NAMES)):
                continue
            for entry in interface.addressEntries():
                value = entry.ip().toString().split('%', 1)[0]
                try:
                    addresses.add(str(ipaddress.ip_address(value)))
                except ValueError:
                    continue
        return addresses
    except (ImportError, OSError, RuntimeError):
        return None


def usable_direct_addresses(addresses, interface_ips: set[str] | None = None) -> list[str]:
    """Keep valid physical LAN and discovered public candidates in SDK order."""
    result = []
    for value in addresses:
        try:
            if not isinstance(value, str) or len(value) > 128:
                continue
            if value.startswith('['):
                host, port = value[1:].split(']:', 1)
            else:
                host, port = value.rsplit(':', 1)
                if ':' in host:
                    continue
            if '%' in host or not port.isdecimal() or not 1 <= int(port) <= 65535:
                continue
            ip = ipaddress.ip_address(host)
            routed = getattr(ip, 'ipv4_mapped', None) or ip
            if (routed.is_unspecified or routed.is_loopback or routed.is_link_local
                    or routed.is_multicast or routed.is_reserved
                    or routed.version == 4 and routed in _FAKE_IP):
                continue
            if interface_ips is not None and not routed.is_global and str(ip) not in interface_ips:
                continue
            candidate = f'[{ip}]:{int(port)}' if ip.version == 6 else f'{ip}:{int(port)}'
            if candidate not in result:
                result.append(candidate)
        except (ValueError, TypeError):
            continue
    return result
