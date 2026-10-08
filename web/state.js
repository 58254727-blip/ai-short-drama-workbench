export function splitShotNotes(notes, shotId) {
  const result = {motivation: '', choice: ''};
  for (const line of String(notes || '').split(/\r?\n/)) {
    for (const [label, key] of [['动机', 'motivation'], ['选择', 'choice']]) {
      const prefix = `${label}[${shotId}]`;
      if (line.startsWith(prefix) && /^\s*[:：]/.test(line.slice(prefix.length))) result[key] = line.slice(prefix.length).replace(/^\s*[:：]/, '').trim();
    }
  }
  return result;
}

export function mergeShotNotes(notes, shotId, motivation, choice) {
  const old = String(notes || '').split(/\r?\n/).filter(line =>
    !['动机', '选择'].some(label => {
      const prefix = `${label}[${shotId}]`;
      return line.startsWith(prefix) && /^\s*[:：]/.test(line.slice(prefix.length));
    }));
  const lines = old.filter((line, index) => line || index < old.length - 1);
  if (String(motivation).trim()) lines.push(`动机[${shotId}]: ${String(motivation).trim()}`);
  if (String(choice).trim()) lines.push(`选择[${shotId}]: ${String(choice).trim()}`);
  return lines.join('\n');
}

export function buildTimelineItem(shot, inMs, outMs) {
  if (!shot?.selected_candidate_id) throw new Error('请先为镜头选片');
  if (!Number.isInteger(inMs) || !Number.isInteger(outMs) || inMs < 0 || outMs <= inMs) throw new Error('切点必须是有效的起止毫秒');
  return {shot_id: shot.id, source_asset_id: shot.selected_candidate_id, in_ms: inMs, out_ms: outMs};
}

export function buildCue(item, startMs, endMs, text, speakerId) {
  if (!item?.source_asset_id || !Number.isInteger(startMs) || !Number.isInteger(endMs) || endMs <= startMs || startMs < 0 || !String(text).trim() || !String(speakerId).trim()) throw new Error('字幕时间、文本、说话人或来源无效');
  return {source_asset_id: item.source_asset_id, start_ms: startMs, end_ms: endMs, text: String(text).trim(), speaker_id: String(speakerId).trim()};
}

export function formatTime(ms) {
  const total = Math.floor(ms / 1000);
  return `${String(Math.floor(total / 60)).padStart(2, '0')}:${String(total % 60).padStart(2, '0')}.${String(ms % 1000).padStart(3, '0')}`;
}

export function filterJobs(jobs, filter) {
  if (filter === 'pending') return jobs.filter(job => job.state === 'queued');
  if (filter === 'running') return jobs.filter(job => job.state === 'running' || job.state === 'submitting');
  if (filter === 'review') return jobs.filter(job => job.state === 'needs_reconcile' || job.state === 'succeeded');
  if (filter === 'failed') return jobs.filter(job => job.state === 'failed');
  return jobs;
}

export function buildDialogue(rows) {
  return rows.map(row => ({speaker_id:String(row.speaker || '').trim(), text:String(row.text || '').trim()})).filter(row => {
    if (!row.speaker_id && !row.text) return false;
    if (!row.speaker_id || !row.text) throw new Error('对白必须同时填写说话人与内容');
    return true;
  });
}

export function parseTranscriptLines(text, defaultSpeaker = '') {
  return String(text || '').split(/\r?\n/).map(line => {
    const trimmed = line.trim();
    const separator = trimmed.search(/[:：]/);
    return separator > 0
      ? {speaker_id:trimmed.slice(0, separator).trim(), text:trimmed.slice(separator + 1).trim()}
      : {speaker_id:defaultSpeaker, text:trimmed};
  }).filter(line => line.text);
}

export function selectReviewContext(shots, timelineItems, selectedShotId) {
  const shot = shots.find(item => item.id === selectedShotId) || shots[0] || null;
  const item = shot ? (timelineItems || []).find(entry => entry.shot_id === shot.id) || null : null;
  return {shot, item};
}

export function timelineOffsetForShot(items, shotId) {
  let offset = 0;
  for (const item of items || []) {
    if (item.shot_id === shotId) return offset;
    offset += item.out_ms - item.in_ms;
  }
  return null;
}

export function refreshProjectSelection(current, projects) {
  return projects.find(project => project.id === current?.id) || projects[0] || null;
}

export async function restoreArchiveFile(context, file, request) {
  if (!(file instanceof Blob) || file.size === 0) throw new Error('请先选择备份 ZIP');
  const restored = await request('/api/restore', {method:'POST', body:file});
  if (typeof restored?.project_id !== 'string' || !restored.project_id) throw new Error('恢复结果缺少作品信息');
  context.project = {id:restored.project_id};
  context.episode = null;
  await context.reloadProjects();
  return restored;
}
