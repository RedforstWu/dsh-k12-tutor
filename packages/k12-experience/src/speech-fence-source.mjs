import { fromMarkdown } from 'mdast-util-from-markdown';

export function speechFenceCodes(text) {
  const codes = [];
  const visit = (node, parent) => {
    if (node.type === 'code') {
      const offset = node.position.start.offset;
      const lineStart = text.lastIndexOf('\n', offset - 1) + 1;
      const opening = /^ {0,3}(`{3,}|~{3,})(.*)$/.exec(text.slice(lineStart).split(/\r?\n/, 1)[0]);
      codes.push({ value: node.value, language: /^[\w-]+/.exec(node.lang || '')?.[0],
        speech: Boolean(parent.type === 'root' && opening && opening[2].trim().split(/\s+/, 1)[0] === 'speech') });
    }
    for (const child of node.children || []) visit(child, node);
  };
  visit(fromMarkdown(text));
  return codes;
}
