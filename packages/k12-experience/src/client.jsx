import React, { useSyncExternalStore } from 'react';
import { Experience } from './Experience.jsx';
import { readStudySkin } from '../../k12-whiteboard/src/study-skin.mjs';
import { createSpeech, observeSpeech } from './speech.mjs';
import { createExperiencePalette } from './experience-palette.mjs';
import { ReplySpeech } from './ReplySpeech.jsx';
import { installSpeechFenceHiding } from './speech-fence.mjs';

export const name = 'k12-experience';
export const inject = ['slots', 'connection', 'uiWorkspace', 'uiConversation', 'sessions', 'theme'];
export function apply(ctx) {
  const rpc = async (action, payload = {}, signal) => {
    const result = await ctx.connection.rpc.call('/api', `k12.experience.${action}`, payload, signal);
    if (!result.ok) {
      const messages = {
        'k12/network': '无法连接学习站点。请检查网络后重试。',
        'k12/auth': '学生授权已失效。请家长重新连接这位学生。',
        'k12/service': '学习站点暂时不可用，请稍后重试。',
        'k12/expired': '身份验证已过期，请重新验证。',
        'k12/active': '当前会话仍在执行任务，请等待结束后切换。',
        'k12/unbound': '当前会话未绑定学生，请先选择学生。',
        'k12/speech': '微软在线语音合成暂不可用，请检查网络后重试。',
      };
      const error = new Error(messages[result.error?.code] || '请求未完成，请检查连接后重试。');
      error.safeExperience = true;
      throw error;
    }
    return result.value;
  };
  const palette = createExperiencePalette(ctx.theme, listener => ctx.on('theme/change', listener));
  const speech = createSpeech({ loadPreferences: sessionId => rpc('preferences', { sessionId }),
    synthesizeOnline: (input, signal) => rpc('speech', input, signal) });
  ctx.provide('k12Speech', speech);
  let currentSession;
  let currentChat;
  let unobserve = () => {};
  const activate = () => {
    const snapshot = ctx.sessions.list.getSnapshot();
    const sessionId = Object.values(snapshot.byId).find(row => (row.retainedBy.mainView || 0) > 0)?.id;
    if (sessionId === currentSession) return;
    unobserve(); currentSession = sessionId; currentChat = undefined; speech.activate(sessionId);
    if (!sessionId) return;
    void speech.load(sessionId).catch(() => {});
    const binding = ctx.sessions.binding(sessionId);
    if (binding) {
      currentChat = ctx.uiConversation.binding(binding).target('chat');
      unobserve = observeSpeech({ sessionId, source: binding.eventSource, speech, chat: currentChat });
    }
  };
  ctx.effect(() => {
    activate();
    const unhide = installSpeechFenceHiding(document, () => currentChat?.getSnapshot());
    const unsubscribe = ctx.sessions.list.subscribe(activate);
    return () => { unsubscribe(); unobserve(); unhide(); speech.dispose(); palette.dispose(); };
  });
  ctx.on('connection/reset', () => { speech.stop(); speech.activate(undefined); currentSession = undefined; currentChat = undefined; activate(); });
  const props = sessionId => ({ sessionId, rpc, speech, palette, navigation: ctx.uiWorkspace, studySkin: () => readStudySkin(document, ctx.theme) });
  function WorkspaceExperience() {
    const snapshot = useSyncExternalStore(ctx.sessions.list.subscribe, ctx.sessions.list.getSnapshot);
    const current = Object.values(snapshot.byId).find(row => (row.retainedBy.mainView || 0) > 0);
    return <Experience {...props(current?.id)} />;
  }
  ctx.effect(() => ctx.slots.inject('conversation.session.header.utilities', () => ctx.slots.register({
    name: 'conversation.session.header.utilities', id: name, order: 5, inject: props,
  }, Experience)));
  ctx.effect(() => ctx.slots.inject('sidebar.footer.action', () => ctx.slots.register({
    name: 'sidebar.footer.action', id: name, order: 5, inject: () => ({}),
  }, WorkspaceExperience)));
  ctx.effect(() => ctx.slots.inject('conversation.chat.assistant-actions', () => ctx.slots.register({
    name: 'conversation.chat.assistant-actions', id: 'k12-speech', order: 15, inject: sessionId => ({ sessionId, speech }),
  }, ReplySpeech)));
}
