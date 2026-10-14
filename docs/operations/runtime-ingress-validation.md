# Synthetic production ingress validation

This qualifies source behavior with disposable fixtures. It is not deployment or trading readiness. Claude retains the protected-main release lane; R5/R6 and LIVE remain NO-GO.

nginx shares a private `monitoring_ingress` bridge with Grafana, while remaining outside datastore and monitoring-backend networks. Grafana shares a separate private `monitoring_data` bridge with Prometheus only. Prometheus retains its internal scrape/delivery edges. Grafana cannot directly connect to API, web, PostgreSQL, Redis, Alertmanager or exporters; access to intended application routes through nginx remains subject to application authentication. Prometheus remains a trusted monitoring dependency, not an authentication boundary.

Grafana is resolved at request time using Docker DNS, a ten-second positive cache and a three-second resolver timeout. nginx has no Grafana startup dependency. A missing Grafana DNS record must leave API/web routes available and return 502 for Grafana. Grafana serves its own `/grafana/` prefix, which the variable proxy preserves. Forwarded protocol is HTTPS. Only nginx publishes host ports.

Set `PUBLIC_ORIGIN` to the intended HTTPS DNS/IPv4 origin and optional port 1–65535, without path, trailing slash, query or user information. Missing origin no longer blocks Compose rendering or nginx/API restoration; Grafana alone refuses startup. Passwords must be 16–4096 printable ASCII bytes, without boundary whitespace. Invalid inputs produce a fixed error without values. The vetted password uses a randomized mode-600 file inside a mode-700 directory on `/tmp` tmpfs and Grafana's later file provider, preserving `${...}`, quotes and shell metacharacters literally.

Normal startup now synchronizes the persisted credential **before HTTP starts**, using the pinned 11.4.0 supported CLI with `--password-from-stdin`. Passwords never enter CLI arguments or emitted CLI diagnostics. CLI initialization migrates/initializes fresh SQLite storage with the vetted credential. Existing storage is retained. Identity discovery uses a private cold copy, including existing SQLite WAL/SHM, so the discovery command cannot change the retained database. It requires exactly one server administrator, username `admin`, ID 1, then resets that explicit identity in the actual `/var/lib/grafana/grafana.db`. Grafana 11.4.0 can exit zero on reset errors: the procedure also requires its positive successful-update message, withholding diagnostics. Ambiguous/missing/nondefault identities, custom database backends/paths/config/home, extra startup arguments, corrupt storage or missing CLI support fail closed. Additional ordinary users are preserved; multiple server administrators require separate qualification. This conservative contract replaces guessing another admin ID.

The procedure assumes a single stopped SQLite writer, as in this single-container Compose recipe. Concurrent database writers/replicas and external databases are unsupported. The private cold copy requires tmpfs space proportional to SQLite storage; insufficient space denies startup rather than bypassing the gate. Captured diagnostics and the copy are deleted before `/run.sh` is executed; only the mode-600 password-provider file remains until tmpfs teardown. Failure cleanup removes the task's private directory. The original umask is restored for the server. Source changes do not perform any production credential operation.

On a future separately authorized deployment, **every Grafana start/restart resets the supported administrator to the vetted supplied credential**. Changed configuration therefore revokes the previous password without deleting dashboards/users. Existing operator sessions and authentication tokens are separate from password validity and require their own deployment policy. Changing the supplied credential requires owner approval in a real environment. A rollback to the old bootstrap-only entrypoint does not restore the prior stored password and can reopen the security defect; retain this gate or keep Grafana stopped while applying a reviewed recovery. Never delete `grafana_data` to recover. Unknown identity recovery must use supported Grafana tools/API on separately authorized storage; the test harness only demonstrates recovery on its own isolated volume.

Grafana Basic authentication is disabled so API Basic requests cannot bypass the strict login budget. Grafana anonymous access stays disabled and session cookies are marked Secure. Its public route relies on Grafana authentication, not an added application login. nginx limits Grafana traffic per IP to 120 requests/minute with a burst of 60; `/grafana/login` also has five requests/minute with a burst of three. Limits return 429 and use budgets separate from application API requests. These controls reduce brute-force exposure; they do not make Grafana internet-inaccessible or prove an authorized production access policy.

Run configuration contracts without starting services:

```sh
python3 -m unittest discover -s infra/tests -p 'test_runtime_boundaries.py' -v
```

The command uses Docker Compose's actual JSON renderer with synthetic variables and `--env-file /dev/null`. It does not read a project `.env` and never prints rendered secrets. Missing shared secrets must fail; missing `PUBLIC_ORIGIN` renders successfully and Grafana preflight rejects it locally.

For the explicit runtime probe, pre-pull source image versions and the fixture image:

```sh
docker pull nginx:1.27-alpine
docker pull grafana/grafana:11.4.0
docker pull python:3.12.13-slim-bookworm
python3 infra/tests/runtime_probe.py --run-synthetic
```

The probe uses actual nginx/Grafana images, source nginx configuration and the rendered Grafana entrypoint/environment with synthetic credentials. It creates random task-owned containers/internal networks, a one-day certificate, tmpfs storage for ingress, and a uniquely labeled persistent Grafana volume for authentication qualification. No host port, real broker, SMTP recipient, credential or owner data is supplied. Requests originate inside the isolated network and verify certificate and hostname. Only task-created resources are removed. API, web, Prometheus, PostgreSQL, Redis and Alertmanager targets in this ingress test are HTTP fixtures; none proves those services' runtime correctness.

The retained-volume assertions cover a weak WAL seed positive control and killed-writer recovery, weak→strong and strong→changed credential rejection/acceptance, literal credentials, repeated startup, preserved dashboard, ordinary user, organization, datasource metadata and volume sentinel, transient database permission failure/recovery, ambiguous-admin fail-closed/recovery, CLI absence/zero-exit-error/interruption, corrupt DB failure and Basic-auth rejection. All test volumes are removed only after verifying task labels. The runtime assertions cover TLS/API/web/Emergency-route forwarding to fixtures, real Grafana login and anonymous 401, invalid-password/origin process exit, intended Grafana-to-Prometheus connectivity, forbidden Grafana and nginx target DNS/IP connectivity with working controls, removal of Grafana DNS advertisement followed by nginx restart/API availability/Grafana 502, recovery after DNS returns, replacement with explicitly different container IPs without nginx reload, stopped-upstream failure/restart, and login 429 with an independent API budget.

Remaining limitations:

- Production external-network egress is replaced with an internal network. Internet-egress denial is not behaviorally qualified.
- real public certificates/origins, browser/SSO flows, full application startup, financial safety, monitoring delivery and backup/restore require separate evidence.
- Retained synthetic 11.4.0 SQLite evidence qualifies password synchronization and specific data preservation; it does not qualify owner data, other Grafana versions, nondefault/multiple administrators, external databases, concurrent writers or general upgrade migration compatibility.
- These infrastructure probes run locally; standard CI badges do not prove they ran. Agent A owns shared workflow integration.
- Source image tags are mutable; each acceptance receipt must record the actually inspected digest and architecture.

Source rollback is a revert through the release owner. It must not delete or downgrade datastore/Grafana records. Future authorized deployment may need service recreation for network changes; deployment, stored-identity verification and real credential authorization remain separate owner actions.

CLI parser tests exercise exact source shell statements against supported output fixtures, including continuation and heading rows. API identity-update fixtures separately prove unknown-identity denial; API normalization means they do not alone prove those malformed CLI strings can be created through that API. Both surviving and caught intermediate mutation attempts remain historical evidence. Interruption testing proves a blocked pre-listener CLI can be killed and recover retained data; it does not prove interruption inside a real password-update transaction.
