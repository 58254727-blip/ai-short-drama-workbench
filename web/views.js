import {escapeHtml} from './api.js';

export const viewNames = {director:'编导',shots:'分镜',assets:'素材',jobs:'任务',review:'校核',export:'导出',settings:'设置'};

export function heading(title, subtitle, action = '') {
  return `<div class="heading"><div><h1>${escapeHtml(title)}</h1><p>${escapeHtml(subtitle)}</p></div>${action}</div>`;
}

export function section(title, body, extraClass = '') {
  return `<section class="section ${extraClass}"><div class="section-heading">${escapeHtml(title)}</div><div class="section-content">${body}</div></section>`;
}

export function empty(title, detail, action = '') {
  return `<div class="empty"><strong>${escapeHtml(title)}</strong><span>${escapeHtml(detail)}</span>${action}</div>`;
}

export function setView(nav) {
  document.querySelectorAll('.sidebar [data-view]').forEach(button => {
    button.classList.toggle('selected', button.dataset.view === nav);
    button.setAttribute('aria-current', button.dataset.view === nav ? 'page' : 'false');
  });
}
