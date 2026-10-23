import React, { useSyncExternalStore } from 'react';
import { Experience } from './Experience.jsx';
import { readStudySkin } from '../../k12-whiteboard/src/study-skin.mjs';

export const name = 'k12-experience';
export const inject = ['slots', 'connection', 'uiWorkspace', 'sessions', 'theme'];
export function apply(ctx) {
  const rpc = async (action, payload = {}) => {
    const result = await ctx.connection.rpc.call('/api', `k12.experience.${action}`, payload);
    if (!result.ok) {
      const messages = {
        'k12/network': '无法连接学习站点。请检查网络后重试。',
        'k12/auth': '学生授权已失效。请家长重新连接这位学生。',
        'k12/service': '学习站点暂时不可用，请稍后重试。',
        'k12/expired': '身份验证已过期，请重新验证。',
        'k12/active': '当前会话仍在执行任务，请等待结束后切换。',
        'k12/unbound': '当前会话未绑定学生，请先选择学生。',
      };
      const error = new Error(messages[result.error?.code] || '请求未完成，请检查连接后重试。');
      error.safeExperience = true;
      throw error;
    }
    return result.value;
  };
  const props = sessionId => ({ sessionId, rpc, navigation: ctx.uiWorkspace, studySkin: () => readStudySkin(document, ctx.theme) });
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
}
