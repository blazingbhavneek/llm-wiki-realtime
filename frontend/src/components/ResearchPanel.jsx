import { useState } from 'react'
import { Check, CircleDashed, Loader, TriangleAlert } from 'lucide-react'
import { isRunning } from '../lib/research'

// Deliberately no markdown or diagram rendering. This panel exists to show what
// is being searched right now and what came back -- not to render documents.
// Every renderer added here is bundle weight on the path to first paint.

const STATUS_ICON = {
  pending: <CircleDashed size={15} />,
  running: <Loader size={15} className="spin" />,
  done: <Check size={15} />,
  partial: <TriangleAlert size={15} />,
}

const STATUS_LABEL = {
  pending: '待機',
  running: '検索中',
  done: '完了',
  partial: '一部のみ',
}

const LEVEL_ICON_COLOR = {
  pending: 'text-line',
  running: 'text-warn',
  done: 'text-ok',
  partial: 'text-warn',
}

function QueryRow({ query, running }) {
  const text = typeof query === 'string' ? query : query.query
  const detail = typeof query === 'string' ? null : query

  return (
    <li className={`flex items-baseline gap-2 py-[3px] text-[12.5px] leading-[1.6] ${detail?.enough === false ? 'text-warn' : 'text-muted'}`}>
      <span className={`h-1 w-1 flex-none -translate-y-0.5 rounded-full ${running ? 'animate-query-ping bg-warn' : 'bg-faint'}`} />
      <span className="min-w-0 flex-1">{text}</span>
      {detail?.error
        ? <span className="flex-none whitespace-nowrap text-[11px] text-danger">失敗</span>
        : detail?.search_result_count != null
          ? <span className="flex-none whitespace-nowrap text-[11px] text-faint">{detail.search_result_count}件</span>
          : running ? <span className="flex-none whitespace-nowrap text-[11px] text-faint">検索中</span> : null}
    </li>
  )
}

function LevelNode({ level, expanded, onToggle }) {
  const result = level.result
  const running = level.status === 'running'
  const queries = result?.queries ?? level.queries ?? []

  return (
    <li className="border-b border-line-soft last:border-b-0">
      <button
        type="button"
        className="grid w-full grid-cols-[20px_1fr_auto] items-start gap-[11px] border-0 bg-transparent px-0.5 py-[15px] text-left"
        onClick={onToggle}
        aria-expanded={expanded}
      >
        <span className={`grid h-[21px] place-items-center ${LEVEL_ICON_COLOR[level.status]}`}>{STATUS_ICON[level.status]}</span>
        <span className={`text-sm leading-[1.5] ${level.status === 'pending' ? 'font-normal text-faint' : 'font-medium text-ink'}`}>
          {level.kind === 'recovery' && (
            <span className="mr-[7px] inline-block rounded-[5px] bg-warn/20 px-[7px] py-px align-[1.5px] text-[10.5px] font-bold tracking-[0.04em] text-warn">
              追加調査
            </span>
          )}
          {level.objective}
        </span>
        <span className="whitespace-nowrap pt-0.5 text-[11.5px] tabular-nums text-faint">
          {result?.latency_ms != null
            ? `${(result.latency_ms / 1000).toFixed(1)}s`
            : STATUS_LABEL[level.status]}
        </span>
      </button>

      {expanded && (
        <div className="animate-rise px-0.5 pb-[18px] pl-[31px]">
          {queries.length > 0 && (
            <ul className="m-0 mb-3.5 list-none p-0">
              {queries.map((query, i) => (
                <QueryRow key={i} query={query} running={running} />
              ))}
            </ul>
          )}

          {result?.text && (
            <p className="m-0 rounded-xl border border-line-soft bg-line-soft/60 px-[15px] py-[13px] text-[13.5px] leading-[1.85] whitespace-pre-wrap">
              {result.text}
            </p>
          )}

          {result?.facts?.length > 0 && (
            <ul className="m-0 mt-3 list-none p-0">
              {result.facts.map((fact, i) => (
                <li key={i} className="flex items-baseline gap-2.5 border-t border-line-soft py-[7px] text-[12.5px] leading-[1.7] text-muted">
                  <span className="flex-1">{fact.text}</span>
                  <span className="flex flex-none flex-wrap gap-1">
                    {fact.node_ids.map(id => (
                      <code key={id} className="rounded-[5px] bg-accent-soft px-1.5 py-px text-[10.5px] text-accent">{id}</code>
                    ))}
                  </span>
                </li>
              ))}
            </ul>
          )}

          {result && !result.complete && (
            <p className="mt-3 mb-0 text-xs text-warn">証拠が不十分です。追加の調査が入る場合があります。</p>
          )}
        </div>
      )}
    </li>
  )
}

export default function ResearchPanel({ research }) {
  // A level is open by default once it is running or its answer lands; this map
  // only records levels the user explicitly toggled, so no effect is needed.
  const [overrides, setOverrides] = useState(() => new Map())

  const toggle = (id, isOpen) => setOverrides(current => {
    const next = new Map(current)
    next.set(id, !isOpen)
    return next
  })

  if (research.status === 'idle') {
    return (
      <div className="max-w-[380px] px-[26px] py-11">
        <p className="m-0 mb-2.5 text-sm">調査はまだ実行されていません。</p>
        <p className="m-0 text-[13px] leading-[1.8] text-faint">
          質問すると、まず調査の計画がここに出ます。各段階の結果は完了した順に追加され、
          読み上げと並行して次の段階が進行します。
        </p>
      </div>
    )
  }

  const total = research.levels.length
  const finished = research.levels.filter(level => level.result).length

  return (
    <div className="px-[22px] pt-5 pb-12">
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="m-0 mb-[5px] text-[15px] leading-[1.55] font-semibold">{research.question}</p>
          <p className="m-0 text-[12.5px] tabular-nums text-faint">
            {research.status === 'planning' && '調査の計画を作成中'}
            {research.status === 'running' && `段階 ${finished + 1} / ${total} を検索中`}
            {research.status === 'complete' && `完了 · ${total} 段階 · ${(research.latencyMs / 1000).toFixed(1)}s`}
            {research.status === 'partial' && '一部の情報が見つかりませんでした'}
            {research.status === 'error' && `失敗: ${research.error}`}
            {research.status === 'cancelled' && '中止されました'}
          </p>
        </div>
        {research.planningFallback && (
          <span className="inline-block rounded-[5px] bg-warn/16 px-[7px] py-px align-[1.5px] text-[10.5px] font-bold tracking-[0.04em] text-warn">単一段階</span>
        )}
      </div>

      <div className="my-4 mb-1 h-0.5 overflow-hidden rounded-full bg-line-soft">
        <div
          className={`h-full rounded-full bg-accent transition-[width] duration-700 ease-[cubic-bezier(0.16,1,0.3,1)] ${isRunning(research) ? 'animate-pulse' : ''}`}
          style={{ width: `${total ? (finished / total) * 100 : 6}%` }}
        />
      </div>

      <ol className="m-0 list-none p-0">
        {research.levels.map(level => {
          const openByDefault = Boolean(level.result) || level.status === 'running'
          const expanded = overrides.get(level.id) ?? openByDefault
          return (
            <LevelNode
              key={level.id}
              level={level}
              expanded={expanded}
              onToggle={() => toggle(level.id, expanded)}
            />
          )
        })}
      </ol>
    </div>
  )
}
