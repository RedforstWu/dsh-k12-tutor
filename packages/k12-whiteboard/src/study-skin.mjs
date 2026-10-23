const skins = new Set(['default', 'minecraft', 'lagrange']);
const appearances = new Set(['light', 'dark']);

export function parseStudySkin(params) {
  const skin = params.getAll('dshSkin');
  const appearance = params.getAll('dshAppearance');
  if (!skin.length && !appearance.length) return undefined;
  if (skin.length !== 1 || appearance.length !== 1 || !skins.has(skin[0]) || !appearances.has(appearance[0])) {
    throw new Error('invalid study skin');
  }
  return { skin: skin[0], appearance: appearance[0] };
}

export function withStudySkin(href, preference) {
  if (!preference) return href;
  const url = new URL(href);
  if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password) return href;
  url.searchParams.set('dshSkin', preference.skin);
  url.searchParams.set('dshAppearance', preference.appearance);
  return url.href;
}

export function readStudySkin(document, theme) {
  const skin = document.body.getAttribute('data-dsh-game-skin');
  return {
    skin: skins.has(skin) ? skin : 'default',
    appearance: theme.getTheme().active.colorScheme,
  };
}
