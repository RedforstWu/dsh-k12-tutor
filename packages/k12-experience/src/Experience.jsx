import React, { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { defaultPreferences, navigateStudent, requestGate } from './client-state.mjs';
import { findSpeechVoice } from './speech.mjs';
import { Practice } from './Practice.jsx';
import { withStudySkin } from '../../k12-whiteboard/src/study-skin.mjs';
import characterRegistry from '../../../characters/registry.json';

const themes = [['classic', '经典课堂'], ['voxel-builder', '方块建造'], ['space-explorer', '星际探索']];
const characters = Object.entries(characterRegistry).map(([id, character]) => [id, character.name]);
const blank = () => ({ bindings: [], current: null, preferences: defaultPreferences(), workspaces: [] });

export function Experience({ sessionId, rpc, navigation, studySkin, speech, palette }) {
  const colors = useSyncExternalStore(palette.subscribe, palette.getSnapshot);
  const audio = useSyncExternalStore(speech.subscribe, speech.getSnapshot);
  const [open, setOpen] = useState(false);
  const [data, setData] = useState(blank);
  const [pending, setPending] = useState('');
  const [error, setError] = useState('');
  const [verification, setVerification] = useState(null);
  const [result, setResult] = useState(null);
  const [connect, setConnect] = useState(false);
  const token = useRef(null);
  const gate = useRef(requestGate());
  const dialog = useRef(null);
  const previewOwner = `preview:${sessionId}`;
  const previewing = audio.kind === 'speaking' && audio.owner === previewOwner;
  const stop = () => {
    speech.stop();
    window.dispatchEvent(new CustomEvent('k12-experience:before-switch', { detail: { sessionId } }));
  };
  const load = async isCurrent => {
    const value = await rpc('list', sessionId ? { sessionId } : {});
    if (isCurrent()) {
      const preferences = { ...defaultPreferences(), ...value.preferences,
        voice: { ...defaultPreferences().voice, ...value.preferences?.voice } };
      setData({ ...blank(), ...value, preferences });
      if (sessionId) speech.setPreferences(sessionId, preferences);
    }
  };
  const run = async (key, work) => {
    const isCurrent = gate.current.begin();
    setPending(key); setError('');
    try { await work(isCurrent); }
    catch (error) { if (isCurrent()) setError(error.safeExperience ? error.message : '操作未完成。请检查站点、授权和网络后重试。'); }
    finally { if (isCurrent()) setPending(''); }
  };
  useEffect(() => {
    setOpen(false); setData(blank()); setVerification(null); setResult(null); setPending(''); setError('');
    if (token.current) token.current.value = '';
    void run('load', load);
    return () => { gate.current.invalidate(); speech.stop(`preview:${sessionId}`); };
  }, [sessionId]);
  useEffect(() => {
    if (open) dialog.current?.showModal();
    else dialog.current?.close();
  }, [open]);
  const close = () => {
    gate.current.invalidate(); setPending(''); setVerification(null); setResult(null);
    if (token.current) token.current.value = '';
    setOpen(false); speech.stop(`preview:${sessionId}`);
  };
  const prefs = data.preferences;
  const save = preferences => run('preferences', async isCurrent => {
    await rpc('preferences', { sessionId, preferences });
    if (isCurrent()) { setData(old => ({ ...old, preferences })); speech.setPreferences(sessionId, preferences); }
  });
  const verify = event => {
    event.preventDefault();
    const form = event.currentTarget;
    const payload = { siteOrigin: form.elements.siteOrigin.value.trim(), token: token.current.value,
      ...(form.elements.workspaceId.value ? { workspaceId: form.elements.workspaceId.value } : {}) };
    token.current.value = '';
    setVerification(null);
    void run('verify', async isCurrent => {
      const value = await rpc('verify', payload);
      if (isCurrent()) setVerification(value);
    });
  };
  const openSite = (event, pathname) => {
    try {
      const url = new URL(event.currentTarget.form.elements.siteOrigin.value.trim());
      if ((url.protocol !== 'https:' && !(url.protocol === 'http:' && ['127.0.0.1', 'localhost'].includes(url.hostname))) ||
        url.username || url.password || url.pathname !== '/' || url.search || url.hash) throw new Error();
      window.open(withStudySkin(`${url.origin}${pathname}`, studySkin?.()), '_blank', 'noopener,noreferrer');
    } catch { setError('请填写 HTTPS 站点地址，本地测试可使用 localhost。'); }
  };
  const choose = binding => run('switch', async isCurrent => {
    setResult(null); setVerification(null);
    await navigateStudent({ rpc, navigation, binding, sessionId, isCurrent, stop });
    if (isCurrent()) { setOpen(false); await load(isCurrent); }
  });
  const study = action => {
    const prompts = {
      question: '我有一道题。请先让我描述题目和已经尝试的方法，再一步步引导我。',
      concept: '我想理解一个数学概念。请先问我想学什么、已经知道什么，再用例子引导我。',
      resource: '请带我尝试一个适合当前学习进度的资源变化任务。先让我预测，再操作观察、写出等量关系并验算，最后做一道不带情境的同构题。',
    };
    setResult(prompts[action]);
  };
  const speak = () => {
    speech.arm();
    void speech.speak({ sessionId, owner: `preview:${sessionId}`, kind: 'preview',
      text: '你好，我是 AI 学习导师。你可以提出问题，解释一个概念，或尝试资源任务。' });
  };
  const selectedVoice = findSpeechVoice(audio.voices, prefs.voice.voiceURI);
  const availableVoice = Boolean(selectedVoice);
  return <>
    <button style={colors} className="k12-experience-trigger" title="学习空间与学生切换" onClick={() => { setOpen(true); void run('load', load); }}>
      {data.current?.studentName || '学习空间'}</button>
    <dialog style={colors} ref={dialog} onCancel={event => { event.preventDefault(); close(); }} className={`k12-experience k12-${prefs.themeId}${prefs.largeText ? ' k12-large' : ''}${prefs.reducedMotion ? ' k12-still' : ''}`} aria-label="家庭学习空间">
      <div className="k12-experience-heading"><h2>学习空间</h2><button onClick={close} aria-label="关闭学习空间">关闭</button></div>
      <p className="k12-trust">家庭信任模式 · 家长监督使用，未隔离家庭成员权限</p>
      <p aria-live="polite">{sessionId ? '当前学生：' : '请选择学生进入对应会话：'}<strong>{data.current?.studentName || '未选择'}</strong>{data.current?.grade ? ` · ${data.current.grade}` : ''}{data.current?.currentBookId ? ` · ${data.current.currentBookId}` : ''}</p>
      {error && <p role="alert">{error}</p>}
      {pending && <p role="status">正在处理…</p>}
      <fieldset disabled={Boolean(pending)}><legend>选择学生</legend>
        <div className="k12-students">{data.bindings.map(binding => <button key={binding.bindingId} onClick={() => choose(binding)} aria-current={binding.bindingId === data.current?.bindingId ? 'true' : undefined}>
          {binding.studentName}{binding.grade ? ` · ${binding.grade}` : ''}{binding.bindingId === data.current?.bindingId ? '（当前）' : ''}
        </button>)}</div>
        {!data.bindings.length && <p>由家长连接学生后开始学习。</p>}
        <button onClick={() => setConnect(!connect)}>{connect ? '收起连接' : '家长连接学生'}</button>
      </fieldset>
      {connect && <section><h3>家长连接</h3><p>先在学习站点登录，为对应孩子签发 Agent 令牌。这里不需要站点登录密码。</p>
        <form onSubmit={verify}><fieldset disabled={Boolean(pending)}>
          <label>站点地址<input name="siteOrigin" type="url" required placeholder="https://学习站点" autoComplete="off" /></label>
          <button type="button" onClick={event => openSite(event, '/login')}>打开站点登录</button>
          <button type="button" onClick={event => openSite(event, '/settings')}>打开站点设置</button>
          <label>学生 Agent 令牌<input ref={token} name="studentToken" type="password" required autoComplete="new-password" spellCheck={false} /></label>
          <label>学生工作区<select name="workspaceId"><option value="">创建独立学生工作区</option>{data.workspaces.map(w => <option key={w.workspaceId || w.id} value={w.workspaceId || w.id}>{w.name || w.title || w.path}</option>)}</select></label>
          <button type="submit">验证学生身份</button>
        </fieldset></form>
        {verification && <div className="k12-confirm"><p>站点：{verification.siteOrigin}</p><p>已验证学生：<strong>{verification.student.displayName}</strong>{verification.student.grade ? ` · ${verification.student.grade}` : ''}{verification.student.currentBookId ? ` · ${verification.student.currentBookId}` : ''}</p>
          <button disabled={Boolean(pending)} onClick={() => run('confirm', async isCurrent => {
            await rpc('confirm', { verificationId: verification.verificationId, expectedStudentId: verification.student.id });
            if (isCurrent()) { setVerification(null); setConnect(false); await load(isCurrent); }
          })}>确认连接这位学生</button><button disabled={Boolean(pending)} onClick={() => setVerification(null)}>取消</button></div>}
      </section>}
      {sessionId && <>
        {!data.current && <p>本地体验。练习保存在当前工作区，不同步到学习站点。</p>}
        <section className="k12-start"><h3>今天从哪里开始</h3>
          <fieldset disabled={Boolean(pending)}><button onClick={() => study('question')}>我有一道题</button><button onClick={() => study('concept')}>解释概念</button><button onClick={() => study('resource')}>试试资源任务</button></fieldset>
          {result && <div className="k12-study-result"><p>复制这段开场到会话输入框，补充你的问题后发送。</p><textarea aria-label="学习开场" readOnly value={result} rows={4} /><button onClick={async () => {
            try { await navigator.clipboard.writeText(result); setError(''); }
            catch { setError('请选中开场文字，手动复制到会话输入框。'); }
          }}>复制开场</button></div>}
        </section>
        {open && <Practice key={sessionId} sessionId={sessionId} rpc={rpc} />}
        <details><summary>兴趣与显示</summary><fieldset disabled={Boolean(pending)}>
          <label>主题<select aria-label="主题" value={prefs.themeId} onChange={e => save({ ...prefs, themeId: e.target.value })}>{themes.map(([id, text]) => <option key={id} value={id}>{text}</option>)}</select></label>
          <label>AI 导师角色<select aria-label="AI 导师角色" value={prefs.characterId} onChange={e => save({ ...prefs, characterId: e.target.value })}>{characters.map(([id, text]) => <option key={id} value={id}>{text}</option>)}</select></label>
          <label>情境表达<select aria-label="情境表达" value={prefs.narrativeLevel} onChange={e => save({ ...prefs, narrativeLevel: e.target.value })}><option value="off">关闭</option><option value="light">轻量</option></select></label>
          {['building', 'exploration'].map((id, index) => <label key={id}><input type="checkbox" checked={prefs.interests.includes(id)} onChange={e => save({ ...prefs, interests: e.target.checked ? [...prefs.interests, id] : prefs.interests.filter(x => x !== id) })} />{['建造', '探索'][index]}</label>)}
          <label><input type="checkbox" checked={prefs.largeText} onChange={e => save({ ...prefs, largeText: e.target.checked })} />大字</label>
          <label><input type="checkbox" checked={prefs.reducedMotion} onChange={e => save({ ...prefs, reducedMotion: e.target.checked })} />减少动画</label>
          <label><input type="checkbox" checked={prefs.voice.enabled} onChange={e => { speech.arm(); save({ ...prefs, voice: { ...prefs.voice, enabled: e.target.checked } }); }} />语音朗读（可选）</label>
          <label>语音音色<select aria-label="语音音色" value={selectedVoice?.voiceURI || prefs.voice.voiceURI} onChange={e => { speech.arm(); save({ ...prefs, voice: { ...prefs.voice, voiceURI: e.target.value } }); }}>
            <option value="">请选择音色</option>
            {prefs.voice.voiceURI && !availableVoice && <option value={prefs.voice.voiceURI}>已选音色标识不可用，请重新选择</option>}
            {audio.voices.map(voice => <option key={voice.voiceURI} value={voice.voiceURI}>{voice.name} · {voice.lang} · {voice.localService ? '本地' : '在线'}</option>)}
          </select></label>
          {selectedVoice?.onlineVoice && <p>所选音色由微软在线服务合成，需要联网。</p>}
          {!audio.voices.length && <p role="status">当前设备尚无可用的中文或英文语音，请检查系统语音包后重新打开。</p>}
          <label><input type="checkbox" checked={prefs.voice.autoRead} onChange={e => { speech.arm(); save({ ...prefs, voice: { ...prefs.voice, autoRead: e.target.checked } }); }} />自动朗读新回复（本次使用需先试听）</label>
          <label>语速<input type="range" min="0.5" max="2" step="0.1" value={prefs.voice.rate} onChange={e => save({ ...prefs, voice: { ...prefs.voice, rate: Number(e.target.value) } })} /></label>
        </fieldset></details>
        {prefs.characterId !== 'none' && <img className="k12-character" src={`/api/k12-experience/assets/${characterRegistry[prefs.characterId].states.listening}`} alt={`${characterRegistry[prefs.characterId].name}，AI 学习导师`} />}
        <div className="k12-voice"><button disabled={!prefs.voice.enabled || !availableVoice} aria-busy={previewing && audio.phase === 'starting'} onClick={() => previewing ? speech.stop(previewOwner) : speak()}>{previewing && audio.phase === 'starting' && <span className="k12-speech-loading" aria-hidden="true" />}{previewing ? (audio.phase === 'starting' ? '取消准备' : '停止试听') : '试听导师介绍'}</button><button onClick={() => speech.stop(previewOwner)}>停止朗读</button></div>
        {audio.phase !== 'starting' && audio.notice && audio.sessionId === sessionId && <p role="status">{audio.notice}</p>}
        {audio.errorOwner === previewOwner && audio.retryVoice && <button onClick={() => void speech.retryLocal(previewOwner)}>本次使用 {audio.retryVoice.name}（本地）重试</button>}
        {data.current && <details><summary>管理连接</summary><p>{data.current.siteOrigin}</p><p>移除本机绑定不会吊销站点令牌，请家长在站点设置中吊销授权。</p><button disabled={Boolean(pending)} onClick={() => run('unlink', async isCurrent => {
          stop(); await rpc('unlink', { bindingId: data.current.bindingId }); if (isCurrent()) await load(isCurrent);
        })}>解除当前学生连接</button></details>}
      </>}
    </dialog>
  </>;
}
