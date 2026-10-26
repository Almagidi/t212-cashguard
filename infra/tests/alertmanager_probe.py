"""Disposable source-derived Compose fixture; no production services or env files.

Requires the pre-pulled source image and Docker Compose. Receipts contain only
synthetic inputs, source hashes and selected inspection fields, never raw config.
"""

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import uuid

from runtime_probe import cleanup_owned, docker
from test_runtime_boundaries import ROOT, render_compose


class AlertmanagerProbe:
    def __init__(self, template=None, environment=None, renderer=None, mutate=None):
        self.run = "cg-b-n41-" + uuid.uuid4().hex[:12]
        self.directory = tempfile.TemporaryDirectory(prefix=self.run)
        self.path = Path(self.directory.name)
        self.containers = []
        self.networks = []
        self.volumes = []
        self.receipt = {"run": self.run, "started": time.time(), "events": []}
        source = render_compose()
        self.source = source
        self.service = copy.deepcopy(source["services"]["alertmanager"])
        assert self.service["image"] == "prom/alertmanager:v0.27.0"
        image = json.loads(docker("image", "inspect", self.service["image"]).stdout)[0]
        self.receipt["image"] = {
            "id": image["Id"],
            "digests": image["RepoDigests"],
            "architecture": image["Architecture"],
            "entrypoint": image["Config"]["Entrypoint"],
        }
        self.receipt["source"] = {
            str(p): hashlib.sha256((ROOT / p).read_bytes()).hexdigest()
            for p in (
                "docker-compose.prod.yml",
                "infra/prometheus/alertmanager.yml",
                "infra/prometheus/render-alertmanager.sh",
                "infra/prometheus/prometheus.yml",
                "infra/prometheus/alerts.yml",
                "infra/tests/alertmanager_probe.py",
                "infra/tests/test_alertmanager_startup.py",
                "infra/nginx/nginx.conf",
            )
        }
        self.receipt["invocation"] = {
            "entrypoint": self.service.get("entrypoint", image["Config"]["Entrypoint"]),
            "command": self.service.get("command", image["Config"]["Cmd"]),
        }
        self.service.update(
            image=image["RepoDigests"][0],
            container_name=self.run + "-am",
            restart="no",
            networks={"fixture": {"aliases": ["alertmanager"]}},
            labels={"cashguard.run": self.run},
        )
        self.service.pop("expose", None)
        self.service["environment"] = {
            "SMTP_HOST": "smtp.example.invalid",
            "SMTP_PORT": "587",
            "SMTP_USER": "synthetic-user",
            "SMTP_PASSWORD": "synthetic-secret-'${SMTP_USER}$(false)",
            "ALERT_EMAIL_FROM": "alerts@example.invalid",
            "ALERT_EMAIL_TO": "ops@example.invalid",
        }
        self.service["environment"].update(environment or {})
        self.secret = self.service["environment"]["SMTP_PASSWORD"]
        self.network = self.run + "-net"
        self.volume = self.run + "-data"
        self.name = self.service["container_name"]
        self.service["volumes"] = [
            {
                "type": "bind",
                "source": str(template or ROOT / "infra/prometheus/alertmanager.yml"),
                "target": "/etc/alertmanager/alertmanager.yml",
                "read_only": True,
            },
            {
                "type": "bind",
                "source": str(
                    renderer or ROOT / "infra/prometheus/render-alertmanager.sh"
                ),
                "target": "/etc/alertmanager/render-alertmanager.sh",
                "read_only": True,
            },
            {"type": "volume", "source": "custody", "target": "/alertmanager"},
        ]
        if mutate:
            mutate(self.service)
        assert not self.service.get("ports")
        self.config = {
            "services": {"alertmanager": self.service},
            "networks": {
                "fixture": {
                    "name": self.network,
                    "internal": True,
                    "labels": {"cashguard.run": self.run},
                }
            },
            "volumes": {
                "custody": {"name": self.volume, "labels": {"cashguard.run": self.run}}
            },
        }
        self.compose_path = self.path / "compose.json"

    def event(self, name, **data):
        self.receipt["events"].append({"event": name, "time": time.time(), **data})

    def compose(self, *args, check=True):
        # --no-interpolate is not an up option: encode literal dollars once for
        # Compose's second parse. Do not print this private fixture configuration.
        def escape(value):
            if isinstance(value, str):
                return value.replace("$", "$$")
            if isinstance(value, list):
                return [escape(x) for x in value]
            if isinstance(value, dict):
                return {k: escape(v) for k, v in value.items()}
            return value

        self.compose_path.write_text(json.dumps(escape(self.config)))
        self.compose_path.chmod(0o600)
        env = {
            k: os.environ[k]
            for k in ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT")
            if k in os.environ
        }
        result = subprocess.run(
            [
                "docker",
                "compose",
                "--env-file",
                "/dev/null",
                "-p",
                self.run,
                "-f",
                str(self.compose_path),
                *args,
            ],
            cwd=self.path,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if check and result.returncode:
            raise RuntimeError(
                "Isolated Compose operation failed (config withheld): " + result.stderr
            )
        return result

    def start(self):
        # Register expected resources BEFORE create, so partial creation cleans up.
        self.containers.append(self.name)
        self.networks.append(self.network)
        self.volumes.append(self.volume)
        self.compose("up", "-d", "--pull", "never", "--no-build", "alertmanager")
        inspected = self.inspect()
        assert not inspected["HostConfig"]["PortBindings"]
        assert all(
            m["Name"] == self.volume
            for m in inspected["Mounts"]
            if m["Type"] == "volume"
        )
        assert (
            json.loads(docker("network", "inspect", self.network).stdout)[0]["Internal"]
            is True
        )
        self.event("created", path=inspected["Path"], args=inspected["Args"])

    def inspect(self):
        return json.loads(docker("inspect", self.name).stdout)[0]

    def logs(self):
        result = docker("logs", self.name, check=False)
        logs = result.stdout + result.stderr
        assert not self.secret or self.secret not in logs, "SMTP secret exposed in logs"
        return logs

    def ready(self):
        return (
            docker(
                "exec",
                self.name,
                "wget",
                "-q",
                "-O",
                "/dev/null",
                "http://127.0.0.1:9093/-/ready",
                check=False,
            ).returncode
            == 0
        )

    def wait_ready(self, seconds=25):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.ready():
                self.event("ready")
                return
            if not self.inspect()["State"]["Running"]:
                break
            time.sleep(0.3)
        raise AssertionError(
            "Normal Compose Alertmanager failed readiness: " + self.logs()
        )

    def wait_failed(self):
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            state = self.inspect()["State"]
            if not state["Running"]:
                assert state["ExitCode"] != 0
                assert not self.ready()
                self.event("failed_closed", exit=state["ExitCode"], stderr=self.logs())
                return
            time.sleep(0.2)
        raise AssertionError("Invalid startup did not stop within deadline")

    def assert_private_pid(self):
        result = docker(
            "exec",
            self.name,
            "sh",
            "-c",
            'tr "\\000" " " < /proc/1/cmdline; echo; '
            "for f in /tmp/cashguard-alertmanager.*/alertmanager.yml; do "
            'stat -c "%a" "$f"; done; '
            'for d in /tmp/cashguard-alertmanager.*; do stat -c "%a" "$d"; done',
        )
        lines = result.stdout.splitlines()
        assert lines[0].startswith(
            "/bin/alertmanager --config.file=/tmp/cashguard-alertmanager."
        )
        assert lines[1:] == [
            "600",
            "700",
        ], "Rendered secret permissions or lifecycle changed"
        assert self.secret not in lines[0]
        assert (
            not docker(
                "exec",
                "--user",
                "10001:10001",
                self.name,
                "sh",
                "-c",
                "cat /tmp/cashguard-alertmanager.*/alertmanager.yml",
                check=False,
            ).returncode
            == 0
        )
        assert self.secret not in json.dumps(self.inspect()["Config"].get("Labels", {}))
        assert self.inspect()["HostConfig"]["Tmpfs"].get("/tmp") == "mode=1777"
        self.event("private_pid1", command=lines[0], modes=lines[1:])
        self.event(
            "binary_version",
            version=docker("exec", self.name, "/bin/alertmanager", "--version").stdout,
        )

    def close(self):
        try:
            cleanup_owned(self.containers, self.networks, self.run, self.volumes)
            self.receipt["cleanup"] = "PASS"
        except Exception:
            self.receipt["cleanup"] = "FAIL"
            raise
        finally:
            self.receipt["finished"] = time.time()
            output = os.environ.get("CASHGUARD_N41_EVIDENCE")
            if output:
                dest = Path(output)
                dest.mkdir(parents=True, exist_ok=True)
                (dest / (self.run + ".json")).write_text(
                    json.dumps(self.receipt, indent=2)
                )
            self.directory.cleanup()


FIXTURE_SERVER = r"""
import http.server, json, threading, time
state = {"active": 1, "events": [], "failed": set()}
lock = threading.Lock()
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        if self.path == "/metrics":
            body = ("# TYPE cashguard_synthetic_probe gauge\ncashguard_synthetic_probe %s\n"
                    "pg_up 1\nredis_up 1\n" % state["active"]).encode()
        elif self.path == "/events": body = json.dumps(state["events"]).encode()
        else: body = b"synthetic HTTP fixture"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4" if self.path == "/metrics" else "application/json")
        self.end_headers(); self.wfile.write(body)
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/control": state["active"] = body["active"]; code = 200
        else:
            key = (body["groupKey"], body["status"])
            with lock:
                code = 503 if key not in state["failed"] else 200
                state["failed"].add(key)
                state["events"].append({"time": time.time(), "path": self.path,
                                        "code": code, "payload": body})
        self.send_response(code); self.end_headers(); self.wfile.write(b"synthetic")
for port in (8000, 8080, 9187, 9121, 3000):
    threading.Thread(target=http.server.ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever,
                     daemon=True).start()
threading.Event().wait()
"""


def chain():
    """Actual source startup + source scrape/routing timers, synthetic webhook transport.

    PyYAML is needed only for this explicit integration probe. Source SMTP receivers
    are replaced by an internal HTTP sink; this does NOT qualify SMTP or producers.
    """
    import datetime
    import urllib.parse
    import yaml

    p = AlertmanagerProbe()
    p.receipt["qualification"] = (
        "PARTIAL_SYNTHETIC; HTTP receivers and inert API/exporters"
    )
    p.receipt["result"] = "FAIL"
    client = p.run + "-fixture"
    prom = p.run + "-prom"

    def wait(description, condition, seconds=45):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            result = condition()
            if result:
                p.event(description)
                return result
            time.sleep(1)
        raise AssertionError("Timed out: " + description)

    def request(url, method="GET", payload=None):
        script = (
            "import json,sys,ssl,urllib.request; "
            "body=json.loads(sys.stdin.read()); "
            "r=urllib.request.Request(sys.argv[1],method=sys.argv[2], "
            "data=None if body is None else json.dumps(body).encode(), "
            "headers={'Content-Type':'application/json'}); "
            "ctx=ssl.create_default_context(cafile='/cert.pem') if sys.argv[1].startswith('https:') else None; "
            "print(urllib.request.urlopen(r,timeout=3,context=ctx).read().decode())"
        )
        result = docker(
            "exec",
            "-i",
            client,
            "python",
            "-c",
            script,
            url,
            method,
            stdin=json.dumps(payload),
            check=False,
        )
        if result.returncode:
            return None
        try:
            return json.loads(result.stdout)
        except ValueError:
            return result.stdout.strip()

    def query(expression):
        result = request(
            "http://prometheus:9090/api/v1/query?"
            + urllib.parse.urlencode({"query": expression})
        )
        return result and result["data"]["result"]

    def notifications(name, status=None, code=200):
        result = request("http://api:8080/events") or []
        return [
            x
            for x in result
            if (code is None or x["code"] == code)
            and x["payload"]["groupLabels"].get("alertname") == name
            and (status is None or x["payload"]["status"] == status)
        ]

    try:
        template = yaml.safe_load(
            (ROOT / "infra/prometheus/alertmanager.yml").read_text()
        )
        for receiver in template["receivers"]:
            resolved = receiver["email_configs"][0].get("send_resolved", False)
            receiver.pop("email_configs")
            receiver["webhook_configs"] = [
                {
                    "url": "http://api:8080/" + receiver["name"],
                    "send_resolved": resolved,
                }
            ]
        template_path = p.path / "alertmanager.yml"
        template_path.write_text(yaml.safe_dump(template, sort_keys=False))
        p.service["volumes"][0]["source"] = str(template_path)
        p.receipt["routing"] = template["route"]
        p.receipt["fixture_template_sha256"] = hashlib.sha256(
            template_path.read_bytes()
        ).hexdigest()
        p.start()
        p.wait_ready()
        p.assert_private_pid()

        python_image = "python:3.12.13-slim-bookworm"
        prom_source = p.source["services"]["prometheus"]
        nginx_source = p.source["services"]["nginx"]
        images = {}
        for image in (python_image, prom_source["image"], nginx_source["image"]):
            inspected = json.loads(docker("image", "inspect", image).stdout)[0]
            images[image] = {
                "digest": inspected["RepoDigests"][0],
                "architecture": inspected["Architecture"],
            }
        p.receipt["chain_images"] = images
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(p.path / "key.pem"),
                "-out",
                str(p.path / "cert.pem"),
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
        # Preserve the source separation: nginx has no monitoring backend edge.
        for network in ("ingress", "monitoring_ingress"):
            name = p.run + "-" + network
            p.networks.append(name)
            p.config["networks"][network] = {
                "name": name,
                "internal": True,
                "labels": {"cashguard.run": p.run},
            }
        (p.path / "fixture.py").write_text(FIXTURE_SERVER)
        prom_config = yaml.safe_load(
            (ROOT / "infra/prometheus/prometheus.yml").read_text()
        )
        prom_config["rule_files"].append("synthetic.yml")
        (p.path / "prometheus.yml").write_text(yaml.safe_dump(prom_config))
        (p.path / "synthetic.yml").write_text(
            yaml.safe_dump(
                {
                    "groups": [
                        {
                            "name": "synthetic_delivery",
                            "rules": [
                                {
                                    "alert": "CashGuardSyntheticDelivery",
                                    "expr": "cashguard_synthetic_probe == 1",
                                    "for": "30s",
                                    "labels": {
                                        "severity": "critical",
                                        "team": "trading",
                                    },
                                }
                            ],
                        }
                    ]
                }
            )
        )
        p.config["services"]["fixture"] = {
            "image": images[python_image]["digest"],
            "container_name": client,
            "labels": {"cashguard.run": p.run},
            "command": ["python", "/fixture.py"],
            "user": "65534:65534",
            "read_only": True,
            "volumes": [
                {
                    "type": "bind",
                    "source": str(p.path / "fixture.py"),
                    "target": "/fixture.py",
                    "read_only": True,
                },
                {
                    "type": "bind",
                    "source": str(p.path / "cert.pem"),
                    "target": "/cert.pem",
                    "read_only": True,
                },
            ],
            "networks": {
                "fixture": {"aliases": ["api", "postgres-exporter", "redis-exporter"]},
                "ingress": {"aliases": ["api", "web"]},
            },
        }
        nginx = p.run + "-nginx"
        p.config["services"]["nginx"] = {
            "image": images[nginx_source["image"]]["digest"],
            "container_name": nginx,
            "labels": {"cashguard.run": p.run},
            "tmpfs": ["/var/log/nginx"],
            "volumes": [
                {
                    "type": "bind",
                    "source": str(ROOT / "infra/nginx/nginx.conf"),
                    "target": "/etc/nginx/nginx.conf",
                    "read_only": True,
                },
                {
                    "type": "bind",
                    "source": str(p.path / "cert.pem"),
                    "target": "/etc/nginx/ssl/cert.pem",
                    "read_only": True,
                },
                {
                    "type": "bind",
                    "source": str(p.path / "key.pem"),
                    "target": "/etc/nginx/ssl/key.pem",
                    "read_only": True,
                },
            ],
            "networks": {"ingress": {"aliases": ["nginx"]}, "monitoring_ingress": {}},
        }
        p.config["services"]["prometheus"] = {
            "image": images[prom_source["image"]]["digest"],
            "container_name": prom,
            "labels": {"cashguard.run": p.run},
            "command": prom_source["command"],
            "tmpfs": ["/prometheus:uid=65534,gid=65534,mode=700"],
            "volumes": [
                {
                    "type": "bind",
                    "source": str(src),
                    "target": "/etc/prometheus/" + target,
                    "read_only": True,
                }
                for src, target in [
                    (p.path / "prometheus.yml", "prometheus.yml"),
                    (p.path / "synthetic.yml", "synthetic.yml"),
                    (ROOT / "infra/prometheus/alerts.yml", "alerts.yml"),
                ]
            ],
            "networks": {"fixture": {"aliases": ["prometheus"]}},
        }
        p.containers.extend([client, prom, nginx])
        p.compose(
            "up",
            "-d",
            "--pull",
            "never",
            "--no-build",
            "fixture",
            "prometheus",
            "nginx",
        )
        for name in (client, prom, nginx):
            inspected = json.loads(docker("inspect", name).stdout)[0]
            assert not inspected["HostConfig"]["PortBindings"]
            assert not any(m["Type"] == "volume" for m in inspected["Mounts"])
        wait("prometheus_ready", lambda: request("http://prometheus:9090/-/ready"))
        wait("nginx_tls_ingress_ready", lambda: request("https://nginx/v1/health/live"))
        docker("exec", nginx, "nginx", "-t")

        def registered():
            registration = request("http://prometheus:9090/api/v1/alertmanagers")
            return registration and registration["data"]["activeAlertmanagers"] == [
                {"url": "http://alertmanager:9093/api/v2/alerts"}
            ]

        wait("source_alertmanager_registered", registered)
        wait("all_source_scrapes_up", lambda: len(query("up == 1") or []) == 4)
        wait(
            "pending",
            lambda: query(
                'ALERTS{alertname="CashGuardSyntheticDelivery",alertstate="pending"}'
            ),
        )
        wait(
            "firing",
            lambda: query(
                'ALERTS{alertname="CashGuardSyntheticDelivery",alertstate="firing"}'
            ),
            60,
        )
        p.receipt["evaluated_rules"] = request("http://prometheus:9090/api/v1/rules")
        wait(
            "notification_503",
            lambda: notifications("CashGuardSyntheticDelivery", "firing", 503),
        )
        wait(
            "retry_accepted",
            lambda: notifications("CashGuardSyntheticDelivery", "firing"),
        )
        first = notifications("CashGuardSyntheticDelivery", "firing")[0]
        rejected = notifications("CashGuardSyntheticDelivery", "firing", 503)[0]
        assert first["time"] > rejected["time"]
        assert first["time"] - rejected["time"] < 45
        assert first["path"] == "/trading-critical"

        now = datetime.datetime.now(datetime.timezone.utc)
        labels = {
            "alertname": "CashGuardSyntheticSilence",
            "severity": "critical",
            "team": "trading",
        }
        silence = request(
            "http://alertmanager:9093/api/v2/silences",
            "POST",
            {
                "matchers": [
                    {
                        "name": "alertname",
                        "value": labels["alertname"],
                        "isRegex": False,
                        "isEqual": True,
                    }
                ],
                "startsAt": now.isoformat(),
                "endsAt": (now + datetime.timedelta(minutes=10)).isoformat(),
                "createdBy": "synthetic-probe",
                "comment": "disposable internal-only test",
            },
        )
        assert silence and "silenceID" in silence
        injected = {
            "labels": labels,
            "startsAt": now.isoformat(),
            "endsAt": (now + datetime.timedelta(minutes=10)).isoformat(),
        }
        request("http://alertmanager:9093/api/v2/alerts", "POST", [injected])
        wait(
            "silenced",
            lambda: any(
                x["status"]["silencedBy"]
                for x in (request("http://alertmanager:9093/api/v2/alerts") or [])
                if x["labels"].get("alertname") == labels["alertname"]
            ),
        )
        assert not notifications(labels["alertname"], code=None)

        # Send the exact same alert fingerprint twice, then a clean restart. The
        # durable notification log should suppress a fresh firing notification.
        existing = first["payload"]["alerts"][0]
        duplicate = {
            k: existing[k] for k in ("labels", "annotations", "startsAt", "endsAt")
        }
        duplicate["endsAt"] = (now + datetime.timedelta(minutes=10)).isoformat()
        request(
            "http://alertmanager:9093/api/v2/alerts", "POST", [duplicate, duplicate]
        )
        p.event("duplicate_submitted")
        docker("stop", "--time", "5", p.name)
        assert not p.ready()
        wait("outage_scrape_down", lambda: query('up{job="alertmanager"} == 0'), 35)
        for path in (
            "/",
            "/v1/health/live",
            "/v1/operator/status",
            "/v1/emergency/kill-switch",
        ):
            assert request("https://nginx" + path) == "synthetic HTTP fixture"
        docker("restart", nginx)
        wait(
            "ingress_cold_start_during_am_outage",
            lambda: request("https://nginx/v1/operator/status"),
        )
        assert p.inspect()["State"]["ExitCode"] == 0
        docker("start", p.name)
        p.wait_ready()
        p.assert_private_pid()
        wait("scrape_recovered", lambda: query('up{job="alertmanager"} == 1'), 35)
        assert (
            request("http://alertmanager:9093/api/v2/silence/" + silence["silenceID"])[
                "status"
            ]["state"]
            == "active"
        )
        request("http://alertmanager:9093/api/v2/alerts", "POST", [injected])
        wait(
            "silenced_alert_replayed_after_restart",
            lambda: any(
                x["labels"] == labels
                and silence["silenceID"] in x["status"]["silencedBy"]
                for x in (request("http://alertmanager:9093/api/v2/alerts") or [])
            ),
        )
        # Active alerts live in memory. Explicitly resend the firing fingerprint
        # AFTER restart and wait beyond the pinned daemon's cluster settle/retry
        # period; absence of resend must never masquerade as durable dedup.
        request(
            "http://alertmanager:9093/api/v2/alerts", "POST", [duplicate, duplicate]
        )
        wait(
            "delivery_active_after_restart",
            lambda: any(
                x["labels"] == duplicate["labels"] and x["status"]["state"] == "active"
                for x in (request("http://alertmanager:9093/api/v2/alerts") or [])
            ),
        )
        hold_until = time.monotonic() + 45
        while time.monotonic() < hold_until:
            assert len(notifications("CashGuardSyntheticDelivery", "firing")) == 1
            time.sleep(1)
        assert query(
            'ALERTS{alertname="CashGuardSyntheticDelivery",alertstate="firing"}'
        )
        assert len(notifications("CashGuardSyntheticDelivery", "firing")) == 1
        assert not notifications(labels["alertname"], code=None)
        p.event("restart_retains_silence_and_dedup")

        request(
            "http://alertmanager:9093/api/v2/silence/" + silence["silenceID"], "DELETE"
        )
        request("http://api:8000/control", "POST", {"active": 0})
        wait(
            "unsilenced_delivery",
            lambda: notifications(labels["alertname"], "firing"),
            155,
        )
        wait(
            "resolved_retry_accepted",
            lambda: notifications("CashGuardSyntheticDelivery", "resolved"),
            155,
        )
        assert len(notifications("CashGuardSyntheticDelivery", "firing")) == 1
        assert not query(
            'ALERTS{alertname="CashGuardSyntheticDelivery",alertstate="firing"}'
        )
        assert p.inspect()["State"]["Health"]["Status"] == "healthy"
        p.receipt["notifications"] = request("http://api:8080/events")
        p.receipt["result"] = "PASS"
        print(
            json.dumps(
                {"result": "PASS", "run": p.run, "scope": p.receipt["qualification"]}
            )
        )
    finally:
        try:
            if p.receipt["result"] != "PASS" and client in p.containers:
                p.receipt["failure_diagnostic"] = {
                    "targets": request("http://prometheus:9090/api/v1/targets"),
                    "alerts": request("http://alertmanager:9093/api/v2/alerts"),
                    "sink_events": request("http://api:8080/events"),
                    "fixture_logs": docker("logs", client, check=False).stderr,
                    "am_logs": p.logs(),
                }
        finally:
            p.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chain", action="store_true", required=True)
    parser.parse_args()
    chain()
