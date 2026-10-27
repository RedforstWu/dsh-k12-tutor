import React, { useSyncExternalStore } from 'react';
import { assistantText } from './speech.mjs';

export function ReplySpeech({ sessionId, messageId, speech, useChat }) {
  const text = useChat(chat => assistantText(chat, messageId));
  const state = useSyncExternalStore(speech.subscribe, speech.getSnapshot);
  const owner = `message:${sessionId}:${messageId}`;
  const playing = state.kind === 'speaking' && state.owner === owner;
  const starting = playing && state.phase === 'starting';
  const label = starting ? '取消准备朗读' : playing ? '停止朗读回复' : '朗读回复';
  if (!text) return null;
  return <><button type="button" className={`k12-reply-speech${speech.reducedMotion(sessionId) ? ' k12-still' : ''}`} aria-label={label} title={starting ? '正在准备语音，点击取消' : label} aria-pressed={playing} aria-busy={starting}
    onClick={() => {
      speech.arm();
      if (playing) speech.stop(owner);
      else void speech.speak({ sessionId, owner, kind: 'message', text });
    }}>{starting ? <span className="k12-speech-loading" aria-hidden="true" /> : <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false">
      {playing ? <rect x="3.5" y="3.5" width="9" height="9" rx="1" /> : <>
        <path d="M7.5 2.5 4 5.5H1.5v5H4l3.5 3z" />
        <path d="M10 5a4 4 0 0 1 0 6M12 2.5a7 7 0 0 1 0 11" />
      </>}
    </svg>}</button>
    {!starting && state.notice && state.sessionId === sessionId && (state.owner === owner || state.errorOwner === owner) && <span role="status">{state.notice}</span>}
    {state.errorOwner === owner && state.retryVoice && <button type="button" className="k12-reply-speech-retry" onClick={() => void speech.retryLocal(owner)}>本次使用 {state.retryVoice.name}（本地）重试</button>}</>;
}
