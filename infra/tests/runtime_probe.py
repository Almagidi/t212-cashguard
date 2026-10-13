"""Explicit disposable nginx/Grafana probe; never starts the production stack.

All projected Docker networks are internal, no production env file is read, and
TLS requests originate inside the isolated harness; no host ports are published. Requires pre-pulled images.
"""

import argparse
import contextlib
import json
import base64
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from test_runtime_boundaries import ROOT, render_compose

PYTHON_IMAGE = "python:3.12.13-slim-bookworm"


def docker(*args, check=True):
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=90
    )
    if check and result.returncode:
        # Do not echo invocation: it may include synthetic password arguments.
        raise RuntimeError("Docker operation failed: " + result.stderr)
    return result


@contextlib.contextmanager
def harness(config):
    prefix = "cg-b-" + uuid.uuid4().hex[:12]
    containers, networks = [], []
    with tempfile.TemporaryDirectory(prefix=prefix) as directory:
        directory = Path(directory)
        try:
            subprocess.run(
                [
                    "openssl",
                    "req",
                    "-x509",
                    "-newkey",
                    "rsa:2048",
                    "-nodes",
                    "-keyout",
                    str(directory / "key.pem"),
                    "-out",
                    str(directory / "cert.pem"),
                    "-days",
                    "1",
                    "-subj",
                    "/CN=nginx",
                    "-addext",
                    "subjectAltName=DNS:nginx",
                ],
                check=True,
                capture_output=True,
                timeout=30,
            )
            netmap = {}
            for network in config["networks"]:
                name = prefix + "-" + network
                docker(
                    "network",
                    "create",
                    "--internal",
                    "--label",
                    "cashguard.agent=codex-b",
                    name,
                )
                networks.append(name)
                netmap[network] = name

            def create(service, image, args, extra=()):
                name = prefix + "-" + service
                nets = list(config["services"][service]["networks"])
                docker(
                    "create",
                    "--name",
                    name,
                    "--label",
                    "cashguard.agent=codex-b",
                    "--network",
                    netmap[nets[0]],
                    "--network-alias",
                    service,
                    *extra,
                    image,
                    *args,
                )
                containers.append(name)
                for network in nets[1:]:
                    docker(
                        "network", "connect", "--alias", service, netmap[network], name
                    )
                docker("start", name)
                return name

            server = (
                "from http.server import BaseHTTPRequestHandler,HTTPServer; import os; "
            )
            server += "exec('class H(BaseHTTPRequestHandler):\\n def do_GET(self):\\n  self.send_response(200); self.end_headers(); self.wfile.write(os.environ[\"MARKER\"].encode())\\n def log_message(self,*args): pass'); "
            server += (
                "HTTPServer(('0.0.0.0',int(os.environ['PORT'])),H).serve_forever()"
            )
            for service, port in (
                ("api", 8000),
                ("web", 3000),
                ("postgres", 5432),
                ("redis", 6379),
            ):
                create(
                    service,
                    PYTHON_IMAGE,
                    ["python", "-u", "-c", server],
                    [
                        "-e",
                        "PORT=" + str(port),
                        "-e",
                        "MARKER=synthetic-" + service,
                        "-v",
                        str(directory / "cert.pem") + ":/synthetic-cert.pem:ro",
                    ],
                )
            grafana_env = config["services"]["grafana"]["environment"]
            extra = [
                item
                for key, value in sorted(grafana_env.items())
                for item in ("-e", key + "=" + value)
            ]
            extra += [
                "-e",
                "GF_ANALYTICS_REPORTING_ENABLED=false",
                "-e",
                "GF_ANALYTICS_CHECK_FOR_UPDATES=false",
                "-e",
                "GF_ANALYTICS_CHECK_FOR_PLUGIN_UPDATES=false",
            ]
            grafana = create(
                "grafana", config["services"]["grafana"]["image"], [], extra
            )
            nginx = create(
                "nginx",
                config["services"]["nginx"]["image"],
                ["nginx", "-g", "daemon off;"],
                [
                    "-v",
                    str(ROOT / "infra/nginx/nginx.conf") + ":/etc/nginx/nginx.conf:ro",
                    "-v",
                    str(directory) + ":/etc/nginx/ssl:ro",
                ],
            )
            yield prefix, nginx, grafana, directory
        finally:
            for name in reversed(containers):
                docker("rm", "-f", name, check=False)
            for name in reversed(networks):
                docker("network", "rm", name, check=False)


def response(url, client, timeout=3):
    script = """import base64,json,ssl,urllib.request,urllib.error,sys
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None
ctx=ssl.create_default_context(cafile='/synthetic-cert.pem')
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect(),urllib.request.HTTPSHandler(context=ctx))
try:
    reply=opener.open(sys.argv[1],timeout=float(sys.argv[2]))
except urllib.error.HTTPError as error:
    reply=error
print(json.dumps([reply.status,dict(reply.headers),base64.b64encode(reply.read()).decode()]))
"""
    result = docker(
        "exec", client, "python", "-c", script, url, str(timeout), check=False
    )
    if result.returncode:
        raise OSError("Synthetic HTTPS request failed: " + result.stderr)
    status, headers, body = json.loads(result.stdout)
    return status, headers, base64.b64decode(body)


def run():
    config = render_compose()
    for image in (
        PYTHON_IMAGE,
        config["services"]["nginx"]["image"],
        config["services"]["grafana"]["image"],
    ):
        docker("image", "inspect", image)
    with harness(config) as (prefix, nginx, grafana, directory):
        # Retry only readiness observations, never a failing contract.
        deadline = time.monotonic() + 45
        while True:
            state = json.loads(docker("inspect", nginx).stdout)[0]["State"]
            if not state["Running"]:
                logs = docker("logs", nginx).stdout + docker("logs", nginx).stderr
                raise AssertionError("nginx failed to start: " + logs)
            check = docker("exec", nginx, "nginx", "-t", check=False)
            if check.returncode == 0:
                break
            if time.monotonic() > deadline:
                raise AssertionError("nginx -t failed: " + check.stderr)
            time.sleep(0.2)
        client = prefix + "-api"
        base = "https://nginx"
        while True:
            try:
                status, _, body = response(base + "/v1/health/live", client)
                if status == 200:
                    break
            except OSError:
                pass
            if time.monotonic() > deadline:
                raise AssertionError("API probe did not become available")
            time.sleep(0.2)
        assert body == b"synthetic-api", body
        assert response(base + "/", client)[2] == b"synthetic-web"
        # Real Grafana readiness, from its own container, without host publishing.
        while docker(
            "exec",
            grafana,
            "wget",
            "-qO-",
            "http://localhost:3000/api/health",
            check=False,
        ).returncode:
            if time.monotonic() > deadline:
                raise AssertionError("Grafana did not become ready")
            time.sleep(0.3)
        status, headers, _ = response(base + "/grafana/", client)
        assert status == 302 and headers["Location"].endswith("/grafana/login"), (
            status,
            headers,
        )
        status, _, body = response(base + "/grafana/login", client)
        assert (
            status == 200 and b"Grafana" in body
        ), "Grafana login is unreachable or redirects to itself"
        status, _, _ = response(base + "/grafana/api/user", client)
        assert status == 401, "Anonymous Grafana user endpoint must reject access"
        # Prove the exact nginx probe executable works before treating failures
        # as connectivity denial. Docker/exec failures must never count as isolation.
        control = docker("exec", nginx, "wget", "-T", "2", "-qO-", "http://api:8000")
        assert control.stdout == "synthetic-api", "nginx wget positive control failed"
        # nginx must not resolve/reach datastore containers, even by IP.
        for service, port in (("postgres", 5432), ("redis", 6379)):
            name = prefix + "-" + service
            data = json.loads(docker("inspect", name).stdout)[0]
            ip = next(iter(data["NetworkSettings"]["Networks"].values()))["IPAddress"]
            for host in (service, ip):
                denied = docker(
                    "exec",
                    nginx,
                    "wget",
                    "-T",
                    "2",
                    "-qO-",
                    "http://" + host + ":" + str(port),
                    check=False,
                )
                assert (
                    denied.returncode == 1
                ), "Datastore probe failed to execute or became reachable"
                assert any(
                    reason in denied.stderr.lower()
                    for reason in (
                        "bad address",
                        "timed out",
                        "no route",
                        "connection refused",
                        "can't connect",
                    )
                ), "Datastore probe did not report a network failure"
            positive = docker(
                "exec",
                prefix + "-api",
                "python",
                "-c",
                'import socket; socket.create_connection(("'
                + service
                + '",'
                + str(port)
                + "),2).close()",
            )
            assert positive.returncode == 0
        docker("stop", grafana)
        status, _, body = response(base + "/grafana/api/user", client, timeout=70)
        assert (
            status in (502, 504) and b"synthetic-web" not in body
        ), "Missing Grafana must fail closed"
        docker("start", grafana)
        deadline = time.monotonic() + 30
        while True:
            status, _, body = response(base + "/grafana/login", client)
            if status == 200 and b"Grafana" in body:
                break
            if time.monotonic() > deadline:
                raise AssertionError("Grafana did not recover")
            time.sleep(0.3)
        print(
            json.dumps(
                {
                    "result": "PASS",
                    "project": prefix,
                    "proof": [
                        "nginx -t",
                        "verified synthetic TLS hostname",
                        "API/web routing",
                        "real Grafana login and anonymous 401",
                        "datastore DNS/IP denied",
                        "API datastore positive control",
                        "Grafana stopped 502/504 and restarted 200",
                    ],
                    "limitations": [
                        "API/web/datastores are synthetic HTTP fixtures",
                        "all projected networks internal; production external egress not tested",
                        "Grafana restart keeps IP; container replacement not tested",
                    ],
                }
            )
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-synthetic", action="store_true", required=True)
    parser.parse_args()
    run()
