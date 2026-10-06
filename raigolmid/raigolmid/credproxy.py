"""The credential never enters a tab: raigolmid holds it, and a proxy
here swaps it in for the placeholder each agent is given.

Every agent container — each tab, the janitor, the preferences judge's one-shot — gets a
placeholder in place of the credential and `HTTPS_PROXY` naming this proxy, on the
host's address on the containers' default network (`ContainerRuntime.bridge_gateway`). Any
container on the machine can reach that address, a body's included, so the proxy forwards only
a request carrying a placeholder it issued, for an owner still open, with the credential: a
request with no credential at all (Claude Code's connectivity check) goes on as it came, and
one with any other is refused. A placeholder is its owner
and an HMAC of it under `Paths.proxy_secret`, so it outlives a daemon restart with the running
containers holding it, and a closed tab's stops working. What leaves a tab is worth nothing
outside this machine.

The request goes on to Anthropic's API unchanged but for the credential, headers and body as
sent — `anthropic-beta` included, which Claude Code's OAuth needs verbatim — and the answer
comes back as it arrives, so a streamed turn streams. The credential is read on each request,
so one set with `rai credential --set` holds at once.

The placeholder has the credential's own shape (`sk-ant-oat01-…` for an OAuth token,
`sk-ant-api03-…` for an API key), since the agent chooses its authentication by which variable
is set and must choose the one the credential needs.

⚠ **An agent reaches this proxy as its HTTPS proxy, not as its base URL.** Claude Code sends
its feature flags and bootstrap straight to `api.anthropic.com` whatever the base URL says, and
a placeholder there turns every flag off — a tab's channel among them. So `CONNECT api.anthropic.com` is answered here under
a certificate from the machine's own authority (`Authority`), which the agent is told to trust,
and every request inside it has its placeholder swapped like any other.

The user's GitHub sign-in (`Paths.registry_token`) reaches agents the same way: a second
placeholder in `GH_TOKEN`, which gh reads and git's credential helper hands on, swapped in on
`GITHUB_HOSTS`, which are opened like the API host. A placeholder is honoured only on its own
kind's hosts, so neither credential can be spent on the other's. The system bundle an agent
mounts carries the authority, so git, gh and curl trust those hosts as Node does.

`CONNECT` to anywhere else is a plain tunnel — agents browse — except to this machine itself
or the network its uplink sits on: a tab never reaches the host layer through the proxy, whose
address is the host's, nor the Windows host behind QEMU's user network. The target is resolved
once, refused if any address it names is one the host can bind or on that network
(`is_off_limits`), and the tunnel opens to the address that was checked, so a name cannot
resolve elsewhere in between. A container's own traffic to that network is the host
firewall's (`host/firewall/raigolmi.nft`); the proxy runs on the host, so it is refused here.
"""
from __future__ import annotations

import base64
import binascii
import datetime
import errno
import hashlib
import hmac
import http.client
import ipaddress
import os
import re
import secrets
import selectors
import socket
import socketserver
import ssl
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from . import claude_login, credential
from .events import EventLog
from .runtime import ContainerRuntime
from .runtime.base import Mount

UPSTREAM = "https://api.anthropic.com"
INTERCEPTED = urlsplit(UPSTREAM).hostname
# The account's claude.ai connectors, which a tab signed in with the claude.ai sign-in reaches
# with its placeholder. Tunnelled, the 401 there sends Claude Code to refresh the sign-in with
# its refresh placeholder, and the refused refresh signs the tab out.
ANTHROPIC_HOSTS = {"mcp-proxy.anthropic.com": "https://mcp-proxy.anthropic.com"}
# The hosts the user's GitHub sign-in is swapped in for: git over HTTPS, the API gh and a
# REST call use, and release uploads. Each goes on to itself.
GITHUB_HOSTS = ("github.com", "api.github.com", "uploads.github.com")
# Where an agent container finds the authority: Node reads it alone, everything else in the
# system bundle it is appended to.
CA_TARGET = "/usr/local/share/raigolmi/proxy-ca.pem"
BUNDLE_TARGET = "/etc/ssl/certs/ca-certificates.crt"
TUNNEL_TIMEOUT = 30.0
# Fixed, because containers started before a daemon restart keep the URL they were given.
PORT = 47100
OAUTH, API_KEY = credential.CREDENTIAL_KEYS
[GITHUB] = credential.REGISTRY_KEYS
# The claude.ai sign-in's access and refresh tokens (`claude_login.py`), in every tab's
# `.credentials.json` while it is set. Only the access token is ever swapped: the daemon alone refreshes.
LOGIN, LOGIN_REFRESH = "CLAUDE_AI_LOGIN", "CLAUDE_AI_LOGIN_REFRESH"
SHAPE = {OAUTH: "sk-ant-oat01-rai-", API_KEY: "sk-ant-api03-rai-", GITHUB: "ghp_rai-",
         LOGIN: "sk-ant-oat01-rail-", LOGIN_REFRESH: "sk-ant-ort01-rail-"}
# Remote Control's session, which authenticates with the worker credential Anthropic hands it
# (Claude Code 2.1.283's `[remote-bridge] Fetched bridge credentials`) rather than a login.
SESSION_PATHS = "/v1/code/sessions/"
# The variable gh reads its token from; git's credential helper hands it on.
GITHUB_VARIABLE = "GH_TOKEN"
GIT_HELPER = ('!f() { test "$1" = get && printf "username=x-access-token\\npassword=%s\\n" '
              '"$GH_TOKEN"; }; f')
# Hop-by-hop headers (RFC 9110 §7.6.1) and the ones this proxy sets itself.
HOP = frozenset({"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
                 "te", "trailer", "transfer-encoding", "upgrade", "host", "content-length"})
UPSTREAM_TIMEOUT = 600.0
# 2100-01-01, in the milliseconds Claude Code keeps an expiry in.
FAR_EXPIRY_MS = 4102444800000


class ProxyError(Exception):
    """A request the proxy refuses to forward."""


class Placeholders:
    """Issued and checked against one secret, made on first use and kept 0600."""

    def __init__(self, secret: Path) -> None:
        self.secret_path = secret
        self._key = self._load()

    def _load(self) -> bytes:
        """Written whole beside the path and linked into place, so no start ever reads a key
        cut short; one that is not 32 bytes is refused, never signed with."""
        if not self.secret_path.exists():
            staged = self.secret_path.with_name(f".{self.secret_path.name}.{os.getpid()}")
            fd = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="ascii") as out:
                out.write(secrets.token_hex(32))
                out.flush()
                os.fsync(out.fileno())
            try:
                os.link(staged, self.secret_path)
            finally:
                staged.unlink()
        text = self.secret_path.read_text(encoding="ascii").strip()
        try:
            key = bytes.fromhex(text)
        except ValueError as exc:
            raise ProxyError(f"{self.secret_path} is not a hex key: {exc}") from exc
        if len(key) != 32:
            raise ProxyError(f"{self.secret_path} holds {len(key)} bytes, not the 32 "
                                       "of a placeholder key")
        return key

    def _mac(self, owner: str) -> str:
        return hmac.new(self._key, owner.encode(), hashlib.sha256).hexdigest()[:40]

    def issue(self, owner: str, kind: str) -> str:
        return f"{SHAPE[kind]}{owner}-{self._mac(owner)}"

    def owner_of(self, token: str, kinds: tuple[str, ...] = tuple(SHAPE)) -> str | None:
        """The owner a placeholder of one of `kinds` was issued to, or None for anything else."""
        for prefix in (SHAPE[kind] for kind in kinds):
            if token.startswith(prefix):
                owner, _, mac = token[len(prefix):].rpartition("-")
                if owner and hmac.compare_digest(mac, self._mac(owner)):
                    return owner
        return None


class Authority:
    """The machine's own certificate authority, made on first use, the key kept 0600: what an
    agent trusts for the intercepted hosts, and nothing else is ever signed by it. `bundle` is
    the host's trusted roots with it appended, rewritten at each start so the roots stay the
    host's current ones."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.cert = directory / "ca.pem"
        self.bundle = directory / "bundle.pem"
        self._key_path = directory / "ca.key"
        if not self._key_path.is_file() or not _identified(self.cert):
            self._make()
        self._key = serialization.load_pem_private_key(self._key_path.read_bytes(), None)
        self._ca = x509.load_pem_x509_certificate(self.cert.read_bytes())
        self.bundle.write_bytes(_roots().rstrip(b"\n") + b"\n" + self.cert.read_bytes())

    def _make(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "RaiGolmi credential proxy")])
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - datetime.timedelta(days=1))
                .not_valid_after(now + datetime.timedelta(days=3650))
                .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                .add_extension(x509.KeyUsage(
                    digital_signature=False, content_commitment=False, key_encipherment=False,
                    data_encipherment=False, key_agreement=False, key_cert_sign=True,
                    crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
                .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
                               critical=False)
                .sign(key, hashes.SHA256()))
        _write_private(self._key_path, key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()))
        self.cert.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

    def context_for(self, host: str) -> ssl.SSLContext:
        """A server context presenting a certificate for `host` under this authority."""
        key = ec.generate_private_key(ec.SECP256R1())
        now = datetime.datetime.now(datetime.timezone.utc)
        leaf = (x509.CertificateBuilder()
                .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)]))
                .issuer_name(self._ca.subject).public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - datetime.timedelta(days=1))
                .not_valid_after(now + datetime.timedelta(days=365))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
                               critical=False)
                .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(
                    self._ca.public_key()), critical=False)
                .sign(self._key, hashes.SHA256()))
        chain = self.directory / f"{host}.pem"
        _write_private(chain, key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()) + leaf.public_bytes(serialization.Encoding.PEM))
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(chain)
        return context


def _roots() -> bytes:
    """The host's trusted roots as one PEM bundle: OpenSSL's CA file where the host has one,
    else every certificate its hashed CA directory names (Fedora 44 ships only that), each
    once however many hash names point at it."""
    paths = ssl.get_default_verify_paths()
    if paths.cafile is not None:
        return Path(paths.cafile).read_bytes()
    if paths.capath is None or not Path(paths.capath).is_dir():
        raise ProxyError("this machine's OpenSSL names no trusted-roots file or directory to "
                         f"extend ({paths})")
    hashed = re.compile(r"[0-9a-f]{8}\.[0-9]+")
    certs = sorted({entry.resolve() for entry in Path(paths.capath).iterdir()
                    if hashed.fullmatch(entry.name)})
    if not certs:
        raise ProxyError(f"{paths.capath}, this machine's OpenSSL CA directory, names no certificate")
    return b"\n".join(cert.read_bytes().rstrip(b"\n") for cert in certs)


def _identified(cert: Path) -> bool:
    """Whether the authority carries its key identifier, which OpenSSL's strict verification
    (Python's default context from 3.13) requires of a CA, as it requires the matching
    authority key identifier of every certificate the CA signs. One without is made again."""
    ca = x509.load_pem_x509_certificate(cert.read_bytes())
    try:
        ca.extensions.get_extension_for_class(x509.SubjectKeyIdentifier)
    except x509.ExtensionNotFound:
        return False
    return True


def _write_private(path: Path, content: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as out:
        out.write(content)


class Broker:
    """The credential, and what an agent container is given in its place."""

    def __init__(self, credentials: Path, secret: Path, authority: Path,
                 runtime: ContainerRuntime, github: Path, login: Path) -> None:
        self.credentials = credentials
        self.github = github
        self.login = login
        self.placeholders = Placeholders(secret)
        self.authority = Authority(authority)
        self.runtime = runtime

    def environment(self, owner: str, login: bool = False) -> dict[str, str]:
        """Raises what `credential.read` raises: an agent is never started without one.
        `login`: the tab signs in with the claude.ai sign-in's placeholders (`login_files`)
        instead, since Claude Code prefers a credential variable to them."""
        [kind] = credential.read(self.credentials)
        proxy = f"http://{self.runtime.bridge_gateway()}:{PORT}"
        # The GitHub placeholder is issued whether or not the user has signed in yet: the
        # sign-in is read on each request, so one made later holds in tabs already open.
        return {**({} if login else {kind: self.placeholders.issue(owner, kind)}),
                GITHUB_VARIABLE: self.placeholders.issue(owner, GITHUB),
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "credential.https://github.com.helper",
                "GIT_CONFIG_VALUE_0": GIT_HELPER,
                "HTTPS_PROXY": proxy, "https_proxy": proxy,
                "NODE_EXTRA_CA_CERTS": CA_TARGET}

    def login_files(self, owner: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """A tab's `.credentials.json` for the claude.ai sign-in, placeholders in place of its
        tokens and an expiry it never reaches, so it never refreshes; and the account for its
        `.claude.json`, which Remote Control reads the organization from. Raises what
        `claude_login.read` raises."""
        login = claude_login.read(self.login)
        oauth = {**login["claudeAiOauth"],
                 "accessToken": self.placeholders.issue(owner, LOGIN),
                 "refreshToken": self.placeholders.issue(owner, LOGIN_REFRESH),
                 "expiresAt": FAR_EXPIRY_MS}
        oauth.pop("refreshTokenExpiresAt", None)
        return {"claudeAiOauth": oauth}, login["oauthAccount"]

    def mounts(self) -> tuple[Mount, ...]:
        return (Mount(source=str(self.authority.cert), target=CA_TARGET, read_only=True),
                Mount(source=str(self.authority.bundle), target=BUNDLE_TARGET, read_only=True))



# The usage limit's reset, as the account's own 429 says it: unix epoch seconds.
LIMIT_RESET = "anthropic-ratelimit-unified-reset"


def _limit_reset(answer: http.client.HTTPResponse) -> dict[str, object]:
    """When a 429's limit resets, or None with what the header said instead (`limits.py`)."""
    said = answer.getheader(LIMIT_RESET)
    try:
        return {"resets_at": float(said), "said": said}
    except (TypeError, ValueError):
        return {"resets_at": None, "said": said}

class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: "_Server"

    def do_GET(self) -> None:
        self._forward()

    do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = do_GET

    def log_message(self, format: str, *args) -> None:
        """Every refusal is an event; a request forwarded is not worth one."""

    def do_CONNECT(self) -> None:
        host, _, port = self.path.rpartition(":")
        proxy = self.server.proxy
        if host in proxy.upstreams and port == "443":
            self.send_response(200)
            self.end_headers()
            # The requests inside are read by this same handler's loop, over TLS.
            self.intercepted = host
            try:
                self.connection = proxy.tls[host].wrap_socket(self.connection, server_side=True)
            except (ssl.SSLError, OSError) as exc:
                # What an agent that does not trust the authority says: CA_TARGET not mounted.
                proxy.events.emit("credproxy.handshake_failed", target=self.path, error=str(exc))
                self.close_connection = True
                return
            self.rfile = self.connection.makefile("rb", self.rbufsize)
            self.wfile = socketserver._SocketWriter(self.connection)
            self.close_connection = False
            return
        try:
            remote = _tunnel_to(host.strip("[]"), int(port), proxy.off_limits)
        except ProxyError as exc:
            proxy.events.emit("credproxy.tunnel_refused", target=self.path, reason=str(exc))
            self.send_error(403, str(exc))
            return
        except (OSError, ValueError) as exc:
            proxy.events.emit("credproxy.tunnel_failed", target=self.path, error=str(exc))
            self.send_error(502, f"cannot reach {self.path}: {exc}")
            return
        self.send_response(200)
        self.end_headers()
        self.close_connection = True
        _pipe(self.connection, remote)

    def _forward(self) -> None:
        proxy = self.server.proxy
        # A request sent to the proxy itself rather than inside a CONNECT is for Anthropic.
        host = getattr(self, "intercepted", proxy.intercepted)
        on_login = proxy.on_login(self.headers.items(), host)
        settled = proxy.renewal.settled() if on_login else None
        try:
            owner, outgoing = proxy.outgoing(self.headers.items(), host, self.path)
        except ProxyError as exc:
            proxy.events.emit("credproxy.refused", host=host, path=self.path, reason=str(exc))
            self._refuse(401, str(exc))
            return
        body = self._body()
        upstream = urlsplit(proxy.upstreams[host])
        connect = (http.client.HTTPSConnection if upstream.scheme == "https"
                   else http.client.HTTPConnection)
        started = time.monotonic()
        conn = connect(upstream.hostname, upstream.port, timeout=UPSTREAM_TIMEOUT)
        try:
            try:
                conn.request(self.command, self.path, body=body, headers=outgoing)
                answer = conn.getresponse()
                if on_login and answer.status == 401 and proxy.renewal.overlapped(settled):
                    # Refused on the token a renewal was retiring: sent again on the new one.
                    answer.read()
                    conn.close()
                    proxy.renewal.settled()
                    owner, outgoing = proxy.outgoing(self.headers.items(), host, self.path)
                    proxy.events.emit("credproxy.resent", owner=owner, path=self.path)
                    conn = connect(upstream.hostname, upstream.port, timeout=UPSTREAM_TIMEOUT)
                    conn.request(self.command, self.path, body=body, headers=outgoing)
                    answer = conn.getresponse()
            except OSError as exc:
                proxy.events.emit("credproxy.upstream_failed", owner=owner, path=self.path,
                                  error=str(exc))
                self._refuse(502, f"{host} could not be reached: {exc}", "api_error")
                return
            except ProxyError as exc:
                proxy.events.emit("credproxy.refused", host=host, path=self.path,
                                  reason=str(exc))
                self._refuse(401, str(exc))
                return
            if answer.status == 429:
                proxy.events.emit("credproxy.limited", owner=owner, path=self.path,
                                  **_limit_reset(answer))
            try:
                self._relay(answer)
            except OSError as exc:
                # The agent hung up before its answer was through — its own timeout or a
                # request it abandoned — which is not the API failing.
                proxy.events.emit("credproxy.client_gone", owner=owner, path=self.path,
                                  status=answer.status, error=str(exc),
                                  seconds=round(time.monotonic() - started, 3))
                self.close_connection = True
        finally:
            conn.close()

    def _relay(self, answer: http.client.HTTPResponse) -> None:
        self.send_response(answer.status, answer.reason)
        for name, value in answer.getheaders():
            if name.lower() not in HOP:
                self.send_header(name, value)
        if self.command == "HEAD" or answer.status in (204, 304):
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        while chunk := answer.read1(65536):
            self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
            self.wfile.flush()
        self.wfile.write(b"0\r\n\r\n")

    def _body(self) -> bytes | None:
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            parts = []
            while (size := int(self.rfile.readline().split(b";")[0], 16)) > 0:
                parts.append(self.rfile.read(size))
                self.rfile.readline()
            while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                pass
            return b"".join(parts)
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else None

    def _refuse(self, status: int, why: str, kind: str = "authentication_error") -> None:
        body = (f'{{"type":"error","error":{{"type":"{kind}",'
                f'"message":"raigolmi credential proxy: {why}"}}}}').encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def is_this_machine(address: str) -> bool:
    """Whether `address` is the host's own — loopback, an unspecified address, or one on any
    of its interfaces. Positive evidence: the kernel lets the host bind only its own."""
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((address, 0))
        except OSError as exc:
            if exc.errno == errno.EADDRNOTAVAIL:
                return False
            raise
    return True


Network = ipaddress.IPv4Network | ipaddress.IPv6Network


def uplink_networks(route: str, ipv6_route: str) -> list[Network]:
    """The networks reached without a gateway on the interface that carries the default route,
    from the kernel's `/proc/net/route` and `/proc/net/ipv6_route`. Under the launcher that is
    QEMU's user network, every address of which is QEMU itself, and its host address is the
    Windows host's own loopback — QMP, and every service listening there."""
    rows4 = [line.split() for line in route.splitlines()[1:] if line.strip()]
    rows6 = [line.split() for line in ipv6_route.splitlines() if line.strip()]
    uplinks = ({r[0] for r in rows4 if int(r[1], 16) == 0 and int(r[2], 16) != 0}
               | {r[9] for r in rows6 if int(r[0], 16) == 0 and int(r[1], 16) == 0})
    networks: list[Network] = []
    for iface, dest, gateway, _flags, _ref, _use, _metric, mask, *_ in rows4:
        if iface in uplinks and int(gateway, 16) == 0 and int(dest, 16) != 0:
            networks.append(ipaddress.IPv4Network(f"{_route_ipv4(dest)}/{_route_ipv4(mask)}",
                                                  strict=False))
    for dest, plen, _src, _splen, nexthop, *_rest, iface in rows6:
        if iface in uplinks and int(nexthop, 16) == 0 and int(plen, 16) != 0:
            networks.append(ipaddress.IPv6Network(
                (ipaddress.IPv6Address(bytes.fromhex(dest)), int(plen, 16)), strict=False))
    return networks


def _route_ipv4(field: str) -> ipaddress.IPv4Address:
    """`/proc/net/route` prints each address as a native-order integer."""
    return ipaddress.IPv4Address(int(field, 16).to_bytes(4, sys.byteorder))


def is_off_limits(address: str) -> bool:
    """Whether a tab may not tunnel to `address`: the host's own, or on the network its uplink
    is attached to (`uplink_networks`)."""
    ip = ipaddress.ip_address(address.split("%")[0])
    # `::ffff:10.0.2.2` connects to 10.0.2.2.
    ip = getattr(ip, "ipv4_mapped", None) or ip
    networks = uplink_networks(Path("/proc/net/route").read_text(),
                               Path("/proc/net/ipv6_route").read_text())
    if any(ip in network for network in networks if network.version == ip.version):
        return True
    return is_this_machine(address)


def _tunnel_to(host: str, port: int,
               off_limits: Callable[[str], bool]) -> socket.socket:
    addresses = [info[4] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]
    refused = [sockaddr[0] for sockaddr in addresses if off_limits(sockaddr[0])]
    if refused:
        raise ProxyError(f"{host} is this machine or the network it sits on "
                         f"({', '.join(refused)}); a tab reaches the internet through the "
                         "proxy, never the host or what is behind it")
    failures = []
    for sockaddr in addresses:
        try:
            return socket.create_connection(sockaddr[:2], timeout=TUNNEL_TIMEOUT)
        except OSError as exc:
            failures.append(f"{sockaddr[0]}: {exc}")
    raise OSError("; ".join(failures))


def _pipe(a: socket.socket, b: socket.socket) -> None:
    """Bytes both ways until either side closes."""
    with b, selectors.DefaultSelector() as sel:
        a.settimeout(None)
        b.settimeout(None)
        sel.register(a, selectors.EVENT_READ, b)
        sel.register(b, selectors.EVENT_READ, a)
        while True:
            for key, _ in sel.select():
                try:
                    data = key.fileobj.recv(65536)
                    if not data:
                        return
                    key.data.sendall(data)
                except OSError:
                    return


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # Every tab's session opens several connections at its start.
    request_queue_size = 64
    proxy: "CredentialProxy"


class CredentialProxy:
    def __init__(self, broker: Broker, events: EventLog, open_owner: Callable[[str], bool],
                 host: str, port: int = PORT, upstream: str = UPSTREAM,
                 intercepted: str = INTERCEPTED,
                 github: dict[str, str] | None = None,
                 anthropic: dict[str, str] | None = None,
                 off_limits: Callable[[str], bool] = is_off_limits,
                 renewal: claude_login.Renewal | None = None) -> None:
        self.broker = broker
        self.renewal = renewal if renewal is not None else claude_login.Renewal()
        self.off_limits = off_limits
        self.events = events
        self.open_owner = open_owner
        self.intercepted = intercepted
        self.github = github if github is not None else {h: f"https://{h}" for h in GITHUB_HOSTS}
        self.upstreams = {intercepted: upstream,
                          **(ANTHROPIC_HOSTS if anthropic is None else anthropic), **self.github}
        self.tls = {h: broker.authority.context_for(h) for h in self.upstreams}
        self._server = _Server((host, port), _Handler)
        self._server.proxy = self

    @property
    def address(self) -> tuple[str, int]:
        return self._server.server_address[:2]

    def on_login(self, headers: list[tuple[str, str]], host: str) -> bool:
        """Whether the request carries a placeholder for the claude.ai sign-in."""
        if host in self.github:
            return False
        sent = {name.lower(): value for name, value in headers}
        token = (sent.get("authorization", "").removeprefix("Bearer ").strip()
                 or sent.get("x-api-key", "").strip())
        return bool(token) and self.broker.placeholders.owner_of(token, (LOGIN,)) is not None

    def outgoing(self, headers: list[tuple[str, str]], host: str,
                 path: str = "") -> tuple[str | None, dict[str, str]]:
        """The request's owner and the headers it goes upstream to `host` with: the credential
        in place of the placeholder, everything else as sent. No owner: it carried none, or it
        is Remote Control's session carrying its own (`SESSION_PATHS`)."""
        if host in self.github:
            return self._outgoing_github(headers)
        sent = {name.lower(): value for name, value in headers}
        token = (sent.get("authorization", "").removeprefix("Bearer ").strip()
                 or sent.get("x-api-key", "").strip())
        if not token or (path.startswith(SESSION_PATHS)
                         and self.broker.placeholders.owner_of(token) is None):
            return None, {name: value for name, value in headers
                          if name.lower() not in HOP}
        if (owner := self.broker.placeholders.owner_of(token, (LOGIN,))) is not None:
            if not self.open_owner(owner):
                raise ProxyError(f"{owner} is closed")
            try:
                real = claude_login.read(self.broker.login)["claudeAiOauth"]["accessToken"]
            except claude_login.LoginError as exc:
                raise ProxyError(str(exc)) from exc
            out = {name: value for name, value in headers
                   if name.lower() not in HOP | {"authorization", "x-api-key"}}
            out["Authorization"] = f"Bearer {real}"
            return owner, out
        owner = self._owner(token, (OAUTH, API_KEY))
        [(kind, real)] = credential.read(self.broker.credentials).items()
        out = {name: value for name, value in headers
               if name.lower() not in HOP | {"authorization", "x-api-key"}}
        if kind == OAUTH:
            out["Authorization"] = f"Bearer {real}"
        else:
            out["x-api-key"] = real
        return owner, out

    def _owner(self, token: str, kinds: tuple[str, ...]) -> str:
        owner = self.broker.placeholders.owner_of(token, kinds)
        if owner is None:
            raise ProxyError("the request carries no placeholder this machine issued for "
                             "this host")
        if not self.open_owner(owner):
            raise ProxyError(f"{owner} is closed")
        return owner

    def _outgoing_github(self, headers: list[tuple[str, str]]) -> tuple[str | None, dict[str, str]]:
        """git sends the placeholder as Basic auth's password, gh as a `token` or Bearer."""
        out = {name: value for name, value in headers if name.lower() not in HOP}
        sent = next((value for name, value in headers if name.lower() == "authorization"), "")
        if not sent:
            return None, out
        scheme, _, value = sent.strip().partition(" ")
        if scheme.lower() == "basic":
            try:
                user, _, token = base64.b64decode(value, validate=True).decode().partition(":")
            except (binascii.Error, UnicodeDecodeError) as exc:
                raise ProxyError(f"unreadable Basic authorization: {exc}") from exc
        else:
            user, token = None, value.strip()
        owner = self._owner(token, (GITHUB,))
        try:
            real = credential.read(self.broker.github, credential.REGISTRY_KEYS)[GITHUB]
        except credential.CredentialError as exc:
            raise ProxyError(str(exc)) from exc
        out = {name: value for name, value in out.items() if name.lower() != "authorization"}
        out["Authorization"] = (f"Basic {base64.b64encode(f'{user}:{real}'.encode()).decode()}"
                                if user is not None else f"{scheme} {real}")
        return owner, out

    def serve(self) -> None:
        self.events.emit("credproxy.listening", host=self.address[0], port=self.address[1])
        self._server.serve_forever()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
