"""
Find IP cameras on the local network.

Scans the agent's own /24 subnet for hosts with the RTSP port (554) open, which
is how almost every IP camera serves video. It can't read a camera without its
username and password — it only helps you find each camera's address so you can
fill in the 'cameras' list in config.json.

    python3 agent/remote_agent.py --scan

Pure standard library (sockets + threads); nothing to install.
"""

import socket
import threading


RTSP_PORTS = (554, 8554)


def _local_ips():
    """Best-effort list of this machine's LAN IPv4 addresses."""
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))  # no packet sent; just picks the outbound interface
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except Exception:
        pass
    return [ip for ip in ips if not ip.startswith("127.")]


def _check(ip, port, timeout, found):
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            found.append((ip, port))
    except Exception:
        pass


def scan(timeout=0.6):
    """Returns a sorted list of (ip, port) with an RTSP port open on the local /24."""
    ips = _local_ips()
    if not ips:
        return []
    found = []
    threads = []
    for base_ip in ips:
        prefix = base_ip.rsplit(".", 1)[0]
        for host in range(1, 255):
            ip = f"{prefix}.{host}"
            for port in RTSP_PORTS:
                t = threading.Thread(target=_check, args=(ip, port, timeout, found), daemon=True)
                t.start()
                threads.append(t)
    for t in threads:
        t.join(timeout + 0.5)
    return sorted(set(found), key=lambda x: (tuple(int(p) for p in x[0].split(".")), x[1]))


def run():
    nets = _local_ips()
    if not nets:
        print("Could not find this machine's local network address.")
        return
    print("Scanning your local network for cameras (RTSP port open)…")
    print("Networks: " + ", ".join(ip.rsplit('.', 1)[0] + ".0/24" for ip in nets))
    hits = scan()
    if not hits:
        print("\nNo cameras found. Check they are on the same Wi-Fi, powered on, and have RTSP enabled.")
        return
    print(f"\nFound {len(hits)} possible camera(s):\n")
    for ip, port in hits:
        suffix = "" if port == 554 else f":{port}"
        print(f"  {ip}{suffix}")
        print(f"      rtsp://USERNAME:PASSWORD@{ip}{suffix}/   (add the brand's stream path, see README)")
    print("\nTo use one: add it to the \"cameras\" list in config.json with its id, token,")
    print("name and the full rtsp:// link, then restart the agent. See the README.")
