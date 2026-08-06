import { Send } from 'lucide-react'

// Text only. The microphone lives on the visualiser disc above, not here.
export default function Composer({ value, onChange, onSend, connected, disabled }) {
  const unavailable = disabled || !value.trim()

  return (
    <div className="flex h-[50px] w-full items-center gap-2 rounded-2xl border border-line bg-surface pr-1.5 pl-4 shadow-[0_1px_2px_rgba(15,23,42,0.04)] transition-[border-color,box-shadow] duration-200 focus-within:border-accent/50 focus-within:shadow-[0_0_0_3px_rgba(37,99,235,0.10)]">
      <input
        value={value}
        onChange={event => onChange(event.target.value)}
        onKeyDown={event => {
          if (event.key === 'Enter' && !event.nativeEvent.isComposing) onSend()
        }}
        placeholder={connected ? '質問を入力' : '入力すると接続します'}
        aria-label="質問を入力"
        className="min-w-0 flex-1 border-0 bg-transparent text-[14px] text-ink outline-none placeholder:text-muted"
      />
      <button
        type="button"
        onClick={() => {
          if (!unavailable) onSend()
        }}
        aria-disabled={unavailable}
        aria-label="送信"
        className={`grid h-9 w-9 flex-none place-items-center rounded-xl border transition-[background-color,box-shadow,transform] duration-150 hover:scale-[1.06] active:scale-[0.92] ${
          unavailable
            ? 'cursor-not-allowed border-line bg-line-soft text-muted shadow-none'
            : 'border-transparent bg-accent text-white shadow-[0_1px_2px_rgba(0,91,221,0.22)] hover:bg-[#004BB8] hover:shadow-[0_3px_8px_rgba(0,91,221,0.24)]'
        }`}
      >
        <Send size={16} strokeWidth={2.2} color={unavailable ? 'currentColor' : '#FFFFFF'} />
      </button>
    </div>
  )
}
