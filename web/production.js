import {api, byId, escapeHtml, option, field, area} from './api.js';
import {heading, section, empty} from './views.js';
import {splitShotNotes, mergeShotNotes, buildTimelineItem, buildCue, formatTime, filterJobs, parseTranscriptLines, selectReviewContext, timelineOffsetForShot} from './state.js';

const jobKinds = {h3:'视频生成',text:'文本建议',probe:'媒体核验',asr:'离线语音识别',export:'成片导出'};
const jobStates = {queued:'待执行',submitting:'提交中',running:'运行中',needs_reconcile:'待核对',succeeded:'技术完成',failed:'失败',cancelled:'已取消'};

export function renderDirector(ctx) {
  const shot = ctx.shots.find(item => item.id === ctx.shotId) || ctx.shots[0];
  const selectedScene = ctx.scenes.find(scene => scene.id === ctx.sceneId);
  const notes = shot ? splitShotNotes(ctx.episode.creative_notes, shot.id) : {motivation:'', choice:''};
  const body = heading('编导工作台', '剧本、场景目标与人物选择由你决定。') + `<div class="split"><div class="stack">` +
    section('分集剧本', `<div class="stack form-panel">${field('分集名称','episode-title',ctx.episode.title)}${area('剧本文本','episode-script',ctx.episode.script)}${area('其他创作备注','episode-notes',ctx.episode.creative_notes.split(/\r?\n/).filter(line => !/^\s*(动机|选择)\[[^\]]+\]\s*[:：]/.test(line)).join('\n'))}<div class="actions"><button id="save-script" class="primary">保存剧本</button></div></div>`) +
    section('场景', `<div class="stack">${ctx.scenes.length ? ctx.scenes.map(scene => `<div><b>${escapeHtml(scene.title)}</b><p class="muted">目标：${escapeHtml(scene.purpose || '待填写')} · 地点：${escapeHtml(scene.location || '待填写')}</p></div>`).join('') : '<p class="muted">尚未建立场景。</p>'}<label class="field">选择要编辑的场景<select id="scene-select"><option value="">新建场景</option>${ctx.scenes.map(scene => option(scene.id,scene.title,scene.id===selectedScene?.id)).join('')}</select></label><div class="field-grid">${field('场景名','scene-title',selectedScene?.title || '')}${field('场景地点','scene-location',selectedScene?.location || '')}${area('这场的目标','scene-purpose',selectedScene?.purpose || '')}</div><button id="save-scene" class="primary">${selectedScene ? '保存场景修改' : '新增场景'}</button></div>`) + `</div><div class="stack">` +
    section('人物动机与选择', shot ? `<div class="stack"><label class="field">镜头<select id="director-shot">${ctx.shots.map((item, i) => option(item.id, `${i + 1}. ${item.story_job || '未命名'}`, item.id === shot.id)).join('')}</select></label>${area('这一镜人物为什么行动','motivation',notes.motivation)}${area('人物作出了什么选择','choice',notes.choice)}<button id="save-choices" class="primary">保存人物动机与选择</button><p class="hint">这是结构化创作记录；机器只能检查是否填写，不能判断故事是否精彩。</p></div>` : empty('先建立镜头', '在分镜工作台新增镜头后，可记录每一镜的人物选择。')) + `</div></div>`;
  ctx.el.innerHTML = body;
  byId('save-script').onclick = () => ctx.run(async () => {
    const other = byId('episode-notes').value;
    const shotLines = ctx.episode.creative_notes.split(/\r?\n/).filter(line => /^\s*(动机|选择)\[[^\]]+\]\s*[:：]/.test(line));
    await api(`/api/episodes/${ctx.episode.id}`, {method:'PUT', body:{revision:ctx.episode.revision, title:byId('episode-title').value, script:byId('episode-script').value, creative_notes:[other, ...shotLines].filter(Boolean).join('\n')}});
    await ctx.reload(); ctx.notify('剧本已保存');
  });
  byId('scene-select').onchange = event => {
    ctx.sceneId = event.target.value || null;
    const chosen = ctx.scenes.find(scene => scene.id === ctx.sceneId);
    byId('scene-title').value = chosen?.title || '';
    byId('scene-location').value = chosen?.location || '';
    byId('scene-purpose').value = chosen?.purpose || '';
    byId('save-scene').textContent = chosen ? '保存场景修改' : '新增场景';
  };
  byId('save-scene').onclick = () => ctx.run(async () => {
    const sceneId = byId('scene-select').value;
    const payload = {title:byId('scene-title').value || '未命名场景', location:byId('scene-location').value, purpose:byId('scene-purpose').value};
    if (sceneId) {
      await api(`/api/episodes/${ctx.episode.id}/scenes/${sceneId}`, {method:'PUT', body:{revision:ctx.episode.revision,...payload}});
    } else {
      const created = await api(`/api/episodes/${ctx.episode.id}/scenes`, {method:'POST', body:{...payload,sequence:ctx.scenes.length}});
      ctx.sceneId = created.id;
    }
    await ctx.reload(); ctx.notify(sceneId ? '场景目标已保存' : '场景已建立');
  });
  if (shot) {
    byId('director-shot').onchange = event => {ctx.shotId = event.target.value; ctx.render();};
    byId('save-choices').onclick = () => ctx.run(async () => {
      const merged = mergeShotNotes(ctx.episode.creative_notes, shot.id, byId('motivation').value, byId('choice').value);
      await api(`/api/episodes/${ctx.episode.id}`, {method:'PUT', body:{revision:ctx.episode.revision, creative_notes:merged}});
      await ctx.reload(); ctx.notify('人物动机与选择已保存');
    });
  }
}

export function renderAssets(ctx) {
  ctx.el.innerHTML = heading('素材库', '来源、权利与版本随文件保存；导入不会自动选片。') + `<div class="split"><div class="stack">` +
    section('导入素材', `<div class="stack form-panel"><label class="field">选择本机文件<input id="asset-file" type="file" accept="image/*,video/*,audio/*,.pdf,.txt"></label><label class="field">用途<select id="asset-kind"><option value="image">参考画面／首帧</option><option value="video">候选视频</option><option value="audio">声音</option><option value="character">人物素材</option><option value="scene">场景素材</option><option value="prop">道具素材</option><option value="document">文档</option></select></label>${field('素材来源','asset-source','手动导入')}${field('使用授权说明','asset-license','自有或已获授权')}<button id="asset-upload" class="primary">上传并记录素材</button><p class="hint">大文件上传可能需要片刻；仅存入当前作品。文件不会自动成为通过审看的候选。</p></div>`) +
    section('当前作品素材', ctx.assets.length ? `<div class="grid-list">${ctx.assets.map(asset => `<div class="asset-item"><div class="asset-thumb">${!asset.binary_available ? '<span class="muted">文件待重连</span>' : asset.kind === 'image' ? `<img src="/api/projects/${ctx.project.id}/assets/${asset.id}/media" alt="素材预览">` : asset.kind === 'video' ? `<video controls preload="metadata" src="/api/projects/${ctx.project.id}/assets/${asset.id}/media"></video>` : `<span class="muted">${escapeHtml(asset.kind)}</span>`}</div><b>${escapeHtml(asset.rights?.source || asset.kind)}</b><small>${escapeHtml(asset.kind)} · ${asset.binary_available ? '文件可用' : '文件待重连'} · 版本 ${asset.sha256.slice(0, 10)}</small><small>${escapeHtml(asset.rights?.license || '未填写授权')}</small></div>`).join('')}</div>` : empty('素材库为空', '从本机选择文件并填写来源与授权。')) + `</div><div class="stack">` +
    section('使用顺序', '<ol><li>导入角色、场景、道具、首帧、声音或视频。</li><li>在分镜中把参考素材绑定到镜头。</li><li>检查视频候选后明确选片。</li><li>在校核与导出工作区处理字幕、时间轴和成片。</li></ol>') + `</div></div>`;
  byId('asset-upload').onclick = () => ctx.run(async () => {
    const file = byId('asset-file').files[0];
    if (!file) throw new Error('请先选择文件');
    const kind = byId('asset-kind').value;
    const rights = {source:byId('asset-source').value.trim(), license:byId('asset-license').value.trim(), status:'unreviewed'};
    if (!rights.source) throw new Error('请填写素材来源');
    await api(`/api/projects/${ctx.project.id}/assets/upload`, {method:'POST', body:file, headers:{'X-Asset-Kind':kind,'X-Asset-Rights':JSON.stringify(rights)}});
    await ctx.reload(); ctx.notify('素材已导入；尚未选片或人工验收');
  });
}

export function renderJobs(ctx) {
  const jobs = ctx.jobs;
  const visible = filterJobs(jobs, ctx.jobFilter || 'all');
  const active = visible.find(job => job.id === ctx.jobId) || visible.at(-1);
  const rows = visible.map(job => `<tr><td>${escapeHtml(ctx.shots.find(s => s.id === job.shot_id)?.story_job || '整集')}</td><td>${escapeHtml(jobKinds[job.kind] || job.kind)}</td><td><span class="${job.state === 'failed' ? 'error' : job.state === 'needs_reconcile' ? 'amber' : ''}">${escapeHtml(jobStates[job.state] || job.state)}</span></td><td>${escapeHtml(job.phases.at(-1)?.phase || '—')}</td><td>${escapeHtml(job.created_at.slice(0, 19).replace('T',' '))}</td><td><button data-job="${job.id}">查看</button></td></tr>`).join('');
  ctx.el.innerHTML = heading('任务队列', '已就绪镜头继续生成，校核与后期并行。', `<div class="actions"><span class="pill">GPU ${ctx.settings.gpu_workers ? '已配置' : '未启用'} · CPU 最多 2</span><button id="refresh-jobs" class="quiet">刷新状态</button><button id="toggle-workers" class="${ctx.settings.workers_active ? 'quiet' : 'primary'}">${ctx.settings.workers_active ? '暂停领取新任务' : '启动本地任务处理'}</button></div>`) +
    `<div class="split"><section class="section"><div class="tabs">${[['all','全部'],['pending','待执行'],['running','运行中'],['review','待校核'],['failed','失败']].map(([value,label])=>`<button data-filter="${value}" class="${(ctx.jobFilter || 'all') === value ? 'active' : ''}">${label}</button>`).join('')}</div>${visible.length ? `<div class="table-wrap"><table><thead><tr><th>镜头</th><th>任务类型</th><th>状态</th><th>当前阶段</th><th>创建时间</th><th>操作</th></tr></thead><tbody>${rows}</tbody></table></div>` : empty(jobs.length ? '此筛选暂无任务' : '暂无任务', jobs.length ? '换一个状态查看，或等待任务实际推进。' : '先在分镜中确认镜头、参考素材与生成参数。', jobs.length ? '' : '<button id="go-shots" class="primary">打开分镜</button>')}</section><div class="stack">` +
    section('执行规则', '<ul><li>单 GPU 任务独占；不接管其他服务的队列。</li><li>CPU 核验与后期最多并行两项。</li><li>失败保留原素材和中间结果。</li><li>不确定的外部提交需核对，不能盲目重试。</li></ul>') +
    section('任务详情', active ? `<div class="stack"><b>${escapeHtml(jobKinds[active.kind] || active.kind)} · ${escapeHtml(jobStates[active.state] || active.state)}</b><small>任务建立：${escapeHtml(active.created_at)}</small>${active.failure_message ? `<p class="error">${escapeHtml(active.failure_message)}</p>` : ''}${active.state === 'succeeded' ? '<p class="hint">技术任务已完成。视频仍需明确选片和人工审看。</p>' : ''}${active.kind === 'text' && active.result?.suggestion ? `<div class="note-box">待采纳文本建议：${escapeHtml(active.result.suggestion)}</div><button id="adopt-text">确认采纳到镜头任务</button>` : ''}${ctx.reconcileReport && active.id === ctx.reconcileJobId ? `<p class="note-box">${escapeHtml(ctx.reconcileReport.message)}${ctx.reconcileReport.external_state ? ` · 外部状态 ${escapeHtml(ctx.reconcileReport.external_state)}` : ''}</p>` : ''}<div class="actions">${active.state === 'queued' ? `<button id="cancel-job">取消排队</button>` : ''}${active.state === 'failed' ? `<label class="field">新方案版本<input id="retry-version" type="number" min="1" value="${Number(active.payload.plan_revision || 0) + 1}"></label><label class="field">调整说明<input id="retry-strategy" placeholder="说明输入或策略的变化"></label><button id="retry-job">改变方案后重试</button>` : ''}${active.state === 'needs_reconcile' ? `<button id="reconcile-job">只读核对外部状态</button>` : ''}${active.state === 'needs_reconcile' && active.id === ctx.reconcileJobId && ['succeeded','failed'].includes(ctx.reconcileReport?.external_state) && ctx.reconcileReport?.started_at && ctx.reconcileReport?.finished_at ? '<button id="resolve-job">按外部证据收束本地任务</button>' : ''}</div>${active.state === 'needs_reconcile' && active.id === ctx.reconcileJobId && ['succeeded','failed'].includes(ctx.reconcileReport?.external_state) && ctx.reconcileReport?.started_at && ctx.reconcileReport?.finished_at ? '<p class="hint">会收集外部候选并更新本地任务；不会向外部重新提交或中断任务。</p>' : ''}<details><summary>阶段记录</summary>${active.phases.map(phase => `<p class="hint">${escapeHtml(phase.phase)} · ${escapeHtml(phase.entered_at)}${phase.duration_ms != null ? ` · ${phase.duration_ms} ms` : ''}</p>`).join('')}</details></div>` : empty('尚未选择任务', '任务出现后可查看阶段与错误。')) + `</div></div>`;
  const events = jobs.flatMap(job => job.phases.map(phase => ({job, phase}))).sort((a,b)=>b.phase.entered_at.localeCompare(a.phase.entered_at)).slice(0,5);
  ctx.el.insertAdjacentHTML('beforeend', `<div style="margin-top:16px">${section('最近事件', events.length ? events.map(({job,phase})=>`<p class="hint">${escapeHtml(phase.entered_at.slice(0,19).replace('T',' '))} · ${escapeHtml(jobKinds[job.kind] || job.kind)} · ${escapeHtml(phase.phase)}</p>`).join('') : empty('尚未开始运行任务','这里会显示真实阶段记录。'))}</div>`);
  document.querySelectorAll('[data-job]').forEach(button => button.onclick = () => {ctx.jobId = button.dataset.job; ctx.render();});
  document.querySelectorAll('[data-filter]').forEach(button => button.onclick = () => {ctx.jobFilter = button.dataset.filter;ctx.render();});
  byId('refresh-jobs').onclick = () => ctx.run(async () => {await ctx.reload();ctx.notify('任务状态已刷新');});
  byId('toggle-workers').onclick = () => ctx.run(async () => {
    const action = ctx.settings.workers_active ? 'stop' : 'start';
    await api(`/api/queue/${action}`, {method:'POST',body:{}});
    await ctx.reload();ctx.notify(action === 'start' ? '任务处理已启动；仅运行已排队的本地任务' : '已停止领取新任务；正在执行的任务会自然完成');
  });
  if (byId('go-shots')) byId('go-shots').onclick = () => ctx.navigate('shots');
  if (active && byId('cancel-job')) byId('cancel-job').onclick = () => ctx.run(async () => {await api(`/api/projects/${ctx.project.id}/jobs/${active.id}/cancel`,{method:'POST',body:{}}); await ctx.reload();});
  if (active && byId('retry-job')) byId('retry-job').onclick = () => ctx.run(async () => {
    const revision = Number(byId('retry-version').value);
    const strategy = byId('retry-strategy').value.trim();
    if (!Number.isInteger(revision) || revision < 1 || !strategy?.trim()) throw new Error('请记录有效的新方案版本与调整说明');
    await api(`/api/projects/${ctx.project.id}/jobs/${active.id}/retry`, {method:'POST', body:{plan_revision:revision, strategy}});
    await ctx.reload(); ctx.notify('重试任务已入队');
  });
  if (active && byId('adopt-text')) byId('adopt-text').onclick = () => ctx.run(async () => {
    await api(`/api/projects/${ctx.project.id}/jobs/${active.id}/adopt`, {method:'POST', body:{revision:active.source_revision}});
    await ctx.reload(); ctx.notify('文本建议已明确采纳并保存');
  });
  if (active && byId('reconcile-job')) byId('reconcile-job').onclick = () => ctx.run(async () => {
    ctx.reconcileReport = await api(`/api/projects/${ctx.project.id}/jobs/${active.id}/reconcile`);
    ctx.reconcileJobId = active.id;
    ctx.render();
  });
  if (active && byId('resolve-job')) byId('resolve-job').onclick = () => ctx.run(async () => {
    await api(`/api/projects/${ctx.project.id}/jobs/${active.id}/resolve`, {method:'POST',body:{}});
    ctx.reconcileReport=null;
    await ctx.reload();ctx.notify('已按外部执行证据更新本地任务；成功候选仍须明确选片和人工审看');
  });
}

export function renderReview(ctx) {
  const {shot, item} = selectReviewContext(ctx.shots, ctx.timeline?.items, ctx.reviewShotId || ctx.shotId);
  const speaker = shot?.dialogue?.[0]?.speaker_id || '';
  const audioAssets = ctx.assets.filter(asset => asset.kind === 'audio' && asset.binary_available);
  const asrResult = ctx.jobs.filter(job => job.kind === 'asr' && job.shot_id === shot?.id && job.state === 'succeeded' && Array.isArray(job.result?.segments)).at(-1);
  const cueRows = (ctx.cues?.cues || []).map((cue, index) => cueRow(cue, index, ctx)).join('');
  ctx.el.innerHTML = heading('声音与校核', '机器检查与人工审看分别记录。', shot ? `<label class="field">当前镜头<select id="review-shot">${ctx.shots.map((candidate,index)=>option(candidate.id,`${index+1}. ${candidate.story_job || '未命名镜头'}`,candidate.id===shot.id)).join('')}</select></label>` : '') + `<div class="stack">` +
    section('选片人工校核', shot?.selected_candidate_id ? `<div class="stack form-panel"><label class="field">结论<select id="review-verdict"><option value="pass">人工通过</option><option value="revise">需要修改</option><option value="reject">拒绝使用</option></select></label>${area('审看记录','review-note')}<button id="save-review" class="primary">保存人工校核</button><div>${ctx.qc.filter(row=>row.shot_id===shot.id).map(row => `<p class="hint">${escapeHtml(shot.story_job || '镜头')} · ${escapeHtml(row.verdict)} · ${row.current ? '当前版本' : '已过期'} · ${escapeHtml(row.note)}</p>`).join('')}</div></div>` : empty('当前镜头还没有选片', '在分镜中导入并明确选择视频后，再记录人工校核。')) +
    section('字幕与原生对白', ctx.timeline?.status === 'ready' ? `<div class="stack"><p class="hint">字幕时间以整集输出起点为零；来源必须属于覆盖的实际片段。当前状态：${escapeHtml(ctx.cues?.status || 'needs_entry')}</p><div id="cue-rows">${cueRows}</div><div class="actions"><button id="add-cue">新增字幕</button><button id="save-cues" class="primary">保存字幕</button>${ctx.cues?.status === 'ready' && ctx.cues?.cues?.length ? `<a href="/api/episodes/${ctx.episode.id}/cues/srt" download="captions.srt">下载 SRT</a>` : ''}</div></div>` : empty('字幕等待实际时间轴', '请先明确选片，并在导出工作区保存实际切点。')) +
    section('原文与实录比较', shot ? `<div class="stack form-panel"><p class="hint">原文取当前镜头的原生对白。实录每行一句，可写“说话人：内容”；机器仅标差异线索，不能代替听审。</p><p>原文：${escapeHtml(shot.dialogue.map(line=>`${line.speaker_id}：${line.text}`).join(' / ') || '当前镜头未填写对白')}</p>${area('人工记录的实录','actual-transcript')}${ctx.settings.asr === 'configured' && audioAssets.length ? `<label class="field">离线识别的声音素材<select id="asr-audio">${audioAssets.map(asset=>option(asset.id,asset.rights?.source || '声音素材')).join('')}</select></label>` : '<span class="hint">离线语音识别未配置或无可用声音素材</span>'}<div class="actions"><button id="compare-transcript">比较文本差异</button>${ctx.settings.asr === 'configured' && audioAssets.length ? `<button id="queue-asr">排队离线语音识别</button>` : ''}${asrResult ? '<button id="load-asr">载入机器识别文本</button>' : ''}</div><div id="transcript-flags" class="hint">${ctx.transcriptReport ? ctx.transcriptReport.findings.map(f=>escapeHtml(f.detail)).join('；') || '没有文本差异线索，仍需人工听审' : ''}</div></div>` : empty('尚无镜头对白', '请先在分镜中记录原生对白。')) +
    section('结构性故事提示', `<p class="hint">仅检查显式填写项，不判断表演、情绪或观众反应。</p>${ctx.story.length ? `<ul>${ctx.story.map(finding=>`<li>${escapeHtml(finding.suggestion)}</li>`).join('')}</ul>` : '<p>当前没有结构性缺项提示。</p>'}`) + `</div>`;
  if (byId('review-shot')) byId('review-shot').onchange = event => {
    ctx.reviewShotId = event.target.value;
    ctx.transcriptReport = null;
    ctx.render();
  };
  if (shot?.selected_candidate_id && byId('save-review')) byId('save-review').onclick = () => ctx.run(async () => {
    await api(`/api/episodes/${ctx.episode.id}/shots/${shot.id}/qc`, {method:'POST', body:{asset_id:shot.selected_candidate_id, verdict:byId('review-verdict').value, note:byId('review-note').value}});
    await ctx.reload(); ctx.notify('人工校核已保存，改选或改时间轴后会标为过期');
  });
  if (shot && byId('compare-transcript')) byId('compare-transcript').onclick = () => ctx.run(async () => {
    const actual = parseTranscriptLines(byId('actual-transcript').value, speaker);
    ctx.transcriptReport = await api(`/api/episodes/${ctx.episode.id}/shots/${shot.id}/transcript-review`, {method:'POST', body:{actual}});
    byId('transcript-flags').textContent = ctx.transcriptReport.findings.map(f=>f.detail).join('；') || '没有文本差异线索，仍需人工听审';
  });
  if (shot && byId('queue-asr')) byId('queue-asr').onclick = () => ctx.run(async () => {
    const audioId = byId('asr-audio').value;
    await api(`/api/episodes/${ctx.episode.id}/shots/${shot.id}/jobs`,{method:'POST',body:{kind:'asr',payload:{asset_id:audioId,plan_revision:1,strategy:'offline-cpu'}}});
    await ctx.reload();ctx.notify('离线语音识别任务已排队，结果需人工对照');
  });
  if (asrResult && byId('load-asr')) byId('load-asr').onclick = () => {
    byId('actual-transcript').value = asrResult.result.segments.map(line => `${line.speaker_id}：${line.text}`).join('\n');
    ctx.notify('机器识别文本已载入；说话人 unknown 与文字仍需人工听审');
  };
  if (ctx.timeline?.status !== 'ready') return;
  if (item) byId('add-cue').onclick = () => {
    const index = byId('cue-rows').children.length;
    const offset = timelineOffsetForShot(ctx.timeline.items, shot.id);
    byId('cue-rows').insertAdjacentHTML('beforeend', cueRow({start_ms:offset,end_ms:offset + Math.min(1000,item.out_ms-item.in_ms),text:'',speaker_id:speaker,source_asset_id:item.source_asset_id}, index, ctx));
    bindCueDelete();
  };
  else {
    byId('add-cue').disabled = true;
    byId('add-cue').insertAdjacentHTML('afterend', '<span class="hint">当前镜头不在已选时间轴，无法为它新增字幕。</span>');
  }
  bindCueDelete();
  byId('save-cues').onclick = () => ctx.run(async () => {
    const cues = [...byId('cue-rows').children].map(row => {
      const source = ctx.timeline.items.find(item => item.source_asset_id === row.querySelector('.cue-source').value);
      const cue = buildCue(source, Number(row.querySelector('.cue-start').value), Number(row.querySelector('.cue-end').value), row.querySelector('.cue-text').value, row.querySelector('.cue-speaker').value);
      if (row.dataset.id) cue.id = row.dataset.id;
      return cue;
    });
    await api(`/api/episodes/${ctx.episode.id}/cues`, {method:'PUT', body:{revision:ctx.episode.revision,cues}});
    await ctx.reload(); ctx.notify('字幕已保存；仍需人工听审');
  });
}

function cueRow(cue, index, ctx) {
  const speakers = [...new Set(ctx.shots.flatMap(s => s.dialogue.map(line => line.speaker_id)))];
  return `<div class="cue-row" data-id="${escapeHtml(cue.id || '')}"><span>${String(index+1).padStart(2,'0')}</span><input class="cue-start" type="number" min="0" value="${cue.start_ms}" aria-label="字幕开始毫秒"><input class="cue-end" type="number" min="1" value="${cue.end_ms}" aria-label="字幕结束毫秒"><input class="cue-text" value="${escapeHtml(cue.text)}" placeholder="字幕文本" aria-label="字幕文本"><select class="cue-speaker" aria-label="说话人">${speakers.map(name=>option(name,name,name===cue.speaker_id)).join('')}</select><select class="cue-source" aria-label="源视频">${ctx.timeline.items.map((item,i)=>option(item.source_asset_id,`片段${i+1}`,item.source_asset_id===cue.source_asset_id)).join('')}</select><button class="remove-cue" aria-label="删除字幕">×</button></div>`;
}
function bindCueDelete(){document.querySelectorAll('.remove-cue').forEach(button=>button.onclick=()=>button.closest('.cue-row').remove());}

export function renderExport(ctx) {
  const selected = ctx.shots.filter(s => s.selected_candidate_id);
  const existing = ctx.timeline?.items || [];
  const rows = existing.length ? existing : selected.map(s => ({shot_id:s.id,source_asset_id:s.selected_candidate_id,in_ms:0,out_ms:''}));
  ctx.el.innerHTML = heading('导出工作台', '用已选真实视频设置切点，生成 MP4、报告与可选 SRT。') + `<div class="stack">` +
    section('实际选片时间轴', selected.length ? `<div class="stack"><p class="hint">以下切点为源视频毫秒。点击“读取时长”可取得实际媒体信息并填入完整出点，再按需要修剪。</p><div id="timeline-rows">${rows.map((row,i)=>`<div class="timeline-row"><span>${String(i+1).padStart(2,'0')} · ${escapeHtml(ctx.shots.find(s=>s.id===row.shot_id)?.story_job || '镜头')}</span><input class="trim-in" type="number" min="0" value="${row.in_ms}" aria-label="入点毫秒"><input class="trim-out" type="number" min="1" value="${row.out_ms}" aria-label="出点毫秒" placeholder="实际毫秒"><button class="probe-clip">读取时长</button><button class="remove-clip">移除</button><input class="clip-id" type="hidden" value="${row.shot_id}"></div>`).join('')}</div><div class="actions"><button id="add-clip">添加已选片段</button><button id="save-timeline" class="primary">保存实际时间轴</button></div></div>` : empty('尚无已选视频', '请先到素材导入视频，在分镜中明确选片。')) +
    section('成片输出', `<div class="stack form-panel"><p>时间轴：${escapeHtml(ctx.timeline?.status || 'needs_selection')} · 字幕：${escapeHtml(ctx.cues?.status || 'needs_entry')}</p><label class="field">字幕选项<select id="export-subtitles"><option value="yes">包含已校对字幕</option><option value="no">明确不带字幕（报告标缺失）</option></select></label><button id="export-mp4" class="primary" ${ctx.timeline?.status === 'ready' ? '' : 'disabled'}>排队生成 MP4 与核验报告</button><p class="hint">在任务工作区显式启动本地处理。导出会解码核验，耗时取决于实际视频长度；机器通过后仍需人工验收。</p>${ctx.exports?.length ? ctx.exports.map(record=>`<p><a href="${record.video_url}" download>下载 MP4</a> · <a href="/api/projects/${ctx.project.id}/exports/${record.id}.json" download>下载报告</a>${record.srt_url ? ` · <a href="${record.srt_url}" download>下载 SRT</a>` : ''}</p>`).join('') : ''}</div>`) +
    section('完整备份与恢复', `<div class="stack form-panel"><p class="hint">备份包含当前作品的分集、镜头、素材二进制、时间轴、字幕和人工校核。恢复遇到已有同 ID 作品会拒绝，不覆盖。</p><div class="actions"><button id="make-archive">生成完整备份</button><a id="archive-link" hidden download>下载备份</a></div><label class="field">选择镜序备份 ZIP<input id="restore-file" type="file" accept=".zip"></label><button id="restore-archive">恢复为独立作品</button></div>`) + `</div>`;
  if (selected.length) {
    byId('add-clip').onclick = () => {
      const unused = selected.find(s => ![...byId('timeline-rows').querySelectorAll('.clip-id')].some(input => input.value === s.id));
      if (!unused) return ctx.notify('全部已选镜头都在时间轴中');
      byId('timeline-rows').insertAdjacentHTML('beforeend', `<div class="timeline-row"><span>${escapeHtml(unused.story_job)}</span><input class="trim-in" type="number" min="0" value="0"><input class="trim-out" type="number" min="1" placeholder="实际毫秒"><button class="probe-clip">读取时长</button><button class="remove-clip">移除</button><input class="clip-id" type="hidden" value="${unused.id}"></div>`);
      bindClipControls(ctx);
    };
    bindClipControls(ctx);
    byId('save-timeline').onclick = () => ctx.run(async () => {
      const items = [...byId('timeline-rows').children].map(row => buildTimelineItem(ctx.shots.find(s => s.id === row.querySelector('.clip-id').value), Number(row.querySelector('.trim-in').value), Number(row.querySelector('.trim-out').value)));
      await api(`/api/episodes/${ctx.episode.id}/timeline`, {method:'PUT', body:{revision:ctx.episode.revision,items}});
      await ctx.reload(); ctx.notify('实际时间轴已保存；旧字幕需要重新对齐');
    });
  }
  byId('export-mp4').onclick = () => ctx.run(async () => {
    const include = byId('export-subtitles').value === 'yes';
    await api(`/api/projects/${ctx.project.id}/episodes/${ctx.episode.id}/export-jobs`, {method:'POST', body:{subtitles:include}});
    await ctx.reload();ctx.navigate('jobs');ctx.notify('导出任务已排队；请在任务工作区显式启动处理');
  });
  byId('make-archive').onclick = () => ctx.run(async () => {
    const result = await api(`/api/projects/${ctx.project.id}/archive`, {method:'POST',body:{}});
    const link = byId('archive-link'); link.href = result.url; link.hidden = false; link.textContent = '下载完整备份'; ctx.notify('备份已生成');
  });
  byId('restore-archive').onclick = () => ctx.run(async () => {
    const file = byId('restore-file').files[0]; if (!file) throw new Error('请先选择备份 ZIP');
    await api('/api/restore',{method:'POST',body:file});
    await ctx.reloadProjects(); ctx.notify('备份已恢复，可从作品列表打开');
  });
}
function bindClipControls(ctx){
  document.querySelectorAll('.remove-clip').forEach(button=>button.onclick=()=>button.closest('.timeline-row').remove());
  document.querySelectorAll('.probe-clip').forEach(button=>button.onclick=()=>ctx.run(async()=>{
    const row=button.closest('.timeline-row');
    const shot=ctx.shots.find(s=>s.id===row.querySelector('.clip-id').value);
    const info=await api(`/api/projects/${ctx.project.id}/assets/${shot.selected_candidate_id}/probe`);
    row.querySelector('.trim-out').value=info.duration_ms;
    ctx.notify(`实际时长 ${formatTime(info.duration_ms)}；${info.has_audio ? '含音轨' : '缺少音轨，无法直接导出'}`);
  }));
}

export function renderSettings(ctx) {
  ctx.el.innerHTML = heading('本地设置', '模型只在显式配置后启动；人工工作不依赖模型。') + `<div class="stack form-panel">` +
    section('接入状态', `<p>视频工作流：${escapeHtml(ctx.settings.video)} · GPU 工作线程：${ctx.settings.gpu_workers}</p><p>文本建议：${escapeHtml(ctx.settings.text)} · 离线语音识别：${escapeHtml(ctx.settings.asr)}</p><p>CPU 工作线程上限：2</p>`) +
    section('操作员配置', '<p>在当前数据目录建立 <code>operator-config.json</code> 后重启本地服务。视频需明确指定兼容的 ComfyUI 地址、workflow 和 bindings；文本建议需指定模型与兼容接口；离线语音识别需指定本地 ASR 配置文件路径，文件内再指向已有模型目录。配置不通过 API 回传，也不进入作品备份。</p><p class="hint">未配置时可继续写剧本、分镜、导入素材、选片、编辑字幕与导出已有视频。</p>') + `</div>`;
}
