'use client'
import { useState } from 'react'
import {
  BookOpen, Star, Tag, Brain, Search, Filter,
  TrendingUp, TrendingDown, Edit2, Check, X, ChevronDown,
} from 'lucide-react'
import { useTradesList, useUpdateTradeJournal } from '@/hooks/use-api'
import {
  Card, CardContent, CardHeader, CardTitle,
  Button, Badge, Spinner, EmptyState, PageHeader,
} from '@/components/ui'
import { formatCurrency, formatPnL, pnlClass, cn } from '@/lib/utils'

// ── Types ─────────────────────────────────────────────────────────────────────

interface Trade {
  id: string
  ticker: string
  side: string
  quantity: number
  open_price: number
  close_price: number | null
  realized_pnl: number | null
  opened_at: string
  closed_at: string | null
  journal_notes: string | null
  journal_tags: string[] | null
  journal_emotion: string | null
  journal_rating: number | null
}

// ── Emotion config ────────────────────────────────────────────────────────────

const EMOTIONS = [
  { value: 'calm',      label: 'Calm',      emoji: '😌', cls: 'text-blue-400' },
  { value: 'confident', label: 'Confident', emoji: '💪', cls: 'text-emerald-400' },
  { value: 'anxious',   label: 'Anxious',   emoji: '😰', cls: 'text-amber-400' },
  { value: 'fearful',   label: 'Fearful',   emoji: '😨', cls: 'text-red-400' },
  { value: 'greedy',    label: 'Greedy',    emoji: '🤑', cls: 'text-yellow-400' },
  { value: 'neutral',   label: 'Neutral',   emoji: '😐', cls: 'text-muted-foreground' },
]

const SUGGESTED_TAGS = [
  'followed_plan', 'impulse', 'FOMO', 'revenge_trade',
  'good_entry', 'bad_exit', 'sized_correctly', 'oversized',
  'news_play', 'technical', 'held_winner', 'cut_early',
]

// ── Journal editor inline ─────────────────────────────────────────────────────

function JournalEditor({ trade, onClose }: { trade: Trade; onClose: () => void }) {
  const update = useUpdateTradeJournal()
  const [notes, setNotes]     = useState(trade.journal_notes ?? '')
  const [tags, setTags]       = useState<string[]>(trade.journal_tags ?? [])
  const [emotion, setEmotion] = useState(trade.journal_emotion ?? '')
  const [rating, setRating]   = useState(trade.journal_rating ?? 0)
  const [tagInput, setTagInput] = useState('')

  const addTag = (t: string) => {
    const clean = t.trim().toLowerCase().replace(/\s+/g, '_')
    if (clean && !tags.includes(clean)) setTags(prev => [...prev, clean])
    setTagInput('')
  }
  const removeTag = (t: string) => setTags(prev => prev.filter(x => x !== t))

  const handleSave = () => {
    update.mutate({ id: trade.id, payload: { notes, tags, emotion: emotion || undefined, rating: rating || undefined } })
    onClose()
  }

  return (
    <div className="mt-4 pt-4 border-t border-border/50 space-y-4">
      {/* Star rating */}
      <div>
        <p className="text-xs text-muted-foreground mb-2">Trade Quality (1–5)</p>
        <div className="flex gap-1">
          {[1,2,3,4,5].map(n => (
            <button key={n} onClick={() => setRating(r => r === n ? 0 : n)}>
              <Star
                className={cn(
                  'w-5 h-5 transition-colors',
                  n <= rating ? 'text-amber-400 fill-amber-400' : 'text-muted-foreground',
                )}
              />
            </button>
          ))}
        </div>
      </div>

      {/* Emotion */}
      <div>
        <p className="text-xs text-muted-foreground mb-2">Emotion at time of trade</p>
        <div className="flex flex-wrap gap-2">
          {EMOTIONS.map(e => (
            <button
              key={e.value}
              onClick={() => setEmotion(v => v === e.value ? '' : e.value)}
              className={cn(
                'px-2.5 py-1 rounded-full text-xs font-medium border transition-colors',
                emotion === e.value
                  ? 'bg-primary/15 border-primary/40 text-primary'
                  : 'border-border text-muted-foreground hover:border-muted-foreground',
              )}
            >
              {e.emoji} {e.label}
            </button>
          ))}
        </div>
      </div>

      {/* Tags */}
      <div>
        <p className="text-xs text-muted-foreground mb-2">Tags</p>
        <div className="flex flex-wrap gap-1.5 mb-2">
          {tags.map(t => (
            <span key={t} className="inline-flex items-center gap-1 px-2 py-0.5 bg-primary/10 text-primary rounded-full text-xs">
              {t}
              <button onClick={() => removeTag(t)}><X className="w-3 h-3" /></button>
            </span>
          ))}
        </div>
        {/* Suggested tags */}
        <div className="flex flex-wrap gap-1.5 mb-2">
          {SUGGESTED_TAGS.filter(t => !tags.includes(t)).slice(0, 8).map(t => (
            <button
              key={t}
              onClick={() => addTag(t)}
              className="px-2 py-0.5 border border-border rounded-full text-[10px] text-muted-foreground hover:border-primary hover:text-primary transition-colors"
            >
              + {t}
            </button>
          ))}
        </div>
        {/* Custom tag input */}
        <div className="flex gap-2">
          <input
            value={tagInput}
            onChange={e => setTagInput(e.target.value)}
            onKeyDown={e => e.key === 'Enter' && addTag(tagInput)}
            placeholder="Add custom tag…"
            className="flex-1 bg-background border border-border rounded-lg px-3 py-1.5 text-xs focus:outline-none focus:ring-1 focus:ring-primary"
          />
          <Button size="sm" variant="outline" onClick={() => addTag(tagInput)}>
            <Tag className="w-3.5 h-3.5" /> Add
          </Button>
        </div>
      </div>

      {/* Notes textarea */}
      <div>
        <p className="text-xs text-muted-foreground mb-2">Notes</p>
        <textarea
          value={notes}
          onChange={e => setNotes(e.target.value)}
          placeholder="What happened? Did you follow your plan? What would you do differently?"
          rows={4}
          className="w-full bg-background border border-border rounded-lg px-3 py-2.5 text-sm focus:outline-none focus:ring-1 focus:ring-primary resize-none placeholder:text-muted-foreground/50"
        />
      </div>

      {/* Actions */}
      <div className="flex gap-2 justify-end">
        <Button variant="outline" size="sm" onClick={onClose}>
          Cancel
        </Button>
        <Button size="sm" loading={update.isPending} onClick={handleSave}>
          <Check className="w-3.5 h-3.5" /> Save Journal
        </Button>
      </div>
    </div>
  )
}

// ── Trade row ─────────────────────────────────────────────────────────────────

function TradeRow({ trade }: { trade: Trade }) {
  const [editing, setEditing] = useState(false)
  const pnl = trade.realized_pnl ?? 0
  const journaled = !!(trade.journal_notes || trade.journal_tags?.length || trade.journal_rating)

  const emotion = EMOTIONS.find(e => e.value === trade.journal_emotion)

  return (
    <Card className={cn(
      'transition-all duration-200',
      journaled ? 'border-primary/20' : '',
    )}>
      <CardContent className="p-4">
        <div className="flex items-start gap-4">
          {/* Ticker icon */}
          <div className={cn(
            'w-10 h-10 rounded-xl flex items-center justify-center text-xs font-bold flex-shrink-0',
            trade.side === 'buy' ? 'bg-emerald-500/10 text-emerald-400' : 'bg-red-500/10 text-red-400',
          )}>
            {trade.ticker.slice(0, 3)}
          </div>

          {/* Trade info */}
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 flex-wrap">
              <span className="text-sm font-bold">{trade.ticker}</span>
              <span className={cn(
                'text-xs font-medium uppercase',
                trade.side === 'buy' ? 'text-emerald-400' : 'text-red-400',
              )}>
                {trade.side === 'buy'
                  ? <><TrendingUp className="w-3 h-3 inline mr-0.5" />LONG</>
                  : <><TrendingDown className="w-3 h-3 inline mr-0.5" />SHORT</>}
              </span>
              <span className="text-xs text-muted-foreground">
                {trade.quantity} shares @ {formatCurrency(trade.open_price)}
              </span>
              {trade.closed_at && (
                <span className="text-[10px] text-muted-foreground">
                  {new Date(trade.closed_at).toLocaleDateString()}
                </span>
              )}
            </div>

            {/* P&L + close price */}
            <div className="flex items-center gap-4 mt-1.5">
              <span className={cn('text-base font-bold', pnlClass(pnl))}>
                {formatPnL(pnl)}
              </span>
              {trade.close_price && (
                <span className="text-xs text-muted-foreground">
                  Exit: {formatCurrency(trade.close_price)}
                </span>
              )}
            </div>

            {/* Journal preview */}
            {journaled && !editing && (
              <div className="mt-2 space-y-1.5">
                {trade.journal_rating && (
                  <div className="flex items-center gap-1">
                    {Array.from({length: 5}, (_, i) => (
                      <Star
                        key={i}
                        className={cn(
                          'w-3 h-3',
                          i < trade.journal_rating! ? 'text-amber-400 fill-amber-400' : 'text-muted-foreground/30',
                        )}
                      />
                    ))}
                    {emotion && (
                      <span className={cn('text-xs ml-2', emotion.cls)}>
                        {emotion.emoji} {emotion.label}
                      </span>
                    )}
                  </div>
                )}
                {trade.journal_tags && trade.journal_tags.length > 0 && (
                  <div className="flex flex-wrap gap-1">
                    {trade.journal_tags.map(t => (
                      <span key={t} className="text-[10px] px-1.5 py-0.5 bg-primary/10 text-primary rounded-full">
                        {t}
                      </span>
                    ))}
                  </div>
                )}
                {trade.journal_notes && (
                  <p className="text-xs text-muted-foreground line-clamp-2 italic">
                    &ldquo;{trade.journal_notes}&rdquo;
                  </p>
                )}
              </div>
            )}

            {editing && <JournalEditor trade={trade} onClose={() => setEditing(false)} />}
          </div>

          {/* Edit button */}
          {!editing && (
            <Button
              variant={journaled ? 'outline' : 'ghost'}
              size="sm"
              onClick={() => setEditing(true)}
              className={cn('flex-shrink-0', !journaled && 'text-muted-foreground')}
            >
              <Edit2 className="w-3.5 h-3.5" />
              <span className="hidden sm:inline">{journaled ? 'Edit' : 'Add Journal'}</span>
            </Button>
          )}
        </div>
      </CardContent>
    </Card>
  )
}

// ── Page ──────────────────────────────────────────────────────────────────────

export default function JournalPage() {
  const [page, setPage]           = useState(1)
  const [hasNotes, setHasNotes]   = useState<boolean | undefined>(undefined)
  const [ticker, setTicker]       = useState('')

  const { data, isLoading } = useTradesList({
    page,
    page_size: 20,
    has_notes: hasNotes,
    ticker: ticker || undefined,
  })

  const trades: Trade[] = data?.items ?? []
  const total: number   = data?.total ?? 0
  const totalPages      = Math.ceil(total / 20)

  const journaledCount = trades.filter(t =>
    t.journal_notes || t.journal_tags?.length || t.journal_rating
  ).length

  return (
    <div className="max-w-4xl space-y-6">
      <PageHeader
        icon={<BookOpen className="h-5 w-5" />}
        label="Trade Journal"
        sub={`${total} trades · ${journaledCount} journaled this page`}
      />

      {/* Filters */}
      <div className="flex items-center gap-2 flex-wrap">
        <div className="relative flex-1 min-w-[160px]">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-muted-foreground/70 pointer-events-none" />
          <input
            value={ticker}
            onChange={e => { setTicker(e.target.value.toUpperCase()); setPage(1) }}
            placeholder="Filter by ticker…"
            className="w-full bg-background/60 border border-input rounded-lg pl-9 pr-3 h-9 text-sm placeholder:text-muted-foreground/70 focus:outline-none focus:border-primary/70 focus:ring-2 focus:ring-primary/20 transition-[border-color,box-shadow]"
          />
        </div>
        <Button
          variant={hasNotes === undefined ? 'default' : 'outline'}
          size="sm"
          onClick={() => { setHasNotes(undefined); setPage(1) }}
        >
          All
        </Button>
        <Button
          variant={hasNotes === true ? 'default' : 'outline'}
          size="sm"
          onClick={() => { setHasNotes(true); setPage(1) }}
        >
          Journaled
        </Button>
        <Button
          variant={hasNotes === false ? 'default' : 'outline'}
          size="sm"
          onClick={() => { setHasNotes(false); setPage(1) }}
        >
          Unreviewed
        </Button>
      </div>

      {/* Trade list */}
      {isLoading ? (
        <div className="flex items-center gap-2 text-muted-foreground text-sm">
          <Spinner className="w-4 h-4" /> Loading trades…
        </div>
      ) : trades.length === 0 ? (
        <Card>
          <CardContent>
            <EmptyState
              icon={<BookOpen className="w-10 h-10" />}
              title="No trades found"
              description="Closed trades will appear here. Use filters above to find specific ones."
            />
          </CardContent>
        </Card>
      ) : (
        <div className="space-y-3">
          {trades.map(trade => <TradeRow key={trade.id} trade={trade} />)}
        </div>
      )}

      {/* Pagination */}
      {totalPages > 1 && (
        <div className="flex items-center justify-between text-xs text-muted-foreground">
          <span>Page {page} of {totalPages} · {total} total</span>
          <div className="flex gap-2">
            <Button
              variant="outline"
              size="sm"
              disabled={page <= 1}
              onClick={() => setPage(p => p - 1)}
            >
              Previous
            </Button>
            <Button
              variant="outline"
              size="sm"
              disabled={page >= totalPages}
              onClick={() => setPage(p => p + 1)}
            >
              Next
            </Button>
          </div>
        </div>
      )}
    </div>
  )
}
