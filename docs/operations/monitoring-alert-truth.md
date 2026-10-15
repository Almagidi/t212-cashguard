# Registered metric alert expressions: producers not wired

This PR changes two alert expressions and their regression tests. It does **not** make them operational coverage: at the verified base, production code does not call their metric producers. Agent A owns that instrumentation work (N-42), including persisted-state initialisation and multiprocess visibility.

`HighOpenPositions` evaluates the registered `cashguard_open_positions_count` gauge above eight for two minutes. It reports the scraped process value, not independently verified holdings or an account-wide financial limit. The previous `cashguard_account_open_positions` name is not emitted by the metrics module. Eight is the unchanged legacy operational threshold, not a newly qualified financial limit. Stale or absent series do not establish healthy positions.

`KillSwitchActivated` reports historical activation **evidence**, using either a counter increase within five minutes or a positive counter whose matching series was absent at the five-minute offset. Actor children are created lazily by their first activation; increase alone misses that first event for every actor after process restart. The positive first-observation branch covers that case, including an actor created after other successful scrapes. Zero-only new series and counter resets to zero do not fire. Existing positive history expires once the same series exists at the offset and there is no recent increase.

A newly observed positive series can contain older activations: a scrape gap, relabeling, new instance or monitoring restart can re-notify history. This rule cannot date those events, prove a fresh activation or deduplicate them across processes. Conversely, a reset to zero between scrapes can hide an event, and missing/stale series are unknown, not healthy. Neither branch establishes current trading state or a persisted safety setting. Alertmanager's `resolved` notification means the historical observation window expired or its series disappeared; **it does not mean the kill switch is off or trading resumed**. Both firing and resolved payloads carry this qualification. Do not use this rule to authorize execution or as a complete safety audit trail.

The separate active-state gauge is process-local. Registration does not prove synchronization with persisted settings. A read-only search at this change's base found no production callers of `record_kill_switch`, `record_kill_switch_deactivated` or `update_account_metrics`; Agent A owns the reproduced persisted-on/metrics-off P2. Multiple API replicas, process restarts and Celery processes need an independently qualified aggregation and refresh contract. These rules do not supply one. Existing unset cash/auto-trading gauges can also produce misleading alerts; no rule-side invented readiness marker can repair that without authoritative backend signals.

Run with the API requirements installed and Docker available:

```sh
docker pull prom/prometheus:v3.1.0
python3 -m unittest discover -s infra/tests -p test_alert_rule_truth.py -v
```

The test selects the source Prometheus image, resolves its immutable local ID and runs real `promtool` with no networking, host ports or anonymous volumes. It prints image identity, architecture, rule hash and evaluation assertion count. It imports only the metrics module into the test process, supplies synthetic helper calls and inspects its actual endpoint exposition; both changed rules must reference the emitted names. No financial service, database, broker or owner process runs. Synthetic sample timelines verify pending, firing, threshold, expiry, first positive observations, zero births, late actor creation, reset, scrape gaps, stale and absent behavior.

Shared CI may not invoke this standalone suite. These tests do not prove production application emission, financial truth, scrape freshness, Alertmanager delivery or real notifications. Separate live-container evidence uses manual module inputs and a custom isolated HTTP receiver. Normal production Alertmanager startup remains a separate blocker pending its ownership release and correction.

Rollback restores prior rules through the protected release lane, without database or financial changes. Source testing does not authorize deployment or reload. R5/R6/LIVE remain NO-GO.
