"""The credential never enters a tab (`credproxy.py`): every agent is
given a placeholder, and the proxy swaps the credential in for one it issued, for an owner still
open, passing everything else — `anthropic-beta` above all — and streaming the answer back."""
from __future__ import annotations

import base64
import http.client
import json
import socket
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from raigolmid import claude_login, credential
from raigolmid.credproxy import Broker, CredentialProxy, Placeholders
from raigolmid.events import EventLog
from tests.fakeruntime import FakeRuntime

LOGIN = {"claudeAiOauth": {"accessToken": "sk-ant-oat01-the-real-login",
                           "refreshToken": "sk-ant-ort01-the-real-refresh",
                           "expiresAt": 1790000000000, "refreshTokenExpiresAt": 1800000000000,
                           "scopes": ["user:inference", "user:profile",
                                      "user:sessions:claude_code"],
                           "subscriptionType": None},
         "oauthAccount": {"organizationUuid": "org-1", "displayName": "C"}}
EVENTS = [b"event: message_start\ndata: {}\n\n", b"event: message_stop\ndata: {}\n\n"]


class Upstream(BaseHTTPRequestHandler):
    """Anthropic's API as the proxy meets it: what it was sent, and a streamed answer."""
    protocol_version = "HTTP/1.1"
    seen: list[dict] = []
    # The headers of the usage limit's 429, when the account is at it.
    limited: dict[str, str] | None = None
    # Access tokens refused as the API refuses a retired one, and what runs as one is refused.
    retired: set[str] = set()
    on_retired = None

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers["Content-Length"]))
        Upstream.seen.append({"path": self.path, "headers": dict(self.headers.items()),
                              "body": body})
        if self.headers.get("Authorization", "").removeprefix("Bearer ") in Upstream.retired:
            if Upstream.on_retired is not None:
                Upstream.on_retired()
            said = b'{"type":"error","error":{"type":"authentication_error"}}'
            self.send_response(401)
            self.send_header("Content-Length", str(len(said)))
            self.end_headers()
            self.wfile.write(said)
            return
        if Upstream.limited is not None:
            said = b'{"type":"error","error":{"type":"rate_limit_error"}}'
            self.send_response(429)
            for name, value in Upstream.limited.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(said)))
            self.end_headers()
            self.wfile.write(said)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for event in EVENTS:
            self.wfile.write(b"%x\r\n%s\r\n" % (len(event), event))
        self.wfile.write(b"0\r\n\r\n")

    def log_message(self, *args) -> None:
        pass


@pytest.fixture()
def machine(tmp_path):
    Upstream.seen, Upstream.limited, Upstream.retired, Upstream.on_retired = [], None, set(), None
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    credentials = tmp_path / "agent-credentials"
    credential.write(credentials, "CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-the-real-one")
    credential.write(tmp_path / "registry-token", "GITHUB_TOKEN", "ghp_the-real-one",
                     credential.REGISTRY_KEYS)
    claude_login.write(tmp_path / "claude-login.json", LOGIN)
    broker = Broker(credentials, tmp_path / "proxy-secret", tmp_path / "proxy-ca", FakeRuntime(),
                    tmp_path / "registry-token", tmp_path / "claude-login.json")
    events = EventLog(tmp_path / "events.jsonl", epoch=1)
    open_tabs = {"tab-1", "raigolmid-judge"}
    proxy = CredentialProxy(broker, events, open_tabs.__contains__, host="127.0.0.1", port=0,
                            upstream=f"http://127.0.0.1:{upstream.server_address[1]}",
                            intercepted="api.test",
                            github={"git.test": f"http://127.0.0.1:{upstream.server_address[1]}"},
                            anthropic={"mcp.test": f"http://127.0.0.1:{upstream.server_address[1]}"})
    threading.Thread(target=proxy.serve, daemon=True).start()
    yield broker, proxy, events, open_tabs
    proxy.close()
    upstream.shutdown()


def _post(proxy, headers: dict[str, str], body: bytes = b'{"model":"x"}'):
    conn = http.client.HTTPConnection(*proxy.address, timeout=10)
    conn.request("POST", "/v1/messages?beta=true", body=body, headers=headers)
    answer = conn.getresponse()
    return answer.status, answer.read()


def test_a_placeholder_names_its_owner_and_nothing_else_passes(tmp_path):
    placeholders = Placeholders(tmp_path / "proxy-secret")
    token = placeholders.issue("tab-12", "CLAUDE_CODE_OAUTH_TOKEN")
    assert token.startswith("sk-ant-oat01-") and placeholders.owner_of(token) == "tab-12"
    assert placeholders.owner_of(token[:-1] + ("0" if token[-1] != "0" else "1")) is None
    assert placeholders.owner_of(token.replace("tab-12", "tab-13")) is None
    assert placeholders.owner_of("sk-ant-oat01-the-real-one") is None
    again = Placeholders(tmp_path / "proxy-secret")
    assert again.owner_of(token) == "tab-12", "a restarted daemon honours what it issued"
    assert (tmp_path / "proxy-secret").stat().st_mode & 0o777 == 0o600


def test_the_credential_is_swapped_in_and_the_rest_passes_verbatim(machine):
    broker, proxy, _, _ = machine
    placeholder = broker.environment("tab-1")["CLAUDE_CODE_OAUTH_TOKEN"]
    status, body = _post(proxy, {
        "Authorization": f"Bearer {placeholder}", "anthropic-beta": "oauth-2025-04-20,x",
        "anthropic-version": "2023-06-01", "Content-Type": "application/json"})
    assert status == 200 and body == b"".join(EVENTS), "the stream comes back whole"
    [sent] = Upstream.seen
    assert sent["headers"]["Authorization"] == "Bearer sk-ant-oat01-the-real-one"
    assert sent["headers"]["anthropic-beta"] == "oauth-2025-04-20,x"
    assert sent["path"] == "/v1/messages?beta=true" and sent["body"] == b'{"model":"x"}'


@pytest.mark.parametrize("said, resets_at", [("1790000000", 1790000000.0), (None, None)])
def test_the_usage_limits_reset_is_read_from_its_429(machine, said, resets_at):
    broker, proxy, events, _ = machine
    Upstream.limited = {} if said is None else {"anthropic-ratelimit-unified-reset": said}
    placeholder = broker.environment("tab-1")["CLAUDE_CODE_OAUTH_TOKEN"]
    status, _ = _post(proxy, {"Authorization": f"Bearer {placeholder}"})
    assert status == 429, "the agent is answered as the API answered"
    [limited] = [e for e in events.tail(50) if e.type == "credproxy.limited"]
    assert (limited.data["owner"], limited.data["resets_at"]) == ("tab-1", resets_at)


def test_a_tab_signs_in_with_placeholders_and_the_sign_in_is_swapped_in(machine):
    broker, proxy, _, _ = machine
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in broker.environment("tab-1", login=True), \
        "Claude Code prefers the variable, and Remote Control refuses what it holds"
    files, account = broker.login_files("tab-1")
    oauth = files["claudeAiOauth"]
    assert "the-real" not in json.dumps(files)
    assert oauth["scopes"] == LOGIN["claudeAiOauth"]["scopes"]
    assert account["organizationUuid"] == "org-1"
    status, _ = _post(proxy, {"Authorization": f"Bearer {oauth['accessToken']}"})
    assert status == 200
    assert Upstream.seen[-1]["headers"]["Authorization"] == "Bearer sk-ant-oat01-the-real-login"
    status, _ = _post(proxy, {"Authorization": f"Bearer {oauth['refreshToken']}"})
    assert status == 401, "the refresh placeholder is never a credential"

    # The account's connectors, whose 401 would have Claude Code sign the tab out.
    conn = _inside(broker, proxy, "mcp.test")
    conn.request("POST", "/v1/mcp/mcpsrv_1", body=b"{}",
                 headers={"Authorization": f"Bearer {oauth['accessToken']}"})
    assert conn.getresponse().status == 200
    assert Upstream.seen[-1]["headers"]["Authorization"] == "Bearer sk-ant-oat01-the-real-login"


def test_remote_controls_session_goes_with_its_own_credential_and_nothing_else_does(machine):
    _, proxy, _, _ = machine
    conn = http.client.HTTPConnection(*proxy.address, timeout=10)
    conn.request("POST", "/v1/code/sessions/cse_1/worker/events", body=b"{}",
                 headers={"Authorization": "Bearer the-session-worker-token"})
    assert conn.getresponse().status == 200
    assert Upstream.seen[-1]["headers"]["Authorization"] == "Bearer the-session-worker-token"
    status, _ = _post(proxy, {"Authorization": "Bearer the-session-worker-token"})
    assert status == 401


def test_an_api_key_goes_as_x_api_key(machine, tmp_path):
    broker, proxy, _, _ = machine
    broker.credentials.unlink()
    credential.write(broker.credentials, "ANTHROPIC_API_KEY", "sk-ant-api03-real")
    env = broker.environment("tab-1")
    assert env["ANTHROPIC_API_KEY"].startswith("sk-ant-api03-rai-")
    status, _ = _post(proxy, {"x-api-key": env["ANTHROPIC_API_KEY"]})
    assert status == 200
    [sent] = Upstream.seen
    assert sent["headers"]["x-api-key"] == "sk-ant-api03-real"
    assert "Authorization" not in sent["headers"]


@pytest.mark.parametrize("token, why", [
    ("sk-ant-oat01-something-else", "no placeholder this machine issued"),
    (None, "tab-9 is closed"),
])
def test_what_it_did_not_issue_or_whose_owner_closed_is_refused(machine, token, why):
    broker, proxy, events, _ = machine
    token = token or broker.environment("tab-9")["CLAUDE_CODE_OAUTH_TOKEN"]
    status, body = _post(proxy, {"Authorization": f"Bearer {token}"})
    assert status == 401 and why in json.loads(body)["error"]["message"]
    assert Upstream.seen == []
    refused = [e for e in events.tail(50) if e.type == "credproxy.refused"]
    assert refused and token not in json.dumps(refused[-1].data)


def test_a_request_with_no_credential_goes_on_untouched(machine):
    """Claude Code's connectivity check (`/api/hello`) carries none."""
    _, proxy, events, _ = machine
    status, _ = _post(proxy, {"Content-Type": "application/json"})
    assert status == 200
    [sent] = Upstream.seen
    assert "Authorization" not in sent["headers"] and "x-api-key" not in sent["headers"]
    assert not [e for e in events.tail(50) if e.type == "credproxy.refused"]


def _connect(proxy, target: str) -> socket.socket:
    sock = socket.create_connection(proxy.address, timeout=10)
    sock.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
    head = b""
    while not head.endswith(b"\r\n\r\n"):
        head += sock.recv(1)
    assert head.startswith(b"HTTP/1.1 200"), head
    return sock


def test_the_api_host_is_opened_under_the_machines_authority_and_swapped_like_any_request(
        machine):
    """Claude Code sends its flags and bootstrap straight to the API host, whatever its base
    URL: they have to pass through here, as the agent's HTTPS proxy, or every flag is off."""
    broker, proxy, _, _ = machine
    trusted = ssl.create_default_context(cafile=str(broker.authority.cert))
    tls = trusted.wrap_socket(_connect(proxy, "api.test:443"), server_hostname="api.test")
    conn = http.client.HTTPConnection("api.test")
    conn.sock = tls
    token = broker.environment("tab-1")["CLAUDE_CODE_OAUTH_TOKEN"]
    conn.request("POST", "/api/eval/sdk-x", body=b"{}",
                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    answer = conn.getresponse()
    assert answer.status == 200 and answer.read() == b"".join(EVENTS)
    [seen] = Upstream.seen
    assert seen["path"] == "/api/eval/sdk-x"
    assert seen["headers"]["Authorization"] == "Bearer sk-ant-oat01-the-real-one"


def test_any_other_host_is_a_plain_tunnel(machine):
    """Agents browse; only the API host is opened. The only
    server a test can reach is on this machine, so it is declared not to be."""
    _, proxy, _, _ = machine
    proxy.off_limits = lambda address: False
    echo = socket.create_server(("127.0.0.1", 0))

    def serve() -> None:
        conn, _ = echo.accept()
        with conn:
            conn.sendall(conn.recv(100).upper())
    threading.Thread(target=serve, daemon=True).start()
    with _connect(proxy, f"127.0.0.1:{echo.getsockname()[1]}") as tunnel:
        tunnel.sendall(b"raw bytes")
        assert tunnel.recv(100) == b"RAW BYTES"
    echo.close()


def test_a_tunnel_to_this_machine_is_refused(machine):
    """A tab never reaches the host layer through the proxy, whose address is the host's:
    loopback, and the host's own addresses, by name or number."""
    _, proxy, events, _ = machine
    listening = socket.create_server(("127.0.0.1", 0))
    port = listening.getsockname()[1]
    for target in (f"127.0.0.1:{port}", f"localhost:{port}", f"{proxy.address[0]}:{port}"):
        with socket.create_connection(proxy.address, timeout=10) as sock:
            sock.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
            assert sock.recv(100).startswith(b"HTTP/1.1 403"), target
    listening.close()
    assert len([e for e in events.tail(50) if e.type == "credproxy.tunnel_refused"]) == 3


# A machine under the launcher, as its kernel prints them: QEMU's user network on the uplink,
# and Docker's two bridges.
VM_ROUTE = """Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT
enp0s3\t00000000\t0202000A\t0003\t0\t0\t100\t00000000\t0\t0\t0
enp0s3\t0002000A\t00000000\t0001\t0\t0\t100\t00FFFFFF\t0\t0\t0
docker0\t000011AC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0
br-bf3c10c8e275\t000013AC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0
"""
VM_IPV6_ROUTE = """\
fe800000000000000000000000000000 40 00000000000000000000000000000000 00 00000000000000000000000000000000 00000100 00000001 00000000 00000001  docker0
fe800000000000000000000000000000 40 00000000000000000000000000000000 00 00000000000000000000000000000000 00000400 00000002 00000000 00000001   enp0s3
fec00000000000000000000000000000 40 00000000000000000000000000000000 00 00000000000000000000000000000000 00000064 00000003 00000000 00000001   enp0s3
00000000000000000000000000000000 00 00000000000000000000000000000000 00 fe800000000000000000000000000002 00000064 0000000d 00000000 00000003   enp0s3
"""


def test_the_network_behind_the_uplink_is_off_limits():
    """Under the launcher the uplink is QEMU's user network, whose host address is Windows'
    own loopback; a tab reaches past it to the internet, never onto it."""
    import ipaddress
    from raigolmid.credproxy import uplink_networks
    networks = uplink_networks(VM_ROUTE, VM_IPV6_ROUTE)
    assert sorted(map(str, networks)) == ["10.0.2.0/24", "fe80::/64", "fec0::/64"]
    inside = lambda a: any(ipaddress.ip_address(a) in n for n in networks
                           if n.version == ipaddress.ip_address(a).version)
    assert inside("10.0.2.2") and inside("fec0::2") and not inside("140.82.112.3")
    assert not inside("172.17.0.2")


def test_an_agent_is_pointed_at_the_proxy_and_given_the_authority(machine):
    broker, _, _, _ = machine
    env = broker.environment("tab-1")
    assert env["HTTPS_PROXY"].endswith(":47100") and "ANTHROPIC_BASE_URL" not in env
    ca, bundle = broker.mounts()
    assert (ca.source, ca.target, ca.read_only) == (
        str(broker.authority.cert), env["NODE_EXTRA_CA_CERTS"], True)
    assert bundle.read_only and broker.authority.bundle.read_bytes().endswith(
        broker.authority.cert.read_bytes())
    assert oct((broker.authority.directory / "ca.key").stat().st_mode & 0o777) == "0o600"




def test_the_hosts_firewall_opens_the_proxys_port():
    """The host drops every container's traffic to it but this port (host/firewall/); the
    two are written apart and must agree, or every tab loses the API."""
    from pathlib import Path
    from raigolmid.credproxy import PORT
    rules = (Path(__file__).resolve().parents[2] / "host" / "firewall" / "raigolmi.nft")
    assert f"tcp dport {PORT} accept" in rules.read_text()


def _inside(broker, proxy, host: str) -> http.client.HTTPConnection:
    trusted = ssl.create_default_context(cafile=str(broker.authority.cert))
    conn = http.client.HTTPConnection(host)
    conn.sock = trusted.wrap_socket(_connect(proxy, f"{host}:443"), server_hostname=host)
    return conn


def test_the_github_sign_in_is_swapped_in_on_githubs_hosts_and_only_there(machine):
    """git sends the placeholder as Basic auth's password; the sign-in goes in its place. Each
    kind of placeholder is refused on the other's hosts."""
    broker, proxy, _, _ = machine
    env = broker.environment("tab-1")
    basic = base64.b64encode(f"x-access-token:{env['GH_TOKEN']}".encode()).decode()
    conn = _inside(broker, proxy, "git.test")
    conn.request("POST", "/o/r.git/git-receive-pack", body=b"pack",
                 headers={"Authorization": f"Basic {basic}"})
    assert conn.getresponse().read() == b"".join(EVENTS)
    [seen] = Upstream.seen
    assert base64.b64decode(seen["headers"]["Authorization"].removeprefix("Basic ")) == (
        b"x-access-token:ghp_the-real-one")

    for host, token in (("git.test", env["CLAUDE_CODE_OAUTH_TOKEN"]), ("api.test", env["GH_TOKEN"])):
        conn = _inside(broker, proxy, host)
        conn.request("POST", "/", body=b"{}", headers={"Authorization": f"Bearer {token}"})
        assert conn.getresponse().status == 401, host
    assert len(Upstream.seen) == 1


def test_a_host_with_only_a_hashed_ca_directory_is_extended_all_the_same(tmp_path, monkeypatch):
    """Fedora 44's OpenSSL names no CA file, only a directory of hash-named links."""
    from raigolmid import credproxy
    store, hashed = tmp_path / "store", tmp_path / "certs"
    store.mkdir()
    hashed.mkdir()
    roots = []
    for name in ("one", "two"):
        root = credproxy.Authority(tmp_path / name).cert
        (store / f"{name}.pem").write_bytes(root.read_bytes())
        roots.append(root.read_bytes())
    (hashed / "0000aaaa.0").symlink_to(store / "one.pem")
    (hashed / "0000aaaa.1").symlink_to(store / "one.pem")
    (hashed / "1111bbbb.0").symlink_to(store / "two.pem")
    (hashed / "README").write_text("not a certificate")
    monkeypatch.setattr(credproxy.ssl, "get_default_verify_paths", lambda: ssl.DefaultVerifyPaths(
        None, str(hashed), "SSL_CERT_FILE", "/nowhere/cert.pem", "SSL_CERT_DIR", str(hashed)))
    authority = credproxy.Authority(tmp_path / "proxy")
    bundle = authority.bundle.read_bytes()
    assert all(bundle.count(root.strip()) == 1 for root in roots), "each root once"
    assert bundle.rstrip().endswith(authority.cert.read_bytes().strip())


def _renewed(tmp_path) -> None:
    """The sign-in as a renewal leaves it: a new access token, the old one retired."""
    renewed = json.loads(json.dumps(LOGIN))
    renewed["claudeAiOauth"]["accessToken"] = "sk-ant-oat01-the-renewed-login"
    claude_login.write(tmp_path / "claude-login.json", renewed)
    Upstream.retired.add(LOGIN["claudeAiOauth"]["accessToken"])


def test_a_request_on_the_sign_in_waits_out_a_renewal(machine, tmp_path):
    broker, proxy, _, _ = machine
    oauth, _ = broker.login_files("tab-1")
    answered = []
    with proxy.renewal.running():
        asking = threading.Thread(target=lambda: answered.append(
            _post(proxy, {"Authorization": f"Bearer {oauth['claudeAiOauth']['accessToken']}"})))
        asking.start()
        asking.join(0.5)
        assert asking.is_alive() and not Upstream.seen, "nothing is sent while it runs"
        _renewed(tmp_path)
    asking.join(10)
    assert answered[0][0] == 200
    assert Upstream.seen[-1]["headers"]["Authorization"] == "Bearer sk-ant-oat01-the-renewed-login"


def test_a_request_a_renewal_overlapped_is_sent_again_on_the_new_token(machine, tmp_path):
    broker, proxy, events, _ = machine
    oauth, _ = broker.login_files("tab-1")
    Upstream.retired.add(LOGIN["claudeAiOauth"]["accessToken"])

    def renew() -> None:
        with proxy.renewal.running():
            _renewed(tmp_path)
    Upstream.on_retired = renew
    status, _ = _post(proxy, {"Authorization": f"Bearer {oauth['claudeAiOauth']['accessToken']}"})
    assert status == 200, "the tab never sees the retired token's 401"
    assert [e.type for e in events.tail(20)].count("credproxy.resent") == 1

    Upstream.on_retired = None
    claude_login.write(tmp_path / "claude-login.json", LOGIN)
    status, _ = _post(proxy, {"Authorization": f"Bearer {oauth['claudeAiOauth']['accessToken']}"})
    assert status == 401, "a 401 no renewal explains is the agent's, as the API said it"


def test_a_key_cut_short_is_refused_never_signed_with(tmp_path):
    from raigolmid.credproxy import Placeholders, ProxyError
    secret = tmp_path / "proxy-secret"
    secret.write_text("")
    with pytest.raises(ProxyError, match="0 bytes"):
        Placeholders(secret)
