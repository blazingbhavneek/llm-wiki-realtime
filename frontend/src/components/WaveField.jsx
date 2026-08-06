import { useEffect, useRef } from 'react'

// A circular speech visualiser: the guiddy interference field rendered inside a
// disc whose rim breathes and ripples with the audio spectrum.
//
// The disc is the microphone control -- there is no separate mic button. Its
// surface keeps a calm wave at rest; mic and agent audio increase the contrast
// and deform the rim.
//
// Plain WebGL1 with scalar uniforms only (no arrays, no dynamic indexing) so it
// compiles everywhere without a fallback path.

const VERTEX_SHADER = [
  'attribute vec2 a_position;',
  'void main(){ gl_Position = vec4(a_position, 0.0, 1.0); }',
].join('\n')

const FRAGMENT_SHADER = [
  'precision highp float;',
  '',
  'uniform float u_time;',
  'uniform vec2  u_resolution;',
  'uniform vec3  u_accent;',
  'uniform float u_level;',
  'uniform float u_think;',
  'uniform float u_listen;',
  'uniform float u_b0;',
  'uniform float u_b1;',
  'uniform float u_b2;',
  'uniform float u_b3;',
  'uniform float u_b4;',
  'uniform float u_b5;',
  '',
  'const vec3 PALE  = vec3(0.710, 0.840, 0.985);',
  'const vec3 WHITE = vec3(0.985, 0.990, 1.000);',
  '',
  'float hash21(vec2 p){',
  '  p = fract(p * vec2(123.34, 456.21));',
  '  p += dot(p, p + 45.32);',
  '  return fract(p.x * p.y);',
  '}',
  '',
  'float noise2(vec2 p){',
  '  vec2 i = floor(p);',
  '  vec2 f = fract(p);',
  '  f = f * f * (3.0 - 2.0 * f);',
  '  float a = hash21(i);',
  '  float b = hash21(i + vec2(1.0, 0.0));',
  '  float c = hash21(i + vec2(0.0, 1.0));',
  '  float d = hash21(i + vec2(1.0, 1.0));',
  '  return mix(mix(a, b, f.x), mix(c, d, f.x), f.y);',
  '}',
  '',
  'float fbm(vec2 p){',
  '  float value = 0.0;',
  '  float amplitude = 0.55;',
  '  mat2 turn = mat2(0.80, -0.60, 0.60, 0.80);',
  '  for(int i = 0; i < 4; i++){',
  '    value += amplitude * noise2(p);',
  '    p = turn * p * 2.03 + vec2(7.1, 3.7);',
  '    amplitude *= 0.48;',
  '  }',
  '  return value;',
  '}',
  '',
  'void main(){',
  '  vec2 uv = gl_FragCoord.xy / u_resolution - 0.5;',
  '  float aspect = u_resolution.x / u_resolution.y;',
  '  uv.x *= aspect;',
  '',
  '  float t = u_time;',
  '  float e = u_level;',
  '  float r = length(uv);',
  '',
  '  // Keep the silhouette genuinely round. Audio changes the whole radius',
  '  // together, rather than putting noisy or high-frequency lobes on its rim.',
  '  float spectrum = (u_b0 + u_b1 + u_b2 + u_b3 + u_b4 + u_b5) / 6.0;',
  '  float R = 0.425 + 0.003 * sin(t * 0.62)',
  '                  + 0.014 * e + 0.002 * spectrum;',
  '',
  '  // Domain-warped light inside the orb continues moving at total silence.',
  '  vec2 flow = uv * (3.0 + 0.9 * e);',
  '  float warp = fbm(flow + vec2(t * 0.19, -t * 0.14));',
  '  float field = fbm(flow + vec2(warp * 1.45, -warp * 1.10)',
  '                    + vec2(-t * 0.11, t * 0.16));',
  '  float sheen = fbm(uv * 5.2 + vec2(t * 0.10, t * 0.07));',
  '  vec3 coolTone = mix(u_accent, vec3(0.18, 0.52, 0.98), 0.28);',
  '  vec3 tone = mix(coolTone, u_accent, u_listen);',
  '  vec3 base = mix(PALE, vec3(1.0, 0.975, 0.975), u_listen);',
  '  vec3 col = mix(base, tone, smoothstep(0.30, 0.78, field));',
  '  col = mix(col, WHITE, smoothstep(0.57, 0.92, sheen) * 0.52);',
  '  col = mix(col, tone, e * smoothstep(0.38, 0.84, warp) * 0.34);',
  '  col = mix(col, WHITE, smoothstep(0.22, 0.48, r) * 0.12);',
  '  col = mix(col, vec3(0.66, 0.82, 1.0), u_think * (0.08 + 0.08 * warp));',
  '',
  '  // Soft, luminous alpha replaces the hard outlined contour.',
  '  float px = 1.0 / min(u_resolution.x, u_resolution.y);',
  '  float feather = max(0.0045, 2.0 * px);',
  '  float mask = 1.0 - smoothstep(R - feather, R + feather, r);',
  '  float innerEdge = smoothstep(R - 0.055, R, r);',
  '  vec3 edge = mix(vec3(0.68, 0.83, 1.0), WHITE, u_listen);',
  '  col = mix(col, edge, innerEdge * (0.22 + 0.10 * u_listen));',
  '  gl_FragColor = vec4(col, mask);',
  '}',
].join('\n')

// Accent colour per conversational state, in the hero's blue family.
const ACCENTS = {
  dormant:   [0.000, 0.357, 0.867],  // #005BDD
  listening: [0.545, 0.012, 0.012],  // deep blood red
  thinking:  [0.486, 0.227, 0.929],  // #7C3AED
  speaking:  [0.146, 0.388, 0.922],  // #2563EB
}

// Slice boundaries over the AudioBus's 32 log-spaced bands, low to high.
const BAND_SLICES = [[0, 3], [3, 7], [7, 12], [12, 18], [18, 25], [25, 32]]

function compile(gl, type, source) {
  const shader = gl.createShader(type)
  gl.shaderSource(shader, source)
  gl.compileShader(shader)
  if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
    gl.deleteShader(shader)
    return null
  }
  return shader
}

// Average a slice of the 32 log-spaced bands from the AudioBus.
function bandAverage(bands, from, to) {
  let sum = 0
  for (let i = from; i < to; i++) sum += bands[i]
  return sum / (to - from)
}

export default function WaveField({ bus, state }) {
  const canvasRef = useRef(null)
  // Read through refs so the render loop never restarts on a state change --
  // restarting would reset the colour easing mid-transition.
  const stateRef = useRef(state)
  const busRef = useRef(bus)
  useEffect(() => {
    stateRef.current = state
    busRef.current = bus
  })

  useEffect(() => {
    const canvas = canvasRef.current
    const gl = canvas.getContext('webgl', {
      antialias: true,
      alpha: true,
      premultipliedAlpha: false,
    })
    if (!gl) return undefined

    const vertex = compile(gl, gl.VERTEX_SHADER, VERTEX_SHADER)
    const fragment = compile(gl, gl.FRAGMENT_SHADER, FRAGMENT_SHADER)
    if (!vertex || !fragment) return undefined

    const program = gl.createProgram()
    gl.attachShader(program, vertex)
    gl.attachShader(program, fragment)
    gl.linkProgram(program)
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) return undefined
    gl.useProgram(program)

    const buffer = gl.createBuffer()
    gl.bindBuffer(gl.ARRAY_BUFFER, buffer)
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW)
    const position = gl.getAttribLocation(program, 'a_position')
    gl.enableVertexAttribArray(position)
    gl.vertexAttribPointer(position, 2, gl.FLOAT, false, 0, 0)

    gl.enable(gl.BLEND)
    gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA)
    gl.clearColor(0, 0, 0, 0)

    const u = name => gl.getUniformLocation(program, name)
    const loc = {
      time: u('u_time'), res: u('u_resolution'), accent: u('u_accent'),
      level: u('u_level'), think: u('u_think'), listen: u('u_listen'),
      bands: BAND_SLICES.map((_, i) => u(`u_b${i}`)),
    }

    const dpr = Math.min(window.devicePixelRatio || 1, 2)
    const resize = () => {
      const rect = canvas.getBoundingClientRect()
      canvas.width = Math.max(1, Math.round(rect.width * dpr))
      canvas.height = Math.max(1, Math.round(rect.height * dpr))
    }

    const accent = [...ACCENTS.dormant]
    const bands = new Float32Array(BAND_SLICES.length)
    const smooth = { level: 0, think: 0, listen: 0 }
    const started = performance.now()
    let raf = 0

    resize()
    const observer = new ResizeObserver(resize)
    observer.observe(canvas)

    const ease = (current, target, rate) => current + (target - current) * rate
    // Attack fast, release slow: speech snaps the disc open and lets it settle
    // rather than flickering with every FFT frame.
    const envelope = (current, target) => ease(current, target, target > current ? 0.40 : 0.08)

    const frame = () => {
      raf = requestAnimationFrame(frame)

      const active = stateRef.current
      const audio = busRef.current?.sample()

      // The clock never stops: silence still has a calm, legible surface wave.
      const clock = ((performance.now() - started) / 1000) * 0.52

      const target = ACCENTS[active.mode] || ACCENTS.dormant
      for (let i = 0; i < 3; i++) accent[i] = ease(accent[i], target[i], 0.035)

      const liveLevel = audio ? Math.max(audio.userLevel, audio.agentLevel) : 0
      // Give ordinary speech enough travel to visibly reshape the rim without
      // making keyboard noise or room tone pulse the disc.
      smooth.level = envelope(smooth.level, Math.min(1, liveLevel * 1.18))
      smooth.think = ease(smooth.think, active.thinking ? 1 : 0, 0.025)
      smooth.listen = ease(smooth.listen, active.mode === 'listening' ? 1 : 0, 0.045)
      for (let i = 0; i < BAND_SLICES.length; i++) {
        const [from, to] = BAND_SLICES[i]
        bands[i] = envelope(bands[i], audio ? bandAverage(audio.bands, from, to) : 0)
      }

      gl.viewport(0, 0, canvas.width, canvas.height)
      gl.clear(gl.COLOR_BUFFER_BIT)
      gl.uniform1f(loc.time, clock)
      gl.uniform2f(loc.res, canvas.width, canvas.height)
      gl.uniform3f(loc.accent, accent[0], accent[1], accent[2])
      gl.uniform1f(loc.level, smooth.level)
      gl.uniform1f(loc.think, smooth.think)
      gl.uniform1f(loc.listen, smooth.listen)
      for (let i = 0; i < BAND_SLICES.length; i++) gl.uniform1f(loc.bands[i], bands[i])
      gl.drawArrays(gl.TRIANGLES, 0, 3)
    }
    frame()

    return () => {
      cancelAnimationFrame(raf)
      observer.disconnect()
      try {
        gl.deleteProgram(program)
        gl.deleteBuffer(buffer)
      } catch { /* context already lost */ }
    }
  }, [])

  return (
    <canvas
      ref={canvasRef}
      className="block h-full w-full drop-shadow-[0_10px_18px_rgba(37,99,235,0.16)]"
      aria-hidden="true"
    />
  )
}
