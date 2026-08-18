import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from 'react'
import { PanelRightClose, PanelRightOpen } from 'lucide-react'
import { Room, RoomEvent, Track } from 'livekit-client'
import { AudioBus } from './lib/audio'
import { getCookie, setCookie } from './lib/cookies'
import {
  DEMO_QUESTION, DEMO_TRANSCRIPT, emptyResearch, isRunning, researchReducer, runDemo,
} from './lib/research'
import WaveField from './components/WaveField'
import Transcript from './components/Transcript'
import ResearchPanel from './components/ResearchPanel'
import Composer from './components/Composer'
import VoiceSelect from './components/VoiceSelect'

const APP_MODE = import.meta.env.VITE_APP_MODE || (import.meta.env.DEV ? 'dev' : 'prod')
const DEV_MODE = APP_MODE === 'dev'
const VOICE_COOKIE = 'tts_voice'
// DEV_MODE never talks to the backend, so GET /tts/voices has nothing to
// return there - this stands in for it so the picker has something to show.
const DEMO_VOICES = ['clone_ref1', 'conan', 'sakura', 'yuki']
const VOICE_CAPTURE = {
  echoCancellation: true,
  noiseSuppression: true,
  // Gain control can amplify the very room tone the gate is meant to reject.
  autoGainControl: false,
  channelCount: 1,
}

export default function App() {
  const [connected, setConnected] = useState(DEV_MODE)
  // Tracked separately from `connected`: the room stays up perfectly well
  // after the agent's session dies, and only this says whether anyone is
  // actually on the other end.
  const [agentPresent, setAgentPresent] = useState(DEV_MODE)
  const [listening, setListening] = useState(false)
  const [draft, setDraft] = useState('')
  const [messages, setMessages] = useState(DEV_MODE ? DEMO_TRANSCRIPT : [])
  const [tab, setTab] = useState(DEV_MODE ? 'research' : 'conversation')
  const [error, setError] = useState('')
  const [mode, setMode] = useState('dormant')
  const [attentionOpen, setAttentionOpen] = useState(false)
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [voices, setVoices] = useState(() => (DEV_MODE ? DEMO_VOICES : []))
  const [voice, setVoice] = useState(() => getCookie(VOICE_COOKIE) || '')
  const [research, dispatch] = useReducer(researchReducer, undefined, () => {
    const base = emptyResearch()
    return DEV_MODE ? researchReducer(base, { type: 'ask', question: DEMO_QUESTION }) : base
  })

  const bus = useMemo(() => new AudioBus(), [])
  const audioMount = useRef(null)
  const agentAudioElements = useRef(new Set())
  const ducked = useRef(false)
  const volumeFrame = useRef(null)
  const connecting = useRef(false)
  const retries = useRef(0)
  const retryTimer = useRef(null)
  const roomRef = useRef(null)
  const devMediaStream = useRef(null)
  const demoStops = useRef(new Set())
  const listenHeld = useRef(false)
  const voiceRef = useRef(voice)
  const microphoneLive = useRef(false)
  const microphoneStarting = useRef(false)
  const agentRetries = useRef(0)
  const agentTimer = useRef(null)

  const thinking = isRunning(research)

  const setAgentDucked = useCallback(nextDucked => {
    ducked.current = nextDucked
    if (volumeFrame.current) cancelAnimationFrame(volumeFrame.current)
    const elements = [...agentAudioElements.current]
    const target = nextDucked ? 0.25 : 1
    const duration = nextDucked ? 150 : 200
    const startedAt = performance.now()
    const starts = elements.map(element => element.volume)

    function step(now) {
      const progress = Math.min(1, (now - startedAt) / duration)
      // Smoothstep avoids a click at either end of the volume ramp.
      const eased = progress * progress * (3 - 2 * progress)
      elements.forEach((element, index) => {
        element.volume = starts[index] + (target - starts[index]) * eased
      })
      if (progress < 1) volumeFrame.current = requestAnimationFrame(step)
      else volumeFrame.current = null
    }
    volumeFrame.current = requestAnimationFrame(step)
  }, [])

  // The orb is a manual override for the server-side attention gate. The mic
  // itself stays published so a wake phrase can be transcribed while dormant.
  const publishListen = useCallback((held, { force = false } = {}) => {
    if (!force && listenHeld.current === held) return
    listenHeld.current = held
    const room = roomRef.current
    if (!room) return
    const payload = new TextEncoder().encode(JSON.stringify({ type: 'listen', held }))
    room.localParticipant.publishData(payload, { reliable: true, topic: 'attention' }).catch(() => {})
  }, [])

  // Data packets are not replayed for participants who join later, so an orb
  // pressed before the agent connects would never reach it and it would sit
  // dormant while the user talked. Re-send the current state on arrival.
  const republishListen = useCallback(() => {
    publishListen(listenHeld.current, { force: true })
  }, [publishListen])

  const enableMicrophone = useCallback(async room => {
    if (!room || !navigator.mediaDevices?.getUserMedia) return null
    if (microphoneLive.current || microphoneStarting.current) return null
    microphoneStarting.current = true
    try {
      const publication = await room.localParticipant.setMicrophoneEnabled(true)
      microphoneLive.current = true
      setListening(true)
      if (publication?.track?.mediaStreamTrack) {
        // Keep audio flowing continuously. Attention decides whether a completed
        // transcript is a turn; muting quiet frames here can delay VAD/STT.
        bus.attach('mic', publication.track.mediaStreamTrack)
      }
      return publication
    } finally {
      microphoneStarting.current = false
    }
  }, [bus])

  // --- orb mode -----------------------------------------------------------
  // Polled rather than pushed: audio levels change every frame, but the orb
  // only needs to know which of four states it is in.
  useEffect(() => {
    const id = setInterval(() => {
      let next = 'dormant'
      if (bus.agentLevel > 0.06) next = 'speaking'
      else if (thinking) next = 'thinking'
      else if (attentionOpen) next = 'listening'
      setMode(current => (current === next ? current : next))
    }, 120)
    return () => clearInterval(id)
  }, [attentionOpen, bus, thinking])

  const orbState = useMemo(() => ({ mode, thinking }), [mode, thinking])

  // --- voice selection ------------------------------------------------------
  // The picker only appears once the active TTS_PROVIDER's voices are known.
  // A cookie from a previous deployment's provider may no longer be valid;
  // drop it silently rather than send an unknown voice to the agent.
  useEffect(() => {
    if (DEV_MODE) return undefined
    let cancelled = false
    fetch('/tts/voices', { cache: 'no-store' })
      .then(response => (response.ok ? response.json() : { voices: [], supports_selection: false }))
      .then(data => {
        if (cancelled) return
        const list = data.supports_selection ? data.voices || [] : []
        setVoices(list)
        if (voiceRef.current && !list.includes(voiceRef.current)) {
          voiceRef.current = ''
          setVoice('')
          setCookie(VOICE_COOKIE, '')
        }
      })
      .catch(() => {})
    return () => { cancelled = true }
  }, [])

  // An explicit choice, so it takes effect now: a fresh /token call mints a
  // new dispatch carrying the new voice, the same route a dropped session
  // already uses to recover (see RoomEvent.Disconnected below).
  const handleVoiceChange = useCallback(next => {
    setVoice(next)
    voiceRef.current = next
    setCookie(VOICE_COOKIE, next)
    roomRef.current?.disconnect()
  }, [])

  // --- development fixtures ----------------------------------------------
  useEffect(() => {
    if (!DEV_MODE) return undefined
    const stops = demoStops.current
    const stop = runDemo(dispatch)
    stops.add(stop)
    return () => {
      for (const cancel of stops) cancel()
      stops.clear()
    }
  }, [])

  // --- livekit ------------------------------------------------------------
  const connectRef = useRef(null)

  const scheduleReconnect = useCallback(() => {
    if (retryTimer.current || roomRef.current) return
    const delay = Math.min(1000 * 2 ** retries.current, 10000)
    retries.current += 1
    retryTimer.current = setTimeout(() => {
      retryTimer.current = null
      connectRef.current?.()
    }, delay)
  }, [])

  // The agent left, or told us its session died. The room is still healthy, so
  // nothing else here notices - the orb keeps its red "listening" look over an
  // agent that cannot hear a word. Put the microphone down, say so, and go
  // back through /token, which mints a new room and a new dispatch: that is
  // the only route to a working agent.
  const handleAgentGone = useCallback(detail => {
    setAgentPresent(false)
    roomRef.current?.localParticipant.setMicrophoneEnabled(false).catch(() => {})
    setListening(false)
    microphoneLive.current = false
    setAttentionOpen(false)
    listenHeld.current = false
    bus.detach('mic', { restoreVoiceGate: false })

    if (agentTimer.current) return
    // Its own backoff, not the room's: the room reconnects fine every time,
    // so the room's counter resets and would retry a dying agent every second.
    const delay = Math.min(2000 * 2 ** agentRetries.current, 20000)
    agentRetries.current += 1
    setError(
      `エージェントが停止しました${detail ? `（${detail}）` : ''}。`
      + `${Math.round(delay / 1000)}秒後に再接続します。`
    )
    agentTimer.current = setTimeout(() => {
      agentTimer.current = null
      roomRef.current?.disconnect()
    }, delay)
  }, [bus])

  const connect = useCallback(async () => {
    if (DEV_MODE || roomRef.current || connecting.current) return roomRef.current
    connecting.current = true
    setError('')
    try {
      const voiceQuery = voiceRef.current ? `?voice=${encodeURIComponent(voiceRef.current)}` : ''
      const credentials = await fetch(`/token${voiceQuery}`, { cache: 'no-store' }).then(async response => {
        if (!response.ok) throw new Error(await response.text())
        return response.json()
      })

      const next = new Room({
        adaptiveStream: true,
        dynacast: true,
        // These defaults are used when setMicrophoneEnabled() creates the
        // LiveKit track, not just by the development loopback below.
        audioCaptureDefaults: VOICE_CAPTURE,
      })

      next.on(RoomEvent.Connected, () => {
        retries.current = 0
        setConnected(true)
      })

      next.on(RoomEvent.ParticipantConnected, participant => {
        if (!participant.identity?.startsWith('agent-')) return
        agentRetries.current = 0
        setAgentPresent(true)
        setError('')
        republishListen()
        enableMicrophone(next).catch(e => {
          setError(`マイクを有効にできませんでした: ${e.message || e}`)
        })
      })

      next.on(RoomEvent.ParticipantDisconnected, participant => {
        if (participant.identity?.startsWith('agent-')) handleAgentGone('')
      })

      next.on(RoomEvent.Disconnected, () => {
        setConnected(false)
        setAgentPresent(false)
        setListening(false)
        microphoneLive.current = false
        setAttentionOpen(false)
        listenHeld.current = false
        roomRef.current = null
        agentAudioElements.current.clear()
        bus.detach('agent')
        bus.detach('mic', { restoreVoiceGate: false })
        scheduleReconnect()
      })

      next.on(RoomEvent.TrackSubscribed, (track, _publication, participant) => {
        if (track.kind !== Track.Kind.Audio) return
        if (!participant.identity.startsWith('agent-')) return

        const element = track.attach()
        element.autoplay = true
        element.volume = ducked.current ? 0.25 : 1
        agentAudioElements.current.add(element)
        audioMount.current?.appendChild(element)
        element.play().catch(() => {})
        bus.attach('agent', track.mediaStreamTrack)
      })

      next.on(RoomEvent.TrackUnsubscribed, track => {
        track.detach().forEach(element => {
          agentAudioElements.current.delete(element)
          element.remove()
        })
        bus.detach('agent')
      })

      next.on(RoomEvent.LocalTrackPublished, publication => {
        if (publication.kind !== Track.Kind.Audio) return
        microphoneLive.current = true
        setListening(true)
        if (publication.track?.mediaStreamTrack) {
          bus.attach('mic', publication.track.mediaStreamTrack)
        }
      })

      next.on(RoomEvent.LocalTrackUnpublished, () => {
        setListening(false)
        microphoneLive.current = false
        bus.detach('mic', { restoreVoiceGate: false })
      })

      // The agent mirrors every realtime RAG SSE event onto this topic
      // unchanged, so the panel and the voice see the same stream.
      next.on(RoomEvent.DataReceived, (payload, participant, _kind, topic) => {
        if (!participant?.identity?.startsWith('agent-')) return
        if (topic !== 'research' && topic !== 'attention') return
        try {
          const event = JSON.parse(new TextDecoder().decode(payload))
          if (topic === 'research') dispatch(event)
          else if (topic === 'attention' && event.type === 'duck') {
            setAgentDucked(Boolean(event.ducked))
          } else if (topic === 'attention' && event.type === 'attention') {
            // Blue means the mic remains available but the server discards
            // ordinary speech. Red is reserved for active attention.
            setAttentionOpen(event.state === 'open')
          } else if (topic === 'attention' && event.type === 'agent_status') {
            // The agent's own diagnosis, which beats waiting for the
            // participant to time out - and it names the component that broke.
            if (event.state === 'closed') handleAgentGone(event.detail || '')
            else setError(`エージェントの${event.detail || '一部'}に問題があります。`)
          }
        } catch (e) {
          setError(`エージェントイベントを読めませんでした: ${e.message || e}`)
        }
      })

      // A streaming STT publishes every interim as its own text stream, all
      // carrying the same lk.segment_id, and then the final as one more.
      // Appending each one turns a single sentence into a ladder of
      // half-finished bubbles, so upsert on the segment id and let the text
      // grow in place instead.
      const applied = new Map()
      let streamSeq = 0
      next.registerTextStreamHandler?.('lk.transcription', async (reader, participant) => {
        // Read before the first await: streams open in order but readAll()
        // can resolve out of order, and a stale interim must not clobber a
        // newer one.
        const seq = streamSeq++
        const attributes = reader.info?.attributes || {}
        const segmentId = attributes['lk.segment_id']
        const final = attributes['lk.transcription_final'] === 'true'

        const value = await reader.readAll()
        if (!value?.trim()) return
        const role = participant?.identity?.startsWith('agent-') ? 'agent' : 'user'

        if (!segmentId) {
          setMessages(current => [...current, { role, text: value }])
          return
        }
        if ((applied.get(segmentId) ?? -1) > seq) return
        if (final) applied.delete(segmentId)
        else applied.set(segmentId, seq)

        setMessages(current => {
          const i = current.findIndex(message => message.segmentId === segmentId)
          if (i === -1) return [...current, { role, text: value, segmentId, final }]
          // The segment is settled; ignore an interim that lost the race to it.
          if (current[i].final && !final) return current
          const updated = [...current]
          updated[i] = { role, text: value, segmentId, final }
          return updated
        })
      })

      await next.connect(credentials.url, credentials.token)
      roomRef.current = next
      republishListen()
      // ParticipantConnected only fires for arrivals after this point, and the
      // dispatch can beat us into the room.
      for (const participant of next.remoteParticipants.values()) {
        if (participant.identity?.startsWith('agent-')) {
          agentRetries.current = 0
          setAgentPresent(true)
          enableMicrophone(next).catch(e => {
            setError(`マイクを有効にできませんでした: ${e.message || e}`)
          })
        }
      }
      return next
    } catch (e) {
      setError(`接続を再試行しています: ${e.message || e}`)
      scheduleReconnect()
      return null
    } finally {
      connecting.current = false
    }
  }, [bus, scheduleReconnect, setAgentDucked, republishListen, handleAgentGone, enableMicrophone])

  useEffect(() => { connectRef.current = connect })

  useEffect(() => {
    // Opening the LiveKit connection is external-system setup, so the state it
    // writes on success or failure is a subscription callback, not a cascade.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (!DEV_MODE) connect()
    const currentBus = bus
    return () => {
      clearTimeout(retryTimer.current)
      clearTimeout(agentTimer.current)
      if (volumeFrame.current) cancelAnimationFrame(volumeFrame.current)
      roomRef.current?.disconnect()
      if (devMediaStream.current) {
        for (const track of devMediaStream.current.getTracks()) track.stop()
        devMediaStream.current = null
      }
      currentBus.close()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // --- actions ------------------------------------------------------------
  function stopDevMicrophone() {
    if (devMediaStream.current) {
      for (const track of devMediaStream.current.getTracks()) track.stop()
      devMediaStream.current = null
    }
    bus.stopDevLoopback()
    bus.detach('mic', { restoreVoiceGate: false })
    setListening(false)
  }

  async function toggleMic() {
    if (DEV_MODE) {
      if (listening) {
        stopDevMicrophone()
        return
      }
      if (!navigator.mediaDevices?.getUserMedia) {
        setError('開発モードの音声テストには HTTPS または localhost が必要です。')
        return
      }
      try {
        setError('')
        const stream = await navigator.mediaDevices.getUserMedia({
          audio: VOICE_CAPTURE,
        })
        const micTrack = stream.getAudioTracks()[0]
        if (!micTrack || !bus.startDevLoopback(stream, 1)) {
          for (const track of stream.getTracks()) track.stop()
          throw new Error('音声ループバックを開始できませんでした')
        }
        devMediaStream.current = stream
        bus.attach('mic', micTrack)
        setListening(true)
      } catch (e) {
        stopDevMicrophone()
        setError(`マイクを開始できませんでした: ${e.message || e}`)
      }
      return
    }
    const active = roomRef.current || await connect()
    if (!active) return
    if (!agentPresent) {
      setError('エージェントに接続していません。再接続を待っています。')
      return
    }
    if (!navigator.mediaDevices?.getUserMedia) {
      setError('マイクを使うには HTTPS または localhost で開いてください。')
      return
    }
    try {
      if (!listening) await enableMicrophone(active)
      // Clicking the orb is now an explicit attention override, not a
      // microphone switch. A wake-word turn controls this state independently.
      publishListen(!listenHeld.current)
    } catch (e) {
      setError(`マイクを有効にできませんでした: ${e.message || e}`)
    }
  }

  async function send() {
    const question = draft.trim()
    if (!question) return
    setDraft('')
    setMessages(current => [...current, { role: 'user', text: question }])
    dispatch({ type: 'ask', question })
    setTab('research')

    if (DEV_MODE) {
      const stop = runDemo(dispatch, {
        question,
        onAgentMessage: text => {
          setMessages(current => [...current, { role: 'agent', text }])
        },
      })
      demoStops.current.add(stop)
      return
    }
    if (!roomRef.current || !connected || !agentPresent) return
    try {
      await roomRef.current.localParticipant.sendText(question, { topic: 'lk.chat' })
    } catch (e) {
      setError(`メッセージを送信できませんでした: ${e.message || e}`)
    }
  }

  // Connected to the room is not the same as having someone to talk to.
  const ready = connected && agentPresent

  return (
    <main data-sidebar-open={sidebarOpen} className="workspace-layout h-dvh w-full overflow-hidden bg-canvas text-ink">
      <section className="relative flex min-w-0 flex-col overflow-hidden pt-[26px] pr-[clamp(24px,4vw,56px)] pb-[clamp(24px,3.4vh,38px)] pl-[clamp(24px,4vw,56px)]">
        <header className="relative z-10 flex items-center justify-between gap-4">
          <div className="flex items-center gap-1.5">
            <VoiceSelect voices={voices} value={voice} onChange={handleVoiceChange} />
          </div>
          <div className="flex items-center gap-1.5">
            <span
              className={`inline-flex items-center gap-1.5 rounded-lg px-2 py-1 text-[12px] font-medium ${
                ready ? 'bg-ok/10 text-ok' : connected ? 'bg-warn/10 text-warn' : 'bg-line-soft text-muted'
              }`}
            >
              <i className={`block h-1.5 w-1.5 rounded-full ${
                ready ? 'bg-ok' : connected ? 'bg-warn' : 'bg-faint'
              }`} /> {DEV_MODE ? '開発モード' : ready ? '接続済み' : connected ? 'エージェント待機中' : '未接続'}
            </span>
            <button
              type="button"
              onClick={() => setSidebarOpen(current => !current)}
              aria-controls="right-sidebar"
              aria-expanded={sidebarOpen}
              aria-label={sidebarOpen ? 'サイドバーを閉じる' : 'サイドバーを開く'}
              className="inline-flex h-8 w-8 items-center justify-center rounded-lg border border-line bg-surface text-muted transition-colors duration-200 hover:border-accent/30 hover:bg-accent-soft hover:text-accent"
            >
              {sidebarOpen
                ? <PanelRightClose size={17} strokeWidth={1.8} aria-hidden="true" />
                : <PanelRightOpen size={17} strokeWidth={1.8} aria-hidden="true" />}
            </button>
          </div>
        </header>

        <div className="relative z-10 flex min-h-0 flex-1 flex-col items-center justify-center gap-6">
          <button
            type="button"
            onClick={toggleMic}
            aria-pressed={attentionOpen}
            aria-label={attentionOpen ? '手動での聞き取りを止める' : '手動での聞き取りを始める'}
            className={`aspect-square w-[min(46vh,40vw,400px)] overflow-visible rounded-full border-0 bg-transparent p-0 transition-transform duration-300 hover:scale-[1.015] active:scale-[0.985] ${
              attentionOpen ? 'scale-[1.01]' : ''
            }`}
          >
            <WaveField bus={bus} state={orbState} />
          </button>
        </div>

        {error && (
          <p className="relative z-10 mb-3 rounded-xl border border-danger/20 bg-danger/[0.06] px-3.5 py-2.5 text-[13px] font-medium text-danger">
            {error}
          </p>
        )}

        <div className="relative z-10">
          <Composer
            value={draft}
            onChange={setDraft}
            onSend={send}
            connected={ready}
            disabled={!ready}
          />
        </div>
      </section>

      <aside
        id="right-sidebar"
        aria-hidden={!sidebarOpen}
        inert={sidebarOpen ? undefined : ''}
        className="sidebar-panel flex min-w-0 flex-col border-l border-line bg-surface max-[940px]:border-t max-[940px]:border-l-0"
      >
        <div className="flex gap-1 border-b border-line-soft px-5 pt-[18px]" role="tablist">
          <button
            role="tab"
            aria-selected={tab === 'conversation'}
            className={`inline-flex items-center gap-[7px] border-0 border-b-2 bg-transparent px-4 py-2.5 text-sm font-semibold transition-colors ${
              tab === 'conversation' ? 'border-accent text-ink' : 'border-transparent text-faint hover:text-muted'
            }`}
            onClick={() => setTab('conversation')}
          >
            会話
            {messages.length > 0 && (
              <span className="rounded-full bg-line-soft px-[7px] py-px text-[11px] font-semibold text-muted">
                {messages.length}
              </span>
            )}
          </button>
          <button
            role="tab"
            aria-selected={tab === 'research'}
            className={`inline-flex items-center gap-[7px] border-0 border-b-2 bg-transparent px-4 py-2.5 text-sm font-semibold transition-colors ${
              tab === 'research' ? 'border-accent text-ink' : 'border-transparent text-faint hover:text-muted'
            }`}
            onClick={() => setTab('research')}
          >
            調査
            {thinking && (
              <span className="animate-blink rounded-full px-[7px] py-px text-[11px] font-semibold text-warn">●</span>
            )}
          </button>
        </div>

          <div className="side-scroll min-h-0 flex-1 overflow-y-auto overscroll-contain bg-canvas">
            {tab === 'conversation'
            ? <Transcript messages={messages} thinking={thinking} />
            : <ResearchPanel key={research.agentRunId ?? 'idle'} research={research} />}
          </div>
      </aside>

      <div ref={audioMount} className="absolute h-0 w-0 overflow-hidden [&_audio]:hidden" />
    </main>
  )
}
