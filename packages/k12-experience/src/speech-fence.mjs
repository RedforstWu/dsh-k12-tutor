import { speechFenceCodes } from './speech-fence-source.mjs';

const rowSelector = '[data-chat-flow-kind="assistant-step"][data-chat-node-key]';
const excluded = '[data-chat-group-part="reasoning"], [data-variant="think"], [data-tool], .genui-dom-fence, [data-genui-surface]';
const marker = 'data-k12-speech-only';

function sourceCodes(node) {
  const codes = [];
  for (const block of node.data.blocks) {
    if (block.kind !== 'text') continue;
    codes.push(...speechFenceCodes(block.text).filter(code =>
      node.data.status === 'running' || code.language !== 'math' || code.value === ''));
  }
  return codes;
}

function surfaces(row) {
  return [...row.querySelectorAll('.md-code-block, pre > code')]
    .map(element => element.matches('code') ? element.parentElement : element)
    .filter(element => element.closest(rowSelector) === row && !element.closest(excluded)
      && !element.parentElement.closest('.md-code-block'));
}

export function installSpeechFenceHiding(document, getChatSnapshot) {
  const marked = new Set();
  const sweep = rows => {
    let chat;
    try { chat = getChatSnapshot(); } catch { return; }
    for (const row of rows) {
      if (row.closest(excluded)) continue;
      const node = chat?.nodes.get(row.getAttribute('data-chat-node-key'));
      if (node?.kind !== 'assistant-step') continue;
      const codes = sourceCodes(node);
      for (const [index, element] of surfaces(row).entries()) {
        const code = codes[index];
        const content = element.querySelector('pre > code') || element.querySelector('code');
        if (!code || !content || content.textContent !== code.value) continue;
        if (code.speech) {
          element.setAttribute(marker, '');
          marked.add(element);
        } else {
          element.removeAttribute(marker);
          marked.delete(element);
        }
      }
    }
    for (const element of marked) if (!element.isConnected || element.closest(excluded)
      || !element.closest(rowSelector)) {
      element.removeAttribute(marker);
      marked.delete(element);
    }
  };
  const observer = new document.defaultView.MutationObserver(records => {
    const rows = new Set();
    const collect = node => {
      const element = node.nodeType === 1 ? node : node.parentElement;
      if (!element) return;
      const row = element.closest(rowSelector);
      if (row) rows.add(row);
      for (const descendant of element.querySelectorAll(rowSelector)) rows.add(descendant);
    };
    for (const record of records) {
      collect(record.target);
      for (const node of record.addedNodes) collect(node);
    }
    sweep(rows);
  });
  observer.observe(document.documentElement, { subtree: true, childList: true, characterData: true,
    attributes: true, attributeFilter: ['class', 'data-chat-node-key', 'data-chat-flow-kind', 'data-chat-group-part', 'data-variant', 'data-tool'] });
  sweep(document.querySelectorAll(rowSelector));
  return () => {
    observer.disconnect();
    for (const element of marked) element.removeAttribute(marker);
    marked.clear();
  };
}
