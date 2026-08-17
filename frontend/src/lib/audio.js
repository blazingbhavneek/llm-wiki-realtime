// Web Audio analysis that drives the orb.
//
// Both the local mic and the agent's remote track are attached as
// MediaStreamSources -- never as MediaElementSources. createMediaElementSource
// hijacks an element's output and would silence the agent unless we re-route it
// to the destination ourselves; MediaStreamSource taps the track without
// touching playback at all.

export const BAND_COUNT = 32

// These thresholds are deliberately above ordinary headphone-mic room tone.
// Opening needs two consecutive animation frames so a keyboard tick cannot
// trigger a turn; closing is delayed so normal gaps between words stay intact.
const VOICE_GATE_OPEN_RMS = 0.035
const VOICE_GATE_CLOSE_RMS = 0.022
const VOICE_GATE_ATTACK_FRAMES = 2
const VOICE_GATE_RELEASE_FRAMES = 18

export class AudioBus {
  constructor() {
    this.ctx = null
    this.sources = new Map()
    this.bands = new Float32Array(BAND_COUNT)
    this.userLevel = 0
    this.agentLevel = 0
    this.devLoopback = null
    this.voiceGate = null
  }

  _ctx() {
    if (!this.ctx) {
      const Ctor = window.AudioContext || window.webkitAudioContext
      if (!Ctor) return null
      this.ctx = new Ctor()
    }
    if (this.ctx.state === 'suspended') this.ctx.resume().catch(() => {})
    return this.ctx
  }

  // key is 'mic' or 'agent'. Passing the same key twice replaces the old tap.
  attach(key, mediaStreamTrack) {
    const ctx = this._ctx()
    if (!ctx || !mediaStreamTrack) return false
    this.detach(key)
    try {
      const stream = new MediaStream([mediaStreamTrack])
      const node = ctx.createMediaStreamSource(stream)
      const analyser = ctx.createAnalyser()
      analyser.fftSize = 512
      analyser.smoothingTimeConstant = 0.72
      node.connect(analyser)
      this.sources.set(key, {
        node,
        analyser,
        data: new Uint8Array(analyser.frequencyBinCount),
        waveform: new Uint8Array(analyser.fftSize),
      })
      return true
    } catch {
      // A track that ended between subscribe and attach throws here. Ignore.
      return false
    }
  }

  detach(key, { restoreVoiceGate = true } = {}) {
    if (key === 'mic') this.disableVoiceGate({ restoreTrack: restoreVoiceGate })
    this._detach(key)
  }

  _detach(key) {
    const source = this.sources.get(key)
    if (!source) return
    try { source.node.disconnect() } catch { /* already gone */ }
    this.sources.delete(key)
  }

  // Send silence to LiveKit until the microphone has crossed a speech-level
  // threshold. The analyser uses a clone: disabling the published track would
  // otherwise also silence the signal used to decide when to reopen the gate.
  enableVoiceGate(mediaStreamTrack) {
    if (!mediaStreamTrack?.clone) return false
    this.disableVoiceGate()
    try {
      const analysisTrack = mediaStreamTrack.clone()
      if (!this.attach('mic', analysisTrack)) {
        analysisTrack.stop()
        return false
      }
      mediaStreamTrack.enabled = false
      this.voiceGate = {
        track: mediaStreamTrack,
        analysisTrack,
        open: false,
        aboveFrames: 0,
        belowFrames: 0,
      }
      return true
    } catch {
      return false
    }
  }

  disableVoiceGate({ restoreTrack = true } = {}) {
    const gate = this.voiceGate
    if (!gate) return
    this.voiceGate = null
    this._detach('mic')
    try { gate.analysisTrack.stop() } catch { /* already ended */ }
    if (restoreTrack) {
      try { gate.track.enabled = true } catch { /* already ended */ }
    }
  }

  _updateVoiceGate(rms) {
    const gate = this.voiceGate
    if (!gate || gate.track.readyState !== 'live') return

    // Once open, use a lower close threshold so a voice does not chatter the
    // gate around a single boundary while syllables naturally soften.
    const aboveThreshold = gate.open
      ? rms >= VOICE_GATE_CLOSE_RMS
      : rms >= VOICE_GATE_OPEN_RMS
    if (aboveThreshold) {
      gate.aboveFrames += 1
      gate.belowFrames = 0
      if (!gate.open && gate.aboveFrames >= VOICE_GATE_ATTACK_FRAMES) {
        gate.open = true
        gate.track.enabled = true
      }
      return
    }

    gate.aboveFrames = 0
    gate.belowFrames += 1
    if (gate.open && gate.belowFrames >= VOICE_GATE_RELEASE_FRAMES) {
      gate.open = false
      gate.track.enabled = false
    }
  }

  // Development-only loopback. The delayed stream is sent both to the local
  // speakers and back through the normal `agent` analyser, so the exact same
  // incoming-audio visualization path used by LiveKit is exercised locally.
  startDevLoopback(mediaStream, delaySeconds = 1) {
    const ctx = this._ctx()
    if (!ctx || !mediaStream) return false
    this.stopDevLoopback()

    try {
      const source = ctx.createMediaStreamSource(mediaStream)
      const delay = ctx.createDelay(Math.max(2, delaySeconds + 0.5))
      const gain = ctx.createGain()
      const delayedStream = ctx.createMediaStreamDestination()
      delay.delayTime.setValueAtTime(delaySeconds, ctx.currentTime)
      gain.gain.setValueAtTime(0.82, ctx.currentTime)

      source.connect(delay)
      delay.connect(gain)
      gain.connect(ctx.destination)
      delay.connect(delayedStream)

      const delayedTrack = delayedStream.stream.getAudioTracks()[0]
      this.attach('agent', delayedTrack)
      this.devLoopback = { source, delay, gain, delayedStream, delayedTrack }
      return true
    } catch {
      this.stopDevLoopback()
      return false
    }
  }

  stopDevLoopback() {
    const loopback = this.devLoopback
    if (!loopback) return
    this.detach('agent')
    try { loopback.source.disconnect() } catch { /* already gone */ }
    try { loopback.delay.disconnect() } catch { /* already gone */ }
    try { loopback.gain.disconnect() } catch { /* already gone */ }
    try { loopback.delayedTrack.stop() } catch { /* already gone */ }
    this.devLoopback = null
  }

  close() {
    this.stopDevLoopback()
    this.disableVoiceGate({ restoreTrack: false })
    for (const key of [...this.sources.keys()]) this.detach(key)
    this.ctx?.close().catch(() => {})
    this.ctx = null
  }

  // Called once per animation frame. Folds the linear FFT into log-spaced bands
  // so low frequencies (where speech lives) get the resolution they deserve.
  sample() {
    this.bands.fill(0)
    let user = 0
    let agent = 0
    let micRms = 0

    for (const [key, source] of this.sources) {
      const { analyser, data, waveform } = source
      analyser.getByteFrequencyData(data)
      analyser.getByteTimeDomainData(waveform)
      const binCount = data.length

      // Time-domain RMS is far more reliable for speech than averaging the
      // whole FFT (most high-frequency bins are naturally empty). A small
      // noise gate keeps open-mic room tone from animating the silhouette.
      let squareSum = 0
      for (let i = 0; i < waveform.length; i++) {
        const sample = (waveform[i] - 128) / 128
        squareSum += sample * sample
      }
      const rms = Math.sqrt(squareSum / waveform.length)
      if (key === 'mic') micRms = rms
      const level = Math.min(1, Math.max(0, (rms - 0.012) * 7.5))
      if (key === 'mic') user = level
      else agent = Math.max(agent, level)

      for (let b = 0; b < BAND_COUNT; b++) {
        const lo = Math.floor((b / BAND_COUNT) ** 1.7 * binCount)
        const hi = Math.max(lo + 1, Math.floor(((b + 1) / BAND_COUNT) ** 1.7 * binCount))
        let peak = 0
        for (let i = lo; i < hi && i < binCount; i++) peak = Math.max(peak, data[i])
        const energy = Math.min(1, Math.max(0, (peak / 255 - 0.025) * 1.9))
        this.bands[b] = Math.max(this.bands[b], energy)
      }
    }

    this._updateVoiceGate(micRms)
    this.userLevel = user
    this.agentLevel = agent
    return this
  }
}
