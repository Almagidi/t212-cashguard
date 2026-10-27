# Safety monitoring contract

The API reads saved safety state for each scrape. These signals describe saved
controls, not execution results: a saved kill switch does not prove positions are
flat, orders were cancelled, or every worker observed the change. No monitoring
operation enables trading or a venue.

## Read quality and missing state

`cashguard_safety_state_read_ok` is always present: `1` means the bounded database
read completed; `0` means it failed or timed out. A successful read can still find
missing settings. `cashguard_safety_state_known{control,scope}` therefore reports
eight fixed combinations: global kill switch and auto trading, plus kill switch,
auto trading and degraded mode for each of `t212` and `kraken`. A marker of `0`
means unknown. The corresponding value series is omitted, never invented as `0`.
The timestamp is also omitted when unavailable or when no retained event exists.

An HTTP 200 scrape is not evidence that safety state is readable. After one
minute, `SafetyStateReadUnavailable` warns for each successfully scraped API
instance whose read marker is `0` or absent. With a successful read,
`SafetyControlUnknown` warns for each present unknown marker. It does not duplicate
all eight markers when the whole read failed. The collector regression test checks
the exact eight-marker contract; the rule does not detect an individually absent
marker. `APIDown` covers configured targets whose scrape fails. Removing a target
from service discovery is outside that rule's coverage.

The exporter cannot distinguish an intentionally unused venue from a missing
required venue row. Both remain unknown and produce the conservative warning.
Check the deployment's intended venue configuration and the operator view. Do not
insert rows, enable a venue or change the disabled default to silence a warning.
Any intentional alert routing or silence needs its own operational decision; this
change creates no silence. Read-quality warnings use team `ops`, avoiding the
existing trading-team critical/warning inhibition. Other inhibition and delivery
settings still apply, so this is not notification-delivery qualification.

## Current state and retained history

`KillSwitchActive` and `VenueKillSwitchActive` report saved current halt state
immediately, only with a successful read. The latter retains the venue label and
is not a global halt assertion. `AutoTradingDisabled` retains the existing
five-minute warning for a *known* disabled global control. Disabled is the default
and may be intentional; this monitoring change does not alter that policy.

`cashguard_kill_switch_last_activation_timestamp_seconds` is the most recent
retained global `kill_switch_on` risk-event time. `KillSwitchActivated` keeps its
existing alert name but now means this timestamp falls in `(now - 300s, now]`.
An old event first seen after a restart, a delayed scrape or a stale-series gap
does not become a new activation. Future timestamps are excluded until their time
arrives. Correct clock synchronization remains an operational requirement.
Resolution means the event left this window or evidence became unavailable; it
does not mean trading resumed. Unknown reads have a separate warning.

This timestamp is not an activation count, actor attribution, per-venue history,
or a complete audit trail. Retention can remove events. The old labelled
`cashguard_kill_switch_activations_total` declaration remains for source
compatibility but has no production producer and emits no fabricated children.
It is not queried by these rules or this dashboard. Removed process-local helper
functions are not restored; the test exercises the actual route through its
database-session dependency, with synthetic state supplied to the reader.

## Unavailable account coverage and Grafana

There is no authoritative producer for account cash, account value or open
positions in this monitoring path. The dashboard explicitly labels these panels
unavailable and links to the operator view. It does not display default zero,
infer a currency, or reconstruct financial data. The historical `LowAvailableCash`
threshold `<100` and `HighOpenPositions` threshold `>8` are retired from active
rules, not silently fulfilled. Restoring this coverage needs a separately reviewed
producer, data ownership/freshness contract and financial threshold policy.

Safety stat panels use instant queries and `last`, rather than a range query's
last non-null historical value. Missing/null values are neutral and marked unknown
where Grafana has a field to render; an empty result can display Grafana's no-data
state. Neither is a healthy state. The read-quality panel and unknown-controls
table must be considered together: an empty unknown table alone proves nothing.
The history panel converts seconds to milliseconds for Grafana's date formatter
and does not claim that absence means no activation ever occurred. Other dashboard
panels retain their existing scope and have not been comprehensively qualified.

## Reproduce the contract checks

Use Python 3.12 with `apps/api/requirements.txt` installed and a Docker daemon.
The repository's Prometheus image is `prom/prometheus:v3.1.0`. Obtain it from the
official Prometheus image distribution, then run:

```sh
docker pull prom/prometheus:v3.1.0
python -m ruff check infra/tests/test_alert_rule_truth.py
python -m ruff format --check infra/tests/test_alert_rule_truth.py
python -m unittest discover -s infra/tests -p test_alert_rule_truth.py -v
```

The test resolves that image to its immutable local image ID, verifies Promtool
3.1.0, checks rules and evaluates synthetic timelines offline with no ports. It
uses read-only public fixture copies and removes its own containers. It fails if
the image, Docker or Promtool is unavailable; it does not replace rule evaluation
with string checks. CI runs the same bounded standalone job. PostgreSQL durability,
missing rows and timeout/read-failure behavior remain covered separately by
`apps/api/tests/integration/test_metrics_safety_state.py` with a synthetic database.
These checks do not certify production retention, multi-process restart behavior,
broker cancellation, notification delivery, or release readiness.

The source changes for the durable reader and its monitoring consumers must be
integrated together. The earlier alert-contract proposal's collector test calls
removed process-local helpers and must not be copied unchanged onto this reader.

## Primary references

- [Official Prometheus image distribution](https://prometheus.io/docs/prometheus/latest/installation/)
- [Prometheus 3.1 operators and vector matching](https://github.com/prometheus/prometheus/blob/v3.1.0/docs/querying/operators.md)
- [Prometheus 3.1 rule-test fixtures, missing and stale samples](https://github.com/prometheus/prometheus/blob/v3.1.0/docs/configuration/unit_testing_rules.md)
- [Alert expressions and pending/firing state](https://prometheus.io/docs/prometheus/latest/configuration/alerting_rules/)
- [Query staleness](https://prometheus.io/docs/prometheus/latest/querying/basics/#staleness)
- [Python labelled-series initialization](https://prometheus.github.io/client_python/instrumenting/labels/)
- [Grafana 11.4 special value mappings](https://github.com/grafana/grafana/blob/v11.4.0/packages/grafana-data/src/types/valueMapping.ts)
- [Grafana stat configuration](https://grafana.com/docs/grafana/latest/panels-visualizations/visualizations/stat/)

The saved-state meanings above come from CashGuard's source contract; these
references explain the monitoring tools and do not establish financial policy.
