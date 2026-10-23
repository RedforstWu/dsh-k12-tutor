import { parseStudySkin, withStudySkin } from './study-skin.mjs';

// Keep owner-authenticated task launches in this browser, including embedded browsers that block popups.
export function openStudyLink(event, location, preference) {
  if (event.defaultPrevented) return false;
  const anchor = (event.target?.closest ? event.target : event.target?.parentElement)?.closest('a[href]');
  if (!anchor) return false;
  let url;
  try { url = new URL(anchor.href, location.href); } catch { return false; }
  if (anchor.hasAttribute?.('data-k12-study-link')) {
    anchor.href = withStudySkin(url.href, preference);
    return false;
  }
  if (url.origin !== location.origin || url.pathname !== '/api/k12.study.open' || url.hash || url.username || url.password
    || [...url.searchParams.keys()].some(key => !['sessionId', 'taskId', 'dshSkin', 'dshAppearance'].includes(key))
    || ['sessionId', 'taskId'].some(key => url.searchParams.getAll(key).length !== 1 || !url.searchParams.get(key))) return false;
  try { parseStudySkin(url.searchParams); } catch { return false; }
  anchor.href = withStudySkin(url.href, preference);
  if (event.type === 'contextmenu' || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return false;
  event.preventDefault();
  event.stopPropagation();
  location.assign(anchor.href);
  return true;
}
