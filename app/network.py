"""Find this machine's local-network (LAN) addresses so other devices can connect."""
import ipaddress
import socket


def _usable(ip):
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return addr.version == 4 and not addr.is_loopback and not addr.is_link_local and not addr.is_unspecified


def lan_addresses():
    """Return a sorted list of IPv4 addresses other devices on the network can use.

    Works without internet: the UDP "connect" below sends no packets, it only
    asks the operating system which interface it would use.
    """
    found = set()
    for probe in ("10.255.255.255", "192.168.255.255", "172.16.255.255"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect((probe, 1))
                ip = s.getsockname()[0]
                if _usable(ip):
                    found.add(ip)
        except OSError:
            pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if _usable(ip):
                found.add(ip)
    except OSError:
        pass
    return sorted(found, key=_rank)


def _rank(ip):
    """Typical Wi-Fi ranges first; 172.16-31.x is often a virtual adapter (Docker, WSL, Hyper-V)."""
    addr = ipaddress.ip_address(ip)
    if ip.startswith("192.168."):
        group = 0
    elif ip.startswith("10."):
        group = 1
    elif addr.is_private:
        group = 2
    else:
        group = 3
    return (group, tuple(int(part) for part in ip.split(".")))
