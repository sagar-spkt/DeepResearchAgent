export async function api(path, options = {}) {
  const response = await fetch(`/api${path}`, {credentials: 'same-origin', ...options, headers: {...(options.body && !(options.body instanceof FormData) ? {'Content-Type': 'application/json'} : {}), ...options.headers}});
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw Object.assign(new Error(typeof error.detail === 'string' ? error.detail : `Request failed (${response.status})`), {status: response.status});
  }
  if (response.status === 204) return null;
  return response.json();
}
export const list = value => Array.isArray(value) ? value : (value?.items || []);
export const terminal = status => ['completed', 'failed', 'cancelled'].includes(status);
export function validateUpload(file) {
  if (!file) return 'Choose a document.';
  if (file.size > 20 * 1024 * 1024) return 'Documents must be 20 MB or smaller.';
  if (!/\.(pdf|docx|txt|md|markdown)$/i.test(file.name)) return 'Use PDF, DOCX, TXT, or Markdown.';
  return null;
}

export function safeConsoleUrl(value, applicationUrl) {
  try {
    const app = new URL(applicationUrl), target = new URL(value);
    if (app.protocol !== 'https:' || target.protocol !== 'https:' || target.hostname !== `observability.${app.hostname}` || target.port || target.username || target.password || target.pathname !== '/' || target.search || target.hash) return null;
    return target.href;
  } catch {return null;}
}
