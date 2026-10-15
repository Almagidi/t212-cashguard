# Alertmanager startup and isolated qualification

`prom/alertmanager:v0.27.0` has `/bin/alertmanager` as its image entrypoint.
Passing `sh` in Compose `command` made the daemon exit with `unexpected sh`.
The production recipe now explicitly invokes `/bin/sh` with the existing private
renderer. The renderer validates configuration without exposing parser output,
checks that persistent storage is writable, then `exec`s the pinned daemon as
PID 1. Its original storage directory and routing template are retained.

The rendered file has mode 600 in a unique mode-700 directory. A `/tmp` tmpfs
removes it when the container stops. Rendering/preflight errors remove the private
file; malformed multiline settings are refused without echoing their values.
SMTP environment values remain accessible to authorised Docker administrators;
never export a real `docker compose config`, container environment inspection or
debug trace into logs/evidence. The tests use synthetic settings and withhold raw
Compose configuration. No shell evaluates password contents.

The Docker healthcheck requests `http://127.0.0.1:9093/-/ready` every 15 seconds
(5-second timeout, three retries, 15-second start period). HTTP readiness confirms
the daemon is serving, not that SMTP delivery, ongoing storage durability or
financial metric production is working. An unwritable storage directory refuses
startup; permission errors in existing silence/notification snapshots also cause
the actual daemon to refuse startup. A subsequent runtime disk failure still
requires monitoring and operator handling.

## Reproduce safely

Pre-pull the source image tags separately, then run from the repository root:

```sh
python3 -m unittest discover -s infra/tests -p test_alertmanager_startup.py -v
python3 -m unittest discover -s infra/tests -p test_alertmanager_renderer.py -v
python3 infra/tests/alertmanager_probe.py --chain
```

The last command requires PyYAML. Set `CASHGUARD_N41_EVIDENCE` to a task-owned
directory to retain timestamped JSON receipts. Tests bind the locally installed
source tags to their inspected immutable image digests and disclose architecture.
They render with `--env-file /dev/null` and an allowlisted synthetic environment,
then start a projected Compose service with the source startup command,
entrypoint, renderer and healthcheck. The projection replaces resource names,
storage and network with labeled disposable resources; restart is disabled so
failures are bounded. No production stack, host port, real `.env`, broker,
external SMTP server or production volume is used. Cleanup verifies ownership
labels and absence of remaining run resources; it never performs global pruning.

The integration probe keeps the source scrape targets, evaluation cadence,
existing rule groups and Alertmanager route/group/repeat timers. It appends an
inert 30-second alert and replaces email transports with an internal HTTP sink.
The API/exporters are HTTP fixtures with no financial telemetry. It checks
registration, pending/firing, rejected 503 delivery and accepted retry, routing,
silence/release, active-fingerprint replay across restart, notification-log
deduplication during the measured window, scrape outage/recovery and resolution.
These are synthetic infrastructure results. They do not qualify real SMTP,
financial producers, at-least-once delivery under every crash, enabled trading,
production readiness or RTO/RPO. Missing financial telemetry remains UNKNOWN.

## Release and rollback

Claude owns protected-main integration and production deployment requires
separate authorisation. Before integration, verify the exact signed source head,
reviewed tree, current-base merge checkout and required CI; standalone Docker
checks are local evidence unless the workflow explicitly runs them. Preserve
notification/silence data and verify operator/site ingress independently of
monitoring availability. The source nginx/API/web have no Alertmanager startup
dependency.

For an authorised future rollout, retain the exact source/image identities and
existing persistent volume, confirm configuration preflight, readiness and an
approved synthetic notification through the intended transport. A failed
preflight must remain stopped and be diagnosed without logging settings. Do not
delete state to make startup green. Reverting this change restores the known
broken entrypoint contract, so that is a source rollback, not a working monitoring
fallback; keep the monitoring outage explicit and retain independent emergency
ingress. R5/R6 and LIVE remain NO-GO.
