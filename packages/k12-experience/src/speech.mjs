import { defaultPreferences } from './client-state.mjs';
import { speechFenceCodes } from './speech-fence-source.mjs';
import onlineVoices from '../../../engine/k12_engine/online_voices.json' with { type: 'json' };

export const supportedVoices = voices => voices.filter(voice => /^(?:zh|en)(?:[-_]|$)/i.test(voice.lang || ''));
const onlineVoice = uri => onlineVoices.find(voice => voice.voiceURI === uri || voice.aliases.includes(uri));
export function speechVoices(voices, online) {
  const native = supportedVoices(voices);
  return online ? [...onlineVoices.map(voice => ({ ...voice, localService: false, onlineVoice: voice.id })),
    ...native.filter(voice => !onlineVoice(voice.voiceURI) && !/^Microsoft .* Online \(Natural\)/i.test(voice.name || voice.voiceURI))] : native;
}
export function findSpeechVoice(voices, uri) {
  return voices.find(voice => voice.voiceURI === uri)
    || voices.find(voice => voice.onlineVoice && voice.onlineVoice === onlineVoice(uri)?.id);
}

function withoutCode(text) {
  let fence;
  const visible = [];
  for (const line of text.split(/\r?\n/)) {
    const marker = line.match(/^ {0,3}(`{3,}|~{3,})(.*)$/);
    if (fence) {
      if (marker && marker[1][0] === fence[0] && marker[1].length >= fence.length && !marker[2].trim()) fence = undefined;
      continue;
    }
    if (marker && (marker[1][0] !== '`' || !marker[2].includes('`'))) {
      fence = marker[1];
    } else visible.push(line);
  }
  return visible.join('\n');
}

export function spokenText(text) {
  return withoutCode(text)
    .replace(/!\[[^\]]*\]\([^)]*\)/g, '').replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    .replace(/https?:\/\/\S+/g, '').replace(/\\frac\{([^{}]+)\}\{([^{}]+)\}/g, '$2 分之 $1')
    .replace(/(\*\*|__)(?=\S)([^\n]*?\S)\1/g, '$2')
    .replace(/(?<![a-zA-Z0-9])\*([^\s*](?:[^*\n]*[^\s*])?)\*(?![a-zA-Z0-9])/g, '$1').replace(/[_`$]/g, '')
    .replace(/\b(\d+(?:\.\d+)?|[a-zA-Z])[ \t]*\*[ \t]*(?=[+-]?(?:\d|[a-zA-Z]\b))/g, '$1 乘 ')
    .replace(/([a-zA-Z0-9])\^2\b/g, '$1 的平方').replace(/([a-zA-Z0-9])\^3\b/g, '$1 的立方')
    .replace(/^\s*(?:(?:#{1,6}|[-*+]|\d+[.)])[ \t]+|>[ \t]?)*/gm, '').trim();
}

export function replyNarration(text) {
  const blocks = Array.isArray(text) ? text : [text];
  const speech = blocks.flatMap(block => speechFenceCodes(block).filter(code => code.speech).map(code => code.value));
  return speech.length ? speech.join('\n').trim() : blocks.map(spokenText).filter(Boolean).join('\n');
}

function assistantTextBlocks(chat, messageId) {
  const node = chat?.nodes.values().find(node => node.data?.status === 'settled'
    && node.data.finalNode?.messageId === messageId && node.visibility === 'visible');
  return (node?.data.blocks || []).filter(block => block.kind === 'text').map(block => block.text);
}

export const assistantText = (chat, messageId) => replyNarration(assistantTextBlocks(chat, messageId));

export function createSpeech({ synthesis = window.speechSynthesis, Utterance = window.SpeechSynthesisUtterance,
  loadPreferences, synthesizeOnline, AudioContext = globalThis.AudioContext || globalThis.webkitAudioContext,
  startupTimeoutMs = 10000, onlineTimeoutMs = 65000 }) {
  const preferences = new Map();
  const listeners = new Set();
  let state = { kind: 'idle', voices: speechVoices(synthesis?.getVoices() || [], Boolean(synthesizeOnline)), notice: '' };
  let audioContext;
  let active;
  let requested;
  let failed;
  let boardOwner;
  let activeSession;
  let epoch = 0;
  let armed = false;
  const emit = patch => { state = { ...state, ...patch }; listeners.forEach(listener => listener()); };
  const voicesChanged = () => emit({ voices: speechVoices(synthesis.getVoices(), Boolean(synthesizeOnline)) });
  synthesis?.addEventListener('voiceschanged', voicesChanged);
  const stop = (owner, keepBoard = false) => {
    if (!keepBoard && (!owner || boardOwner === owner)) boardOwner = undefined;
    if (owner && active?.owner !== owner && requested?.owner !== owner) return;
    epoch++;
    requested = undefined;
    failed = undefined;
    const prior = active;
    active = undefined;
    if (prior) { clearTimeout(prior.timer); if (prior.cancel) prior.cancel(); else synthesis.cancel(); prior.resolve('canceled'); }
    emit({ kind: 'idle', phase: undefined, owner: undefined, sessionId: undefined, notice: '', errorOwner: undefined, retryVoice: undefined });
  };
  const service = {
    getSnapshot: () => state,
    subscribe(listener) { listeners.add(listener); return () => listeners.delete(listener); },
    async load(sessionId) {
      if (!preferences.has(sessionId)) {
        const value = await loadPreferences(sessionId);
        if (!preferences.has(sessionId)) service.setPreferences(sessionId, value);
      }
      return preferences.get(sessionId).voice;
    },
    setPreferences(sessionId, value) {
      const voice = { ...defaultPreferences().voice, ...value?.voice };
      preferences.set(sessionId, { voice, reducedMotion: value?.reducedMotion ?? defaultPreferences().reducedMotion });
      if (active?.sessionId === sessionId) stop();
      emit({});
    },
    preferences: sessionId => preferences.get(sessionId)?.voice,
    reducedMotion: sessionId => preferences.get(sessionId)?.reducedMotion ?? defaultPreferences().reducedMotion,
    activate(sessionId) {
      if (activeSession !== sessionId) { stop(); activeSession = sessionId; armed = false; }
    },
    arm() {
      armed = true;
      if (synthesizeOnline && AudioContext) {
        try { audioContext ||= new AudioContext(); void audioContext.resume().catch(() => {}); } catch {}
      }
    },
    isArmed: () => armed,
    stop,
    reserveBoard(owner) { stop(); boardOwner = owner; },
    releaseBoard(owner) { if (boardOwner === owner) boardOwner = undefined; },
    retryLocal(owner) {
      if (!failed || state.errorOwner !== owner || failed.sessionId !== activeSession || !state.retryVoice) return Promise.resolve('canceled');
      const request = failed;
      const voice = state.retryVoice;
      service.arm();
      return service.speak({ ...request, voiceURI: voice.voiceURI });
    },
    async speak({ sessionId, owner, kind, text, automatic = false, voiceURI }) {
      if (automatic && (boardOwner || active?.kind === 'board')) return 'canceled';
      stop(undefined, true);
      const ticket = epoch;
      requested = { sessionId, owner };
      emit({ kind: 'speaking', phase: 'starting', owner, sessionId, notice: '正在准备朗读…' });
      let voicePrefs;
      try { voicePrefs = await service.load(sessionId); }
      catch {
        if (ticket === epoch) emit({ kind: 'idle', phase: undefined, notice: '无法读取语音设置，请重新打开学习空间。', sessionId, errorOwner: owner });
        return 'unavailable';
      }
      if (ticket !== epoch || activeSession !== sessionId) return 'canceled';
      requested = undefined;
      if (automatic && (!armed || !voicePrefs.autoRead || active?.kind === 'board')) {
        emit({ kind: 'idle', phase: undefined, owner: undefined, notice: '' });
        return 'canceled';
      }
      const voice = findSpeechVoice(state.voices, voiceURI || voicePrefs.voiceURI);
      if (!voicePrefs.enabled) {
        emit({ kind: 'idle', phase: undefined, notice: '请先在学习空间启用浏览器朗读并选择音色。', sessionId, errorOwner: owner });
        return 'disabled';
      }
      if (!voice || (!voice.onlineVoice && (!synthesis || !Utterance))) {
        emit({ kind: 'idle', phase: undefined, notice: '请在学习空间选择当前设备可用的语音音色。', sessionId, errorOwner: owner });
        return 'unavailable';
      }
      const content = kind === 'message' ? text.trim() : spokenText(text);
      if (!content) { emit({ kind: 'idle', phase: undefined, notice: '', owner: undefined }); return 'ended'; }
      const version = epoch;
      if (voice.onlineVoice) {
        emit({ notice: `正在生成${voice.name}在线语音…` });
        return new Promise(resolve => {
          const controller = new AbortController();
          let source, sourceStarted = false;
          const run = { sessionId, owner, kind, resolve, cancel() {
            controller.abort();
            if (source) { source.onended = null; if (sourceStarted) source.stop(); source.disconnect(); }
          } };
          const current = () => active === run && epoch === version;
          const finish = (result, notice = '') => {
            if (!current()) return;
            clearTimeout(run.timer); active = undefined;
            if (result !== 'ended') run.cancel();
            else source?.disconnect();
            emit({ kind: 'idle', phase: undefined, owner: undefined, sessionId, notice,
              errorOwner: result === 'unavailable' ? owner : undefined, retryVoice: undefined });
            resolve(result);
          };
          active = run;
          run.timer = setTimeout(() => finish('unavailable', '微软在线语音生成超时，请重试（online-timeout）。'), onlineTimeoutMs);
          void (async () => {
            try {
              if (!audioContext) throw new Error('请再次点击试听以启用音频播放（audio-permission）。');
              await audioContext.resume();
              if (!current()) return;
              const audio = await synthesizeOnline({ sessionId, voice: voice.onlineVoice, text: content, rate: voicePrefs.rate }, controller.signal);
              if (!current()) return;
              if (audio?.voice !== voice.onlineVoice || audio.mime !== 'audio/mpeg' || typeof audio.audioBase64 !== 'string'
                || audio.audioBase64.length > 2800000) throw new Error('在线语音返回无效音频（online-audio）。');
              const bytes = Uint8Array.from(atob(audio.audioBase64), value => value.charCodeAt(0));
              const buffer = await audioContext.decodeAudioData(bytes.buffer);
              if (!current()) return;
              source = audioContext.createBufferSource(); source.buffer = buffer; source.connect(audioContext.destination);
              source.onended = () => finish('ended');
              if (audioContext.state !== 'running') throw new Error('浏览器尚未允许音频播放，请再次点击试听（audio-permission）。');
              clearTimeout(run.timer); source.start(); sourceStarted = true; emit({ phase: 'speaking', notice: '' });
            } catch (error) {
              if (current()) finish('unavailable', error.name === 'NotAllowedError' ? '浏览器未允许播放，请再次点击试听（audio-permission）。' : error.safeExperience ? error.message :
                /audio-permission|online-audio/.test(error.message || '') ? error.message : '微软在线语音暂不可用，请检查网络后重试（online-service）。');
            }
          })();
        });
      }
      return new Promise(resolve => {
        const utterance = new Utterance(content);
        utterance.voice = voice; utterance.lang = voice.lang; utterance.rate = voicePrefs.rate;
        const finish = (result, code) => {
          if (active?.utterance !== utterance || epoch !== version) return;
          clearTimeout(active.timer);
          active = undefined;
          let notice = '';
          let retryVoice;
          if (result === 'unavailable') {
            const causes = {
              network: '在线音色的网络请求失败', 'synthesis-failed': '所选音色合成失败',
              'synthesis-unavailable': '浏览器语音服务不可用', 'not-allowed': '浏览器未允许开始播放，请再次点击朗读',
              'voice-unavailable': '所选音色已不可用，请重新选择', 'language-unavailable': '此音色不支持当前语言',
              'audio-busy': '音频设备正忙', 'audio-hardware': '音频设备不可用', 'text-too-long': '朗读内容过长',
              'startup-timeout': '音色在 10 秒内未开始播放',
            };
            notice = `“${voice.name}”：${causes[code] || '浏览器朗读失败'}（${code}）。`;
            failed = { sessionId, owner, kind, text, automatic: false };
            retryVoice = state.voices.find(item => item.localService && item.voiceURI !== voice.voiceURI
              && item.lang.split(/[-_]/)[0].toLowerCase() === voice.lang.split(/[-_]/)[0].toLowerCase());
          }
          emit({ kind: 'idle', phase: undefined, owner: undefined, notice, sessionId,
            errorOwner: result === 'unavailable' ? owner : undefined, retryVoice });
          resolve(result);
        };
        utterance.onstart = () => {
          if (active?.utterance !== utterance || epoch !== version) return;
          clearTimeout(active.timer);
          emit({ phase: 'speaking', notice: '' });
        };
        utterance.onend = () => finish('ended');
        utterance.onerror = event => finish(['canceled', 'interrupted'].includes(event.error) ? 'canceled' : 'unavailable', event.error);
        active = { sessionId, owner, kind, utterance, resolve };
        active.timer = setTimeout(() => {
          if (active?.utterance !== utterance || epoch !== version) return;
          finish('unavailable', 'startup-timeout');
          synthesis.cancel();
        }, startupTimeoutMs);
        try {
          if (synthesis.paused) synthesis.resume();
          synthesis.speak(utterance);
        } catch { finish('unavailable', 'synthesis-failed'); }
      });
    },
    dispose() { stop(); synthesis?.removeEventListener('voiceschanged', voicesChanged); if (audioContext) void audioContext.close().catch(() => {}); listeners.clear(); },
  };
  return service;
}

export function observeSpeech({ sessionId, source, speech, chat }) {
  let revision = source.getSnapshot().revision;
  let generation = 0;
  const turns = new Map();
  const unsubscribe = source.subscribe(() => {
    const window = source.getSnapshot();
    if (window.revision === revision) return;
    const contiguous = window.revision === revision + 1;
    revision = window.revision;
    if (!contiguous || window.change.kind === 'replace') { generation++; turns.clear(); speech.stop(); return; }
    const entries = window.change.kind === 'append' ? window.change.entries
      : window.change.kind === 'settle-assistant' && window.change.entry ? [window.change.entry] : [];
    for (const { event } of entries) {
      if (event.type === 'user/message') { generation++; speech.stop(); }
      if (event.type === 'turn/start') {
        const voice = speech.preferences(sessionId);
        if (speech.isArmed() && voice?.enabled && voice.autoRead) turns.set(event.data.turn, []);
      }
      if (event.type === 'assistant/message' && !event.data.interrupted && turns.has(event.data.turn)) {
        turns.get(event.data.turn).push(event.data.message.id);
      }
      if (event.type === 'turn/end') {
        const ids = turns.get(event.data.turn);
        turns.delete(event.data.turn);
        if (event.data.reason.kind !== 'completed') { generation++; speech.stop(); continue; }
        const admitted = generation;
        if (ids?.length) queueMicrotask(() => {
          if (admitted !== generation) return;
          const snapshot = chat.getSnapshot();
          const text = replyNarration(ids.flatMap(id => assistantTextBlocks(snapshot, id)));
          void speech.speak({ sessionId, owner: `message:${sessionId}:${ids.at(-1)}`, kind: 'message', text, automatic: true });
        });
      }
    }
  });
  return () => { generation++; turns.clear(); unsubscribe(); };
}
