# Position and kill-switch alert qualification

`HighOpenPositions` evaluates the registered `cashguard_open_positions_count` gauge above eight for two minutes. It reports the scraped process value, not independently verified holdings or an account-wide financial limit. The previous `cashguard_account_open_positions` name is not emitted by the metrics module. Stale or absent series do not establish healthy positions.

`KillSwitchActivated` reports a historical telemetry event: the scraped activation counter increased within five minutes. It expires when the observed increment leaves the window. It does not establish current trading state or a persisted safety setting. Static lifetime counts, counter resets alone and a first sample without an observed increment do not fire. An activation before the first scrape can be missed; this is not a complete safety audit trail or current kill-switch monitor. Do not use it to authorize execution.

The separate active-state gauge is process-local. Registration does not prove synchronization with persisted settings. A read-only search at this change's base found no production callers of `record_kill_switch`, `record_kill_switch_deactivated` or `update_account_metrics`; that instrumentation gap belongs to Agent A and remains open. Multiple API replicas, process restarts and Celery processes need an independently qualified aggregation and refresh contract. These rules do not supply one.

Run with the API requirements installed and Docker available:

```sh
docker pull prom/prometheus:v3.1.0
python3 -m unittest discover -s infra/tests -p test_alert_rule_truth.py -v
```

The test selects the source Prometheus image, resolves its immutable local ID and runs real `promtool` with no networking, host ports or anonymous volumes. It prints image identity, architecture, rule hash and evaluation assertion count. It imports only the metrics module into the test process, supplies synthetic helper calls and inspects its actual endpoint exposition. No financial service, database, broker or owner process runs. Synthetic sample timelines verify pending, firing, threshold, expiry, reset, zero, stale and absent behavior.

Shared CI may not invoke this standalone suite. These tests do not prove application metric emission, financial truth, scrape freshness, Alertmanager delivery or real notifications. Normal production Alertmanager startup remains a separate blocker pending its ownership release and correction.

Rollback restores prior rules through the protected release lane, without database or financial changes. Source testing does not authorize deployment or reload. R5/R6/LIVE remain NO-GO.
