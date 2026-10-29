# Backtest configuration identity

The intraday website catalog (`GET /v1/backtest/strategies`) returns each preset's
`preset_key` and complete effective `strategy_params`: the unchanged strategy class
defaults merged with the unchanged named preset. The website sends both, together
with matching `strategy_type`, to `POST /v1/backtest/run`.

For a named preset, omit `strategy_params` to deliberately request the server's current
configuration, or supply the complete catalog snapshot. Unknown IDs, a mismatched
strategy type, incomplete snapshots (including explicit `{}`), and changed values
return HTTP 422 before creating a job or requesting data. A stale catalog must be
reloaded; the server never silently replaces conflicting user parameters.

Existing API requests without `preset_key` retain their class-default or explicit
custom-parameter merge. They do not acquire a preset identity, even if their values
happen to match a preset. Custom parameters must be supported keys with the expected
JSON scalar family: booleans are not numbers, integer counts are integers, numeric
values are finite, and UTC clock strings use `HH:MM`. Positive counts/divisors and
non-negative minute buffers are required. Formerly ignored unknown keys/fields and
malformed values now return 422. This validation does not choose new filter, risk or
strategy policies. The website does not provide a custom-parameter editor.
ORB's effective opening range must contain at least one configured candle. The run
route returns validation locations/messages/types without echoing raw input, including
for nonfinite numeric input; other routes retain their existing validation responses.

New jobs retain `configuration` with `source` (`preset`, `custom`, or
`class_defaults`), strategy type, nullable preset key, label, and effective parameters.
The submitted snapshot is copied into the queued task and retained on success and
data-error paths. The UI distinguishes that captured run from the currently selected
form preset. Legacy records without metadata have no reconstructed preset identity.
The job store remains process-local; this change does not add durable job storage.

Walk-forward remains the existing **separate registry parameter-grid search**. Its
`walk_forward_configuration` identifies that grid; it does not validate the exact
primary preset. This change neither modifies that algorithm nor runs an economic
study. Preset binding does not establish runtime/backtest parity, GBP accounting,
broker execution fidelity, profitability or live readiness.

Primary implementation contracts reviewed:

- [Pydantic model validators](https://docs.pydantic.dev/latest/concepts/validators/)
  and [extra-field configuration](https://docs.pydantic.dev/latest/api/config/).
- [React effect cleanup and stale responses](https://react.dev/reference/react/useEffect#fetching-data-with-effects).
- [React ref access in handlers](https://react.dev/reference/eslint-plugin-react-hooks/lints/refs).
- [FastAPI custom route handling](https://fastapi.tiangolo.com/how-to/custom-request-and-route/).

Preset values, runtime strategies, risk controls, execution, data providers and
trading enablement are unchanged. The separately accepted full-target backtest fix
is not included in this package.
