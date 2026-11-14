"""
Finds a network camera's stream address by itself (M9) — the owner only types the camera's own
username and password. Same rules as the Android app (OnvifClient.java + RtspProbe.java):

  1. ONVIF (when the scan saw it): GetCapabilities -> GetProfiles -> GetStreamUri gives the exact rtsp:// links
     (main stream first, then the lighter sub stream). WS-Security digest: the password itself never travels.
  2. Each link is confirmed with one RTSP DESCRIBE (Basic or Digest answer).
  3. No ONVIF, or it failed: the usual links of the camera's brand, then the common ones of other brands.

Only the owner's credentials are used; no password is ever guessed. Pure standard library.
"""

import base64
import datetime
import hashlib
import os
import re
import socket
import urllib.parse


class AuthFailed(Exception):
    def __init__(self):
        super().__init__("The camera refused this username or password.")


class NotFound(Exception):
    pass


def _xml(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _status(resp):
    m = re.match(r"^(?:HTTP|RTSP)/\d\.\d (\d{3})", resp)
    return int(m.group(1)) if m else 0


def _header(resp, name):
    m = re.search(r"(?im)^" + re.escape(name) + r":\s*(.+?)\s*$", resp)
    return m.group(1) if m else None


def same_host(reported, reached):
    """Cameras sometimes report another address (NAT, second interface): keep the one we reached."""
    try:
        r, b = urllib.parse.urlsplit(reported), urllib.parse.urlsplit(reached)
        if not r.hostname or not b.hostname or r.hostname == b.hostname:
            return reported
        netloc = b.hostname + (f":{r.port}" if r.port else "")
        return urllib.parse.urlunsplit((r.scheme, netloc, r.path, r.query, ""))
    except ValueError:
        return reported


# ---- ONVIF -----------------------------------------------------------------------------------------------

class Onvif:
    def __init__(self, user, password, timeout=2.5):
        self.user, self.password, self.timeout = user or "", password or "", timeout

    def _security(self):
        if not self.user:
            return ""
        nonce = os.urandom(16)
        created = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + self.password.encode()).digest()).decode()
        return (
            '<Security s:mustUnderstand="1" xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">'
            f"<UsernameToken><Username>{_xml(self.user)}</Username>"
            '<Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">'
            f"{digest}</Password>"
            '<Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">'
            f"{base64.b64encode(nonce).decode()}</Nonce>"
            f'<Created xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">{created}</Created>'
            "</UsernameToken></Security>"
        )

    def call(self, url, body):
        envelope = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Header>{self._security()}</s:Header>'
            f"<s:Body>{body}</s:Body></s:Envelope>"
        ).encode()
        u = urllib.parse.urlsplit(url)
        if u.scheme.lower() != "http":
            raise OSError("Only http ONVIF addresses are supported: " + url)
        port = u.port or 80
        path = (u.path or "/") + (("?" + u.query) if u.query else "")
        with socket.create_connection((u.hostname, port), timeout=self.timeout) as s:
            s.settimeout(self.timeout * 3)
            s.sendall((
                f"POST {path} HTTP/1.1\r\nHost: {u.hostname}:{port}\r\n"
                f"Content-Type: application/soap+xml; charset=utf-8\r\nContent-Length: {len(envelope)}\r\n"
                "Connection: close\r\n\r\n"
            ).encode() + envelope)
            chunks, size = [], 0
            while size < 2_000_000:
                b = s.recv(8192)
                if not b:
                    break
                chunks.append(b)
                size += len(b)
        resp = b"".join(chunks).decode("utf-8", "replace")
        code = _status(resp)
        if code == 401 or "NotAuthorized" in resp or "FailedAuthentication" in resp:
            raise AuthFailed()
        if code != 200:
            raise OSError(f"The camera answered HTTP {code}.")
        return resp

    def stream_uris(self, device_url):
        caps = self.call(device_url, '<GetCapabilities xmlns="http://www.onvif.org/ver10/device/wsdl"><Category>Media</Category></GetCapabilities>')
        m = re.search(r"<[^>]*Media[^>]*>\s*<[^>]*XAddr[^>]*>([^<]+)<", caps, re.S)
        media = same_host(m.group(1).strip(), device_url) if m else device_url
        profiles = self.call(media, '<GetProfiles xmlns="http://www.onvif.org/ver10/media/wsdl"/>')
        tokens = re.findall(r'<[^>]*Profiles[^>]*token="([^"]+)"', profiles)[:4]
        uris = []
        for token in tokens:
            r = self.call(media,
                '<GetStreamUri xmlns="http://www.onvif.org/ver10/media/wsdl">'
                '<StreamSetup><Stream xmlns="http://www.onvif.org/ver10/schema">RTP-Unicast</Stream>'
                '<Transport xmlns="http://www.onvif.org/ver10/schema"><Protocol>RTSP</Protocol></Transport></StreamSetup>'
                f"<ProfileToken>{_xml(token)}</ProfileToken></GetStreamUri>")
            m = re.search(r"<[^>]*Uri[^>]*>([^<]+)<", r)
            if m and m.group(1).strip().startswith("rtsp://"):
                uris.append(same_host(m.group(1).strip().replace("&amp;", "&"), device_url))
        return uris


# ---- RTSP DESCRIBE probe ---------------------------------------------------------------------------------

OK, AUTH_FAILED, NOT_FOUND, UNREACHABLE = "ok", "auth", "notfound", "unreachable"


def known_paths(brand):
    """(main, sub) path pairs: the brand's own first, then the common ones (cheap cameras often reuse them)."""
    b = (brand or "").lower()
    out = []
    if "hik" in b or "hiwatch" in b:
        out.append(("/Streaming/Channels/101", "/Streaming/Channels/102"))
    if "dahua" in b or "amcrest" in b or "lorex" in b or "imou" in b:
        out.append(("/cam/realmonitor?channel=1&subtype=0", "/cam/realmonitor?channel=1&subtype=1"))
    if "reolink" in b:
        out.append(("/h264Preview_01_main", "/h264Preview_01_sub"))
    if "tp-link" in b or "tapo" in b or "tplink" in b:
        out.append(("/stream1", "/stream2"))
    if "ezviz" in b:
        out.append(("/h264/ch1/main/av_stream", "/h264/ch1/sub/av_stream"))
    if "axis" in b:
        out.append(("/axis-media/media.amp", "/axis-media/media.amp?resolution=640x360"))
    out += [
        ("/stream1", "/stream2"),
        ("/Streaming/Channels/101", "/Streaming/Channels/102"),
        ("/cam/realmonitor?channel=1&subtype=0", "/cam/realmonitor?channel=1&subtype=1"),
        ("/h264Preview_01_main", "/h264Preview_01_sub"),
        ("/h264/ch1/main/av_stream", "/h264/ch1/sub/av_stream"),
        ("/live/ch00_0", "/live/ch00_1"),
        ("/onvif1", "/onvif2"),
        ("/11", "/12"),
        ("/live", None),
        ("/", None),
    ]
    seen, unique = set(), []
    for p in out:
        if p[0] not in seen:
            seen.add(p[0])
            unique.append(p)
    return unique


class Rtsp:
    def __init__(self, user, password, timeout=2.5):
        self.user, self.password, self.timeout = user or "", password or "", timeout

    def _auth(self, challenge, url):
        if not challenge:
            return None
        if challenge[:5].lower() == "basic":
            return "Basic " + base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
        if challenge[:6].lower() != "digest":
            return None
        realm = re.search(r'realm="([^"]*)"', challenge)
        nonce = re.search(r'nonce="([^"]*)"', challenge)
        if not realm or not nonce:
            return None
        md5 = lambda s: hashlib.md5(s.encode()).hexdigest()  # noqa: E731
        ha1 = md5(f"{self.user}:{realm.group(1)}:{self.password}")
        ha2 = md5(f"DESCRIBE:{url}")
        resp = md5(f"{ha1}:{nonce.group(1)}:{ha2}")
        return (f'Digest username="{self.user}", realm="{realm.group(1)}", nonce="{nonce.group(1)}", '
                f'uri="{url}", response="{resp}"')

    @staticmethod
    def _read(s):
        buf = b""
        while b"\r\n\r\n" not in buf and len(buf) < 64_000:
            b = s.recv(4096)
            if not b:
                break
            buf += b
        head, _, body = buf.partition(b"\r\n\r\n")
        text = head.decode("utf-8", "replace")
        n = _header(text, "Content-Length")
        if n:
            want = min(int(n), 64_000)
            while len(body) < want:
                b = s.recv(want - len(body))
                if not b:
                    break
                body += b
        return text + "\r\n\r\n" + body.decode("utf-8", "replace")

    def _request(self, s, url, cseq, auth):
        s.sendall((f"DESCRIBE {url} RTSP/1.0\r\nCSeq: {cseq}\r\nAccept: application/sdp\r\nUser-Agent: Remote\r\n"
                   + (f"Authorization: {auth}\r\n" if auth else "") + "\r\n").encode())
        return self._read(s)

    def describe(self, url):
        try:
            u = urllib.parse.urlsplit(url)
            with socket.create_connection((u.hostname, u.port or 554), timeout=self.timeout) as s:
                s.settimeout(self.timeout * 3)
                r = self._request(s, url, 1, None)
                code = _status(r)
                if code == 401 and self.user:
                    auth = self._auth(_header(r, "WWW-Authenticate"), url)
                    if not auth:
                        return AUTH_FAILED
                    r = self._request(s, url, 2, auth)
                    code = _status(r)
            if code == 200:
                return OK
            if code in (401, 403):
                return AUTH_FAILED
            return NOT_FOUND
        except (OSError, ValueError):
            return UNREACHABLE

    def find(self, host, port, brand):
        reached = False
        for main_path, sub_path in known_paths(brand):
            main = f"rtsp://{host}:{port}{main_path}"
            r = self.describe(main)
            if r == UNREACHABLE:
                if not reached:
                    break  # same host and port for every link: the port is closed, don't wait on each one
                continue
            reached = True
            if r == AUTH_FAILED:
                raise AuthFailed()
            if r != OK:
                continue
            sub = None
            if sub_path:
                cand = f"rtsp://{host}:{port}{sub_path}"
                if self.describe(cand) == OK:
                    sub = cand
            return main, sub
        if reached:
            raise NotFound("The camera answers, but none of the usual stream links worked. "
                           "Turn on RTSP/ONVIF in the camera's own app, then try again.")
        raise NotFound("The camera's video port doesn't answer. Some brands (Tapo, Ezviz, Imou…) turn RTSP/ONVIF "
                       "off by default: turn it on in the camera's own app, then try again.")


def with_credentials(url, user, password):
    """rtsp://host/path -> rtsp://user:password@host/path (what ffmpeg needs to play it)."""
    if not user:
        return url
    u = urllib.parse.urlsplit(url)
    cred = urllib.parse.quote(user, safe="") + ":" + urllib.parse.quote(password or "", safe="")
    netloc = cred + "@" + u.hostname + (f":{u.port}" if u.port else "")
    return urllib.parse.urlunsplit((u.scheme, netloc, u.path, u.query, ""))


def detect(ip, ports, brand, onvif, user, password, timeout=2.5):
    """Returns {"main": rtsp-url, "sub": rtsp-url-or-""} WITHOUT credentials. Raises AuthFailed / NotFound."""
    open_ports = set(int(p) for p in ports or [])
    main = sub = ""
    if onvif:
        try:
            uris = Onvif(user, password, timeout).stream_uris(onvif)
            if uris:
                main, sub = uris[0], (uris[1] if len(uris) > 1 else "")
        except AuthFailed:
            raise
        except Exception:  # noqa: BLE001 — ONVIF unusable: the usual links below
            pass
    probe = Rtsp(user, password, timeout)
    if main:
        r = probe.describe(main)
        if r == AUTH_FAILED:
            raise AuthFailed()
        if r != OK:
            main = sub = ""
        elif sub and probe.describe(sub) != OK:
            sub = ""
    if not main:
        rtsp_port = 554 if 554 in open_ports else 8554 if 8554 in open_ports else 554
        main, sub = probe.find(ip, rtsp_port, brand)
    return {"main": main, "sub": sub or ""}
