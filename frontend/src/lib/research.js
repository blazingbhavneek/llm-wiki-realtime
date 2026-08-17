// Reducer over the realtime RAG SSE events, which the agent mirrors verbatim
// onto the LiveKit data channel under the `research` topic.
//
// Event names and payload shapes follow the realtime SSE protocol:
//   run | plan | plan_update | level_start | level | done | error | cancelled
//
// Two protocol rules are enforced here rather than in the view:
//   - only the highest plan `version` wins, replaced atomically
//   - a level is rendered at most once, deduplicated by `level_id`

export const emptyResearch = () => ({
  runId: null,
  agentRunId: null,
  activeId: null,
  question: '',
  planVersion: 0,
  planningFallback: false,
  levels: [],           // plan order, decorated with live status
  results: new Map(),   // level_id -> level event
  status: 'idle',       // idle | planning | running | complete | partial | error | cancelled
  error: null,
  startedAt: 0,
  latencyMs: 0,
})

function decorate(levels, results, activeId) {
  return levels.map(level => {
    const result = results.get(level.id)
    let status = 'pending'
    if (result) status = result.complete ? 'done' : 'partial'
    else if (level.id === activeId) status = 'running'
    return { ...level, status, result }
  })
}

export function researchReducer(state, event) {
  // `ask` is the only frame allowed to change runs. Anything else carrying a
  // different run id is a straggler from a superseded or retried attempt, and
  // its level ids and plan version would be matched against the wrong state.
  const runId = event.agent_run_id
  if (runId && state.agentRunId && runId !== state.agentRunId && event.type !== 'ask') {
    return state
  }

  switch (event.type) {
    case 'ask': {
      // Local optimistic event: the user asked, the stream has not opened yet.
      const next = emptyResearch()
      next.agentRunId = runId ?? null
      next.question = event.question
      next.status = 'planning'
      next.startedAt = performance.now()
      return next
    }

    case 'run':
      return { ...state, runId: event.run_id, status: 'planning' }

    case 'plan':
    case 'plan_update': {
      if (event.version < state.planVersion) return state
      return {
        ...state,
        planVersion: event.version,
        question: event.question ?? state.question,
        planningFallback: event.planning_fallback ?? state.planningFallback,
        levels: decorate(event.levels, state.results, state.activeId),
        status: state.status === 'planning' ? 'running' : state.status,
      }
    }

    case 'level_start':
      return {
        ...state,
        activeId: event.level_id,
        status: 'running',
        levels: decorate(state.levels, state.results, event.level_id),
      }

    case 'level': {
      if (state.results.has(event.level_id)) return state
      const results = new Map(state.results)
      results.set(event.level_id, event)
      return {
        ...state,
        results,
        activeId: null,
        levels: decorate(state.levels, results, null),
      }
    }

    case 'done':
      return {
        ...state,
        status: event.status === 'partial' ? 'partial' : 'complete',
        activeId: null,
        latencyMs: event.latency_ms ?? state.latencyMs,
        facts: event.facts ?? [],
        levels: decorate(state.levels, state.results, null),
      }

    case 'error':
      return { ...state, status: 'error', error: event.message, activeId: null }

    case 'cancelled':
      return { ...state, status: 'cancelled', activeId: null }

    default:
      return state
  }
}

export const isRunning = state =>
  state.status === 'planning' || state.status === 'running'

// ---------------------------------------------------------------------------
// Development stream. Vite's development mode replays this through the same
// reducer used by production LiveKit events; no backend is involved.
// ---------------------------------------------------------------------------

export const DEMO_QUESTION = 'なんでビルドがCIだけで落ちるの?'

const DEMO_SCRIPT = [
  [120, { type: 'run', run_id: 'demo-31cb54f2' }],
  [520, {
    type: 'plan', version: 1, planning_fallback: false,
    question: 'なんでビルドがCIだけで落ちるの?',
    levels: [
      { id: 'level_1', position: 1, objective: 'CIとローカルのビルド設定の差分を特定する',
        queries: ['CIのビルド設定はどこで定義されているか', 'ローカルビルドとの差分は何か'],
        depends_on: [], kind: 'planned', recovery_for: null, status: 'pending' },
      { id: 'level_2', position: 2, objective: '差分がビルド失敗を引き起こす仕組みを説明する',
        queries: ['キャッシュ無効化がビルドに与える影響'],
        depends_on: ['level_1'], kind: 'planned', recovery_for: null, status: 'pending' },
    ],
  }],
  [700, { type: 'level_start', plan_version: 1, level_id: 'level_1', position: 1,
          objective: 'CIとローカルのビルド設定の差分を特定する',
          queries: ['CIのビルド設定はどこで定義されているか'] }],
  [2400, {
    type: 'level', plan_version: 1, level_id: 'level_1', position: 1,
    objective: 'CIとローカルのビルド設定の差分を特定する',
    queries: [{ query: 'CIのビルド設定はどこで定義されているか', enough: true,
                reference_node_ids: ['node:14'], search_result_count: 8, latency_ms: 184, error: null },
              { query: 'ローカルビルドとの差分は何か', enough: true,
                reference_node_ids: ['node:22'], search_result_count: 6, latency_ms: 201, error: null }],
    text: 'CIのビルドはキャッシュを無効化した状態で実行されます。ローカルとの違いはその一点だけです。',
    facts: [
      { text: 'CIのビルドはキャッシュを無効化した状態で実行される。', node_ids: ['node:14'] },
      { text: 'ローカル環境ではキャッシュが有効なままになっている。', node_ids: ['node:22'] },
    ],
    reference_node_ids: ['node:14', 'node:22'], complete: true, latency_ms: 1690,
  }],
  [2600, { type: 'level_start', plan_version: 1, level_id: 'level_2', position: 2,
           objective: '差分がビルド失敗を引き起こす仕組みを説明する',
           queries: ['キャッシュ無効化がビルドに与える影響'] }],
  [5200, {
    type: 'level', plan_version: 1, level_id: 'level_2', position: 2,
    objective: '差分がビルド失敗を引き起こす仕組みを説明する',
    queries: [{ query: 'キャッシュ無効化がビルドに与える影響', enough: true,
                reference_node_ids: ['node:31'], search_result_count: 5, latency_ms: 260, error: null }],
    text: 'キャッシュが無い状態では依存関係が毎回解決し直されるため、ロックファイルに固定されていないパッケージが新しい版に更新され、そこでビルドが落ちています。',
    facts: [{ text: 'キャッシュ無効時は依存解決がやり直され、未固定の依存が更新される。', node_ids: ['node:31'] }],
    reference_node_ids: ['node:31'], complete: true, latency_ms: 2510,
  }],
  [5400, { type: 'done', status: 'complete', plan_version: 1, levels_completed: 2,
           reference_node_ids: ['node:14', 'node:22', 'node:31'], latency_ms: 5280,
           facts: [] }],
]

export function runDemo(emit, { question = DEMO_QUESTION, onAgentMessage } = {}) {
  const runId = `demo-${Date.now()}-${Math.random().toString(16).slice(2)}`
  const timers = DEMO_SCRIPT.map(([delay, template]) => setTimeout(() => {
    let event = template
    if (template.type === 'run') event = { ...template, run_id: runId }
    else if (template.type === 'plan') event = { ...template, question }
    emit(event)

    if (template.type === 'plan') {
      onAgentMessage?.('質問を確認しました。設定の差分と、その影響の二つから調べます。')
    } else if (template.type === 'level' && template.text) {
      onAgentMessage?.(template.text)
    }
  }, delay))
  return () => timers.forEach(clearTimeout)
}

export const DEMO_TRANSCRIPT = [
  { role: 'user', text: 'なんでビルドがCIだけで落ちるの?' },
  { role: 'agent', text: 'CIだけで落ちる件ですね。ビルド設定の差分と、その影響、この2つから見ます。' },
  { role: 'agent', text: 'CIのビルドはキャッシュを無効化した状態で実行されます。ローカルとの違いはその一点だけです。' },
]
