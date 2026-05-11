# Demo RC Checklist

Demo mode is future guarded work. Do not enable live trading for Demo RC.

- Configure `APP_MODE=demo`.
- Keep `LIVE_TRADING_ENABLED=false`.
- Configure `T212_DEMO_API_KEY` and `T212_DEMO_API_SECRET`.
- Leave `T212_LIVE_API_KEY` and `T212_LIVE_API_SECRET` unset.
- Verify the broker base URL is `https://demo.trading212.com`.
- Add tests proving demo mode cannot reach `https://live.trading212.com`.
- Label every demo order as demo execution in UI, audit logs, and order records.
- Confirm kill switch blocks demo order submission immediately before broker submit.
- Confirm disabling kill switch does not enable auto-trading.
- Confirm broker errors are safe and do not expose credentials.
