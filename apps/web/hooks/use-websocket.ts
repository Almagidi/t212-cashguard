/**
 * useWebSocket — live feed hook with auto-reconnect.
 *
 * Connects to /v1/ws/live (no query params), then sends
 * {"type":"auth","token":"<jwt>"} as the very first frame so the JWT never
 * appears in Nginx access logs or browser history.
 * Reconnects automatically with exponential back-off on unexpected disconnects.
 * All incoming frames are validated against WsSnapshotSchema (Zod) so malformed
 * payloads surface as console errors and trigger a reconnect rather than silently
 * corrupting UI state.
 */
"use client";

import { useEffect, useRef, useState, useCallback } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { z } from "zod";

// ── Zod schemas (source of truth for runtime validation) ──────────────────────

const WsAccountSnapshotSchema = z.object({
  total_value: z.number(),
  free_cash: z.number(),
  invested: z.number(),
  currency: z.string(),
  unrealized_pnl: z.number(),
});

const WsPositionSchema = z.object({
  ticker: z.string(),
  quantity: z.number(),
  average_price: z.number(),
  current_price: z.number(),
  unrealized_pnl: z.number(),
  market_value: z.number(),
});

const WsSignalSchema = z.object({
  id: z.string(),
  ticker: z.string(),
  side: z.string(),
  signal_type: z.string(),
  status: z.string(),
  confidence: z.number(),
  generated_at: z.string(),
});

const WsOrderSchema = z.object({
  id: z.string(),
  ticker: z.string(),
  side: z.string(),
  order_type: z.string(),
  quantity: z.number(),
  status: z.string(),
  created_at: z.string(),
});

const WsRegimeSchema = z.object({
  regime: z.string(),
  label: z.string(),
  color: z.string(),
  adx: z.number(),
  vol_percentile: z.number(),
  confidence: z.number(),
  breadth_pct: z.number().optional(),
  primary_trend: z.string().optional(),
  detail: z.string().optional(),
  active_strategies: z.array(z.string()),
  suppressed_strategies: z.array(z.string()),
});

const WsSnapshotSchema = z.object({
  type: z.string(),
  ts: z.string(),
  account: WsAccountSnapshotSchema,
  positions: z.array(WsPositionSchema),
  signals: z.array(WsSignalSchema),
  orders: z.array(WsOrderSchema),
  system: z.object({
    auto_trading_enabled: z.boolean(),
    kill_switch_active: z.boolean(),
    max_daily_loss_pct: z.number(),
  }),
  regime: WsRegimeSchema,
});

// ── Inferred TypeScript types from schemas ────────────────────────────────────

export type WsAccountSnapshot = z.infer<typeof WsAccountSnapshotSchema>;
export type WsPosition        = z.infer<typeof WsPositionSchema>;
export type WsSignal          = z.infer<typeof WsSignalSchema>;
export type WsOrder           = z.infer<typeof WsOrderSchema>;
export type WsRegime          = z.infer<typeof WsRegimeSchema>;
export type WsSnapshot        = z.infer<typeof WsSnapshotSchema>;

export type WsStatus = "connecting" | "connected" | "reconnecting" | "disconnected";

// ── Constants ─────────────────────────────────────────────────────────────────

const BASE_DELAY_MS        = 1_000;
const MAX_DELAY_MS         = 30_000;
const MAX_RETRIES          = 10;
// Server sends a snapshot every 2s and a ping every 30s.  If nothing arrives
// for 10s the connection is silently dead — force a reconnect.
const HEARTBEAT_TIMEOUT_MS = 10_000;

// ── Hook ──────────────────────────────────────────────────────────────────────

interface UseWebSocketOptions {
  token: string | null;
  /** ws:// or wss:// base, derived from window.location if omitted */
  wsBaseUrl?: string;
  /** Whether to auto-connect (default true) */
  enabled?: boolean;
}

export function useWebSocket(options: UseWebSocketOptions) {
  const { token, enabled = true } = options;

  const [status, setStatus]   = useState<WsStatus>("disconnected");
  const [snapshot, setSnapshot] = useState<WsSnapshot | null>(null);
  const [lastPing, setLastPing] = useState<Date | null>(null);

  const wsRef          = useRef<WebSocket | null>(null);
  const retries        = useRef(0);
  const timerRef       = useRef<ReturnType<typeof setTimeout> | null>(null);
  const heartbeatRef   = useRef<ReturnType<typeof setInterval> | null>(null);
  const lastPingRef    = useRef<Date | null>(null);
  const unmountedRef   = useRef(false);
  const qc             = useQueryClient();

  const getWsUrl = useCallback(() => {
    if (options.wsBaseUrl) return options.wsBaseUrl;
    if (typeof window === "undefined") return null;
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const publicApiUrl = process.env.NEXT_PUBLIC_API_URL?.replace(/\/$/, "");
    if (publicApiUrl?.startsWith("http")) {
      return `${proto}//${new URL(publicApiUrl).host}/v1/ws/live`;
    }
    return `${proto}//${window.location.host}${publicApiUrl || ""}/v1/ws/live`;
  }, [options.wsBaseUrl]);

  const connect = useCallback(() => {
    if (!token || unmountedRef.current) return;

    const url = getWsUrl();
    if (!url) return;

    setStatus(retries.current > 0 ? "reconnecting" : "connecting");

    const ws = new WebSocket(url);
    wsRef.current = ws;

    ws.onopen = () => {
      if (unmountedRef.current) { ws.close(); return; }
      // Send auth as the very first frame — token never touches the URL.
      ws.send(JSON.stringify({ type: "auth", token }));
      retries.current = 0;
      lastPingRef.current = new Date();
      setStatus("connected");

      // Client-side heartbeat monitor: if the server goes silent for
      // HEARTBEAT_TIMEOUT_MS we treat the connection as dead and force a
      // reconnect rather than waiting for a TCP timeout that can take minutes.
      heartbeatRef.current = setInterval(() => {
        const last = lastPingRef.current;
        if (last && Date.now() - last.getTime() > HEARTBEAT_TIMEOUT_MS) {
          console.warn("[WebSocket] Heartbeat timeout — forcing reconnect.");
          ws.close(4002, "heartbeat_timeout");
        }
      }, HEARTBEAT_TIMEOUT_MS);
    };

    ws.onmessage = (event: MessageEvent) => {
      if (unmountedRef.current) return;

      // Update the liveness timestamp on every frame (snapshots AND pings).
      const now = new Date();
      lastPingRef.current = now;
      setLastPing(now);

      try {
        const raw = JSON.parse(event.data);

        // Respond to server heartbeat pings immediately so the server's
        // receive-loop timeout doesn't evict this connection.
        if (raw?.type === "ping") {
          ws.send(JSON.stringify({ type: "pong" }));
          return;
        }

        const result = WsSnapshotSchema.safeParse(raw);

        if (!result.success) {
          // Schema validation failed — log details and force a reconnect so we
          // don't silently display stale or partially-corrupted state.
          console.error(
            "[WebSocket] Snapshot schema validation failed — reconnecting.",
            result.error.flatten(),
          );
          ws.close(4000, "schema_mismatch");
          return;
        }

        setSnapshot(result.data);

        // Invalidate react-query caches so any page that uses them stays fresh
        qc.invalidateQueries({ queryKey: ["account"] });
        qc.invalidateQueries({ queryKey: ["positions"] });
      } catch (err) {
        console.error("[WebSocket] Failed to parse frame:", err);
      }
    };

    ws.onerror = () => {
      // onerror is always followed by onclose — nothing to do here
    };

    ws.onclose = (event: CloseEvent) => {
      if (unmountedRef.current) return;
      wsRef.current = null;

      if (heartbeatRef.current) {
        clearInterval(heartbeatRef.current);
        heartbeatRef.current = null;
      }

      if (event.code === 4001) {
        // Auth failure — don't retry
        setStatus("disconnected");
        return;
      }

      if (retries.current >= MAX_RETRIES) {
        setStatus("disconnected");
        return;
      }

      // Exponential back-off with jitter
      const delay = Math.min(
        BASE_DELAY_MS * 2 ** retries.current + Math.random() * 500,
        MAX_DELAY_MS,
      );
      retries.current += 1;
      setStatus("reconnecting");
      timerRef.current = setTimeout(connect, delay);
    };
  }, [token, getWsUrl, qc]);

  const disconnect = useCallback(() => {
    if (timerRef.current) clearTimeout(timerRef.current);
    if (heartbeatRef.current) {
      clearInterval(heartbeatRef.current);
      heartbeatRef.current = null;
    }
    wsRef.current?.close();
    wsRef.current = null;
    setStatus("disconnected");
  }, []);

  useEffect(() => {
    unmountedRef.current = false;
    if (enabled && token) {
      connect();
    }
    return () => {
      unmountedRef.current = true;
      disconnect();
    };
  }, [enabled, token]); // eslint-disable-line react-hooks/exhaustive-deps

  return { snapshot, status, lastPing, disconnect, reconnect: connect };
}
