import { ChevronDown, Volume2 } from 'lucide-react'

// Hidden entirely when the active TTS_PROVIDER has nothing to offer
// (GET /tts/voices -> supports_selection: false, or an empty voice list).
export default function VoiceSelect({ voices, value, onChange }) {
  if (!voices.length) return null

  return (
    <label className="relative flex items-center gap-1.5 rounded-lg border border-line bg-surface py-1 pr-6 pl-2 text-[12px] font-medium text-muted transition-colors hover:border-accent/30">
      <Volume2 size={14} strokeWidth={1.8} className="text-faint" aria-hidden="true" />
      <span className="sr-only">音声を選択</span>
      <select
        value={value}
        onChange={event => onChange(event.target.value)}
        className="cursor-pointer appearance-none bg-transparent text-ink outline-none"
      >
        <option value="">デフォルト</option>
        {voices.map(voiceId => (
          <option key={voiceId} value={voiceId}>{voiceId}</option>
        ))}
      </select>
      <ChevronDown size={12} strokeWidth={2} className="pointer-events-none absolute right-2 text-faint" aria-hidden="true" />
    </label>
  )
}
