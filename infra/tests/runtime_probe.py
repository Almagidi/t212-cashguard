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
import hashlib
import platform
import os
from pathlib import Path

from test_runtime_boundaries import ROOT, render_compose, runtime_environment

PYTHON_IMAGE = "python:3.12.13-slim-bookworm"


def docker(*args, check=True, stdin=None):
    # Keep even synthetic fixture credentials out of the Docker client argv.
    # Docker's '-e KEY' takes the exact value from its child environment; no
    # shell evaluates it. Never share container-inspect environment output.
    invocation = list(args)
    environment = None
    if invocation and invocation[0] in ("run", "create"):
        environment = os.environ.copy()
        for index, argument in enumerate(invocation[:-1]):
            if argument == "-e" and "=" in invocation[index + 1]:
                key, value = invocation[index + 1].split("=", 1)
                environment[key] = value
                invocation[index + 1] = key
    result = subprocess.run(
        ["docker", *invocation],
        env=environment,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=90,
    )
    if check and result.returncode:
        # Do not echo invocation: it may include synthetic password arguments.
        raise RuntimeError("Docker operation failed: " + result.stderr)
    return result


def cleanup_owned(containers, networks, prefix, volumes=(), verify_all=True):
    errors = []
    for kind, names in (
        ("container", containers),
        ("network", networks),
        ("volume", volumes),
    ):
        for name in reversed(names):
            try:
                result = docker(kind, "inspect", name, check=False)
                if result.returncode:
                    if "no such" in result.stderr.lower():
                        continue
                    raise RuntimeError("Cannot verify owned resource " + name)
                data = json.loads(result.stdout)[0]
                labels = (
                    data.get("Config", {}).get("Labels", {})
                    if kind == "container"
                    else data.get("Labels", {})
                )
                if labels.get("cashguard.run") != prefix:
                    raise RuntimeError("Refusing unowned resource " + name)
                removed = (
                    docker("rm", "-f", name, check=False)
                    if kind == "container"
                    else docker(kind, "rm", name, check=False)
                )
                if removed.returncode:
                    raise RuntimeError("Removal failed for " + name)
            except (RuntimeError, subprocess.TimeoutExpired, ValueError) as error:
                errors.append(str(error))
    # Filters are run-scoped. Failure to inspect/list is never evidence of absence.
    listings = [("ps", "-aq"), ("network", "ls", "-q")]
    if volumes:
        listings.append(("volume", "ls", "-q"))
    for args in listings if verify_all else []:
        try:
            remaining = docker(*args, "--filter", "label=cashguard.run=" + prefix)
            if remaining.stdout.strip():
                errors.append(
                    "Residual task-owned resource IDs: " + remaining.stdout.strip()
                )
        except (RuntimeError, subprocess.TimeoutExpired) as error:
            errors.append(str(error))
    if errors:
        raise RuntimeError("Synthetic cleanup failed: " + "; ".join(errors))
    print(
        json.dumps(
            {
                "cleanup": "PASS",
                "scope": "all run resources" if verify_all else "listed subset only",
                "run": prefix,
                "containers": len(containers),
                "networks": len(networks),
                "volumes": len(volumes),
            }
        ),
        flush=True,
    )


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
                networks.append(name)
                docker(
                    "network",
                    "create",
                    "--internal",
                    "--label",
                    "cashguard.agent=codex-b",
                    "--label",
                    "cashguard.run=" + prefix,
                    name,
                )
                netmap[network] = name

            def create(service, image, args, extra=()):
                name = prefix + "-" + service
                nets = list(config["services"][service]["networks"])
                containers.append(name)
                docker(
                    "create",
                    "--name",
                    name,
                    "--label",
                    "cashguard.agent=codex-b",
                    "--label",
                    "cashguard.run=" + prefix,
                    "--network",
                    netmap[nets[0]],
                    "--network-alias",
                    service,
                    *extra,
                    image,
                    *args,
                )
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
                ("prometheus", 9090),
                ("alertmanager", 9093),
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
            grafana_env = runtime_environment(config["services"]["grafana"])
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
            extra += ["--tmpfs", "/var/lib/grafana:uid=472,gid=0"]
            extra += [
                item
                for mount in config["services"]["grafana"].get("tmpfs", [])
                for item in ("--tmpfs", mount)
            ]
            for mount in config["services"]["grafana"].get("volumes", []):
                if mount["target"] == "/etc/grafana/start-grafana.sh":
                    extra += ["-v", mount["source"] + ":" + mount["target"] + ":ro"]
            entrypoint = config["services"]["grafana"].get("entrypoint") or []
            if entrypoint:
                extra += ["--entrypoint", entrypoint[0]]
            grafana = create(
                "grafana", config["services"]["grafana"]["image"], entrypoint[1:], extra
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
            yield prefix, nginx, grafana, directory, containers
        except Exception:
            for name in containers:
                if name.endswith("-nginx"):
                    print(docker("logs", name, check=False).stderr, flush=True)
            raise
        finally:
            cleanup_owned(containers, networks, prefix)


def credential_failures(config):
    service = config["services"]["grafana"]
    environment = runtime_environment(service)
    cases = [
        (v, environment["GF_SERVER_ROOT_URL"])
        for v in (
            "\u00a0synthetic-unicode-password",
            "synthetic-unicode-password\u2003",
            "\u2003" * 20,
            "synthetic-line\nbreak-password",
            "",
            "synthetic-short",
            "change-me",
            "admin",
            "CHANGE-ME",
            " " * 20,
            " synthetic-boundary-password",
            "synthetic-boundary-password ",
        )
    ]
    cases += [
        (environment["GF_SECURITY_ADMIN_PASSWORD"], v)
        for v in (
            "/grafana/",
            "http://nginx/grafana/",
            "https://nginx/path/grafana/",
            "https://user@nginx/grafana/",
            "https://:/grafana/",
            "https://nginx:bad/grafana/",
            "https://nginx:99999/grafana/",
            "https://nginx:/grafana/",
            "https://-nginx/grafana/",
        )
    ]
    for password, origin in cases:
        name = "cg-b-invalid-" + uuid.uuid4().hex[:12]
        env = dict(
            environment,
            GF_SECURITY_ADMIN_PASSWORD=password,
            GF_SERVER_ROOT_URL=origin,
        )
        extra = [item for k, v in env.items() for item in ("-e", k + "=" + v)]
        entrypoint = service.get("entrypoint") or []
        if entrypoint:
            extra += ["--entrypoint", entrypoint[0]]
        for mount in service.get("volumes", []):
            if mount["target"] == "/etc/grafana/start-grafana.sh":
                extra += ["-v", mount["source"] + ":" + mount["target"] + ":ro"]
        extra += [
            item for mount in service.get("tmpfs", []) for item in ("--tmpfs", mount)
        ]
        try:
            docker(
                "create",
                "--name",
                name,
                "--label",
                "cashguard.agent=codex-b",
                "--label",
                "cashguard.run=" + name,
                "--network",
                "none",
                "--tmpfs",
                "/var/lib/grafana:uid=472,gid=0",
                *extra,
                service["image"],
                *entrypoint[1:],
            )
            docker("start", name)
            deadline = time.monotonic() + 4
            while True:
                state = json.loads(docker("inspect", name).stdout)[0]["State"]
                if not state["Running"]:
                    break
                assert (
                    time.monotonic() < deadline
                ), "Grafana accepted an invalid/placeholder password"
                time.sleep(0.1)
            assert state["ExitCode"] != 0, "Invalid password falsely succeeded"
            logs = docker("logs", name)
            assert (
                "Grafana preflight" in logs.stderr
            ), "Failure was not the credential guard"
            assert "change-me" not in logs.stderr and "CHANGE-ME" not in logs.stderr
            if password.strip():
                assert password not in logs.stdout + logs.stderr
        finally:
            cleanup_owned([name], [], name)


def response(url, client, timeout=3, payload=None, headers=None):
    script = """import base64,json,ssl,urllib.request,urllib.error,sys
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None
ctx=ssl.create_default_context(cafile='/synthetic-cert.pem')
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect(),urllib.request.HTTPSHandler(context=ctx))
r=json.load(sys.stdin)
try:
    payload=None if r['payload'] is None else json.dumps(r['payload']).encode()
    req=urllib.request.Request(r['url'],data=payload,headers=r['headers'])
    reply=opener.open(req,timeout=r['timeout'])
except urllib.error.HTTPError as error:
    reply=error
print(json.dumps([reply.status,dict(reply.headers),base64.b64encode(reply.read()).decode()]))
"""
    result = docker(
        "exec",
        "-i",
        client,
        "python",
        "-c",
        script,
        check=False,
        stdin=json.dumps(
            dict(url=url, timeout=timeout, payload=payload, headers=headers or {})
        ),
    )
    if result.returncode:
        raise OSError("Synthetic HTTPS request failed: " + result.stderr)
    status, headers, body = json.loads(result.stdout)
    return status, headers, base64.b64decode(body)


def persistent_credentials(config):
    """Cold-start retained, task-owned SQLite storage; never owner volumes.

    HTTP payloads travel through private stdin, not process arguments. The
    baseline intentionally bypasses the new entrypoint to seed weak state.
    """
    prefix = "cg-b-persist-" + uuid.uuid4().hex[:12]
    volume, network = prefix + "-data", prefix + "-net"
    volumes = [volume, prefix + "-corrupt"]
    grafana, client = prefix + "-grafana", prefix + "-client"
    service = config["services"]["grafana"]
    image = service["image"]
    strong = runtime_environment(service)["GF_SECURITY_ADMIN_PASSWORD"]
    changed = strong + "-changed"
    weak = "change-me"
    containers = [client, grafana]

    def request(path, payload=None, cookie=None, basic=None, method=None):
        script = """import sys,json,urllib.request,urllib.error
r=json.load(sys.stdin)
h={'Content-Type':'application/json'}
if r['cookie']:h['Cookie']=r['cookie']
if r['basic']:h['Authorization']=r['basic']
data=None if r['payload'] is None else json.dumps(r['payload']).encode()
q=urllib.request.Request('http://grafana:3000/grafana'+r['path'],data=data,headers=h,method=r['method'])
try:
 with urllib.request.urlopen(q,timeout=3) as x: out=[x.status,dict(x.headers),x.read().decode()]
except urllib.error.HTTPError as x:out=[x.code,dict(x.headers),x.read().decode()]
print(json.dumps(out))
"""
        result = subprocess.run(
            ["docker", "exec", "-i", client, "python", "-c", script],
            input=json.dumps(
                dict(
                    path=path,
                    payload=payload,
                    cookie=cookie,
                    basic=basic,
                    method=method,
                )
            ),
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode:
            raise OSError("Synthetic persistent HTTP request failed")
        return json.loads(result.stdout)

    def login(password):
        status, headers, _ = request("/login", {"user": "admin", "password": password})
        return status, headers.get("Set-Cookie", "").split(";", 1)[0]

    def start(password, guard=True, wrapper=None, reject=False, wal=False):
        env = runtime_environment(service)
        env.update(
            GF_SECURITY_ADMIN_PASSWORD=password,
            GF_SERVER_ROOT_URL="https://nginx/grafana/",
            GF_ANALYTICS_REPORTING_ENABLED="false",
            GF_ANALYTICS_CHECK_FOR_UPDATES="false",
            GF_ANALYTICS_CHECK_FOR_PLUGIN_UPDATES="false",
        )
        args = [item for k, v in env.items() for item in ("-e", k + "=" + v)]
        if wal:
            args += ["-e", "GF_DATABASE_WAL=true"]
        if guard:
            args += [
                "--entrypoint",
                "/bin/sh",
                "--tmpfs",
                "/tmp:mode=1777",
                "-v",
                str(ROOT / "infra/grafana/start-grafana.sh")
                + ":/etc/grafana/start-grafana.sh:ro",
            ]
        if wrapper:
            args += ["-v", str(wrapper) + ":/usr/share/grafana/bin/grafana:ro"]
        docker(
            "run",
            "-d",
            "--name",
            grafana,
            "--network",
            network,
            "--network-alias",
            "grafana",
            "--label",
            "cashguard.run=" + prefix,
            "-v",
            volume + ":/var/lib/grafana",
            *args,
            image,
            *(["/etc/grafana/start-grafana.sh"] if guard else []),
        )
        deadline = time.monotonic() + 60
        while True:
            state = json.loads(docker("inspect", grafana).stdout)[0]["State"]
            try:
                ready = request("/api/health")[0] == 200
            except OSError:
                ready = False
            if reject:
                assert not ready, "Rejected startup exposed an HTTP listener"
                if not state["Running"]:
                    assert state["ExitCode"] != 0, "Rejected startup falsely succeeded"
                    break
            elif ready:
                break
            elif not state["Running"]:
                raise AssertionError(
                    "Persistent Grafana failed startup (logs withheld)"
                )
            assert (
                time.monotonic() < deadline
            ), "Persistent readiness/exit deadline exceeded"
            time.sleep(0.2)
        logs = docker("logs", grafana)
        for secret in (weak, strong, changed):
            assert secret not in logs.stdout + logs.stderr, "Credential leaked in logs"
        if not reject:
            argv = docker("exec", grafana, "cat", "/proc/1/cmdline").stdout
            assert (
                strong not in argv and changed not in argv
            ), "Credential in server argv"

    def remove():
        docker("rm", "-f", grafana)

    def preserve(cookie):
        status, _, body = request("/api/dashboards/uid/cg-persist", cookie=cookie)
        assert (
            status == 200
            and json.loads(body)["dashboard"]["title"] == "Synthetic retained dashboard"
        )
        status, _, body = request("/api/users", cookie=cookie)
        assert status == 200 and any(
            u["login"] == "synthetic-reader" for u in json.loads(body)
        ), "Existing user lost"
        status, _, body = request("/api/org", cookie=cookie)
        assert status == 200 and json.loads(body)["id"] == 1
        status, _, body = request("/api/datasources/uid/cg-retained", cookie=cookie)
        assert (
            status == 200
            and json.loads(body)["url"] == "http://prometheus:9090"
            and json.loads(body)["type"] == "prometheus"
        )
        assert (
            docker("exec", grafana, "cat", "/var/lib/grafana/cg-retained-data").stdout
            == "synthetic-volume-sentinel"
        )

    try:
        docker("volume", "create", "--label", "cashguard.run=" + prefix, volume)
        docker(
            "network",
            "create",
            "--internal",
            "--label",
            "cashguard.run=" + prefix,
            network,
        )
        docker(
            "run",
            "-d",
            "--name",
            client,
            "--network",
            network,
            "--label",
            "cashguard.run=" + prefix,
            PYTHON_IMAGE,
            "python",
            "-c",
            "import time;time.sleep(3600)",
        )
        start(weak, guard=False, wal=True)
        status, cookie = login(weak)
        assert status == 200, "Weak seed positive control failed"
        assert (
            request(
                "/api/dashboards/db",
                {
                    "dashboard": {
                        "uid": "cg-persist",
                        "title": "Synthetic retained dashboard",
                        "panels": [],
                    }
                },
                cookie,
            )[0]
            == 200
        )
        assert (
            request(
                "/api/admin/users",
                {
                    "name": "Synthetic reader",
                    "login": "synthetic-reader",
                    "email": "reader@example.invalid",
                    "password": "synthetic-reader-only-password",
                },
                cookie,
            )[0]
            == 200
        )
        assert (
            request(
                "/api/datasources",
                {
                    "uid": "cg-retained",
                    "name": "Synthetic retained datasource",
                    "type": "prometheus",
                    "url": "http://prometheus:9090",
                    "access": "proxy",
                },
                cookie,
            )[0]
            == 200
        )
        docker(
            "exec",
            grafana,
            "sh",
            "-c",
            "printf synthetic-volume-sentinel > /var/lib/grafana/cg-retained-data",
        )
        preserve(cookie)
        assert (
            docker(
                "exec",
                grafana,
                "test",
                "-s",
                "/var/lib/grafana/grafana.db-wal",
                check=False,
            ).returncode
            == 0
        ), "WAL seed control missing"
        docker("kill", "--signal", "KILL", grafana)
        remove()
        start(strong)
        assert login(weak)[0] == 401, "Old weak stored credential remains valid"
        status, cookie = login(strong)
        assert status == 200, "Configured literal credential not accepted"
        preserve(cookie)
        basic = "Basic " + base64.b64encode(("admin:" + strong).encode()).decode()
        assert (
            request("/api/user", basic=basic)[0] == 401
        ), "Basic auth bypassed login boundary"
        remove()
        start(changed)
        assert login(strong)[0] == 401, "Superseded strong credential remains valid"
        status, cookie = login(changed)
        assert status == 200
        preserve(cookie)
        remove()
        start(changed)
        status, cookie = login(changed)
        assert status == 200
        preserve(cookie)
        # Supported API permits an internal newline in a login. It must not
        # forge an accepted first row in the CLI's human-readable identity list.
        spoof = "admin ID: 1\nx"
        assert (
            request(
                "/api/users/1",
                {
                    "login": spoof,
                    "email": "admin@example.invalid",
                    "name": "Synthetic admin",
                },
                cookie,
                method="PUT",
            )[0]
            == 200
        )
        remove()
        start(changed, reject=True)
        remove()
        start(changed, guard=False)
        status, headers, _ = request("/login", {"user": spoof, "password": changed})
        assert status == 200, "Identity rejection altered retained credentials"
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        preserve(cookie)
        assert (
            request(
                "/api/users/1",
                {
                    "login": "admin",
                    "email": "admin@example.invalid",
                    "name": "Synthetic admin",
                },
                cookie,
                method="PUT",
            )[0]
            == 200
        )
        remove()
        start(changed)
        status, cookie = login(changed)
        assert status == 200
        preserve(cookie)
        # A second weak administrator can mimic the CLI heading with a plain
        # printable login. Its row must never be skipped as another heading.
        heading = "Please try to run the command again specifying a user-id"
        status, _, body = request(
            "/api/admin/users",
            {
                "name": "Synthetic heading attacker",
                "login": heading,
                "email": "heading@example.invalid",
                "password": weak,
            },
            cookie,
        )
        assert status == 200
        extra_admin = json.loads(body)["id"]
        assert (
            request(
                f"/api/admin/users/{extra_admin}/permissions",
                {"isGrafanaAdmin": True},
                cookie,
                method="PUT",
            )[0]
            == 200
        )
        remove()
        start(changed, reject=True)
        remove()
        start(changed, guard=False)
        assert (
            request("/login", {"user": heading, "password": weak})[0] == 200
        ), "Identity rejection changed unexpected administrator credentials"
        status, cookie = login(changed)
        assert status == 200
        preserve(cookie)
        assert (
            request(f"/api/admin/users/{extra_admin}", cookie=cookie, method="DELETE")[
                0
            ]
            == 200
        )
        remove()
        start(changed)
        status, cookie = login(changed)
        assert status == 200
        preserve(cookie)
        # Another server administrator is ambiguous: fail closed, preserve data,
        # and permit recovery through supported API on the isolated fixture only.
        status, _, body = request("/api/users", cookie=cookie)
        reader = next(
            u["id"] for u in json.loads(body) if u["login"] == "synthetic-reader"
        )
        assert (
            request(
                f"/api/admin/users/{reader}/permissions",
                {"isGrafanaAdmin": True},
                cookie,
                method="PUT",
            )[0]
            == 200
        )
        remove()
        start(changed, reject=True)
        remove()
        start(changed, guard=False)
        status, cookie = login(changed)
        assert status == 200
        preserve(cookie)
        assert (
            request(
                f"/api/admin/users/{reader}/permissions",
                {"isGrafanaAdmin": False},
                cookie,
                method="PUT",
            )[0]
            == 200
        )
        remove()
        with tempfile.TemporaryDirectory(prefix=prefix) as temporary:
            wrapper = Path(temporary) / "grafana"
            wrapper.write_text("#!/bin/sh\nexit 127\n")
            wrapper.chmod(0o755)
            start(changed, wrapper=wrapper, reject=True)
            remove()
            # The pinned CLI itself has zero-exit error branches. Never count
            # exit status without the supported successful-update evidence.
            wrapper.write_text(
                "#!/bin/sh\ncase \"$*\" in *--user-id=-1*) printf 'Admin user cannot be found\\nPlease try to run the command again specifying a user-id (--user-id) from the list below:\\n\\t Username: admin ID: 1\\n';; *) printf 'simulated zero-exit update failure\\n';; esac\nexit 0\n"
            )
            start(changed, wrapper=wrapper, reject=True)
            remove()
            # Stop while the offline CLI is blocked; no HTTP may have appeared.
            wrapper.write_text(
                "#!/bin/sh\nprintf blocked > /tmp/cg-cli-blocked\nsleep 60\nexit 1\n"
            )
            # Start asynchronously because readiness is intentionally blocked.
            env = runtime_environment(service)
            env.update(
                GF_SECURITY_ADMIN_PASSWORD=changed,
                GF_SERVER_ROOT_URL="https://nginx/grafana/",
            )
            args = [item for k, v in env.items() for item in ("-e", k + "=" + v)]
            docker(
                "run",
                "-d",
                "--name",
                grafana,
                "--network",
                network,
                "--network-alias",
                "grafana",
                "--label",
                "cashguard.run=" + prefix,
                "--entrypoint",
                "/bin/sh",
                "--tmpfs",
                "/tmp:mode=1777",
                "-v",
                volume + ":/var/lib/grafana",
                "-v",
                str(ROOT / "infra/grafana/start-grafana.sh")
                + ":/etc/grafana/start-grafana.sh:ro",
                "-v",
                str(wrapper) + ":/usr/share/grafana/bin/grafana:ro",
                *args,
                image,
                "/etc/grafana/start-grafana.sh",
            )
            deadline = time.monotonic() + 10
            while docker(
                "exec", grafana, "test", "-f", "/tmp/cg-cli-blocked", check=False
            ).returncode:
                assert (
                    time.monotonic() < deadline
                ), "Offline CLI interruption control not reached"
                time.sleep(0.1)
            argv = docker(
                "exec", grafana, "sh", "-c", "cat /proc/[0-9]*/cmdline"
            ).stdout
            assert (
                strong not in argv and changed not in argv
            ), "Credential leaked in CLI argv"
            try:
                assert request("/api/health")[0] != 200, "Interrupted CLI exposed HTTP"
            except OSError:
                pass
            docker("stop", "-t", "1", grafana)
            remove()
        start(changed)
        status, cookie = login(changed)
        assert status == 200
        preserve(cookie)
        remove()
        # Task-only transient DB permissions fail closed and then recover.
        for mode, rejected in (("000", True), ("600", False)):
            docker(
                "run",
                "--rm",
                "--network",
                "none",
                "--user",
                "0",
                "--entrypoint",
                "/bin/sh",
                "--label",
                "cashguard.run=" + prefix,
                "-v",
                volume + ":/var/lib/grafana",
                image,
                "-c",
                "chmod " + mode + " /var/lib/grafana/grafana.db",
            )
            start(changed, reject=rejected)
            if not rejected:
                status, cookie = login(changed)
                assert status == 200
                preserve(cookie)
            remove()
        # A supported API transition to a sole nondefault administrator must
        # fail closed rather than guessing its ID or rewriting authentication SQL.
        start(changed)
        status, cookie = login(changed)
        assert status == 200
        assert (
            request(
                f"/api/admin/users/{reader}/permissions",
                {"isGrafanaAdmin": True},
                cookie,
                method="PUT",
            )[0]
            == 200
        )
        status, headers, _ = request(
            "/login",
            {"user": "synthetic-reader", "password": "synthetic-reader-only-password"},
        )
        assert status == 200
        reader_cookie = headers["Set-Cookie"].split(";", 1)[0]
        assert (
            request("/api/admin/users/1", cookie=reader_cookie, method="DELETE")[0]
            == 200
        )
        remove()
        start(changed, reject=True)
        remove()
        start(changed, guard=False)
        status, headers, _ = request(
            "/login",
            {"user": "synthetic-reader", "password": "synthetic-reader-only-password"},
        )
        assert status == 200, "Rejected startup altered nondefault admin credentials"
        preserve(headers["Set-Cookie"].split(";", 1)[0])
        remove()
        # Corrupt a separate disposable DB, retaining the valid volume untouched.
        corrupt = prefix + "-corrupt"
        docker("volume", "create", "--label", "cashguard.run=" + prefix, corrupt)
        valid = volume
        try:
            docker(
                "run",
                "--rm",
                "--network",
                "none",
                "--user",
                "0",
                "--entrypoint",
                "/bin/sh",
                "--label",
                "cashguard.run=" + prefix,
                "-v",
                corrupt + ":/var/lib/grafana",
                image,
                "-c",
                "printf corrupt > /var/lib/grafana/grafana.db; chown 472 /var/lib/grafana/grafana.db",
            )
            volume = corrupt
            start(changed, reject=True)
            remove()
        finally:
            volume = valid
        print(
            json.dumps(
                {
                    "persisted_auth": "PASS",
                    "synthetic": True,
                    "assertions": [
                        "weak seed positive control with uncheckpointed WAL and killed writer",
                        "old weak rejected; literal configured accepted",
                        "changed strong and repeated startup",
                        "dashboard, ordinary user, org, datasource metadata and volume sentinel preserved",
                        "transient unreadable database denies HTTP then recovers",
                        "Basic auth disabled",
                        "newline-spoofed identity rejected; supported API recovery preserves data",
                        "second weak administrator with CLI-heading login denied; credentials and data preserved",
                        "ambiguous admin fails closed; isolated supported API recovery",
                        "sole nondefault admin denies exposure without credential mutation",
                        "missing CLI and interrupted offline CLI deny HTTP; recovery preserves data",
                        "corrupt DB denies HTTP",
                    ],
                    "identity_contract": "single admin username admin ID1, fixed SQLite path; nondefault/external/concurrent writers unsupported",
                }
            ),
            flush=True,
        )
    finally:
        cleanup_owned(containers, [network], prefix, volumes)


def run():
    config = render_compose()
    for image in (
        PYTHON_IMAGE,
        config["services"]["nginx"]["image"],
        config["services"]["grafana"]["image"],
    ):
        docker("image", "inspect", image)
    source_files = (
        "docker-compose.prod.yml",
        "infra/nginx/nginx.conf",
        "infra/grafana/start-grafana.sh",
        "infra/tests/runtime_probe.py",
        "infra/tests/test_runtime_boundaries.py",
    )
    images = {}
    for image in (
        PYTHON_IMAGE,
        config["services"]["nginx"]["image"],
        config["services"]["grafana"]["image"],
    ):
        inspected = json.loads(docker("image", "inspect", image).stdout)[0]
        images[image] = {
            "digests": inspected["RepoDigests"],
            "id": inspected["Id"],
            "os": inspected["Os"],
            "architecture": inspected["Architecture"],
            "entrypoint": inspected["Config"].get("Entrypoint"),
            "user": inspected["Config"].get("User"),
        }
    print(
        json.dumps(
            {
                "provenance": {
                    "source_sha256": {
                        rel: hashlib.sha256((ROOT / rel).read_bytes()).hexdigest()
                        for rel in source_files
                    },
                    "images": images,
                    "host": platform.system() + "/" + platform.machine(),
                    "python": platform.python_version(),
                    "topology": "internal networks; no host ports; nginx/Grafana real, others HTTP fixtures",
                }
            }
        ),
        flush=True,
    )
    credential_failures(config)
    persistent_credentials(config)
    with harness(config) as (prefix, nginx, grafana, directory, owned_containers):
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
            "http://localhost:3000/grafana/api/health",
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
        login = {
            "user": config["services"]["grafana"]["environment"].get(
                "GF_SECURITY_ADMIN_USER", "admin"
            ),
            "password": runtime_environment(config["services"]["grafana"])[
                "GF_SECURITY_ADMIN_PASSWORD"
            ],
        }
        status, headers, _ = response(
            base + "/grafana/login",
            client,
            payload=login,
            headers={"Content-Type": "application/json"},
        )
        assert status == 200, "Synthetic Grafana login failed"
        cookie = headers.get("Set-Cookie", "")
        assert (
            "grafana_session=" in cookie
            and "secure" in cookie.lower()
            and "httponly" in cookie.lower()
            and "samesite=lax" in cookie.lower()
        ), "Grafana session cookie protections missing"
        token = cookie.split(";", 1)[0]
        assert (
            response(base + "/grafana/api/user", client, headers={"Cookie": token})[0]
            == 200
        )
        mode = docker(
            "exec",
            grafana,
            "sh",
            "-c",
            "find /tmp -path '/tmp/cashguard-grafana.*/password' -type f -exec stat -c '%a' {} ';'",
        )
        assert (
            mode.stdout.strip() == "600"
        ), "Grafana private credential file mode wrong"
        logs = docker("logs", grafana)
        assert (
            login["password"] not in logs.stdout + logs.stderr
        ), "Grafana logged credential value"
        argv = docker("exec", grafana, "cat", "/proc/1/cmdline")
        assert (
            login["password"] not in argv.stdout
        ), "Grafana credential appeared in argv"
        basic = (
            "Basic "
            + base64.b64encode(("admin:" + login["password"]).encode()).decode()
        )
        assert (
            response(
                base + "/grafana/api/user", client, headers={"Authorization": basic}
            )[0]
            == 401
        ), "Basic auth bypassed ingress login limit"
        wrong_login = dict(login, password="synthetic-wrong-password")
        assert (
            response(
                base + "/grafana/login",
                client,
                payload=wrong_login,
                headers={"Content-Type": "application/json"},
            )[0]
            == 401
        )
        assert (
            response(
                base + "/grafana/login/",
                client,
                payload=wrong_login,
                headers={"Content-Type": "application/json"},
            )[0]
            == 401
        ), "Trailing-slash login did not reach credential handler"
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
        positive = docker(
            "exec", grafana, "wget", "-T", "2", "-qO-", "http://prometheus:9090"
        )
        assert (
            positive.stdout == "synthetic-prometheus"
        ), "Grafana Prometheus positive control failed"
        for service, port in (
            ("postgres", 5432),
            ("redis", 6379),
            ("api", 8000),
            ("web", 3000),
            ("alertmanager", 9093),
        ):
            data = json.loads(docker("inspect", prefix + "-" + service).stdout)[0]
            # Authorized-peer HTTP control proves the target is live before refusal
            # from Grafana can count as network isolation.
            control_script = "import urllib.request,sys; print(urllib.request.urlopen(sys.argv[1],timeout=2).read().decode())"
            control = docker(
                "exec",
                client,
                "python",
                "-c",
                control_script,
                "http://" + service + ":" + str(port),
            )
            assert (
                control.stdout.strip() == "synthetic-" + service
            ), "Forbidden target fixture positive control failed"
            # Target each IP; membership must not be hidden by probing only DNS.
            hosts = [service] + [
                n["IPAddress"] for n in data["NetworkSettings"]["Networks"].values()
            ]
            for host in hosts:
                denied = docker(
                    "exec",
                    grafana,
                    "wget",
                    "-T",
                    "2",
                    "-qO-",
                    "http://" + host + ":" + str(port),
                    check=False,
                )
                assert denied.returncode == 1 and any(
                    reason in denied.stderr.lower()
                    for reason in (
                        "bad address",
                        "timed out",
                        "no route",
                        "connection refused",
                        "can't connect",
                    )
                ), (
                    "Grafana reached forbidden target or probe failed to execute: "
                    + service
                )
        # Remove DNS advertisement entirely, then restart the actual nginx process.
        grafana_networks = list(
            json.loads(docker("inspect", grafana).stdout)[0]["NetworkSettings"][
                "Networks"
            ]
        )
        docker("stop", grafana)
        for network in grafana_networks:
            docker("network", "disconnect", network, grafana)
        docker("restart", nginx)
        deadline = time.monotonic() + 10
        while True:
            try:
                if response(base + "/v1/health/live", client)[0] == 200:
                    break
            except OSError:
                pass
            if time.monotonic() > deadline:
                raise AssertionError("nginx cannot serve API with Grafana absent")
            time.sleep(0.2)
        state = json.loads(docker("inspect", nginx).stdout)[0]["State"]
        assert state["Running"], (
            "nginx exits when Grafana DNS is absent: " + docker("logs", nginx).stderr
        )
        assert response(base + "/v1/health/live", client)[0] == 200
        # Fixture forwarding only; no real kill-switch or financial action.
        assert (
            response(base + "/v1/emergency/kill-switch", client)[2] == b"synthetic-api"
        )
        assert response(base + "/", client)[2] == b"synthetic-web"
        assert response(base + "/grafana/api/user", client, timeout=15)[0] == 502
        for network in grafana_networks:
            docker("network", "connect", "--alias", "grafana", network, grafana)
        docker("start", grafana)
        deadline = time.monotonic() + 40
        while True:
            status, _, body = response(base + "/grafana/api/health", client, timeout=15)
            if status == 200 and b"database" in body:
                break
            if time.monotonic() > deadline:
                raise AssertionError("request-time DNS did not recover Grafana")
            time.sleep(0.5)
        docker("stop", grafana)
        status, _, body = response(base + "/grafana/api/user", client, timeout=70)
        assert (
            status in (502, 504) and b"synthetic-web" not in body
        ), "Missing Grafana must fail closed"
        docker("start", grafana)
        deadline = time.monotonic() + 30
        while True:
            status, _, body = response(base + "/grafana/api/health", client)
            if status == 200 and b"database" in body:
                break
            if time.monotonic() > deadline:
                raise AssertionError("Grafana did not recover")
            time.sleep(0.3)
        # Replace Grafana with different IPs. Hold its old addresses so Docker
        # cannot silently reuse them; nginx must recover without reload/restart.
        previous = json.loads(docker("inspect", grafana).stdout)[0]["NetworkSettings"][
            "Networks"
        ]
        holder = prefix + "-old-ip-holder"
        owned_containers.append(holder)
        networks = list(previous)
        try:
            docker("rm", "-f", grafana)
            docker(
                "create",
                "--name",
                holder,
                "--label",
                "cashguard.run=" + prefix,
                "--network",
                networks[0],
                "--ip",
                previous[networks[0]]["IPAddress"],
                PYTHON_IMAGE,
                "python",
                "-c",
                "import time;time.sleep(3600)",
            )
            for network in networks[1:]:
                docker(
                    "network",
                    "connect",
                    "--ip",
                    previous[network]["IPAddress"],
                    network,
                    holder,
                )
            docker("start", holder)
            env = runtime_environment(config["services"]["grafana"])
            env.update(
                GF_ANALYTICS_REPORTING_ENABLED="false",
                GF_ANALYTICS_CHECK_FOR_UPDATES="false",
                GF_ANALYTICS_CHECK_FOR_PLUGIN_UPDATES="false",
            )
            args = [item for k, v in env.items() for item in ("-e", k + "=" + v)]
            docker(
                "create",
                "--name",
                grafana,
                "--label",
                "cashguard.run=" + prefix,
                "--network",
                networks[0],
                "--network-alias",
                "grafana",
                "--entrypoint",
                "/bin/sh",
                "--tmpfs",
                "/var/lib/grafana:uid=472,gid=0",
                "--tmpfs",
                "/tmp:mode=1777",
                "-v",
                str(ROOT / "infra/grafana/start-grafana.sh")
                + ":/etc/grafana/start-grafana.sh:ro",
                *args,
                config["services"]["grafana"]["image"],
                "/etc/grafana/start-grafana.sh",
            )
            for network in networks[1:]:
                docker("network", "connect", "--alias", "grafana", network, grafana)
            docker("start", grafana)
            current = json.loads(docker("inspect", grafana).stdout)[0][
                "NetworkSettings"
            ]["Networks"]
            assert all(
                current[n]["IPAddress"] != previous[n]["IPAddress"] for n in networks
            ), "Replacement reused old IP control"
            deadline = time.monotonic() + 45
            while response(base + "/grafana/api/health", client, timeout=15)[0] != 200:
                assert time.monotonic() < deadline, "nginx failed changed-IP recovery"
                time.sleep(0.3)
            assert response(base + "/v1/health/live", client)[0] == 200
        finally:
            cleanup_owned([holder], [], prefix, verify_all=False)
        # More accepted Grafana requests than API's burst20; a shared API zone
        # mutation must fail here rather than hiding behind the small login budget.
        budget_script = """import ssl,urllib.request,sys,json,time
ctx=ssl.create_default_context(cafile='/synthetic-cert.pem')
started=time.monotonic()
for i in range(35):
    with urllib.request.urlopen(sys.argv[1],context=ctx,timeout=3) as r:
        assert r.status==200
assert time.monotonic()-started<10, 'Synthetic burst too slow to qualify distinct rate budget'
print('35 accepted Grafana requests within 10 seconds')
"""
        accepted = docker(
            "exec",
            client,
            "python",
            "-c",
            budget_script,
            base + "/grafana/api/health",
            check=False,
        )
        assert (
            accepted.returncode == 0 and "35 accepted" in accepted.stdout
        ), "Grafana traffic leaked into smaller API request budget"
        assert response(base + "/v1/health/live", client)[0] == 200
        assert response(base + "/grafana/login", client)[0] == 200
        statuses = [
            response(
                base + ("/grafana/login/" if i % 2 else "/grafana/login"),
                client,
                payload=wrong_login,
                headers={"Content-Type": "application/json"},
            )[0]
            for i in range(6)
        ]
        assert (
            429 in statuses[::2] and 429 in statuses[1::2]
        ), "Grafana login variants escaped shared strict budget"
        assert (
            response(base + "/v1/health/live", client)[0] == 200
        ), "Grafana rate limit consumed API budget"
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
                        "Grafana replacement at different IPs recovers without nginx reload",
                        "nginx startup with Grafana DNS absent; API/web available; Grafana 502 then DNS recovery",
                        "Grafana normal recipe rejects invalid credentials and insecure public origins",
                        "Grafana DNS/IP forbidden-target denial with Prometheus positive control",
                        "Canonical/trailing-slash Grafana POST login share 429 budget; 35 accepted Grafana requests leave API budget available",
                        "Successful login, wrong credentials, authenticated API and Secure/HttpOnly/SameSite session cookie",
                    ],
                    "limitations": [
                        "API/web/datastores are synthetic HTTP fixtures",
                        "all projected networks internal; production external egress not tested",
                        "changed-IP recovery uses new disposable Grafana DB; persistence tested separately",
                    ],
                }
            )
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-synthetic", action="store_true", required=True)
    parser.parse_args()
    run()
