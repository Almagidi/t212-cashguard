## 2026-05-08 - [Market Regime Evaluation Caching]
**Learning:** `MarketRegimeService.evaluate()` was being called on every WebSocket broadcast (every 2 seconds) and by multiple API routes. Each call triggered redundant network I/O to market data providers for benchmarks (SPY, QQQ, IWM), creating a significant bottleneck and increasing the risk of rate limiting.
**Action:** Implement a module-level TTL cache (60s) for expensive market regime computations to ensure the application remains responsive even under high WebSocket load.
