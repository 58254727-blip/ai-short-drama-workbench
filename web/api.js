export async function api(path, options = {}) {
  const headers = {'X-Jingxu-Request': '1', ...(options.headers || {})};
  let body = options.body;
  if (body !== undefined && !(body instanceof Blob)) {
    headers['Content-Type'] = 'application/json';
    body = JSON.stringify(body);
  }
  const response = await fetch(path, {method: options.method || 'GET', headers, body, credentials: 'same-origin'});
  if (!response.ok) {
    let payload;
    try { payload = await response.json(); } catch { payload = {}; }
    const error = new Error(payload.error?.message || `请求失败（${response.status}）`);
    error.code = payload.error?.code || 'http_error';
    error.status = response.status;
    throw error;
  }
  const type = response.headers.get('Content-Type') || '';
  return type.includes('application/json') ? response.json() : response;
}

export function asciiJsonHeader(value) {
  return JSON.stringify(value).replace(/[\u007f-\uffff]/g, character =>
    `\\u${character.charCodeAt(0).toString(16).padStart(4, '0')}`);
}

export const byId = id => document.getElementById(id);
export const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, character => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[character]));
export const option = (value, label, selected = false) => `<option value="${escapeHtml(value)}"${selected ? ' selected' : ''}>${escapeHtml(label)}</option>`;
export const field = (label, id, value = '', type = 'text') => `<label class="field">${escapeHtml(label)}<input id="${id}" type="${type}" value="${escapeHtml(value)}"></label>`;
export const area = (label, id, value = '') => `<label class="field">${escapeHtml(label)}<textarea id="${id}">${escapeHtml(value)}</textarea></label>`;
