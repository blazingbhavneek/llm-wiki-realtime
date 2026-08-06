# Realtime Voice Wiki Assistant — Design Plan

Target: a voice assistant that stays pleasant while a RAG system takes 2–15s to
answer. The plan does not try to make waiting shorter. It makes waiting
**informative, interruptible, and visible**.

---

## 0. Principles

| # | Principle | Consequence |
|---|-----------|-------------|
| P1 | Dead air is the enemy, not milliseconds | 4s of useful speech beats 1.5s of silence |
| P2 | Every ms of RAG time is covered by content-bearing speech | The ack must carry information |
| P3 | A result is never dropped | Everything enters a queue with a terminal state |
| P4 | Voice is a summary channel; screen is the detail channel | Never voice identifiers, paths, code |
| P5 | The user must never tiptoe around the system | Ambient human speech must not break it |
| P6 | Correct early, cheaply | Confirm understanding at 1s, not at 4min |

---

## 1. Why the current version feels broken

Timeline of one question today:

```
 0.0s  user speaks ────┐
                       │ Silero VAD endpoint             ~800ms
 1.0s                  ├─ batch STT (whole utterance, no partials)
 1.8s                  ├─ LLM decides to call start_deep_research
 2.5s                  ├─ TTS: "少々お待ちください"      ← dead air with words on top
 3.0s   ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░
         ░░░░░  SILENCE. 30s – 600s. no progress signal.  ░░░░░
         ░░░  user speaks once here → answer DELETED forever  ░░░
         ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░
240.0s                 ├─ markdown stripped by regex
                       ├─ SECOND full LLM pass over up to 9000 chars
250.0s                 ├─ TTS buffers the ENTIRE reply
                       │   (basic SentenceTokenizer never splits Japanese)
258.0s                 └─ one long unstoppable monologue
```

Three structural faults:

1. **No fast tier.** Every non-greeting goes to a 600s multi-agent research call.
2. **No delivery guarantee.** Completed results are discarded if the user was
   recently speaking — `return`, not enqueue.
3. **No attention model.** Any nearby human voice is treated as a barge-in and
   destroys in-flight state.

---

## 2. Target timeline

Same question, new architecture:

```
 0.0s  user speaking .....................................
 0.5s  │ partial transcript contains topic (「CIについて〜」)
       │   └─► SPECULATIVE level-1 retrieval fires early
 0.9s  │ turn detector says the turn ended (not bare VAD)
 1.1s  ├─ RAG planner emits the query TREE (no search yet)
 1.3s  ├─ TTS starts ─────────────────────────────────────────┐
       │   "CIだけで落ちる件ですね。                            │ FIRST SOUND
       │    ビルド設定の差分と、CI環境の依存関係、               │  at 1.3s
       │    この2つから見ます。"                                │
       │                                                       │ covers ~3s
       │   ▲ user can CORRECT THE PLAN right here             │
 2.4s  │ level-1 result lands (searched during narration)     │
 4.3s  ├─ narration ends ─────────────────────────────────────┘
       ├─ level-1 answer speaks, seamless, no gap
       │  full text → screen. tree UI: level-2 running 3/6, eta 4s
 8.0s  ├─ level-2 answer speaks
       └─ anything unspoken stays in the queue, replayable
```

Perceived latency = **1.3s**, not 258s. Real total work is similar.

---

## 3. System shape

```
┌──────────────────────── BROWSER ─────────────────────────┐
│  mic ──► AEC ──► LiveKit publish                         │
│  speaker ◄── GainNode (ducking) ◄── LiveKit subscribe     │
│                                                           │
│  ┌────────────────────┬──────────────────────────────┐   │
│  │  conversation      │  RESEARCH PANEL              │   │
│  │  bubbles           │   • live query tree          │   │
│  │                    │   • per-node ▶ read/hear     │   │
│  │                    │   • full markdown + sources  │   │
│  └────────────────────┴──────────────────────────────┘   │
└──────────────────────────┬────────────────────────────────┘
                           │ WebRTC audio (wss) + data channel
┌──────────────────────────▼──────── AGENT ─────────────────┐
│  ┌─────────────────────────────────────────────────────┐  │
│  │ ATTENTION FSM     wake word · dormant/open · duck   │  │
│  └───────────────────────┬─────────────────────────────┘  │
│                  gated audio                              │
│  ┌───────────────────────▼─────────────┐                  │
│  │ STT  streaming, partials exposed    │                  │
│  └───────────────────────┬─────────────┘                  │
│                          │                                │
│  ┌───────────────────────▼─────────────┐  ┌────────────┐  │
│  │ LLM  conversation + tools           │◄─┤ STATE      │  │
│  └───────────────────────┬─────────────┘  │ BLOCK      │  │
│                          │                └─────▲──────┘  │
│  ┌───────────────────────▼─────────────┐        │         │
│  │ SPEECH SCHEDULER  floor control,ETA │        │         │
│  └───────────────────────┬─────────────┘  ┌─────┴──────┐  │
│                          │                │ DELIVERY   │  │
│  ┌───────────────────────▼─────────────┐  │ QUEUE      │  │
│  │ TTS  streaming, JA sentence split   │  └─────▲──────┘  │
│  └─────────────────────────────────────┘        │         │
└─────────────────────────────────────────────────┼─────────┘
                                                  │ plan/progress/
                              ┌───────────────────┴─────┐ result events
                              │  FAST RAG (streaming)   │
                              │  levelled subqueries    │
                              └─────────────────────────┘
```

Four new components: **Attention FSM**, **Delivery Queue**, **Speech
Scheduler**, **State Block**. Everything else already exists.

---

## 4. Attention model

### 4.1 Why a wake word is not enough

Alexa's model — wake word per turn — is unusable for conversation. You would
say the name before every follow-up. What you want is a **conversation window**:
the name opens it, then it stays open.

```
         ┌──────────────────────────────┐
    ┌───►│          DORMANT             │
    │    │  audio → wake detector ONLY  │
    │    │  nothing reaches STT or LLM  │
    │    └──────────────┬───────────────┘
    │                   │ wake word detected  ("モーヴィ")
    │                   ▼
    │    ┌──────────────────────────────┐
    │    │           OPEN               │
    │    │  full pipeline active        │
    │    │  follow-ups need NO wake word│
    │    │  idle timer 20s, reset on    │
    │    │  every exchange              │
    │    └──────────────┬───────────────┘
    │                   │
    └───────────────────┘
      20s idle  ·  "ありがとう" / "もういいよ"  ·  research queue empty + idle
```

While research is in flight, the window does **not** close — the agent still
owes the user an answer.

### 4.2 Barge-in: duck, don't stop

The fatal current behaviour: any VAD speech during agent TTS is a hard
interrupt and state is lost. Two people chatting nearby destroys the answer.

Split "someone is talking" from "someone is talking **to me**":

```
agent speaking ══════════════════════════════════════════════►
                        │
              VAD speech-start
                        │
              duck to 25% over 150ms       ← instant, reversible, no state loss
                        │
        ┌───────────────┴────────────────┐
        │  addressed to the agent?       │   decided in ~400ms
        │  ─────────────────────────     │
        │  1. wake word present          │
        │  2. short imperative:          │
        │     待って/ちょっと/違う/やめて   │
        │  3. within 3s of agent asking  │
        │     a question (reply window)  │
        │  4. OPEN + no other speaker    │
        └────┬──────────────────────┬────┘
             │ NO                   │ YES
             ▼                      ▼
     restore over 200ms      hard stop TTS
     agent continues         chunk.state = INTERRUPTED
     ══════════════════►     chunk.spoken_upto = <char offset>
     user never noticed      process the user turn
```

Ducking is cheap and reversible. Stopping is neither. Default to ducking.

### 4.3 Wake word implementation

- Run **openWakeWord** or **Porcupine** on raw audio frames. <10ms, on-device,
  no network hop. Do **not** use the LLM or a round-trip for this.
- Interim fallback until a custom model is trained: keyword-spot on STT
  partials. Works, adds ~400ms, acceptable for phase 1.

**Name criteria:** 3–4 morae · plosive or fricative onset · effectively zero
frequency in natural Japanese speech · no homophone.

- Recommended: **「モーヴィ」** — ties to `moove_wiki`, does not occur in
  ordinary Japanese, /m/ + long vowel + /v/ is easy for a detector.
- Avoid: ソラ, アカリ, ヒカリ, ミナト — common words or names, constant false fires.

---

## 5. The ack IS the decomposition

The single largest perceived-latency win, and it costs nothing.

The RAG planner already produces a levelled subquery tree before searching.
**Speak that tree.**

```
User:   「なんでビルドがCIだけで落ちるの?」

        planner (~300ms) ──► tree:
                              L1: CIのビルド設定の差分
                              L1: CI環境の依存関係
                              L2: (depends on L1) キャッシュの影響

Agent:  「CIだけで落ちる件ですね。
         ビルド設定の差分と、CI環境の依存関係、この2つから見ます。」
         └──────────────── ~3s of speech ────────────────┘
              meanwhile: level-1 searches run in parallel
```

Three wins from one move:

| Win | Mechanism |
|-----|-----------|
| Latency covered | Planner is one small LLM call; speech starts before search does |
| Comprehension confirmed | User hears that you understood, at 1s |
| **Plan is steerable** | 「違う、依存関係だけでいい」 → prune the tree → cheaper AND faster AND more accurate |

The third is the real prize: a human in the query-planning loop at zero latency
cost.

**Gate it.** If the planner returns depth 1 with high confidence, skip the
narration and answer directly. Narrate only when the tree is non-trivial.

**Requirement on the RAG:** emit the tree as an early standalone event, before
searching. Do not bury it in the final response.

---

## 6. RAG streaming contract (realtime SSE)

The endpoint is implemented. This section records what it gives us, how the
speaking agent consumes it, and where the agent must compensate.

### 6.1 The invariant everything rests on

```
        speech_duration(level N)  >=  compute_duration(level N+1)
```

Hold it and the user hears continuous speech from ~1.4s to the end; the RAG's
total runtime never surfaces. Latency is not reduced, it is **hidden behind
speech**.

```
level 1 compute  ######
level 1 speech         ######################
level 2 compute        ########                  <- hidden
level 2 speech                 ####################
level 3 compute                ##########            <- hidden
level 3 speech                           ###############
                 |___________ user hears no gap ___________>

                              vs

level 1 compute  ######
level 1 speech         ######
level 2 compute        ##################
                             ............  <- DEAD AIR. bridge, or lose them.
```

Japanese TTS runs at roughly **6 characters per second**, so a 60-character
level answer buys about 10s of cover. A level that computes in <=3s is fully
hidden. This is exactly why levels must be **shallow and wide**, not deep and
narrow -- width gives the speaker more to say per unit of compute.

Measure the real chars/sec of the TTS voice once and store it as a constant;
the whole scheduler keys off it.

### 6.2 Endpoint

```http
POST {WIKI_PREFIX}/{database}/api/ask/realtime/stream
Content-Type: application/json
Accept: text/event-stream
```

Default prefix `/llm-wiki`, e.g. `/llm-wiki/wiki/api/ask/realtime/stream`.
It is a POST stream, so browser `EventSource` cannot be used -- the agent reads
it with an HTTP client; a browser would need `fetch` plus a manual SSE frame
parser.

Cancellation, used on barge-in:

```http
POST {WIKI_PREFIX}/{database}/api/agent-runs/{run_id}/stop
```

Closing the connection also cancels, but the explicit stop reaches the server
immediately -- prefer it when the user interrupts.

### 6.3 Knobs, tuned for voice

| Field | Default | Voice setting | Why |
|---|---:|---:|---|
| `max_levels` | 4 | **3** | Each level costs a speech turn. 3 covers most questions. |
| `max_queries_per_level` | 4 | **4** | Wide levels give the speaker more to say -- protects 6.1. |
| `max_recovery_levels` | 2 | **1** | Recovery is announced mid-answer; more than one gets tedious. |
| `search_limit` | 8 | 8 | -- |
| `max_context_chars` | 14000 | **8000** | Smaller context = faster level = easier to hide. |
| `min_search_results` | 3 | 3 | -- |

### 6.4 Event flow

```
         RAG                                        SPEAKING AGENT
          |                                              |
   run    |  {run_id}                                    |  store run_id
          |=============================================>|  (cancellation handle)
          |                                              |
   plan   |  {version, levels[]:                         |  paraphrase as the ack
          |    {id, position, objective, queries[],       |  render the tree UI
          |     depends_on, kind, status}}                |  DO NOT read verbatim
          |=============================================>|
          |                                              |
level_start  {level_id, position, objective, queries[]}   |  mark node running
          |=============================================>|  start ETA clock
          |                                              |
  level   |  {level_id, text, facts[], queries[],        |  ENQUEUE text -> speech
          |   reference_node_ids[], complete, latency_ms} |  render answer+sources
          |=============================================>|  dedupe by level_id
          |                                              |
 plan_update {version, reason, inserted_level_id,         |  replace pending plan
          |   after_level_id, levels[]}                   |  maybe voice the change
          |=============================================>|
          |                    ...                       |
   done   |  {status, levels_completed, facts[]}         |  drain buffer, close
          |=============================================>|  DO NOT speak again
          |                                              |
  error / cancelled                                       |  stop accepting,
          |=============================================>|  keep what was spoken
```

Normal order: `run -> plan -> (level_start -> level)* -> done`.
Recovery order inserts `plan_update` after the `level_start` of the level that
came up short, before its `level` arrives.

`: connected` and `: ping` comments are transport only -- never enqueue them.

### 6.5 `level` is the speech unit

One `level` event = one coherent thought = one speech segment. Rules:

1. Append non-empty `text` to the speech buffer **immediately**. Never wait for
   `done`.
2. Deduplicate by `level_id`. A reconnect starts a new run and will replay.
3. `text` may be empty when no fact survived reference validation -- do not
   enqueue an empty string, and do not invent a bridging sentence.
4. `complete: false` means partial evidence. Speak the text as-is; a recovery
   level may supply the rest. Do not editorialise about what is missing.
5. Never voice `reference_node_ids`. Store them on the queued chunk; the panel
   renders them as source chips.
6. `facts[]` is the source map for that segment -- use it for the screen channel
   and for "where did that come from?" follow-ups.

### 6.6 The plan: paraphrase, never recite

The protocol says the plan should not be spoken, and that is right -- `queries`
are retrieval strings, not sentences. But section 5 still holds: **speak a
one-line paraphrase of the `objective` fields**, which is what buys the TTFT
cover.

```
plan.levels[].objective:
  1. "CIとローカルのビルド設定の差分を特定する"
  2. "差分がビルド失敗を引き起こす仕組みを説明する"

spoken ack:
  「CIだけで落ちる件ですね。ビルド設定の差分と、その影響、この2つから見ます。」

not spoken:
  the query strings, level ids, positions, depends_on
```

Gate on size: 1 level -> skip the ack and wait for the answer. 2+ levels ->
narrate. `planning_fallback: true` means planning failed and everything
collapsed to one level -- skip the ack and hedge the answer.

### 6.7 `plan_update` while already speaking

A recovery level is inserted **after** the ack was already spoken. Reconcile:

```
  plan_update arrives
         |
  version > stored?  -- no --> ignore
         | yes
  replace pending plan atomically (never patch, never replay spoken text)
         |
  +------+--------------------------------+
  |  reason == insufficient_evidence?      |
  +---+-------------------+----------------+
      | yes               | no (reorder only)
      v                   v
  worth one clause     silent. update the tree UI,
  when the floor is    say nothing.
  next free:
  「もう少し裏付けが要るので、追加で見ます。」
```

Already-emitted levels are immutable. Never retract spoken text.

### 6.8 What the protocol does not give us, and what to do about it

| Gap | Impact | Agent-side compensation |
|---|---|---|
| **No `eta_ms`** | Section 8 cannot choose seamless / stretch / bridge | Predict: `eta = median(previous level latency_ms)`, seeded at **2500ms**. Clock starts at `level_start`. Overshoot by 1.3x before bridging. |
| **No confidence score** | Cannot hedge honestly | Derive a proxy: `complete=false`, any `queries[].enough=false`, `search_result_count < min_search_results`, or `planning_fallback=true` -> hedge with 「確実ではありませんが」. |
| **No `id:` / `Last-Event-ID` / resume** | Reconnect replays and duplicates speech | Dedupe by `level_id` at the conversation layer (already in the frontend reducer). On reconnect, call the stop endpoint for the old `run_id` first. |
| **No per-query progress** | Tree UI can only animate at level granularity | Accept it. Show each in-flight query under the running level with a pulsing marker. |
| **No `voice_text`** | `text` is written prose; may carry markdown or identifiers | Strip on the agent side before TTS. Better fix below. |
| **Language not guaranteed** | `text` follows the source docs, not the speaker | Pin it via `overrides` so the shallow-answer model always writes speakable Japanese. |

### 6.9 Asks for the RAG implementer

Small additions, large payoff -- in priority order:

1. **`expected_ms` on `level_start`.** Even a crude estimate restores section
   8's stretch/bridge decision. Single most valuable addition.
2. **`voice_text` alongside `text`** -- same content, no markdown, no bare
   identifiers, one or two sentences, Japanese. Removes a whole LLM pass from
   the speech path.
3. **Guarantee `text` contains no markdown.** If `voice_text` lands this stops
   mattering; until then the agent regex-strips and may mangle things.
4. **A numeric `confidence` on `level`**, even if it is only a function of
   `enough` and `search_result_count`.
5. **`id:` on each frame** so a dropped connection can resume instead of
   restarting and re-speaking.

### 6.10 Where this lands in the system

The agent owns the SSE connection, not the browser. It consumes the stream for
speech **and** mirrors every event verbatim onto the LiveKit data channel under
topic `research`, so the panel and the speech scheduler are driven by exactly
the same event sequence -- no second source of truth, no duplicate run.

```
  RAG  --SSE-->  AGENT  --+-->  delivery queue --> scheduler --> TTS
                          +-->  data channel `research` -->  panel UI
```

---

## 7. Delivery queue

The fix for "the agent forgets the answer." Nothing is ever discarded.

Field names mirror the SSE `level` event (§6.5) so nothing is renamed in flight.

```python
@dataclass
class ResultChunk:
    run_id: str
    level_id: str            # dedupe key -- a level is spoken at most once
    position: int            # plan order
    objective: str           # short spoken name: "ビルド設定の差分"
    text: str                # -> speech, verbatim from the RAG
    facts: list[dict]        # [{text, node_ids}] -> screen, source chips
    reference_node_ids: list[str]
    complete: bool           # False -> hedge; recovery level may follow
    latency_ms: int          # feeds the ETA predictor in §8

    state: Literal["pending", "speaking", "spoken",
                   "interrupted", "stale"] = "pending"
    spoken_upto: int = 0     # char offset -- resume point after barge-in
    arrived_at: float = 0.0
    turns_since_arrival: int = 0
```

There is no `confidence` field in the protocol; derive the hedge from
`complete`, `queries[].enough` and `search_result_count` per §6.8.

### State machine

```
                  RAG result event
                         │
                         ▼
                  ┌─────────────┐
                  │   PENDING   │
                  └──┬───────┬──┘
     floor is clear   │       │  held > 2 turns  OR  topic moved on
                      │       │
                      ▼       ▼
              ┌────────────┐  ┌──────────────────────────┐
              │  SPEAKING  │  │          STALE           │
              └──┬──────┬──┘  │ screen only.             │
     barge-in    │      │     │ at most ONE line:        │
                 │      │     │ 「さっきのCI設定の件、    │
                 │      │     │   画面に出してます」      │
                 │      │     │ never a monologue.       │
                 │      │     └──────────┬───────────────┘
                 │      │ completes      │ user asks for it
                 ▼      ▼                │
     ┌───────────────┐ ┌──────────┐      │
     │  INTERRUPTED  │ │  SPOKEN  │◄─────┘
     │  spoken_upto  │ │ stays in │
     │  = N          │ │ queue,   │
     └───────┬───────┘ │ replay-  │
             │         │ able     │
  user: 「続き」       └──────────┘
  or scheduler offers        ▲
             │               │ 「もう一回」/「さっきの何だっけ」
             └──► SPEAKING ──┘
                  (resumes from spoken_upto, not from the start)
```

### Rules

1. **Never `return` on a completed result.** Always enqueue. This is the single
   line that fixes the worst current bug.
2. **Max hold, then degrade.** Held >2 turns or the topic shifted → `stale`.
   Stale means screen-only plus at most one short mention. Do not nag.
3. **Interrupted resumes from offset.** 「続きですが、〜」 not from the top.
4. **Spoken chunks stay in the queue.** Replay costs 0ms and 0 searches.
5. **Backpressure.** If 4 chunks land at once, do not queue 4 monologues.
   Merge into one summary; offer detail per `objective`.
6. **Interrupted ≠ spoken.** On interrupt the agent later says: 「途中でしたが、
   キャッシュの件がまだ残っています。聞きますか?それとも解決しましたか?」

---

## 8. Speech scheduler

Owns the audio floor. Decides what is spoken, when, and whether to stretch.

```
           ┌──────────────────────────────────────────┐
           │  FLOOR STATE                             │
           │   • user_speaking      (from VAD/STT)    │
           │   • agent_speaking     (from TTS handle) │
           │   • last_user_speech_at                  │
           └────────────────┬─────────────────────────┘
                            │
        ┌───────────────────▼────────────────────┐
        │  pick next PENDING chunk               │
        │  priority: low level first,            │
        │            then arrival order          │
        └───────────────────┬────────────────────┘
                            │
        ┌───────────────────▼────────────────────┐
        │  eta_ms of NEXT chunk vs               │
        │  remaining_speech_ms of CURRENT        │
        └───┬──────────────┬─────────────────┬───┘
            │              │                 │
     eta < remaining   eta slightly over   eta way over
            │              │                 │
            ▼              ▼                 ▼
      seamless.       STRETCH:          BRIDGE ONCE:
      just continue   add a citation    「もう少しかかります」
      into the next   sentence, or      then SHUT UP.
      chunk.          restate the       Silence beats
                      subquery.         repeated filler.
```

The protocol has no `eta_ms` (§6.8), so the scheduler predicts it:

```
  on level_start:  clock = now
                   eta   = median(latency_ms of completed levels)  or 2500ms

  seamless   when  eta < remaining_speech_ms
  stretch    when  eta < remaining_speech_ms * 1.3
  bridge     when  eta >= remaining_speech_ms * 1.3   -- once, then silence
```

Each completed `level` carries `latency_ms`, so the estimate self-corrects
within one turn. An `expected_ms` field on `level_start` (§6.9, ask #1) would
replace this guesswork outright.

---

## 9. Screen is a co-equal channel

The monologue problem is not solved by better prose. It is solved by **not
saying the long thing**.

```
       level event
         │
    ┌────┴────────────────────────────────┐
    ▼                                     ▼
  VOICE                                 SCREEN
  level.text, stripped                  what is being searched right now
  concepts only                         level.text as plain text
  hedge when complete=false             facts[] + node_ids as source chips
  「画面に出しました」                    per-query state and timings
```

**The screen is a live status surface, not a document viewer.** No markdown
rendering, no syntax highlighting, no diagrams, no tables. Those cost bundle
weight and paint time on the exact path that has to stay fast, and none of them
help a user who is mid-conversation. Text arrives as text.

**Hard rule: never voice an identifier.** Function names, file paths, flags,
version numbers, error codes → screen only. The voice says
「3つの関数が関係します。画面に出しました。」

This replaces the current katakana-reading rules in the system prompt
(`IO_BUF_SIZE` → 「アイオー・バッファー・サイズ」). Delete those rules. Just do
not say the thing.

---

## 10. Research panel: the live query tree

Not a spinner. The tree itself, updating in place.

Not a spinner, and not a document. It shows **which searches are running right
now** and what each one came back with.

```
┌───────────────────────────────────────────────────────────┐
│  なんでビルドがCIだけで落ちるの?                              │
│  段階 2 / 2 を検索中                                          │
│  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━──────────────────────────│
├───────────────────────────────────────────────────────────┤
│  ✓  ビルド設定の差分を特定する                      1.7s   │
│     · CIのビルド設定はどこで定義されているか          8件   │
│     · ローカルビルドとの差分は何か                    6件   │
│     ┌───────────────────────────────────────────┐    │
│     │ CIのビルドはキャッシュを無効化した状態で実行 │    │
│     │ されます。ローカルとの違いはその一点だけです。 │    │
│     └───────────────────────────────────────────┘    │
│     CIのビルドはキャッシュ無効で実行される。 [node:14]  │
│     ローカルはキャッシュが有効なまま。        [node:22]  │
│                                                           │
│  ⟳  失敗を引き起こす仕組みを説明する              検索中   │
│     ● キャッシュ無効化がビルドに与える影響        検索中   │
└───────────────────────────────────────────────────────────┘

  ✓ done   ⟳ running   ○ pending   △ partial evidence
  ● = query in flight (pulsing)      [node:NN] = source chip
```

Why this matters beyond looking nice:

- **Perceived latency drops with identical real latency.** Visible in-flight
  queries read as fast; a spinner reads as broken.
- **The user can see what the system understood** from the objectives, and
  correct it while level 2 is still running.
- **Reading is an escape hatch from voice.** If the agent is being long-winded,
  the answer is already on screen. Never trap someone in the voice path.
- **Partial levels are visible**, so the user learns which answers to trust.
- **No document rendering.** Plain text only — see §9.

---

## 11. What the LLM sees

Do **not** put full chunk text into `chat_ctx`. Context explodes, quality drops.
Inject a compact state block each turn instead:

```
[research state]
  handle          state        conf   note
  ─────────────────────────────────────────────────
  CI設定の件       spoken       0.9
  依存関係の件     UNSPOKEN     0.6    ready, not yet said
  キャッシュの件   running      —      3/6, eta 4s
  古いXの件        stale        0.8    2 turns old, on screen only
```

Full text lives in a store. Give the LLM a `read_result(handle)` tool to pull
it on demand. This is what actually fixes "the agent forgets the answer" —
it always sees the index, and can retrieve the body.

Also fix the current bug where the research path calls `generate_reply(instructions=...)`
without `user_input`, so the user's question never enters `chat_ctx` at all.
Every turn — voice and text — must go through the same path and write history.

---

## 12. Additional cases

| Case | Problem | Fix |
|------|---------|-----|
| **Speculative search** | Endpointing costs ~800ms of pure wait | Japanese marks topic early (「Xについて〜」). Fire level-1 retrieval on the partial transcript; discard if the final differs. Retrieval is cheap, latency is not. Enable `preemptive_generation=True`. |
| **Self-echo** | Mic open during TTS → agent transcribes itself → replies to itself | Browser AEC covers headsets; speakerphone/meeting rooms leak. Agent-side guard: drop transcripts with high similarity to TTS output in the last 3s. |
| **Cutting the user off** | Bare Silero VAD chops Japanese at natural pauses | Use `livekit-plugins-turn-detector` MultilingualModel. Plus: never start within ~700ms if the utterance ends on a filler (えーと/その/なんか) or mid-clause (te-form, dangling particle). |
| **Cancellation** | No cancel path — a wrong query burns the full budget | 「やっぱりいい」/「違う」 → kill the task tree, free budget, confirm in four words. |
| **Confidence** | Voice has no citations to eyeball; a confident wrong answer is worse than none | Wire `confidence` into the voice prompt. Low → 「確実ではありませんが」/「Wikiには明記されていません」. |
| **Cache & dedupe** | Same subquery re-run across sessions | Levelled decomposition makes subqueries reusable across *different* top-level questions. High hit rate. Cache by subquery, not by question. |
| **Concurrent research** | Agent cannot say which one finished | Each gets a `handle` auto-named from the subquery head noun. Cap ~3 concurrent, queue the rest. |
| **Session persistence** | Refresh mints a new random room, nukes everything | Stable room per user. Rejoin restores the queue and the panel. |
| **Correction** | 「違う、Yの方」 currently means restart | Re-plan, don't restart. Keep completed level-1 results; replan levels 2+. The tree makes this natural. |
| **Transport** | HTTPS page + `ws://` = blocked mixed content; hence all the polyfills | `wss://` with a real cert. Then delete the `crypto.randomUUID` and `getUserMedia` polyfills. |
| **Duplicate agents** | Auto-dispatch + manual dispatch = 2 agents talking over each other; frontend mutes one as a workaround | Fix dispatch. Remove the `primaryAgent` workaround. |

---

## 13. Also fix while in here (known defects)

- `tokenize.basic.SentenceTokenizer()` never splits Japanese (no spaces, uses
  `。`). TTS buffers the entire reply → time-to-first-audio scales with answer
  length. Use a JA-aware tokenizer / punctuation set `。！？、`.
- `lk_stt.StreamAdapter` wraps a model literally named `...-Realtime-...` in the
  non-streaming shim. Use the streaming path.
- `quick_mcp_lookup` is ~200 lines of dead code, never called. Either wire it as
  the fast tier or delete it.
- `is_friendly_smalltalk()` is a hardcoded whitelist — 「もう一回言って」 and
  「ちょっと待って」 currently trigger a 600s research call. Replace with the
  attention FSM + planner depth.
- Single-slot `deep_task` mutex locks the assistant for up to 10 minutes.
  Replace with per-request tasks + the delivery queue.
- Greeting is hidden by regex-matching the LLM's own words; a paraphrase breaks
  it. Send the greeting as a typed event instead.
- `/token` has no auth and creates rooms + dispatches agents for anyone who
  reaches it.

Already fixed in the frontend rebuild (§15):

- ~~`send()` sets `loadingWiki = true` for any text~~ — panel state now derives
  from the `plan` / `level` event stream.
- ~~chunked `wiki.result` reassembly drops empty chunks~~ — protocol replaced by
  per-level events, deduplicated by `level_id`.
- ~~`livekit-client` pulled from `esm.sh` at runtime~~ — bundled dependency.
- ~~mermaid + react-markdown in the render path~~ — removed; plain text only.
- ~~hand-written CSS file~~ — replaced by Tailwind v4 utilities.

---

## 14. Build order

```
PHASE 1 ── independent of the RAG. Fixes the catastrophic cases.
  ├─ Attention FSM: dormant/open window
  ├─ Duck-don't-stop barge-in (keyword-spot fallback, no custom model yet)
  ├─ Delivery queue + state machine
  ├─ State block into chat_ctx + read_result tool
  └─ Unify voice and text through one path
     ▸ Outcome: answers stop vanishing; the agent stops forgetting.

PHASE 2 ── consumes the realtime SSE endpoint (§6). Endpoint is DONE.
  ├─ SSE client in the agent: run/plan/level_start/level/plan_update/done
  ├─ Mirror every event onto the `research` data-channel topic
  ├─ Decomposition-as-ack from plan[].objective (paraphrase, §6.6)
  ├─ Speech scheduler with predicted ETA (§8)
  └─ Cancel via /api/agent-runs/{run_id}/stop on barge-in
     ▸ Outcome: perceived latency drops to ~1.3s.

PHASE 3 ── UI.  DONE — see §15.
  ├─ Speech-reactive disc, doubling as the mic control
  ├─ Live level/query panel with in-flight markers
  └─ Source chips from facts[].node_ids
     ▸ Outcome: waiting becomes legible; voice stops being the only channel.

PHASE 4 ── polish and latency.
  ├─ Custom wake word model (openWakeWord / Porcupine)
  ├─ Streaming STT + JA sentence tokenizer + preemptive generation
  ├─ Speculative retrieval on partials
  ├─ Subquery cache
  └─ wss + cert, drop polyfills, fix dispatch, session persistence
```

Phases 1 and 2 are separable and can be built in parallel.

---

## 15. Frontend (built)

Light, two-column, Tailwind v4, latency-first. No document rendering anywhere.

```
┌────────────────────────────────┐┌──────────────────────────┐
│ モーヴィ            ● 接続済み  ││  会話 3 │ 調査 ●        │
│                              │├──────────────────────────┤
│           ╱─────────╲          ││                          │
│          │ ≈≈≈≈≈≈≈≈≈ │         ││  transcript bubbles      │
│         │ ≈≈ shader ≈≈≈ │        ││   or                     │
│         │ ≈ inside the ≈ │        ││  level list:             │
│         │ ≈≈ circle ≈≈≈ │        ││   ✓ objective    1.7s   │
│          │ ≈≈≈≈≈≈≈≈≈ │         ││     · query      8件    │
│           ╲─────────╱          ││     answer text          │
│          rim reacts to audio  ││     [node:14]            │
│          ▲ click = mic        ││   ⟳ objective   検索中   │
│            話しています         ││     ● query      検索中   │
│           段階 2/2 検索中       ││                          │
│ ┌────────────────────────┐ ││                          │
│ │ 質問を入力            →  │ ││                          │
│ └────────────────────────┘ ││                          │
└────────────────────────────────┘└──────────────────────────┘
```

### The disc

A circular speech visualiser: the guiddy interference field rendered inside a
disc, sized `min(46vh, 40vw, 400px)`. **The disc is the microphone control** —
click it to talk. There is no separate mic button; the composer below is text
only.

One WebGL1 fragment shader, one full-screen triangle, no 3D library, scalar
uniforms only (no arrays, no dynamic indexing) so it compiles everywhere.

- Concentric ripples radiate from the centre — the voice signature. Speech
  tightens them and speeds them up.
- Three spectral bands at separate spatial frequencies: bass swell, mid ripple,
  high shimmer.
- **The rim is part of the visualisation.** Radius breathes with level and
  ripples angularly with mid/high, so the silhouette itself reacts. The disc is
  masked in the shader, not clipped by CSS, which is what makes that possible.
- Outer band lifts toward white so it reads as a sphere, not a flat sticker.
- A slow wide swell while researching — motion with no speech energy.
- Attack fast / release slow on the envelope, so speech snaps it open and lets
  it settle instead of flickering per FFT frame.
- Four accent colours in the hero's blue family, eased over ~1s: dormant
  #005BDD, listening #0EA5E9, thinking #7C3AED, speaking #2563EB.
- Time runs at 0.35x so motion reads as ambient, not busy.

**It stays gently animated at rest.** The clock and low-amplitude domain-warped
surface continue moving with no audio. Mic and agent RMS share the same
attack-fast/release-slow envelope and visibly increase the field response.
Listening smoothly changes the whole palette from blue/light blue to blood red
and white; it does not leave the cool rim behind.

Audio taps are `MediaStreamSource` on the mic track and the agent track — never
`MediaElementSource`, which would hijack playback.

### Files

```
src/lib/audio.js                  AudioBus: FFT -> {userLevel, agentLevel, bands[32]}
src/lib/research.js               SSE-event reducer + development fixtures
src/components/WaveField.jsx      WebGL1 reactive disc; doubles as the mic button
src/components/ResearchPanel.jsx  live level/query list, plain text only
src/components/Transcript.jsx     conversation bubbles
src/components/Composer.jsx       text input + animated send button
src/App.jsx                       layout, LiveKit, event routing
src/index.css                     @theme tokens + the 5 keyframes
```

Styling is Tailwind v4 utilities throughout. `index.css` holds only the
`@theme` light palette, base element rules, the scrollbar, and the four
animations utilities cannot express (`query-ping`, `spin`, `rise`, `blink`)
plus the reduced-motion override.

### Palette (light)

```
canvas #FAFAFF   surface #FFFFFF   line #E2E6F0   line-soft #EEF1F7
ink    #0F172A   muted   #64748B   faint #94A3B8
accent #005BDD   accent-soft #E8F0FD
warn   #B45309   danger  #DC2626   ok    #059669
```

### Latency decisions

- `mermaid`, `react-markdown`, `remark-gfm` **removed**. They were ~40 lazy
  chunks and the largest single cost in the bundle. Bundle went 973kB + mermaid
  chunks → **705kB / 196kB gzip**, which is now mostly `livekit-client`.
- `livekit-client` is a bundled dependency, not a runtime `esm.sh` import — no
  CDN round-trip at connect time, no silent version drift.
- Level answers render as plain text. Sources render as `[node:NN]` chips.
- The panel auto-expands a level when it starts running and when its answer
  lands, with no effect and no cascading render.

### Open wiring (needs the agent side, Phase 2)

The agent must publish each SSE event verbatim on the LiveKit data channel:

```
topic:   "research"
payload: the SSE `data` JSON, unchanged (it already carries `type`)
```

Development mode replays a scripted run against the real reducer. Production
mode receives these events from LiveKit and contains no scripted fixture data.

### Development and production modes

- `cd frontend && npm run dev` starts a backend-free development UI with a
  seeded conversation, scripted research events, and typed-question fixtures.
  Clicking the orb captures the microphone and returns the same audio after one
  second through the normal agent-audio analyser path.
- `cd frontend && npm run build` always creates a production bundle. It uses
  `/token`, LiveKit, and real `research` events. `server.py` serves the generated
  `frontend/dist` directory after its API routes.
- The right sidebar defaults closed in both modes. The send button alone scales
  on hover and press, including its visually unavailable state; the click
  handler still rejects empty or disconnected sends.

---

## 16. Handoff — everything still to do

Written for whoever picks this up next. Sections 1–15 are the design; this is
the task list.

### 16.0 State of the repo

| Area | State |
|---|---|
| `frontend/` | Rebuilt with explicit dev/prod modes. Lint plus both mode builds pass. **Not visually verified** in this checkout. |
| `server.py` | Combined FastAPI/token/frontend server and realtime LiveKit agent worker. |
| Old entry points | `test_agent.py` and `text_test_server.py` removed after consolidation. |
| RAG realtime SSE endpoint | Implemented by the RAG team. Contract in §6. |

### 16.1 Frontend — remaining

1. **Load it and look at it.** `cd frontend && npm run dev`. Confirm the seeded
   transcript/research stream, the one-second microphone return, the calm idle
   shader motion, the blue-to-red/white listening transition, and the send
   button's hover/press scale in both empty and populated states. Use headphones
   for the delayed-audio check.
2. **Mobile.** Below 940px the grid stacks to rows and the panel caps at
   `44dvh`. Untested.
3. **Deployed production smoke test.** Build the frontend, run `server.py`, and
   exercise `/token`, LiveKit audio, and the `research` topic using the GPU
   server's real model endpoints.

### 16.2 `test_agent.py` — delete first

About 400 of the 1121 lines are dead or superseded. Removing them first makes
the rest obvious.

| Delete | Why |
|---|---|
| `mcp_list_tools`, `quick_mcp_lookup`, `_build_args_for_tool`, `_score_tool_for_search`, `_extract_mcp_result_text`, `_tool_to_debug_dict`, `_MCP_TOOLS_CACHE`, the `mcp` imports | ~200 lines, never called from anywhere |
| `ASK_OVERRIDES` | Superseded by the realtime knobs in §6.3 |
| `_call_deep_research_api`, `_run_deep_research_in_background`, `_extract_text_from_deep_response` | Replaced by the SSE client |
| `publish_wiki_markdown` | Replaced by verbatim event mirroring (§16.5) |
| `prepare_wiki_answer_for_speech` | The RAG supplies `text`; do not re-render it. Keep only a minimal markdown strip until `voice_text` exists (§6.9 ask #2) |
| `is_friendly_smalltalk` | A hardcoded whitelist. Replaced by the attention FSM plus planner depth |
| `tool_canary`, `start_deep_research` | Debug scaffolding and the old blocking tool |
| `WikiVoiceState.deep_*` fields, `delivering_deep_result`, `deep_delivery_cancelled`, `research_ack_count` | Replaced by the delivery queue |

### 16.3 `text_test_server.py`

1. **`PUBLIC_LIVEKIT_URL` must be `wss://`.** It is currently
   `ws://10.160.152.38:7880`. An HTTPS page cannot open a `ws://` socket —
   browsers block it as mixed content. This is the root cause of the polyfill
   pile in the old frontend.
2. **Vite proxy target mismatch.** `TEXT_TEST_TLS` defaults to `True`, so the
   server speaks HTTPS, but `vite.config.js` proxies `/token` to
   `http://127.0.0.1:51027`. Pick one. Simplest: run the server with
   `TEXT_TEST_TLS=0` behind the Caddy proxy and keep the plain-HTTP proxy
   target.
3. **Stable room per user.** Every `/token` hit currently mints
   `japanese-assistant-{uuid}`, so a refresh destroys all conversation and
   research state. Derive the room from a signed cookie or a client-supplied
   session id.
4. **Fix agent dispatch.** The file's own comment says auto-dispatch and manual
   dispatch both fire, producing two agents in one room. Settle on one. The
   frontend's `primaryAgent` guard is a workaround, not a fix — the second
   agent still burns STT, LLM, and TTS.
5. **Authenticate `/token`.** It currently creates rooms and dispatches agents
   for anyone who can reach it.
6. Caddy needs `flush_interval -1` on any SSE route, and gzip disabled there —
   compression buffers and destroys TTFT.

### 16.4 `test_agent.py` — pipeline fixes

These are small, independent, and each one is worth doing on its own.

```python
# 1. STT: stop wrapping a streaming model in the non-streaming shim.
#    Delete lk_stt.StreamAdapter(stt=base_stt, vad=vad) and pass base_stt
#    directly. The model is literally named ...-Realtime-...

# 2. TTS: the basic tokenizer never splits Japanese (no spaces, uses 。),
#    so TTS buffers the ENTIRE reply before emitting audio.
#    Replace tokenize.basic.SentenceTokenizer() with a JA-aware tokenizer or
#    one configured for 。！？、 . This alone is seconds of TTFT.

# 3. Turn taking: bare Silero VAD chops Japanese at natural pauses.
from livekit.plugins.turn_detector.multilingual import MultilingualModel
session = AgentSession[WikiVoiceState](
    userdata=state, vad=vad, stt=stt, llm=llm, tts=tts,
    turn_detection=MultilingualModel(),
    preemptive_generation=True,
    min_endpointing_delay=0.7,
)
```

### 16.5 `test_agent.py` — new modules

Four new files. Specs are in the sections named.

| File | Contents | Spec |
|---|---|---|
| `rag_client.py` | SSE consumer + cancel | §6, skeleton below |
| `attention.py` | DORMANT/OPEN FSM, wake word, duck-not-stop | §4 |
| `delivery.py` | `ResultChunk` + queue state machine | §7 |
| `scheduler.py` | Audio floor, predicted ETA, stretch/bridge | §8 |

#### `rag_client.py` skeleton

The one piece worth writing out, because getting the HTTP client options wrong
silently breaks streaming.

```python
import json
import httpx

RAG_BASE = os.getenv("LLM_WIKI_BASE_URL", "http://10.160.152.38:8000")
RAG_DB   = os.getenv("LLM_WIKI_DATABASE", "moove_wiki")

VOICE_KNOBS = {              # see 6.3
    "max_levels": 3,
    "max_queries_per_level": 4,
    "max_recovery_levels": 1,
    "search_limit": 8,
    "max_context_chars": 8000,
    "min_search_results": 3,
}


async def stream_answer(question: str, on_event):
    """Yields nothing; calls on_event(dict) per SSE frame. Returns run_id."""
    url = f"{RAG_BASE}/llm-wiki/{RAG_DB}/api/ask/realtime/stream"
    run_id = None

    # connect timeout only. A read timeout would kill the stream mid-research.
    timeout = httpx.Timeout(None, connect=5.0)

    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream(
            "POST", url,
            json={"question": question, **VOICE_KNOBS},
            headers={"Accept": "text/event-stream"},
        ) as response:
            response.raise_for_status()

            event_name, data_lines = None, []
            async for line in response.aiter_lines():
                if line.startswith(":"):        # ': connected' / ': ping'
                    continue
                if line == "":                  # frame boundary
                    if data_lines:
                        event = json.loads("".join(data_lines))
                        if event.get("type") == "run":
                            run_id = event["run_id"]
                        await on_event(event)
                    event_name, data_lines = None, []
                    continue
                field, _, value = line.partition(":")
                value = value.lstrip()
                if field == "event":
                    event_name = value
                elif field == "data":
                    data_lines.append(value)

    return run_id


async def cancel_run(run_id: str):
    """Call this on barge-in. Preferred over closing the connection."""
    url = f"{RAG_BASE}/llm-wiki/{RAG_DB}/api/agent-runs/{run_id}/stop"
    async with httpx.AsyncClient(timeout=5.0) as client:
        await client.post(url)
```

Gotchas that will cost an afternoon each:

- **No read timeout.** `httpx.Timeout(None, connect=5.0)`. A default read
  timeout kills the stream partway through a long run.
- **`EventSource` cannot be used** — this is a POST stream.
- **Never enqueue `:` comment lines.** They are heartbeats.
- **Deduplicate by `level_id`.** There is no resume; a reconnect replays.

#### Mirroring to the frontend

One function, called from `on_event` for every event. The frontend reducer
already understands the protocol verbatim, so do not reshape anything.

```python
async def mirror(room, event: dict):
    await room.local_participant.publish_data(
        json.dumps(event, ensure_ascii=False).encode("utf-8"),
        reliable=True,
        topic="research",          # the frontend listens on exactly this
    )
```

### 16.6 Order of work

```
1. Delete §16.2.                      Nothing depends on it. Do it first.
2. §16.4 pipeline fixes.              Independent, each is a real TTFT win.
3. rag_client.py + mirror().          Now the panel comes alive end to end.
4. delivery.py.                       Answers stop vanishing.
5. scheduler.py.                      Speech stops colliding with itself.
6. attention.py.                      Ambient human speech stops breaking it.
7. §16.3 transport fixes.             Then delete the frontend polyfills.
8. Wake word model.                   Keyword-spot on STT partials until then.
```

Steps 1–3 are the minimum to see the whole system move: ask a question, watch
the plan appear, watch levels land and get spoken as they arrive.

### 16.7 Acceptance checks

- Ask a 2-level question. First audio starts before the first `level` event
  lands (the ack is spoken off `plan`).
- Talk over the agent mid-answer. The answer is **not** lost — it resumes from
  `spoken_upto` or is offered again.
- Have two people talk near the mic while the agent speaks. It ducks and
  recovers; it does not stop.
- Ask a question, then ask another before the first finishes. Neither is
  dropped and neither says "調査中です".
- Refresh the page mid-research. State survives.
- Kill the RAG mid-stream. The agent keeps what it already said and does not
  hang.

---

## 17. Implementation checkpoint — 2026-08-06

This section supersedes the stale repository-state notes in §16.0. The first
end-to-end agent slice is now implemented.

### Completed

- Deleted the old MCP helpers, blocking `/api/ask` client, tool canary,
  `start_deep_research`, result re-rendering pass, small-talk whitelist, and all
  single-slot `deep_*` state. The replacement agent now lives in `server.py`.
- Added `rag_client.py`: POST SSE parsing, heartbeat filtering, EOF flush,
  voice knobs, no read timeout, explicit cancellation, and verbatim LiveKit
  mirroring on topic `research`.
- Added `delivery.py`: retained result state machine, replay dedupe, partial
  evidence hedging, interruption offsets, stale degradation, backpressure
  compaction, state block, and stored-result reads.
- Added `scheduler.py`: one owner for the speech floor, plan notices and level
  speech serialization, predicted ETA primitives, user-floor gating, and
  interrupt/resume bookkeeping.
- Added `attention.py`: dormant/open conversation window, STT keyword-spot
  fallback for 「モーヴィ」, idle expiry that is suspended during research,
  and cancel/continue/repeat/close controls.
- Voice and text now enter the same `ResearchCoordinator`. The default LLM
  answer is stopped; `plan` supplies the acknowledgement and each `level` is
  spoken immediately without a second full LLM pass.
- The realtime STT is passed directly, multilingual turn detection and
  preemptive generation are enabled, and the TTS stream adapter uses a
  low-buffer Japanese-punctuation-aware configuration.
- Browser ducking is wired over LiveKit topic `attention`: 25% over 150ms,
  restore over 200ms. The temporary `primaryAgent` muting workaround is gone.
- Visual review replaced the original oversized bloom and later outlined rim
  with a soft, luminous noise-driven boundary. The orb now has continuous
  fluid motion at idle and gated RMS-driven response to both mic and agent
  audio, without a mechanical scalloped silhouette.
- `/token` now derives a stable room from a signed HttpOnly session cookie,
  defaults the public LiveKit URL to `wss://`, supports optional bearer auth
  through `TEXT_TEST_ACCESS_TOKEN`, retains one dispatch mode, and defaults the
  app server to plain HTTP behind the TLS proxy.
- `test_agent.py` and `text_test_server.py` are consolidated into `server.py`.
  One command starts Uvicorn in a supervised background thread and the LiveKit
  worker in the main thread; model services remain independent HTTP endpoints.

### Verification completed

- `python3 -m unittest -v test_realtime_modules.py` — 8 tests pass.
- `python3 -m py_compile server.py rag_client.py delivery.py scheduler.py attention.py` — passes.
- `cd frontend && npm run lint` — passes.
- `cd frontend && npm run build` — passes (708.46 kB / 196.53 kB gzip; the
  remaining large dependency is LiveKit).
- Development-mode Vite build — passes and contains the scripted fixtures.
- Production bundle contains `/token` and excludes the development fixture
  markers.

### Still requires the deployed environment

1. Install/use the project's real Python environment and smoke-test the exact
   installed LiveKit version. This checkout has no `livekit`, `httpx`,
   FastAPI, or dotenv packages, so only syntax and dependency-free modules were
   exercised locally.
2. Run the RAG/LiveKit end-to-end acceptance checks in §16.7. In particular,
   confirm the deployed OpenAI-compatible STT actually exposes streaming via
   `openai.STT`; if it does not, use the provider's Voxtral realtime plugin
   rather than restoring the batch `StreamAdapter`.
3. Visually inspect development mode on desktop and mobile. There is no browser
   runtime in this checkout, so the shader, microphone-loopback, and interaction
   checks in §16.1 remain open.
4. Supply the real TLS proxy configuration and certificate. The default is now
   `wss://`, but the repository still has no `Caddyfile`; therefore the exact
   public WSS port and SSE `flush_interval -1` route cannot be chosen here.
5. Replace STT keyword spotting with an openWakeWord/Porcupine model and add
   speaker discrimination. Until then, OPEN mode necessarily treats the only
   captured speaker as addressed.
6. Finish scheduler polish: use `expected_ms` when the RAG adds it and schedule
   the one-time stretch/bridge clause from live remaining-audio estimates.
7. Add speculative retrieval on partial transcripts, cross-session subquery
   caching, and persistent queue storage beyond the lifetime of a LiveKit room.
