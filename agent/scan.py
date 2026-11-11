"""
Find the cameras on the local network (M7 — same rules as the Android app's NetworkScanner).

  1. ONVIF WS-Discovery: one Probe to 239.255.255.250:3702; cameras answer with their service address and,
     often, their brand and model.
  2. Port check: a TCP connect (no data sent) to the usual camera ports on every address of this machine's /24.

Nothing logs in to anything: no password is ever tried. Only private addresses (10/8, 172.16/12, 192.168/16).

    python3 agent/remote_agent.py --scan        prints what it finds
    dashboard "Scan for cameras"                the `discovery` capability (only on networks listed in
                                                config.json "discovery": {"networks": ["192.168.1.0/24"]})

Pure standard library (sockets + threads); nothing to install.
"""

import concurrent.futures
import ipaddress
import re
import socket
import threading
import time
import urllib.parse
import uuid

PORTS = {
    554: "RTSP video", 8554: "RTSP video", 80: "web page", 8080: "web page", 443: "web page (https)",
    8000: "Hikvision", 37777: "Dahua", 1935: "RTMP",
}
ONVIF_MULTICAST = ("239.255.255.250", 3702)


def probe_message(message_id):
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"'
        ' xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"'
        ' xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"'
        ' xmlns:dn="http://www.onvif.org/ver10/network/wsdl">'
        f"<e:Header><w:MessageID>uuid:{message_id}</w:MessageID>"
        '<w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>'
        '<w:Action e:mustUnderstand="true">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action></e:Header>'
        "<e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body></e:Envelope>"
    )


class Host:
    """One device found. Fields only grow while the scan runs."""

    def __init__(self, ip):
        self.ip = ip
        self.ports = set()
        self.manufacturer = self.model = self.name = self.onvif = ""

    def kind(self):
        if self.onvif or self.ports & {554, 8554, 8000, 37777}:
            return "camera"
        return "maybe" if self.ports else "other"

    def to_dict(self):
        return {
            "ip": self.ip,
            "kind": self.kind(),
            "ports": [{"port": p, "label": PORTS.get(p, "")} for p in sorted(self.ports)],
            "manufacturer": self.manufacturer,
            "model": self.model,
            "name": self.name,
            "onvif": self.onvif,
        }


def apply_probe_match(host, xml):
    """Reads the service address and the brand/model/name scopes of a ProbeMatch answer."""
    m = re.search(r"<[^>]*XAddrs[^>]*>([^<]+)<", xml)
    if m:
        host.onvif = m.group(1).split()[0].strip()
    m = re.search(r"<[^>]*Scopes[^>]*>([^<]+)<", xml)
    if not m:
        return
    for scope in m.group(1).split():
        for key, attr in (("hardware/", "model"), ("name/", "name"), ("mfr/", "manufacturer"), ("manufacturer/", "manufacturer")):
            prefix = "onvif://www.onvif.org/" + key
            if scope.startswith(prefix) and not getattr(host, attr):
                setattr(host, attr, urllib.parse.unquote(scope[len(prefix):]).replace("_", " ").strip())
    if not host.manufacturer and host.name:  # many cameras put the brand in "name" only
        host.manufacturer = host.name


def is_private(ip):
    try:
        a = ipaddress.IPv4Address(ip)
    except ValueError:
        return False
    return any(a in ipaddress.IPv4Network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


def subnet(self_ip, prefix=24):
    """Addresses to check around self_ip: its /22–/30 subnet, else its /24; never public, never itself."""
    if not is_private(self_ip):
        return []
    p = prefix if 22 <= prefix <= 30 else 24
    net = ipaddress.IPv4Network(f"{self_ip}/{p}", strict=False)
    return [str(a) for a in net.hosts() if str(a) != self_ip]


def local_ips():
    """Best-effort list of this machine's private LAN IPv4 addresses."""
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))  # no packet sent; just picks the outbound interface
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return sorted(ip for ip in ips if is_private(ip))


def trusted_network(networks):
    """The first local address inside one of the owner's networks (config "discovery.networks"), else None."""
    for ip in local_ips():
        for n in networks or []:
            try:
                if ipaddress.IPv4Address(ip) in ipaddress.IPv4Network(n, strict=False):
                    return ip, ipaddress.IPv4Network(n, strict=False)
            except ValueError:
                continue
    return None


def discover(targets, probe_to=ONVIF_MULTICAST, timeout=0.4, parallel=128, on_update=None):
    """Scans `targets` and asks `probe_to` who speaks ONVIF. Returns hosts (dicts) sorted by address."""
    found = {}
    lock = threading.Lock()

    def update(host):
        if on_update:
            on_update(host.to_dict())

    def check(ip, port):
        try:
            with socket.create_connection((ip, port), timeout=timeout):
                pass
        except OSError:
            return
        with lock:
            h = found.setdefault(ip, Host(ip))
            h.ports.add(port)
            snapshot = h
        update(snapshot)

    def onvif():
        probe = probe_message(uuid.uuid4()).encode()
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(timeout)
            s.sendto(probe, probe_to)
            until = time.time() + timeout * 4 + 1.5
            resent = False
            while time.time() < until:
                if not resent and time.time() > until - timeout * 2:
                    s.sendto(probe, probe_to)  # UDP can be lost; some cameras ignore the first probe
                    resent = True
                try:
                    data, addr = s.recvfrom(65535)
                except socket.timeout:
                    continue
                xml = data.decode("utf-8", "replace")
                if "ProbeMatch" not in xml:
                    continue
                with lock:
                    h = found.setdefault(addr[0], Host(addr[0]))
                    apply_probe_match(h, xml)
                update(h)
            s.close()
        except OSError:
            pass  # no multicast here: the port check still finds the cameras

    t = threading.Thread(target=onvif, daemon=True)
    t.start()
    with concurrent.futures.ThreadPoolExecutor(max_workers=parallel) as pool:
        for ip in targets:
            for port in PORTS:
                pool.submit(check, ip, port)
    t.join(timeout * 4 + 3)
    hosts = [h.to_dict() for h in found.values()]
    return sorted(hosts, key=lambda h: tuple(int(o) for o in h["ip"].split(".")))


def run():
    ips = local_ips()
    if not ips:
        print("Could not find this machine's local (private) network address.")
        return
    print("Looking for cameras on your local network (ONVIF + camera ports)…")
    print("Networks: " + ", ".join(str(ipaddress.IPv4Network(f"{ip}/24", strict=False)) for ip in ips))
    hosts = []
    for ip in ips:
        hosts += discover(subnet(ip))
    if not hosts:
        print("\nNothing found. Check the cameras are on the same network and powered on.")
        return
    order = {"camera": 0, "maybe": 1, "other": 2}
    print()
    for h in sorted(hosts, key=lambda h: order[h["kind"]]):
        brand = " · ".join(x for x in (h["manufacturer"], h["model"]) if x)
        ports = ", ".join(f"{p['port']} ({p['label']})" for p in h["ports"])
        print(f"  {h['ip']:<15} {h['kind']:<7} {brand}")
        if ports:
            print(f"      ports: {ports}")
        if h["onvif"]:
            print(f"      ONVIF: {h['onvif']}")
    print("\nTo relay one to the dashboard: add it to \"cameras\" in config.json (id, name, rtsp://user:password@ip/path),")
    print("without a token — it then shows in this device's camera picker. See the README.")
