export const defaultPreferences = () => ({
  themeId: 'classic', characterId: 'none', narrativeLevel: 'light',
  voice: { enabled: false, mode: 'browser', rate: 1, voiceURI: '', autoRead: false },
  reducedMotion: true, largeText: false, interests: [],
});

export function requestGate() {
  let epoch = 0;
  return {
    begin() { const ticket = ++epoch; return () => ticket === epoch; },
    invalidate() { epoch++; },
  };
}

export async function navigateStudent({ rpc, navigation, binding, sessionId, isCurrent, stop }) {
  stop();
  const target = await rpc('target', { bindingId: binding.bindingId, ...(sessionId ? { fromSessionId: sessionId } : {}) });
  if (!isCurrent()) return false;
  if (target.binding?.bindingId !== binding.bindingId || target.workspaceId !== binding.workspaceId
    || target.workspacePath !== binding.workspacePath || !target.workspaceId) {
    throw new Error('学生工作区不匹配，请重新连接。');
  }
  if (target.sessionId) navigation.openSession(target.sessionId);
  else await navigation.openWorkspace(target.workspaceId);
  return isCurrent();
}
