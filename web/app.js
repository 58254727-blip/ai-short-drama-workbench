import {api, byId, escapeHtml, option} from './api.js';
import {setView} from './views.js';
import {renderShots} from './editor.js';
import {renderDirector, renderAssets, renderJobs, renderReview, renderExport, renderSettings} from './production.js';
import {restoreArchiveFile} from './state.js';

const renderers = {director:renderDirector, shots:renderShots, assets:renderAssets, jobs:renderJobs, review:renderReview, export:renderExport, settings:renderSettings};
const state = {
  el:byId('workspace'), view:'shots', projects:[], project:null, episode:null, episodes:[], scenes:[], shots:[], assets:[], jobs:[], timeline:null, cues:null, story:[], qc:[], settings:{}, shotId:null, jobId:null, exports:[], busy:false,
  notify(message, error=false){const notice=byId('notice'); notice.textContent=message; notice.classList.toggle('error',error);},
  navigate(view){this.view=view; this.render();},
  render(){setView(this.view === 'settings' ? '' : this.view); if(!this.project && this.view !== 'settings') return renderWelcome(this); if(!this.episode && !['assets','jobs','settings'].includes(this.view)) return renderNoEpisode(this); renderers[this.view]?.(this);},
  async run(action){if(this.busy)return; this.busy=true; this.notify('正在处理…'); try{await action();}catch(error){this.notify(error.message || '操作失败',true);}finally{this.busy=false;}},
  async reloadProjects(){
    this.projects=await api('/api/projects');
    if(!this.projects.some(project=>project.id===this.project?.id)) this.project=this.projects[0]||null;
    await this.reload();
  },
  async reload(){
    this.el.innerHTML='<div class="loading">正在读取本地记录…</div>';
    this.settings=await api('/api/settings/status');
    if(this.project){
      this.episodes=await api(`/api/projects/${this.project.id}/episodes`);
      if(!this.episodes.some(episode=>episode.id===this.episode?.id))this.episode=this.episodes[0]||null;
      this.assets=await api(`/api/projects/${this.project.id}/assets`);
      this.jobs=await api(`/api/projects/${this.project.id}/jobs`);
      this.exports=await api(`/api/projects/${this.project.id}/exports`);
    }else{this.episodes=[];this.episode=null;this.assets=[];this.jobs=[];this.exports=[];}
    if(this.episode){
      const id=this.episode.id;
      [this.episode,this.scenes,this.shots,this.timeline,this.cues,this.story,this.qc]=await Promise.all([
        api(`/api/episodes/${id}`),api(`/api/episodes/${id}/scenes`),api(`/api/episodes/${id}/shots`),api(`/api/episodes/${id}/timeline`),api(`/api/episodes/${id}/cues`),api(`/api/episodes/${id}/story`),api(`/api/episodes/${id}/qc`)]);
      if(!this.shots.some(shot=>shot.id===this.shotId))this.shotId=this.shots[0]?.id||null;
    }else{this.scenes=[];this.shots=[];this.timeline=null;this.cues=null;this.story=[];this.qc=[];this.shotId=null;}
    byId('project-select').innerHTML=(this.projects.length?this.projects.map(project=>option(project.id,project.title,project.id===this.project?.id)).join(''):'<option value="">尚无作品</option>')+'<option value="__new__">＋ 新建作品</option>';
    byId('episode-select').innerHTML=(this.episodes.length?this.episodes.map(episode=>option(episode.id,episode.title,episode.id===this.episode?.id)).join(''):'<option value="">尚无分集</option>')+(this.project?'<option value="__new__">＋ 新建分集</option>':'');
    byId('status-text').textContent=this.jobs.some(job=>['queued','running','submitting','needs_reconcile'].includes(job.state))?'有任务待处理':'尚未提交生成任务';
    this.render();
  }
};

function renderWelcome(ctx){
  ctx.el.innerHTML=`<div class="project-prompt"><h1>从一部作品开始</h1><p>建立自己的原创短剧，逐镜记录剧本、素材、选片与成片。<br>也可明确创建一部虚构演示作品熟悉工作台。</p><div class="actions"><button id="welcome-create" class="primary">新建作品</button><button id="welcome-demo">创建“雨夜来客”演示</button></div>${restoreControls()}</div>`;
  byId('welcome-create').onclick=()=>openCreate('project');
  byId('welcome-demo').onclick=()=>ctx.run(createDemo);
  bindRestore(ctx);
}
function renderNoEpisode(ctx){
  ctx.el.innerHTML=`<div class="project-prompt"><h1>${escapeHtml(ctx.project.title)}</h1><p>这部作品还没有分集。先建分集，之后可编写剧本和镜头。</p><button id="welcome-episode" class="primary">新建分集</button>${restoreControls()}</div>`;
  byId('welcome-episode').onclick=()=>openCreate('episode');
  bindRestore(ctx);
}

function restoreControls(){
  return `<div class="restore-entry"><label class="field">从镜序备份恢复<input id="welcome-restore-file" type="file" accept=".zip,application/zip"></label><button id="welcome-restore" type="button">恢复备份 ZIP</button><p class="hint">同 ID 的作品会拒绝恢复，不会覆盖已有内容。</p></div>`;
}
function bindRestore(ctx){
  byId('welcome-restore').onclick=()=>ctx.run(async()=>{
    await restoreArchiveFile(ctx, byId('welcome-restore-file').files[0], api);
    ctx.notify('备份已恢复，已打开恢复的作品与分集');
  });
}

let createKind='project';
function openCreate(kind){createKind=kind; byId('create-title').textContent=kind==='project'?'新建作品':'新建分集'; byId('create-name').value=''; byId('create-dialog').showModal(); byId('create-name').focus();}
byId('create-cancel').onclick=()=>byId('create-dialog').close();
byId('create-form').onsubmit=event=>{
  event.preventDefault();
  const title=byId('create-name').value.trim(); if(!title)return;
  byId('create-dialog').close();
  state.run(async()=>{
    if(createKind==='project') {state.project=await api('/api/projects',{method:'POST',body:{title}});state.episode=null;}
    else state.episode=await api(`/api/projects/${state.project.id}/episodes`,{method:'POST',body:{title}});
    await state.reloadProjects(); state.notify('已创建并保存');
  });
};
byId('project-select').onchange=event=>{if(event.target.value==='__new__'){event.target.value=state.project?.id||'';return openCreate('project');}state.run(async()=>{state.project=state.projects.find(p=>p.id===event.target.value)||null;state.episode=null;await state.reload();});};
byId('episode-select').onchange=event=>{if(event.target.value==='__new__'){event.target.value=state.episode?.id||'';return openCreate('episode');}state.run(async()=>{state.episode=state.episodes.find(e=>e.id===event.target.value)||null;await state.reload();});};
byId('settings-button').onclick=()=>state.navigate('settings');
document.querySelectorAll('.sidebar [data-view]').forEach(button=>button.onclick=()=>state.navigate(button.dataset.view));

async function createDemo(){
  const project=await api('/api/projects',{method:'POST',body:{title:'雨夜来客（演示）'}});
  const episode=await api(`/api/projects/${project.id}/episodes`,{method:'POST',body:{title:'第一集'}});
  const scene=await api(`/api/episodes/${episode.id}/scenes`,{method:'POST',body:{title:'雨巷',purpose:'访客寻找一扇紧闭的门',location:'虚构雨巷',sequence:0}});
  const descriptions=[
    ['雨巷外景','空镜，雨后街巷，路面湿润。','镜头缓慢向前推进。','停在前方街口。'],
    ['门前停步','访客走近门前。','听见屋内脚步，停下。','手悬在门环前。'],
    ['回望街口','身后传来水声。','访客回头寻找声源。','街口空无一人。']
  ];
  const shots=[];
  for(let i=0;i<descriptions.length;i++){
    const [story_job,start_state,action,end_state]=descriptions[i];
    shots.push(await api(`/api/episodes/${episode.id}/shots`,{method:'POST',body:{scene_id:scene.id,order:i,story_job,start_state,action,end_state,transition:'环境雨声',screen_duration_ms:4000,generation_duration_ms:6000}}));
  }
  const creative_notes=shots.map((shot,i)=>`动机[${shot.id}]: ${['想知道巷中是否有人','希望找到屋内的人','确认声音来源'][i]}\n选择[${shot.id}]: ${['继续向前','暂缓敲门','回头查看'][i]}`).join('\n');
  await api(`/api/episodes/${episode.id}`,{method:'PUT',body:{revision:episode.revision,script:'雨夜，一名访客来到陌生街巷，在门前听见脚步声。',creative_notes}});
  state.project=project;state.episode=episode;state.shotId=shots[0].id;
  await state.reloadProjects();state.notify('虚构演示已创建；画面仅为静帧，尚无视频任务或选片');
}

state.reloadProjects().catch(error=>{state.el.innerHTML=`<div class="empty"><strong>工作台读取失败</strong><span>${escapeHtml(error.message)}</span><button id="retry-load">重新载入</button></div>`;byId('retry-load').onclick=()=>state.reloadProjects();});
