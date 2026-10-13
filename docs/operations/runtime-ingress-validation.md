# Synthetic production ingress validation

This checks a source configuration, not a deployed release or a trading-readiness verdict.

The production proxy resolves `api`, `web`, and `grafana` while loading its configuration. Grafana must be on a network shared with nginx, and Compose must start it before nginx. The private `monitoring_ingress` bridge contains only nginx and Grafana. nginx remains outside the `internal` datastore and monitoring backend network. Grafana retains its backend connection to Prometheus and anonymous authentication remains disabled. Only nginx publishes host ports.

Grafana serves its own `/grafana/` prefix. The proxy must preserve that prefix; stripping it makes Grafana redirect back to the prefixed URL. Grafana authentication still protects its user API. This change does not add application authentication to nginx.

Run the configuration contracts without starting services:

```sh
python3 -m unittest discover -s infra/tests -p 'test_runtime_boundaries.py' -v
```

The command uses Docker Compose's actual JSON renderer with synthetic values and `--env-file /dev/null`. It does not read a project `.env` or start the production stack. Rendered secrets are not printed. Missing required settings must fail rendering.

For the explicit container probe, first pull the images matching the source Compose configuration:

```sh
docker pull nginx:1.27-alpine
docker pull grafana/grafana:11.4.0
docker pull python:3.12.13-slim-bookworm
python3 infra/tests/runtime_probe.py --run-synthetic
```

The probe requires Docker, Docker Compose, Python 3, and OpenSSL supporting `-addext`. It creates random task-owned container and network names, synthetic Grafana credentials, and a one-day test certificate. It verifies the certificate and `nginx` hostname. Every projected network is internal, no host port is published, and HTTPS requests originate within the harness. No real broker, SMTP, telemetry, or application credential is supplied. Cleanup removes only the containers and networks created by that invocation; persistent production volumes and other agents' resources are untouched.

The probe runs the actual nginx configuration and real Grafana image. It verifies API/web routing, the Grafana login page, anonymous user rejection, nginx-to-datastore DNS and IP denial, a positive API-to-datastore TCP control, stopped-Grafana HTTP 502/504 (within the existing nginx timeout), and restart recovery. API, web, PostgreSQL, and Redis are HTTP fixtures, not the real applications/datastores. Internet egress is not behaviorally tested; the internal-network flags are configuration evidence only.

Limitations and outstanding release evidence:

- Production `external` network egress is intentionally replaced by an internal network in the probe. The static test checks production membership; the runtime test proves the corresponding cross-network boundaries in the synthetic topology.
- Grafana uses the rendered production environment with synthetic credentials; only analytics/update checks are disabled. The production recipe's public root URL, TLS certificate provisioning, actual public origin, and external authentication flows still need operator-specific validation.
- Container replacement with a changed IP is not tested; nginx may need a reload after an upstream is replaced. The recovery check restarts the same container.
- Full API/web image startup, database transactions, financial safety, metrics alerts, backup recovery, and browser application behavior are not certified by these probes.
- The standalone infrastructure contracts are not yet wired into the shared CI workflow. Do not infer that a standard CI badge ran them. Agent A owns workflow integration.
- R5/R6 and LIVE remain NO-GO until their independent gates and operational authorizations are satisfied.

Rollback is a source revert through the release owner. It restores the prior topology and routing defect. It must not delete, restore, or downgrade any datastore or financial record. After an authorized future deployment, network-topology changes may require service recreation; that operational action is outside source-only engineering authority.
