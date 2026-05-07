# T-OPS-017 — Manual Paper Execution QA

## Summary

Manual paper execution was tested end-to-end in local manual QA mode.

Environment:

- Frontend: `http://localhost:3002`
- Backend: `http://127.0.0.1:8002`
- App mode: `mock`
- Login: `admin@localhost`
- Execution type: paper/mock/local only

## Safety checks

The first paper order attempt was made while the global kill switch was active.

Result:

- `POST /v1/orders/paper`
- Response: `422 Unprocessable Entity`
- Detail: `Kill switch is active. Disable it from Emergency Controls.`

This confirms paper execution does not bypass the kill switch.

The manual QA SQLite database kill switch was then disabled locally for the successful fill test.

No live unlock was enabled. No broker credentials were used. No Trading212 or Kraken broker order was sent.

## Successful paper execution

Paper order request:

```json
{
  "ticker": "PAPERXYZ",
  "side": "buy",
  "quantity": "2",
  "estimated_price": "25.50",
  "source": "manual_qa",
  "strategy": "paper-test",
  "venue": "paper",
  "paper_only": true
}
