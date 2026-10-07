import {api, byId, escapeHtml, option} from './api.js';
import {heading, empty} from './views.js';
import {formatTime, buildDialogue} from './state.js';

function selectedShot(ctx) {
  return ctx.shots.find(shot => shot.id === ctx.shotId) || ctx.shots[0];
}

export function renderShots(ctx) {
  const shot = selectedShot(ctx);
  const demo = ctx.project?.title === '雨夜来客（演示）';
  const candidate = shot && ctx.assets.find(asset => asset.id === shot.selected_candidate_id);
  const references = shot?.asset_version_ids || [];
  const speakers = [...new Set(ctx.shots.flatMap(item => item.dialogue.map(line => line.speaker_id)))];
  const preview = candidate?.binary_available ? `<video controls preload="metadata" src="/api/projects/${ctx.project.id}/assets/${candidate.id}/media"></video>`
    : candidate ? `<span class="preview-empty">已选视频文件缺失，请从备份恢复或重连素材</span>`
    : demo ? `<img src="/assets/rain-alley-demo.png" alt="雨巷演示静帧"><span class="still-label">演示静帧 · 非生成视频</span>`
      : `<span class="preview-empty">尚未选择视频素材</span>`;
  const clips = ctx.timeline?.items || [];
  const body = heading('分镜工作台', '把每一个镜头，接成一场戏。', '<button id="add-shot" class="primary">＋ 新增镜头</button>') +
    `<div class="desk"><section class="section shot-list"><div class="section-heading">镜头列表</div>${ctx.shots.length ? ctx.shots.map((item, index) => `<button class="shot-row ${shot?.id === item.id ? 'active' : ''}" data-shot="${item.id}"><span class="num">${String(index + 1).padStart(3, '0')}</span><span>${escapeHtml(item.story_job || '未命名镜头')}</span><span class="tag">${item.selected_candidate_id ? '已选片' : '草稿'}</span></button>`).join('') : empty('还没有镜头', '点击右上方新增镜头。')}</section>
    <section class="section"><div class="preview">${preview}</div><div class="preview-note">${candidate?.binary_available ? '正在预览当前选片；任务成功不代表人工验收。' : candidate ? '选片记录仍在，但原文件不可用。' : demo ? '此画面仅为明确创建的虚构演示静帧；暂无可播放视频。' : '画面预算仅供规划；暂无实际视频。'}</div><div class="middle-form">
    ${editorInput('镜头任务', 'shot-story', shot?.story_job)}${editorInput('起始状态', 'shot-start', shot?.start_state)}${editorInput('主要动作', 'shot-action', shot?.action)}${editorInput('结束状态', 'shot-end', shot?.end_state)}${editorInput('转场与声音', 'shot-transition', shot?.transition)}</div></section>
    <section class="section"><div class="tabs"><button class="active" type="button">镜头</button><button id="assets-tab" type="button">资产</button></div><div class="inspector">
    <label class="field">所属场景<select id="shot-scene"><option value="">未指定场景</option>${ctx.scenes.map(scene => option(scene.id, scene.title, scene.id === shot?.scene_id)).join('')}</select></label>
    <div class="field-grid"><label class="field">画面时长预算（秒）<input id="screen-seconds" type="number" min="0" step="0.1" value="${shot ? shot.screen_duration_ms / 1000 : 4}"></label><label class="field">生成时长预算（秒）<input id="generation-seconds" type="number" min="0" step="0.1" value="${shot ? shot.generation_duration_ms / 1000 : 6}"></label></div>
    <div class="field">原生对白<div id="dialogue-rows">${(shot?.dialogue?.length ? shot.dialogue : [{speaker_id:'',text:''}]).map(line => dialogueRow(line, speakers)).join('')}</div><button id="add-dialogue" type="button">新增对白行</button></div>
    <label class="field">参考素材（可多选）<select id="reference-assets" multiple size="4">${ctx.assets.filter(asset => asset.kind !== 'video').map(asset => option(asset.id, `${asset.kind} · ${asset.rights?.source || '未命名'}`, references.includes(asset.id))).join('')}</select></label>
    ${ctx.settings.video_requires_first_frame ? `<label class="field">视频工作流首帧<select id="first-frame"><option value="">请选择已保存的参考画面</option>${ctx.assets.filter(asset => asset.kind === 'image' && references.includes(asset.id)).map(asset => option(asset.id, asset.rights?.source || '画面素材')).join('')}</select></label>` : ''}
    <label class="field">候选视频<select id="candidate-select"><option value="">尚未选片</option>${ctx.assets.filter(asset => asset.kind === 'video').map(asset => option(asset.id, `${asset.rights?.source || '视频'}${asset.rights?.shot_id && asset.rights.shot_id !== shot?.id ? ' · 来自其他镜头，需明确复用' : ''} · ${asset.id.slice(0, 8)}`, asset.id === shot?.selected_candidate_id)).join('')}</select></label>
    <div class="actions"><button id="save-shot" class="primary" ${shot ? '' : 'disabled'}>保存修改</button><button id="select-candidate" ${shot ? '' : 'disabled'}>明确选片</button><button id="queue-h3" ${shot ? '' : 'disabled'}>加入队列</button><button id="queue-text" ${shot ? '' : 'disabled'}>请求文本建议</button></div></div></section></div>
    <section class="section timeline"><div class="section-heading">选片与时间轴 <span class="hint">${clips.length ? '实际片段' : '尚无已保存的实际片段'}</span></div>${clips.length ? `<div class="timeline-track">${clips.map((clip, index) => `<div class="timeline-clip"><b>${String(index + 1).padStart(3, '0')} ${escapeHtml(ctx.shots.find(s => s.id === clip.shot_id)?.story_job || '镜头')}</b><small>${formatTime(clip.out_ms - clip.in_ms)} · 已选视频</small></div>`).join('')}</div>` : empty('时间轴尚空', '先导入视频、明确选片，再到导出工作区设置实际切点。')}</section>`;
  ctx.el.innerHTML = body;
  byId('add-shot').onclick = () => ctx.run(async () => {
    const next = ctx.shots.length ? Math.max(...ctx.shots.map(s => s.order)) + 1 : 0;
    const created = await api(`/api/episodes/${ctx.episode.id}/shots`, {method:'POST', body:{order:next, story_job:`镜头 ${next + 1}`, screen_duration_ms:4000, generation_duration_ms:6000}});
    ctx.shotId = created.id; await ctx.reload();
  });
  document.querySelectorAll('[data-shot]').forEach(button => button.onclick = () => {ctx.shotId = button.dataset.shot; ctx.render();});
  byId('assets-tab').onclick = () => ctx.navigate('assets');
  if (!shot) return;
  bindDialogueControls();
  byId('add-dialogue').onclick = () => {byId('dialogue-rows').insertAdjacentHTML('beforeend', dialogueRow({speaker_id:'',text:''}, speakers));bindDialogueControls();};
  byId('save-shot').onclick = () => ctx.run(async () => {
    const dialogue = buildDialogue([...byId('dialogue-rows').children].map(row => ({speaker:row.querySelector('.dialogue-speaker').value, text:row.querySelector('.dialogue-text').value})));
    const selected = [...byId('reference-assets').selectedOptions].map(option => option.value);
    const payload = {revision:shot.revision, scene_id:byId('shot-scene').value || null, story_job:byId('shot-story').value, start_state:byId('shot-start').value, action:byId('shot-action').value, end_state:byId('shot-end').value, transition:byId('shot-transition').value,
      screen_duration_ms:Math.round(Number(byId('screen-seconds').value) * 1000), generation_duration_ms:Math.round(Number(byId('generation-seconds').value) * 1000),
      dialogue, asset_version_ids:selected};
    await api(`/api/episodes/${ctx.episode.id}/shots/${shot.id}`, {method:'PUT', body:payload});
    await ctx.reload(); ctx.notify('镜头修改已保存');
  });
  byId('select-candidate').onclick = () => ctx.run(async () => {
    const assetId = byId('candidate-select').value;
    if (!assetId) throw new Error('请先选择实际视频候选');
    await api(`/api/episodes/${ctx.episode.id}/shots/${shot.id}/select`, {method:'POST', body:{asset_id:assetId, revision:shot.revision}});
    await ctx.reload(); ctx.notify('已选片，请继续设置实际切点并人工校核');
  });
  byId('queue-h3').onclick = () => ctx.run(async () => {
    if (ctx.settings.video !== 'configured') throw new Error('视频工作流尚未配置；人工编辑仍可继续');
    const firstFrame = byId('first-frame')?.value;
    if (ctx.settings.video_requires_first_frame && !firstFrame) throw new Error('当前工作流要求首帧：先绑定参考画面并保存镜头');
    await api(`/api/episodes/${ctx.episode.id}/shots/${shot.id}/jobs`, {method:'POST', body:{kind:'h3', payload:{plan_revision:1, strategy:'configured-workflow', ...(firstFrame ? {first_frame_asset_id:firstFrame} : {})}}});
    await ctx.reload(); ctx.notify('生成任务已加入持久队列；完成后仍需选片和人工审看');
  });
  byId('queue-text').onclick = () => ctx.run(async () => {
    if (ctx.settings.text !== 'configured') throw new Error('文本模型未配置；人工编辑仍可继续');
    await api(`/api/episodes/${ctx.episode.id}/shots/${shot.id}/jobs`, {method:'POST', body:{kind:'text', payload:{plan_revision:1, strategy:'reviewable-proposal'}}});
    await ctx.reload(); ctx.notify('文本建议已入队；结果需要明确采纳');
  });
}

function editorInput(label, id, value = '') {
  return `<label class="field">${label}<input id="${id}" value="${escapeHtml(value || '')}"></label>`;
}

function dialogueRow(line, speakers) {
  return `<div class="dialogue-row"><select class="dialogue-pick" aria-label="选择说话人"><option value="">自定义说话人</option>${speakers.map(name=>option(name,name,name===line.speaker_id)).join('')}</select><input class="dialogue-speaker" value="${escapeHtml(line.speaker_id)}" placeholder="说话人姓名" aria-label="说话人姓名"><input class="dialogue-text" value="${escapeHtml(line.text)}" placeholder="实际对白" aria-label="实际对白"><button class="remove-dialogue" type="button" aria-label="删除这句对白">×</button></div>`;
}

function bindDialogueControls() {
  document.querySelectorAll('.dialogue-pick').forEach(select => select.onchange = () => {if(select.value) select.closest('.dialogue-row').querySelector('.dialogue-speaker').value = select.value;});
  document.querySelectorAll('.remove-dialogue').forEach(button => button.onclick = () => button.closest('.dialogue-row').remove());
}
