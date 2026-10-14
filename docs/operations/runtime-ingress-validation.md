# Synthetic production ingress validation

This qualifies source behavior with disposable fixtures. It is not deployment or trading readiness. Claude retains the protected-main release lane; R5/R6 and LIVE remain NO-GO.

nginx shares a private `monitoring_ingress` bridge with Grafana, while remaining outside datastore and monitoring-backend networks. Grafana shares a separate private `monitoring_data` bridge with Prometheus only. Prometheus retains its internal scrape/delivery edges. Grafana cannot directly connect to API, web, PostgreSQL, Redis, Alertmanager or exporters; access to intended application routes through nginx remains subject to application authentication. Prometheus remains a trusted monitoring dependency, not an authentication boundary.

Grafana is resolved at request time using Docker DNS, a ten-second positive cache and a three-second resolver timeout. nginx has no Grafana startup dependency. A missing Grafana DNS record must leave API/web routes available and return 502 for Grafana. Grafana serves its own `/grafana/` prefix, which the variable proxy preserves. Forwarded protocol is HTTPS. Only nginx publishes host ports.

Set `PUBLIC_ORIGIN` to the intended HTTPS origin, with a DNS/IPv4 hostname and optional numeric port 1–65535, without a trailing slash, path, query or user information (IPv6 literal origins are unsupported by this preflight) (for example `https://cashguard.example`). Compose requires the setting; Grafana's normal startup preflight rejects an insecure/malformed origin, blank/whitespace passwords and admin passwords outside the printable-ASCII contract or shorter than sixteen characters. The error prints no values. This guard applies to configured initial credentials; it does not rotate or validate credentials already stored in a persistent Grafana database. Existing deployments require separate operator verification of their stored authentication state. The script passes signals through `exec /run.sh`. It writes the initial password to a randomized mode-600 file in a private mode-700 directory, then supplies Grafana’s file provider, which runs after environment expansion. This keeps token-like passwords literal. Boundary spaces and all non-ASCII/control bytes are rejected because Grafana’s file provider trims Unicode whitespace. This explicit supported-password contract avoids silent mutation. The file remains on the container’s `/tmp` tmpfs for startup and is removed by tmpfs teardown on stop, rather than placed on a data volume. No shell evaluation of credential text occurs.

Grafana anonymous access stays disabled and session cookies are marked Secure. Its public route relies on Grafana authentication, not an added application login. nginx limits Grafana traffic per IP to 120 requests/minute with a burst of 60; `/grafana/login` also has five requests/minute with a burst of three. Limits return 429 and use budgets separate from application API requests. These controls reduce brute-force exposure; they do not make Grafana internet-inaccessible or prove an authorized production access policy.

Run configuration contracts without starting services:

```sh
python3 -m unittest discover -s infra/tests -p 'test_runtime_boundaries.py' -v
```

The command uses Docker Compose's actual JSON renderer with synthetic variables and `--env-file /dev/null`. It does not read a project `.env` and never prints rendered secrets. Missing required settings must fail.

For the explicit runtime probe, pre-pull source image versions and the fixture image:

```sh
docker pull nginx:1.27-alpine
docker pull grafana/grafana:11.4.0
docker pull python:3.12.13-slim-bookworm
python3 infra/tests/runtime_probe.py --run-synthetic
```

The probe uses actual nginx/Grafana images, source nginx configuration and the rendered Grafana entrypoint/environment with synthetic credentials. It creates random task-owned containers/internal networks, a one-day certificate, and tmpfs Grafana storage. No host port, real broker, SMTP recipient, credential or owner data is supplied. Requests originate inside the isolated network and verify certificate and hostname. Only task-created resources are removed. API, web, Prometheus, PostgreSQL, Redis and Alertmanager targets in this ingress test are HTTP fixtures; none proves those services' runtime correctness.

The runtime assertions cover TLS/API/web routing, real Grafana login and anonymous 401, invalid-password/origin process exit, intended Grafana-to-Prometheus connectivity, forbidden Grafana and nginx target DNS/IP connectivity with working controls, removal of Grafana DNS advertisement followed by nginx restart/API availability/Grafana 502, recovery after DNS returns, stopped-upstream failure/restart, and login 429 with an independent API budget.

Remaining limitations:

- Production external-network egress is replaced with an internal network. Internet-egress denial is not behaviorally qualified.
- Grafana container replacement with a changed IP, real public certificates/origins, persisted existing admin credentials, browser/SSO flows, full application startup, financial safety, monitoring delivery and backup/restore require separate evidence.
- Test Grafana storage is disposable and does not establish persistence or migration compatibility.
- These infrastructure probes run locally; standard CI badges do not prove they ran. Agent A owns shared workflow integration.
- Source image tags are mutable; each acceptance receipt must record the actually inspected digest and architecture.

Source rollback is a revert through the release owner. It must not delete or downgrade datastore/Grafana records. Future authorized deployment may need service recreation for network changes; deployment and stored-credential verification remain separate operator actions.
