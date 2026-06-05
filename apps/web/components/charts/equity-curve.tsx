'use client'
import { useId } from 'react'
import {
  ResponsiveContainer, AreaChart, Area, XAxis, YAxis,
  CartesianGrid, Tooltip, ReferenceLine,
} from 'recharts'
import { cn } from '@/lib/utils'

export interface EquityPoint {
  date: string
  pnl: number        // cumulative P&L
  daily?: number     // daily P&L bar
}

interface EquityCurveProps {
  data: EquityPoint[]
  className?: string
  height?: number
  showGrid?: boolean
}

function formatValue(v: number) {
  return v >= 0 ? `+$${v.toFixed(2)}` : `-$${Math.abs(v).toFixed(2)}`
}

const chartColors = {
  positive: 'hsl(var(--chart-positive))',
  negative: 'hsl(var(--chart-negative))',
  grid: 'hsl(var(--chart-grid) / 0.18)',
  zero: 'hsl(var(--chart-zero) / 0.35)',
}

// Custom tooltip
function CustomTooltip({ active, payload, label }: any) {
  if (!active || !payload?.length) return null
  const val = payload[0]?.value as number
  return (
    <div className="bg-card/95 backdrop-blur-sm border border-border rounded-lg px-3 py-2 shadow-xl text-xs">
      <p className="text-muted-foreground mb-1">{label}</p>
      <p className={cn('font-semibold', val >= 0 ? 'text-emerald-400' : 'text-red-400')}>
        {formatValue(val)}
      </p>
    </div>
  )
}

export function EquityCurve({ data, className, height = 180, showGrid = false }: EquityCurveProps) {
  if (!data || data.length === 0) {
    return (
      <div
        className={cn('flex items-center justify-center text-muted-foreground text-xs', className)}
        style={{ height }}
      >
        No trade history yet
      </div>
    )
  }

  const lastVal   = data[data.length - 1]?.pnl ?? 0
  const isPositive = lastVal >= 0
  const strokeColor = isPositive ? chartColors.positive : chartColors.negative
  const fillId      = isPositive ? 'green-fill' : 'red-fill'

  return (
    <div className={className} style={{ height }}>
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={data} margin={{ top: 4, right: 4, left: 0, bottom: 0 }}>
          <defs>
            <linearGradient id="green-fill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="5%"  stopColor={chartColors.positive} stopOpacity={0.22} />
              <stop offset="95%" stopColor={chartColors.positive} stopOpacity={0} />
            </linearGradient>
            <linearGradient id="red-fill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="5%"  stopColor={chartColors.negative} stopOpacity={0.22} />
              <stop offset="95%" stopColor={chartColors.negative} stopOpacity={0} />
            </linearGradient>
          </defs>

          {showGrid && (
            <CartesianGrid strokeDasharray="3 3" stroke={chartColors.grid} vertical={false} />
          )}

          <XAxis
            dataKey="date"
            tick={{ fill: 'hsl(var(--muted-foreground))', fontSize: 10 }}
            tickLine={false}
            axisLine={false}
            interval="preserveStartEnd"
          />
          <YAxis
            tick={{ fill: 'hsl(var(--muted-foreground))', fontSize: 10 }}
            tickLine={false}
            axisLine={false}
            tickFormatter={(v) => `$${v}`}
            width={45}
          />
          <Tooltip content={<CustomTooltip />} />
          <ReferenceLine y={0} stroke={chartColors.zero} strokeDasharray="3 3" />

          <Area
            type="monotone"
            dataKey="pnl"
            stroke={strokeColor}
            strokeWidth={2}
            fill={`url(#${fillId})`}
            dot={false}
            activeDot={{ r: 4, fill: strokeColor, strokeWidth: 0 }}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  )
}

// ── Sparkline (mini, no axes) ─────────────────────────────────────────────────

interface SparklineProps {
  data: number[]
  width?: number
  height?: number
  positive?: boolean
  className?: string
}

export function Sparkline({ data, width = 80, height = 32, positive, className }: SparklineProps) {
  const gradientId = useId().replace(/:/g, '')
  const pts = data.map((v, i) => ({ i, v }))
  const isPos = positive ?? (data[data.length - 1] ?? 0) >= (data[0] ?? 0)
  const color = isPos ? chartColors.positive : chartColors.negative
  const fillId = `spark-${isPos ? 'g' : 'r'}-${gradientId}`

  if (!data.length) return null

  return (
    <div className={className} style={{ width, height }}>
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={pts} margin={{ top: 2, right: 2, left: 2, bottom: 2 }}>
          <defs>
            <linearGradient id={fillId} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%"   stopColor={color} stopOpacity={0.3} />
              <stop offset="100%" stopColor={color} stopOpacity={0}   />
            </linearGradient>
          </defs>
          <Area
            type="monotone"
            dataKey="v"
            stroke={color}
            strokeWidth={1.5}
            fill={`url(#${fillId})`}
            dot={false}
            isAnimationActive={false}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  )
}
