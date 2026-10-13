# CFD funding: retired

CashGuard is a cash-only application. It does not trade CFDs, use margin or leverage, or sell
short. The feature that estimated overnight CFD financing has been retired from active use.

## What changed

- `CASH_ONLY_MODE` is an invariant, not a setting. Any value other than true, from the
  environment, an env file or the settings constructor, makes the API, the worker and the
  scheduler refuse to start. Assigning to it or deleting it on the running settings is
  refused. This closes every configuration path; code already running inside the process
  could still overwrite the value by other means, which no setting can prevent.
- The scheduled task `track_cfd_funding`, its service module and its broker-provider purpose
  are removed. Nothing is scheduled, and nothing in the application reads or writes the
  `cfd_funding_costs` table. A test fails if application code starts to.
- A strategy cannot be created or updated with `allow_short`. If a strategy row carries it
  anyway, the runner opens no position for that strategy (its existing positions are still
  managed). The runner refuses any entry that is not a buy, and the risk engine refuses any
  order treated as a CFD.
- Outside mock mode, a manual sell is refused before it reaches the broker unless a
  position reported by the broker covers it (the smaller of the holding and the amount
  free to sell). If the positions cannot be read, the sell is refused.

## What was kept, and why

- The table `cfd_funding_costs`, its model and migration `0004_cfd_funding_cost`. Removing
  them is a schema change and needs its own approval.
- The CFD columns on `risk_profiles` (migration `0003_cfd_risk_profile`) and the function
  `RiskEngine.check_cfd_limits`. Neither is reachable from an order path any more. They are
  left for the same approved clean-up.
- `allow_short` inside the ORB and Opening Fade strategy classes. The backtester can still
  model it for research; no order path will act on it.

## What is still open

- A manual **market buy** is checked against a placeholder price of 100 when no limit or
  stop price is given, so the cash check can under-count for an expensive share (finding
  N-02, Unit 7). The broker remains the last line for such an order.
- In mock mode a manual sell through the order route is not checked against holdings.
  Nothing reaches a broker there: the route uses the mock adapter, which accepts a sell of
  an instrument it does not hold. The paper engine's oversell guard covers the paper order
  route and the scheduled runner, not this route (Unit 5).
- The holding check is in the manual order route, not inside the execution engine. Every
  other place that submits a sell today sells from a held position, but a future caller
  would not be covered by this check.
- The check compares one sell with the reported holding. It does not subtract working sell
  orders or a second request made at the same moment, and it is not known here whether the
  broker's "free to sell" figure already excludes shares committed to pending sells. When
  the broker gives no "free to sell" figure, shares held in pies are counted. The broker
  is still the final arbiter in each of these cases.
- Kraken has no manual order route and no holding check. It relies on its strategies only
  buying and on its adapter never sending a leverage parameter.
- The scheduled runner's limit price is 0.1% above the estimate used in its cash check.
- A refused manual sell and a refused short-selling strategy leave no audit row or risk
  event: the first is visible as an HTTP 422, the second in the logs once per ticker per
  run.

## Status of the existing rows

Treat every row in `cfd_funding_costs` as unverified.

- Each row is an estimate: quantity x price x annual rate / 360. No row was reported by a
  broker.
- Until the cash-only fix (pull request 282) the task charged every open position, including
  ordinary shares, which carry no overnight financing.
- A second run on the same day wrote the same day's rows again.
- No report, API route or P&L calculation reads the table.

## Read-only inventory

`apps/api/scripts/inventory_cfd_funding_costs.py` counts and summarises the rows. It runs
SELECT statements only, inside a read-only transaction on PostgreSQL, and prints JSON:

    cd apps/api
    python scripts/inventory_cfd_funding_costs.py > cfd-funding-inventory.json

It reports totals, first and last timestamps, rows per ticker, per day, per currency and per
annual rate, and up to 200 ticker-days that were recorded more than once. It needs the
database settings of the environment it is run in, so it is run by the owner, not by an
automated agent.

## Archival and clean-up (not done; each step needs separate approval)

1. Run the inventory and keep its output with the date and the environment.
2. Export the table to a file (for example `\copy cfd_funding_costs TO ... CSV HEADER` in
   psql) and record the file's SHA-256 next to the inventory. This reads only.
3. In a new, reversible Alembic migration, copy the rows to an archive table. This adds a
   table and changes no existing row. Its downgrade removes the archive table only.
4. Only then, in a further migration, empty or remove `cfd_funding_costs`. Its downgrade
   must recreate the table and restore the rows from the archive.
5. Removing the model, the CFD risk-profile columns and `check_cfd_limits` belongs to the
   same clean-up.

Steps 3 to 5 change the database. None of them has been done.
