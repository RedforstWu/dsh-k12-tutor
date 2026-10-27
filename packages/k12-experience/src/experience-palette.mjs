const defaults = {
  light: { background: '#ffffff', surface: '#f8fafc', ink: '#0f172a', muted: '#475569', border: '#94a3b8', accent: '#1d4ed8' },
  dark: { background: '#171c24', surface: '#202731', ink: '#e8edf3', muted: '#bdc8d6', border: '#526277', accent: '#f0c272' },
};
export function experiencePalette(snapshot) {
  const mode = snapshot.active.colorScheme;
  const tokens = snapshot.active.tokens;
  const fallback = defaults[mode];
  const keys = { background: '--dsw-alias-bg-base', surface: '--dsw-alias-bg-layer-1', ink: '--dsw-alias-label-primary',
    muted: '--dsw-alias-label-secondary', border: '--dsw-alias-border-l3', accent: '--dsw-alias-brand-text' };
  return { colorScheme: mode, ...Object.fromEntries(Object.entries(keys).map(([name, key]) =>
    [`--k12-${name}`, /^#(?:[\da-f]{3}|[\da-f]{6})$/i.test(tokens[key] || '') ? tokens[key] : fallback[name]])) };
}
export function createExperiencePalette(theme, onChange) {
  let palette = experiencePalette(theme.getTheme());
  const listeners = new Set();
  const unsubscribe = onChange(snapshot => { palette = experiencePalette(snapshot); listeners.forEach(listener => listener()); });
  return { getSnapshot: () => palette, subscribe(listener) { listeners.add(listener); return () => listeners.delete(listener); },
    dispose() { unsubscribe(); listeners.clear(); } };
}
