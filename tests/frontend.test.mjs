import test from 'node:test';
import assert from 'node:assert/strict';
import {mergeShotNotes, splitShotNotes, buildTimelineItem, buildCue, formatTime, filterJobs, buildDialogue, parseTranscriptLines} from '../web/state.js';

test('per-shot motivation and choice preserve unrelated creative notes', () => {
  const original = '人物关系待定\n动机[shot-a]: 旧动机\n选择[shot-b]: 别的镜头';
  const merged = mergeShotNotes(original, 'shot-a', '想找雨伞', '决定敲门');
  assert.equal(merged, '人物关系待定\n选择[shot-b]: 别的镜头\n动机[shot-a]: 想找雨伞\n选择[shot-a]: 决定敲门');
  assert.deepEqual(splitShotNotes(merged, 'shot-a'), {motivation: '想找雨伞', choice: '决定敲门'});
});

test('fullwidth separators are edited without duplicate shot notes', () => {
  const old = '动机[shot-a]：旧动机\n选择[shot-a]：旧选择\n自由备注';
  assert.deepEqual(splitShotNotes(old, 'shot-a'), {motivation:'旧动机', choice:'旧选择'});
  assert.equal(mergeShotNotes(old, 'shot-a', '新动机', '新选择'), '自由备注\n动机[shot-a]: 新动机\n选择[shot-a]: 新选择');
});

test('actual timeline item uses selected media and nonzero trim', () => {
  assert.deepEqual(buildTimelineItem({id: 'shot-a', selected_candidate_id: 'media-a'}, 250, 1500), {
    shot_id: 'shot-a', source_asset_id: 'media-a', in_ms: 250, out_ms: 1500
  });
  assert.throws(() => buildTimelineItem({id: 'shot-a'}, 0, 1000), /选片/);
  assert.throws(() => buildTimelineItem({id: 'shot-a', selected_candidate_id: 'media-a'}, 1000, 1000), /切点/);
});

test('cue references real source, named speaker and exported-episode offsets', () => {
  assert.deepEqual(buildCue({source_asset_id: 'media-a'}, 100, 700, '你好', '访客'), {
    source_asset_id: 'media-a', start_ms: 100, end_ms: 700, text: '你好', speaker_id: '访客'
  });
  assert.equal(formatTime(61002), '01:01.002');
});

test('queue filters distinguish pending, running, review and failure', () => {
  const jobs = ['queued','running','needs_reconcile','succeeded','failed'].map((state,id)=>({id,state}));
  assert.deepEqual(filterJobs(jobs,'pending').map(job=>job.id), [0]);
  assert.deepEqual(filterJobs(jobs,'running').map(job=>job.id), [1]);
  assert.deepEqual(filterJobs(jobs,'review').map(job=>job.id), [2,3]);
  assert.deepEqual(filterJobs(jobs,'failed').map(job=>job.id), [4]);
});

test('multiple native dialogue lines retain speaker names and reject partial rows', () => {
  assert.deepEqual(buildDialogue([{speaker:'访客',text:'有人吗'},{speaker:'屋内人',text:'进来'}]), [{speaker_id:'访客',text:'有人吗'},{speaker_id:'屋内人',text:'进来'}]);
  assert.throws(()=>buildDialogue([{speaker:'访客',text:''}]), /对白/);
});

test('manual transcript lines preserve different named speakers', () => {
  assert.deepEqual(parseTranscriptLines('访客：有人吗\n屋内人: 进来', '访客'), [{speaker_id:'访客',text:'有人吗'},{speaker_id:'屋内人',text:'进来'}]);
});
