'use client'
/**
 * Candlestick chart with signal overlays.
 * Uses Recharts ComposedChart — renders OHLC bars as Rectangles + Lines
 * and overlays buy/sell signals as custom scatter dots.
 */
import {
  ResponsiveContainer, ComposedChart, Bar, Line, XAxis, YAxis,
  CartesianGrid, Tooltip, Scatter, ReferenceLine,
} from 'recharts'
import { ArrowUpRight, ArrowDownRight } from 'lucide-react'
import { cn, formatCurrency } from '@/lib/utils'

function deterministicUnit(seed: string, index: number) {
  let hash = 0
  for (let i = 0; i < seed.length; i += 1) {
    hash = (hash * 31 + seed.charCodeAt(i)) >>> 0
  }
  return (Math.sin(hash + index * 101) + 1) / 2
}

const chartColors = {
  positive: 'hsl(var(--chart-positive))',
  negative: 'hsl(var(--chart-negative))',
  neutral: 'hsl(var(--chart-neutral) / 0.55)',
  grid: 'hsl(var(--chart-grid) / 0.16)',
}

// ── Types ─────────────────────────────────────────────────────────────────────

export interface OHLCBar {
  date: string       // display label (e.g. "09:30")
  open: number
  high: number
  low: number
  close: number
  volume?: number
}

export interface SignalOverlay {
  date: string      // must match an OHLCBar date
  side: 'buy' | 'sell'
  price: number
  label?: string
}

interface CandlestickChartProps {
  data: OHLCBar[]
  signals?: SignalOverlay[]
  height?: number
  showVolume?: boolean
  className?: string
}

// ── Custom candlestick bar shape ──────────────────────────────────────────────

function CandleShape(props: any) {
  const { x, y, width, height, payload } = props
  if (!payload) return null

  const { open, close, high, low } = payload
  const isUp = close >= open

  const fill   = isUp ? chartColors.positive : chartColors.negative
  const stroke = isUp ? chartColors.positive : chartColors.negative

  // We compute pixel positions manually. recharts passes y = top of bar, height = bar height
  // We need to map price range → pixel coords
  // The chart domain handles this via the axis — we just draw a simple shape
  // using the Bar's provided x/y/width/height props which correspond to the body

  const bodyX      = x + width * 0.2
  const bodyWidth  = width * 0.6
  const centerX    = x + width / 2

  // y and height correspond to the body (min(open,close) → max(open,close))
  const bodyTop    = y
  const bodyHeight = Math.max(Math.abs(height), 1)

  return (
    <g>
      {/* High-low wick */}
      <line
        x1={centerX} y1={bodyTop - 8}
        x2={centerX} y2={bodyTop}
        stroke={stroke} strokeWidth={1}
      />
      <line
        x1={centerX} y1={bodyTop + bodyHeight}
        x2={centerX} y2={bodyTop + bodyHeight + 8}
        stroke={stroke} strokeWidth={1}
      />
      {/* OHLC body */}
      <rect
        x={bodyX}
        y={bodyTop}
        width={bodyWidth}
        height={bodyHeight}
        fill={fill}
        opacity={0.9}
        rx={1}
      />
    </g>
  )
}

// ── Signal dot ────────────────────────────────────────────────────────────────

function SignalDot(props: any) {
  const { cx, cy, payload } = props
  if (!payload) return null
  const isBuy = payload.side === 'buy'
  const color = isBuy ? chartColors.positive : chartColors.negative
  const arrow = isBuy ? '▲' : '▼'
  return (
    <g>
      <circle cx={cx} cy={cy} r={8} fill={color} fillOpacity={0.25} stroke={color} strokeWidth={1.5} />
      <text x={cx} y={cy + 4} textAnchor="middle" fill={color} fontSize={9} fontWeight="bold">
        {arrow}
      </text>
    </g>
  )
}

// ── Tooltip ───────────────────────────────────────────────────────────────────

function OHLCTooltip({ active, payload, label }: any) {
  if (!active || !payload?.length) return null
  const d = payload[0]?.payload as OHLCBar | undefined
  if (!d) return null
  const isUp = d.close >= d.open
  return (
    <div className="bg-card/95 backdrop-blur-sm border border-border rounded-lg px-3 py-2.5 shadow-xl text-xs min-w-[140px]">
      <p className="text-muted-foreground font-medium mb-2">{label}</p>
      <div className="grid grid-cols-2 gap-x-3 gap-y-0.5">
        <span className="text-muted-foreground">Open</span>
        <span className="text-right tabular-nums">{formatCurrency(d.open)}</span>
        <span className="text-muted-foreground">High</span>
        <span className="text-right tabular-nums text-emerald-400">{formatCurrency(d.high)}</span>
        <span className="text-muted-foreground">Low</span>
        <span className="text-right tabular-nums text-red-400">{formatCurrency(d.low)}</span>
        <span className="text-muted-foreground">Close</span>
        <span className={cn('text-right tabular-nums font-bold', isUp ? 'text-emerald-400' : 'text-red-400')}>
          {formatCurrency(d.close)}
        </span>
      </div>
      {d.volume !== undefined && (
        <p className="text-muted-foreground mt-1.5 text-[10px]">Vol: {d.volume.toLocaleString()}</p>
      )}
    </div>
  )
}

// ── Main chart ────────────────────────────────────────────────────────────────

export function CandlestickChart({
  data,
  signals = [],
  height = 280,
  showVolume = false,
  className,
}: CandlestickChartProps) {
  if (!data || data.length === 0) {
    return (
      <div
        className={cn('flex items-center justify-center text-muted-foreground text-xs', className)}
        style={{ height }}
      >
        No OHLC data available
      </div>
    )
  }

  // Merge signals into data by date key for Scatter plot
  const signalMap = Object.fromEntries(signals.map(s => [s.date, s]))

  // Price domain with padding
  const allPrices = data.flatMap(d => [d.high, d.low])
  const minPrice  = Math.min(...allPrices)
  const maxPrice  = Math.max(...allPrices)
  const padding   = (maxPrice - minPrice) * 0.05
  const domain    = [minPrice - padding, maxPrice + padding]

  // Signal scatter data (price on y, date on x)
  const scatterData = signals.map(s => ({
    ...s,
    x: s.date,
    y: s.price,
  }))

  return (
    <div className={className} style={{ height }}>
      <ResponsiveContainer width="100%" height="100%">
        <ComposedChart data={data} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke={chartColors.grid} vertical={false} />
          <XAxis
            dataKey="date"
            tick={{ fill: 'hsl(var(--muted-foreground))', fontSize: 10 }}
            tickLine={false}
            axisLine={false}
            interval="preserveStartEnd"
          />
          <YAxis
            domain={domain}
            tick={{ fill: 'hsl(var(--muted-foreground))', fontSize: 10 }}
            tickLine={false}
            axisLine={false}
            tickFormatter={(v) => `$${v.toFixed(0)}`}
            width={50}
          />
          <Tooltip content={<OHLCTooltip />} />

          {/* Candle body rendered via Bar with custom shape */}
          <Bar
            dataKey="close"
            shape={<CandleShape />}
            isAnimationActive={false}
          />

          {/* VWAP line (if close data can serve as proxy) */}
          <Line
            type="monotone"
            dataKey="close"
            stroke={chartColors.neutral}
            strokeWidth={1}
            dot={false}
            strokeDasharray="4 4"
            isAnimationActive={false}
          />

          {/* Signal markers */}
          {scatterData.length > 0 && (
            <Scatter
              data={scatterData}
              shape={<SignalDot />}
              isAnimationActive={false}
            />
          )}
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  )
}

// ── Demo component with mock data ─────────────────────────────────────────────

export function CandlestickDemo({ ticker = 'AAPL', className }: { ticker?: string; className?: string }) {
  // Generate plausible mock OHLC data
  const data: OHLCBar[] = []
  let price = 178.50
  const now = new Date()
  now.setHours(9, 30, 0, 0)

  for (let i = 0; i < 30; i++) {
    const t = new Date(now.getTime() + i * 15 * 60000)
    const label = t.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', hour12: false })
    const change = (deterministicUnit(ticker, i * 4) - 0.48) * 1.2
    const open   = price
    const close  = Math.max(open + change, 1)
    const high   = Math.max(open, close) + deterministicUnit(ticker, i * 4 + 1) * 0.5
    const low    = Math.min(open, close) - deterministicUnit(ticker, i * 4 + 2) * 0.5
    const volume = Math.floor(deterministicUnit(ticker, i * 4 + 3) * 50000 + 10000)
    data.push({ date: label, open: +open.toFixed(2), high: +high.toFixed(2), low: +low.toFixed(2), close: +close.toFixed(2), volume })
    price = close
  }

  const signals: SignalOverlay[] = [
    { date: data[5].date,  side: 'buy',  price: data[5].close,  label: 'ORB Breakout' },
    { date: data[18].date, side: 'sell', price: data[18].close, label: 'Take Profit' },
  ]

  const lastClose = data[data.length - 1].close
  const firstClose = data[0].open
  const pct = ((lastClose - firstClose) / firstClose * 100).toFixed(2)
  const isUp = lastClose >= firstClose

  return (
    <div className={cn('space-y-2', className)}>
      <div className="flex items-center gap-3">
        <span className="text-sm font-bold">{ticker}</span>
        <span className="text-sm font-bold">{formatCurrency(lastClose)}</span>
        <span className={cn('text-xs font-medium flex items-center gap-0.5', isUp ? 'text-emerald-400' : 'text-red-400')}>
          {isUp ? <ArrowUpRight className="w-3 h-3" /> : <ArrowDownRight className="w-3 h-3" />}
          {isUp ? '+' : ''}{pct}%
        </span>
        <span className="text-[10px] text-muted-foreground ml-auto">15-min · Demo</span>
      </div>
      <CandlestickChart data={data} signals={signals} height={220} />
      {/* Signal legend */}
      <div className="flex items-center gap-4 text-[10px] text-muted-foreground">
        <span className="flex items-center gap-1">
          <span className="w-2 h-2 rounded-full inline-block" style={{ backgroundColor: chartColors.positive }} /> Buy signal
        </span>
        <span className="flex items-center gap-1">
          <span className="w-2 h-2 rounded-full inline-block" style={{ backgroundColor: chartColors.negative }} /> Sell signal
        </span>
      </div>
    </div>
  )
}
