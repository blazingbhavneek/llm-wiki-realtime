import { useEffect, useRef } from 'react'

export default function Transcript({ messages, thinking }) {
  const endRef = useRef(null)

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [messages.length])

  if (messages.length === 0) {
    return (
      <div className="animate-chat-panel max-w-[380px] px-[26px] py-11">
        <p className="m-0 mb-2.5 text-sm">まだ会話はありません。</p>
        <p className="m-0 text-[13px] leading-[1.8] text-faint">マイクを押して話しかけるか、左下の入力欄から質問してください。</p>
      </div>
    )
  }

  return (
    <div className="flex flex-col gap-3 px-[22px] pt-[22px] pb-10">
      {messages.map((message, i) => (
        <div
          key={i}
          className={`animate-chat-message max-w-[88%] whitespace-pre-wrap rounded-2xl px-[15px] py-3 text-sm leading-[1.75] transition-[transform,box-shadow] duration-300 hover:-translate-y-px ${
            message.role === 'user'
              ? 'self-end rounded-br-[5px] bg-accent font-medium text-white hover:shadow-[0_4px_12px_rgba(37,99,235,0.16)]'
              : 'self-start rounded-bl-[5px] border border-line bg-surface text-ink hover:shadow-[0_4px_12px_rgba(15,23,42,0.05)]'
          }`}
        >
          {message.interrupted && <span className="mb-1 block text-[11px] font-bold tracking-[0.08em] text-warn">途中で中断</span>}
          {message.text}
        </div>
      ))}
      {thinking && (
        <div className="animate-chat-message chat-typing self-start rounded-2xl rounded-bl-[5px] border border-line bg-surface px-3.5 py-3" role="status" aria-label="考えています">
          <span className="chat-typing-dot" />
          <span className="chat-typing-dot" />
          <span className="chat-typing-dot" />
        </div>
      )}
      <div ref={endRef} />
    </div>
  )
}
