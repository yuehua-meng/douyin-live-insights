'use strict';
const $=s=>document.querySelector(s), el=(tag,text,cls)=>{const e=document.createElement(tag);if(text!==undefined)e.textContent=text;if(cls)e.className=cls;return e};
let csrf='', recordings=[], selected=new Set(), activeJob='', transcripts=[], edited=false, pollBusy=false;
const drafts=new Map();
let dashboardJobs=[];
let resultCatalogStamp='', resultViewRequest=0;
let caseLibrary=[], selectedCase='', selectedCaseStamp='', currentCaseDetail=null, caseRequest=0, caseListRequest=0, caseListStamp='', pendingCaseRefresh=false;
let submitting=false;
let feedEntries=[],automationState=null,feedSupported=true;
let listRecording=null,listPlaybackAttempt=0,listPlaybackTimer;
function updateListPlayButtons(){
  const audio=$('#list-audio');
  for(const control of document.querySelectorAll('.card-play')){
    const active=listRecording?.id===control.dataset.recording;
    control.textContent=active&&!audio.paused?'Ⅱ 暂停':'▶ 播放';
    control.setAttribute('aria-pressed',String(active&&!audio.paused));
    control.closest('.audio-card').classList.toggle('is-playing',active&&!audio.paused);
  }
}
function closeListPlayer(){
  listPlaybackAttempt++;clearTimeout(listPlaybackTimer);
  const audio=$('#list-audio');audio.pause();audio.removeAttribute('src');audio.load();
  listRecording=null;$('#list-player').hidden=true;updateListPlayButtons();
}
async function playListRecording(recording){
  const audio=$('#list-audio');
  if(listRecording?.id===recording.id&&!audio.paused){audio.pause();return}
  const attempt=++listPlaybackAttempt;clearTimeout(listPlaybackTimer);
  if(listRecording?.id!==recording.id||!audio.getAttribute('src')||audio.error){
    audio.pause();listRecording=recording;audio.src='api/media/'+recording.id;
  }
  $('#list-player').hidden=false;
  const name=beijingTime(recording.started_at)+' · '+recording.name;
  $('#list-playing').textContent='正在加载 · '+name;$('#list-playing').title=name;
  try{
    await Promise.race([audio.play(),new Promise((_,reject)=>{listPlaybackTimer=setTimeout(()=>reject(Error('音频加载超时，请重试')),20000)})]);
    if(attempt===listPlaybackAttempt)$('#list-playing').textContent=name;
  }catch(e){
    if(attempt===listPlaybackAttempt){audio.pause();$('#list-playing').textContent='加载失败，请点击卡片重试';throw Error(e.name==='AbortError'?'播放已中断，请重试':e.message)}
  }finally{if(attempt===listPlaybackAttempt){clearTimeout(listPlaybackTimer);updateListPlayButtons()}}
}
function recordingPreview(recording){
  const shell=el('div',undefined,'server-preview card-preview');
  const duration=el('span',recording.duration_ms>0?time(recording.duration_ms):'时长暂不可用','server-duration');
  duration.title='音频总时长';
  const playButton=button('▶ 播放',()=>playListRecording(recording));
  playButton.className='card-play';playButton.dataset.recording=recording.id;playButton.setAttribute('aria-pressed','false');
  const download=link('下载录音','media/'+recording.id+'?download=true');download.download=recording.name;download.className='card-download';
  shell.append(duration,playButton,download);return shell;
}
$('#list-player-close').onclick=closeListPlayer;
for(const event of ['play','pause','ended'])$('#list-audio').addEventListener(event,updateListPlayButtons);
$('#list-audio').addEventListener('error',()=>{if(listRecording){$('#list-playing').textContent='音频无法播放，请点击卡片重试';updateListPlayButtons()}});
const beijingFormatter=new Intl.DateTimeFormat('sv-SE',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'});
function beijingTime(value){const d=new Date(value);return Number.isNaN(d.getTime())?'时间未知':beijingFormatter.format(d)}
const recordingStates={stalled:'录制无进展后断开',stream_ended:'流已结束',manual_stop:'手动停止',interrupted:'录制中断',test_limit:'测试录音',recording:'仍在录制',no_valid_audio:'无有效音频'};
function formatBytes(n){return n>=1e6?(n/1e6).toFixed(1)+' MB':n>=1e3?(n/1e3).toFixed(1)+' KB':n+' B'}
const labels={queued:'排队中',running:'处理中',completed:'已完成',failed:'失败',cancelled:'已取消',awaiting_budget:'预算不足',transcribing:'转文字',summarizing:'生成总结'};
function statusBadge(job){
  const badge=el('span',undefined,'task-status');badge.dataset.state=job.state;
  if(job.content_filtered||job.result?.content_filtered){badge.append(el('span','已过滤 · 无有效文字'));return badge}
  const icon=el('span',({completed:'✓',failed:'!',cancelled:'−',awaiting_budget:'!'})[job.state]||'','status-icon');
  icon.setAttribute('aria-hidden','true');
  badge.append(icon,el('span',`${labels[job.state]||job.state} · ${job.mode==='business'?(job.stage==='transcribing'?'自动转文字':'整理商业案例'):job.mode==='script'?'生成口播稿':labels[job.stage]||job.stage}`));
  return badge;
}
function renderResultMeta(job){
  const box=$('#resultmeta'),stamp=JSON.stringify([job.id,job.state,job.stage,job.progress,job.error]);
  if(box.dataset.stamp===stamp)return;
  box.dataset.stamp=stamp;box.dataset.state=job.state;box.replaceChildren();
  const line=el('div',undefined,'task-status-line');
  line.append(el('strong',`任务 ${job.id.slice(0,8)}`),statusBadge(job));box.append(line);
  const p=job.progress||{};
  if(p.total)box.append(el('div',`${p.file?'当前文件 '+p.file+'/'+p.files+' · ':''}已处理 ${p.done}/${p.total} 个分块`,'task-progress-text'));
  if(job.error)box.append(el('div',job.error,'task-error'));
  if(job.mode==='business'&&job.result?.script?.cases?.length&&typeof job.locked==='boolean'){
    const retain=el('div',undefined,'toolbar');
    retain.append(button(job.locked?'取消保留本批全部稿件':'保留本批全部稿件',async()=>{
      const updated=await api(`jobs/${job.id}/lock`,{locked:!job.locked});
      box.dataset.stamp='';renderResultMeta(updated);await loadCaseLibrary();
      message(updated.locked?'已保留本批全部稿件。':'已取消保留本批稿件，可查看后续更新。');
    }),el('span','此操作影响本批录音生成的全部口播版本。','detail'));box.append(retain);
  }
}
// Includes both list previews and transcript/summary playback: only one audio at a time.
document.addEventListener('play',event=>{if(event.target.tagName==='AUDIO')document.querySelectorAll('audio').forEach(a=>{if(a!==event.target)a.pause()})},true);
function message(text=''){$('#message').textContent=text}
async function api(path,body){const r=await fetch('api/'+path,{method:body===undefined?'GET':'POST',headers:body===undefined?{}:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:body===undefined?undefined:JSON.stringify(body)});let d;try{d=await r.json()}catch{const e=Error('服务器响应异常，请稍后重试');e.status=r.status;throw e}if(!r.ok){if(r.status===401&&path!=='login')showLogin();const e=Error(typeof d.detail==='string'?d.detail:'输入内容无效');e.status=r.status;throw e}return d}
function button(text,action){const b=el('button',text);b.type='button';b.onclick=async()=>{b.disabled=true;try{await action()}catch(e){message(e.message)}finally{b.disabled=b.dataset.locked==='true'}};return b}
function link(text,path){const a=el('a',text);a.href='api/'+path;return a}
function downloadText(name,text,extension='txt'){const url=URL.createObjectURL(new Blob([text],{type:'text/plain;charset=utf-8'})),a=el('a');a.href=url;a.download=(name||'项目案例').replace(/[<>:"/\\|?*\x00-\x1f]/g,'').slice(0,80)+'.'+extension;a.hidden=true;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000)}
function showLogin(){caseRequest++;resultViewRequest++;document.querySelectorAll('audio').forEach(a=>a.pause());$('#login').hidden=false;$('#workspace').hidden=true;$('#logout').hidden=true;csrf=''}
async function enter(){
  $('#workspace').hidden=false;$('#logout').hidden=false;$('#login').hidden=true;
  renderTranscripts();
  await Promise.all([showDefaultOutputs(),loadFeed(),loadRecordings().then(()=>refreshJobs()),loadProjects()]);
  const s=await api('status');
  $('#filterinfo').textContent=`音频过滤 ${s.filtering?.quarantined||0} 个损坏或静音片段；转写内容过滤 ${s.filtering?.content?.excluded||0} 个无有效文字片段。原件保留备查，正常短句保留。`;
  $('#budget').textContent=`ASR：${s.models_ready?'已就绪':'模型准备中'} · 模型：${s.model} · 今日费用估算/预留 $${(s.usage.committed/1e6).toFixed(4)} / $${(s.daily_budget/1e6).toFixed(2)} · 每批上限 $${(s.task_budget/1e6).toFixed(2)}。`;
}
$('#loginform').onsubmit=async e=>{e.preventDefault();try{csrf=(await api('login',{password:$('#password').value})).csrf;$('#password').value='';message();await enter()}catch(e){message(e.message)}};
$('#logout').onclick=async()=>{try{await api('logout',{});showLogin()}catch(e){message(e.message)}};
function time(ms){const sec=Math.floor(ms/1000);return `${String(Math.floor(sec/3600)).padStart(2,'0')}:${String(Math.floor(sec/60)%60).padStart(2,'0')}:${String(sec%60).padStart(2,'0')}`}
function updateSelection(){$('#selection').textContent=`已选 ${selected.size} 个${selected.size>40?' · 将分 '+Math.ceil(selected.size/40)+' 批':''}`}
async function loadRecordings(){
  recordings=await api('recordings');
  const ids=new Set(recordings.map(r=>r.id));selected=new Set([...selected].filter(id=>ids.has(id)));
  if(listRecording&&!recordings.some(r=>r.id===listRecording.id&&r.recording_version===listRecording.recording_version))closeListPlayer();
  const box=$('#recordings');box.replaceChildren();
  const groups=new Map();
  for(const r of recordings){if(!groups.has(r.session))groups.set(r.session,[]);groups.get(r.session).push(r)}
  for(const [session,files] of groups){
    const group=el('div',undefined,'recording-group');
    const head=el('div',undefined,'recording-group-heading'),info=el('div');
    info.append(el('strong',beijingTime(files[0].started_at)),el('span',`${files.length} 个片段 · ${recordingStates[files[0].session_status]||files[0].session_status||'状态未知'}`,'detail'));
    const select=button('选择此组',()=>{for(const r of files)selected.add(r.id);renderChecks();updateSelection()});
    select.title='选择本次连接的全部片段';head.append(info,select);group.append(head);
    const grid=el('div',undefined,'recording-grid');grid.setAttribute('role','group');grid.setAttribute('aria-label',beijingTime(files[0].started_at)+' 的录音片段');
    for(const r of files){
      const row=el('div',undefined,'audio-card'),label=el('label'),check=el('input');
      check.type='checkbox';check.dataset.id=r.id;check.checked=selected.has(r.id);
      check.setAttribute('aria-label','选择 '+beijingTime(r.started_at)+' '+r.name);
      check.onchange=()=>{check.checked?selected.add(r.id):selected.delete(r.id);updateSelection()};
      const name=el('span',r.name,'audio-card-name');name.title=r.path;label.append(check,name);
      const size=el('span',formatBytes(r.size)+(r.size<1024?' · 小文件':''),'audio-card-size');
      const mark=el('small','已生成录音','recording-transcribed');mark.dataset.recording=r.id;mark.hidden=!r.transcribed;mark.title='这段录音已完成转文字';
      const meta=el('div',undefined,'audio-card-meta');meta.append(size,mark);
      row.append(label,meta,recordingPreview(r));grid.append(row);
    }
    group.append(grid);box.append(group);
  }
  if(!recordings.length)box.append(el('p','暂无已完成录音'));
  updateSelection();updateDashboardMetrics();updateListPlayButtons();
}

function renderChecks(){document.querySelectorAll('[data-id]').forEach(c=>c.checked=selected.has(c.dataset.id))}
$('#refresh').onclick=()=>loadRecordings().catch(e=>message(e.message));
$('#selectall').onclick=()=>{selected=new Set(recordings.map(r=>r.id));renderChecks();updateSelection()};
$('#clearall').onclick=()=>{selected.clear();renderChecks();updateSelection()};
async function create(mode){
  if(!selected.size)throw Error('请先勾选录音');
  const ids=recordings.filter(r=>selected.has(r.id)).sort((a,b)=>a.path.localeCompare(b.path)).map(r=>r.id);
  let completed=0,lastJob;
  try{
    for(let i=0;i<ids.length;i+=40){
      const batch=ids.slice(i,i+40);
      lastJob=await api('jobs',{mode,recording_ids:batch});completed++;
      for(const id of batch)selected.delete(id);
      renderChecks();updateSelection();
      message(`已提交 ${completed}/${Math.ceil(ids.length/40)} 批，每批单独处理。`);
    }
  }catch(e){throw Error(`已提交 ${completed} 批，未提交的录音仍保持勾选。${e.message}`)}
  finally{await refreshJobs()}
  message(`已提交 ${completed} 批任务，每批分别生成结果；关闭页面也会继续处理。`);
  if(lastJob)await openJob(lastJob.id);
}
for(const mode of ['both','transcribe'])$('#'+mode).onclick=async()=>{if(submitting)return;submitting=true;$('#both').disabled=true;$('#transcribe').disabled=true;try{await create(mode)}catch(e){message(e.message)}finally{submitting=false;$('#both').disabled=false;$('#transcribe').disabled=false}};
let transcriptionStamp='';
async function refreshTranscriptionMarks(jobs){
  const stamp=JSON.stringify(jobs.filter(j=>j.stage==='transcribing').map(j=>[j.id,j.state,j.progress]));
  if(stamp===transcriptionStamp)return;
  const completed=await api('recordings/transcribed');
  const keys=new Set(completed.map(r=>JSON.stringify([r.id,r.size,r.mtime_ns])));
  for(const r of recordings)r.transcribed=keys.has(JSON.stringify([r.id,r.size,r.recording_version]));
  const byId=new Map(recordings.map(r=>[r.id,r]));
  for(const mark of document.querySelectorAll('.recording-transcribed'))mark.hidden=!byId.get(mark.dataset.recording)?.transcribed;
  transcriptionStamp=stamp;updateDashboardMetrics();
}
async function refreshJobs(){const jobs=await api('jobs'), box=$('#jobs');await refreshTranscriptionMarks(jobs);dashboardJobs=jobs;updateDashboardMetrics();box.replaceChildren();for(const j of jobs){const row=el('div',undefined,'job');row.dataset.state=j.state;row.append(statusBadge(j));const p=j.progress;row.append(el('div',`${beijingTime(j.created*1000)} · ${j.id.slice(0,8)}`,'detail'));if(p.total)row.append(el('div',`${p.file?'文件 '+p.file+'/'+p.files+' · ':''}进度 ${p.done}/${p.total}`,'detail'));if(j.state==='running'&&p.total){const meter=el('progress');meter.className='job-progress';meter.max=p.total;meter.value=p.done||0;meter.setAttribute('aria-label','当前任务进度');row.append(meter)}if(j.error)row.append(el('p',j.error,'task-error'));row.append(button('查看结果',()=>openJob(j.id)));if(['queued','running'].includes(j.state))row.append(button('取消',async()=>{await api(`jobs/${j.id}/cancel`,{});await refreshJobs()}));if(['failed','cancelled','awaiting_budget'].includes(j.state))row.append(button('重试',async()=>{await api(`jobs/${j.id}/retry`,{});await refreshJobs()}));box.append(row)}if(!jobs.length)box.append(el('p','尚无任务，先选择左侧录音。'));return jobs}
async function play(rid,ms){
  const audio=$('#player'),src=new URL('api/media/'+rid,location.href).href;
  $('#playerbox').hidden=false;$('#playing').textContent=(recordings.find(r=>r.id===rid)?.path||rid)+' · '+time(ms);
  if(audio.src!==src||audio.readyState===0){
    audio.preload='metadata';
    await new Promise((resolve,reject)=>{
      const cleanup=()=>{clearTimeout(timer);audio.onloadedmetadata=null;audio.onerror=null};
      const timer=setTimeout(()=>{cleanup();reject(Error('录音加载超时，请重试'))},20000);
      audio.onloadedmetadata=()=>{cleanup();resolve()};
      audio.onerror=()=>{cleanup();reject(Error('录音无法播放，请尝试下载原音频'))};
      audio.src=src;audio.load();
    });
  }
  audio.currentTime=Math.min(ms/1000,audio.duration||ms/1000);await audio.play();
}
async function openJob(id,scroll=true,preserveCase=false){
  if(edited&&!confirm('当前文字尚未保存，是否放弃修改并切换结果？'))return;
  const request=++resultViewRequest,caseAtStart=caseRequest;
  const j=await api('jobs/'+id);
  const loaded=await Promise.all((j.result.transcript_ids||j.payload?.transcript_ids||[]).map(t=>api('transcripts/'+t)));
  if(request!==resultViewRequest)return;
  activeJob=id;edited=false;drafts.clear();transcripts=loaded;selectedTranscriptIndex=0;
  $('#result').hidden=false;$('#result').dataset.loaded=j.state==='completed'?id:'';
  renderResultMeta(j);renderTranscripts();
  if(!preserveCase){
    await loadCaseLibrary();
    if(request!==resultViewRequest)return;
    const match=caseLibrary.find(c=>c.job_id===id);
    if(match&&caseAtStart===caseRequest)await openCase(match.id,{scroll:false});
  }
  $('#materials').open=true;
  if(scroll)$('#result').scrollIntoView({behavior:'smooth',block:'start'});
}
function staleSummary(){
  message('文字版本已变化。下方保留的是旧版本成果；自动模式下，保存后的关联内容将重新整理。也可在“素材与处理”中手动生成。');
  $('#result').dataset.loaded=activeJob;
}
let selectedTranscriptIndex=0;
function transcriptMarker(ms){return '['+time(ms)+'.'+String(Math.round(ms)%1000).padStart(3,'0')+']'}
function transcriptText(t){return t.segments.map(seg=>transcriptMarker(seg.start_ms)+'\n'+seg.text).join('\n\n')}
function transcriptEdits(value,t){
  const markers=[...value.matchAll(/^\[\d{2,}:\d{2}:\d{2}\.\d{3}\]\s*$/gm)];
  if(markers.length!==t.segments.length||markers.some((m,i)=>m[0].trim()!==transcriptMarker(t.segments[i].start_ms))||value.slice(0,markers[0]?.index||0).trim()){
    throw Error('请保留每段开头的 [时:分:秒.毫秒] 时间标记及顺序，仅修改正文，避免原音频定位错位。');
  }
  return markers.map((m,i)=>value.slice(m.index+m[0].length,markers[i+1]?.index??value.length).trim());
}
function renderTranscripts(){
  const box=$('#transcripts');box.replaceChildren();
  $('#summarize').disabled=!transcripts.length;
  $('#generate-script').disabled=!transcripts.length;
  if(!transcripts.length){box.append(el('p','转写结果将在处理完成后显示。'));return}
  selectedTranscriptIndex=Math.min(selectedTranscriptIndex,transcripts.length-1);
  const index=selectedTranscriptIndex,t=transcripts[index];
  if(transcripts.length>1){
    const files=el('select');files.className='transcript-file-select';files.setAttribute('aria-label','选择转写录音');
    transcripts.forEach((item,i)=>{const option=el('option',`${i+1}. ${beijingTime(item.recording.started_at)} · ${item.recording.name}${drafts.has(item.id)?'（未保存）':''}`);option.value=i;option.selected=i===index;files.append(option)});
    files.onchange=()=>{selectedTranscriptIndex=Number(files.value);renderTranscripts()};box.append(files);
  }
  const wrap=el('div',undefined,'transcript');
  wrap.append(el('strong',`${t.recording.name} · 版本 ${t.revision}`),el('div',t.recording.path,'detail'),el('p',t.timing,'detail'));
  const versions=el('select');versions.setAttribute('aria-label','已保存的文字版本');
  api(`transcripts/${t.id}/versions`).then(items=>{
    for(const v of items){const option=el('option',`版本 ${v.revision} · ${v.id.slice(0,6)}`);option.value=v.id;option.selected=v.id===t.id;versions.append(option)}
  }).catch(e=>message(e.message));
  versions.onchange=async()=>{try{
    if(drafts.has(t.id)&&!confirm('放弃这份文字尚未保存的修改？')){versions.value=t.id;return}
    const next=await api('transcripts/'+versions.value);drafts.delete(t.id);transcripts[index]=next;
    edited=drafts.size>0;renderTranscripts();staleSummary();
  }catch(e){message(e.message)}};
  wrap.append(versions);
  const exports=el('div',undefined,'exports');
  for(const kind of ['txt','srt','json'])exports.append(link('下载 '+kind.toUpperCase(),`transcripts/${t.id}/export/${kind}`));
  wrap.append(exports);
  const input=el('textarea');input.className='transcript-editor';input.setAttribute('aria-label','完整转写文字');
  input.value=drafts.get(t.id)??transcriptText(t);input.placeholder='没有识别到有效讲话';input.readOnly=!t.segments.length;
  input.oninput=()=>{drafts.set(t.id,input.value);edited=true};
  if(t.segments.length){
    const controls=el('div',undefined,'transcript-controls'),seek=el('select');seek.setAttribute('aria-label','选择回听时间');
    t.segments.forEach((seg,i)=>{const option=el('option',`${time(seg.start_ms)} · ${seg.text.slice(0,22)}`);option.value=i;seek.append(option)});
    controls.append(seek,button('回听此处',()=>play(t.recording.id,t.segments[Number(seek.value)].start_ms)));wrap.append(controls);
  }
  wrap.append(input,el('p','在框内上下滚动查看全文；编辑时请保留时间标记。切换录音会保留未保存的修改。','detail'));
  const save=button('保存修改为新版本',async()=>{
    const revised=await api(`transcripts/${t.id}/revision`,{texts:transcriptEdits(input.value,t)});
    drafts.delete(t.id);transcripts[index]=revised;edited=drafts.size>0;renderTranscripts();staleSummary();
    message('新版本已保存，可通过版本选择框再次打开。');
  });save.disabled=!t.segments.length;wrap.append(save);box.append(wrap);
}
$('#summarize').onclick=async()=>{if(edited){message('请先保存文字修改');return}try{const job=await api('jobs',{mode:'business',transcript_ids:transcripts.map(t=>t.id)});await refreshJobs();await openJob(job.id)}catch(e){message(e.message)}};
async function pollContent(){
  if(!csrf||pollBusy)return;pollBusy=true;
  try{
    const jobs=await refreshJobs();const stamp=JSON.stringify(jobs.map(j=>[j.id,j.updated,j.state]));
    if(stamp!==resultCatalogStamp){resultCatalogStamp=stamp;await Promise.all([loadCaseLibrary(),loadProjects()])}
    await loadFeed();
    const j=jobs.find(j=>j.id===activeJob);if(j)renderResultMeta(j);
  }catch(e){message(e.message)}finally{pollBusy=false}
}
setInterval(pollContent,5000);
api('auth').then(async d=>{csrf=d.csrf;await enter()}).catch(()=>showLogin());

$('#generate-script').onclick=async()=>{
  if(edited){message('请先保存文字修改，再生成口播稿');return}
  const button=$('#generate-script');button.disabled=true;
  try{const job=await api('jobs',{mode:'business',transcript_ids:transcripts.map(t=>t.id),script_topic:$('#script-topic').value.trim()});await refreshJobs();await openJob(job.id);$('#script-result').scrollIntoView({behavior:'smooth',block:'center'})}catch(e){message(e.message)}finally{button.disabled=!transcripts.length}
};

function updateDashboardMetrics(){
  $('#metric-recordings').textContent=recordings.length.toLocaleString('zh-CN');
  const known=recordings.filter(r=>Number.isFinite(r.duration_ms)&&r.duration_ms>0);
  const minutes=Math.floor(known.reduce((sum,r)=>sum+r.duration_ms,0)/60000);
  $('#metric-duration').textContent=`${Math.floor(minutes/60)}h ${minutes%60}m`;
  $('#metric-duration-note').textContent=known.length===recordings.length?'音频总时长':`已知时长 · ${known.length}/${recordings.length} 个片段`;
  $('#metric-transcribed').textContent=recordings.filter(r=>r.transcribed).length.toLocaleString('zh-CN');
  $('#metric-active').textContent=dashboardJobs.filter(j=>['running','queued'].includes(j.state)).length;
}
function savedPreference(key){try{return localStorage.getItem(key)}catch{return null}}
function savePreference(key,value){try{localStorage.setItem(key,value)}catch{}}
function setTheme(theme){document.documentElement.dataset.theme=theme;$('#theme-toggle').textContent=theme==='light'?'深色模式':'浅色模式';$('#theme-toggle').setAttribute('aria-label',`切换到${theme==='light'?'深色':'浅色'}模式`)}
function setMotion(paused){document.documentElement.dataset.motion=paused?'paused':'on';$('#motion-toggle').textContent=paused?'开启动效':'暂停动效';$('#motion-toggle').setAttribute('aria-pressed',String(paused))}
setTheme(savedPreference('audio-theme')==='light'?'light':'dark');
const reducedMotion=matchMedia('(prefers-reduced-motion: reduce)');
const initialMotion=savedPreference('audio-motion');
setMotion(initialMotion==='paused'||(initialMotion!=='on'&&reducedMotion.matches));
$('#theme-toggle').onclick=()=>{const next=document.documentElement.dataset.theme==='light'?'dark':'light';setTheme(next);savePreference('audio-theme',next)};
$('#motion-toggle').onclick=()=>{const paused=document.documentElement.dataset.motion!=='paused';setMotion(paused);savePreference('audio-motion',paused?'paused':'on')};
reducedMotion.addEventListener('change',e=>{if(!savedPreference('audio-motion'))setMotion(e.matches)});

function outputSource(job){
  const source=el('div',undefined,'output-source');
  source.append(el('span',`生成于 ${beijingTime(job.created*1000)}`,'detail'),button('查看原文与回听',async()=>{if(edited){message('请先保存当前文字修改，再切换来源');return}if(activeJob!==job.id)await openJob(job.id,false,true);$('#materials').open=true;$('#result').scrollIntoView({behavior:'smooth',block:'start'})}));
  return source;
}
async function loadProjects(){
  const projects=await api('projects'),box=$('#project-catalog'),scrollTop=box.scrollTop;box.replaceChildren();
  $('#project-catalog-heading').textContent=`已识别项目线索 · ${projects.length} 项`;
  const names={need:'需求',workflow:'业务流程',decision:'方案选择',delivery:'推进交付',result:'当前结果',commercial:'商业模式',value:'业务价值'};
  for(const project of projects){
    const card=el('article',undefined,'project-lead');
    card.append(el('h4',project.title));
    card.append(el('p',`${project.stage} · ${project.readiness} · ${project.facts} 条事实 · ${project.state==='completed'?'本批已检查':'仍在处理'}`,'detail'));
    for(const fact of project.highlights||[])card.append(el('p',`${names[fact.kind]||fact.kind}：${fact.text}${fact.nature==='计划'?'（计划）':''}`));
    if(project.missing?.length)card.append(el('p',`待补充：${project.missing.join('；')}`,'detail'));
    card.append(button('查看对应录音与成果',()=>openJob(project.job_id)));
    box.append(card);
  }
  if(!projects.length)box.append(el('p','还没有提取到项目线索；历史录音正在逐批分析。','detail'));
  box.scrollTop=scrollTop;
}
function caseStamp(row){return row?JSON.stringify([row.id,row.job_id,row.case_index,row.title,row.updated,row.version,row.locked,row.summary_available]):''}
function requestedCase(){try{return new URLSearchParams(location.hash.slice(1)).get('case')||''}catch{return ''}}
function setCaseHash(id){const url=new URL(location.href);url.hash=id?'case='+encodeURIComponent(id):'';history.replaceState(null,'',url)}
function caseHighlights(){
  for(const card of document.querySelectorAll('.case-library-card'))card.setAttribute('aria-pressed',String(card.dataset.case===selectedCase));
}
function renderCaseLibrary(){
  const box=$('#case-list'),scrollTop=box.scrollTop,query=$('#case-search').value.trim().toLocaleLowerCase();
  const rows=caseLibrary.filter(c=>!query||[c.title,c.excerpt,c.stage].join(' ').toLocaleLowerCase().includes(query));
  const stamp=JSON.stringify([query,rows.map(caseStamp)]);
  $('#case-list-meta').textContent=`共 ${caseLibrary.length} 篇口播稿${query?' · 找到 '+rows.length+' 篇':''} · 点击一篇，同时看口播与商业总结`;
  if(stamp!==caseListStamp){
    caseListStamp=stamp;box.replaceChildren();
    for(const row of rows){
      const card=el('button',undefined,'case-library-card');card.type='button';card.dataset.case=row.id;card.setAttribute('aria-controls','generated');
      card.append(el('span',row.stage||'项目案例','case-library-stage'),el('strong',row.title||'项目案例口播稿'));
      card.append(el('span',row.excerpt||'点击查看完整口播和商业总结','case-library-excerpt'));
      const date=beijingTime((row.updated||row.created)*1000).slice(0,16);
      card.append(el('span',`${date}${row.locked?' · 已保留':''}${row.summary_available?' · 附商业总结':''}`,'case-library-date'));
      card.onclick=()=>openCase(row.id,{scroll:true}).catch(e=>message(e.message));box.append(card);
    }
    if(!rows.length)box.append(el('p',query?'没有找到匹配的口播，换个关键词试试。':'暂时还没有可用口播稿。未处理录音将在每日计划中自动整理。','case-library-empty'));
    box.scrollTop=scrollTop;
  }
  caseHighlights();
}
async function loadCaseLibrary({initial=false}={}){
  const request=++caseListRequest;
  let rows;
  try{rows=await api('cases')}catch(e){if(request===caseListRequest)$('#case-list-meta').textContent='口播列表暂时无法读取，请点击刷新重试。';throw e}
  if(request!==caseListRequest)return;
  caseLibrary=Array.isArray(rows)?rows:[];renderCaseLibrary();
  const selected=caseLibrary.find(c=>c.id===selectedCase);
  if(selectedCase&&!selected){
    caseRequest++;selectedCase='';selectedCaseStamp='';currentCaseDetail=null;setCaseHash('');
    clearCaseDetail('这篇口播已归档或不再可用，请从列表中选择其他口播。');
  }else if(selected&&selectedCaseStamp&&caseStamp(selected)!==selectedCaseStamp){
    pendingCaseRefresh=true;$('#show-update').hidden=false;$('#show-update').textContent='当前口播有新版 · 更新';
  }
  if(initial){
    const wanted=requestedCase()||selectedCase||caseLibrary[0]?.id;
    if(wanted)await openCase(wanted,{scroll:false});
    else clearCaseDetail('暂无可用口播稿；新成果完成后会自动出现在上方列表。');
  }
}
async function showDefaultOutputs(){await loadCaseLibrary({initial:true})}
function clearCaseDetail(note){
  $('#content-title').textContent='选一篇口播，开始准备';$('#output-context').textContent=note;
  $('#script-result').replaceChildren(el('p','从上方列表选一篇口播，即可查看完整内容。'));
  $('#summary').replaceChildren(el('p','商业总结会与所选口播一起展示。'));
  $('#case-summary-label').textContent='理解需求、价值与经营逻辑';
  pendingCaseRefresh=false;$('#show-update').hidden=true;caseHighlights();
}
async function openCase(id,{scroll=false,fromHash=false}={}){
  const request=++caseRequest;
  $('#player').pause();$('#playerbox').hidden=true;
  selectedCase=id;selectedCaseStamp='';currentCaseDetail=null;caseHighlights();
  const title=caseLibrary.find(c=>c.id===id)?.title;
  $('#content-title').textContent=title||'正在读取口播稿';$('#output-context').textContent='正在读取这篇口播与商业总结…';
  $('#script-result').replaceChildren(el('p','正在读取口播正文…'));$('#summary').replaceChildren(el('p','正在读取对应商业总结…'));
  pendingCaseRefresh=false;$('#show-update').hidden=true;
  try{
    const detail=await api('cases/'+encodeURIComponent(id));
    if(request!==caseRequest||!csrf)return;
    if(!detail.case?.text)throw Error('这篇口播内容暂不可用，请刷新列表。');
    currentCaseDetail=detail;selectedCase=id;selectedCaseStamp=caseStamp(caseLibrary.find(c=>c.id===id));
    renderCaseDetail(detail);caseHighlights();
    if(!fromHash)setCaseHash(id);
    if(scroll)$('#generated').scrollIntoView({behavior:'smooth',block:'start'});
  }catch(e){
    if(request!==caseRequest)return;
    selectedCase='';currentCaseDetail=null;
    clearCaseDetail(e.status===404?'这篇口播已归档或链接已失效，请在上方选择其他口播。':e.message);
    if(e.status!==401)message(e.status===404?'这篇口播暂不可用，其他口播仍可查看。':e.message);
  }
}
function renderCaseDetail(detail){
  const c=detail.case,s=detail.script||{},summary=detail.summary,box=$('#script-result');
  const job={id:detail.job_id,created:detail.created||detail.updated};
  $('#content-title').textContent=c.title||'项目案例口播稿';
  $('#output-context').textContent=`${c.stage||'项目案例'} · ${beijingTime((detail.updated||detail.created)*1000)}${detail.locked?' · 已保留此版':''} · 不限制口播时长`;
  box.replaceChildren();box.append(outputSource(job));
  const input=el('textarea');input.className='script-editor';input.readOnly=true;input.value=c.text;input.setAttribute('aria-label',(c.title||'商业案例')+'口播正文');box.append(input);
  const toolbar=el('div',undefined,'toolbar');
  toolbar.append(button('复制口播稿',async()=>{try{await navigator.clipboard.writeText(c.text);message('口播稿已复制')}catch{input.focus();input.select();message('请按 Ctrl+C 复制已选中的口播稿')}}),button('下载口播稿',()=>downloadText(c.title,c.title+'\n\n'+c.text)));
  box.append(toolbar);
  const refs=new Map((s.evidence||[]).map(e=>[e.id,e])),sources=el('details');sources.append(el('summary','原文依据与待补充信息'));
  for(const item of c.missing_details||[])sources.append(el('p','待补充：'+item,'detail'));
  for(const ref of c.refs||[]){const evidence=refs.get(ref);if(evidence)sources.append(el('p',evidence.text),button('回听 '+time(evidence.start_ms),()=>play(evidence.recording_id,evidence.start_ms)))}
  box.append(sources);
  const summaryBox=$('#summary');summaryBox.replaceChildren();
  const scope=detail.summary_scope;
  $('#case-summary-label').textContent=scope==='case'?'按本案例原文依据关联':scope==='batch'?'同批商业总结 · 包含其他项目':'等待商业总结';
  if(!summary?.items?.length){summaryBox.append(el('p','这篇口播暂时没有可关联的商业总结。口播正文和原文依据仍可查看。'));return}
  if(scope==='batch')summaryBox.append(el('p','以下是同一批录音的商业总结，可能包含其他项目，供理解完整讨论背景。','case-summary-note'));
  const text=summary.items.map(i=>i.heading+'\n'+i.text).join('\n\n'),tools=el('div',undefined,'toolbar');
  tools.append(button('复制商业总结',async()=>{try{await navigator.clipboard.writeText(text);message('商业总结已复制')}catch{const range=document.createRange();range.selectNodeContents(summaryBox);const selection=getSelection();selection.removeAllRanges();selection.addRange(range);message('请按 Ctrl+C 复制已选中的总结')}}),button('下载商业总结',()=>downloadText((c.title||'项目案例')+'-商业总结','# '+(c.title||'项目案例')+'\n\n'+text,'md')));
  summaryBox.append(tools);
  const summaryRefs=new Map((summary.evidence||[]).map(e=>[e.id,e]));
  for(const item of summary.items){const row=el('div',undefined,'summaryitem');row.append(el('strong',item.heading),el('p',item.text));for(const ref of item.refs||[]){const evidence=summaryRefs.get(ref);if(evidence)row.append(button('回听 '+time(evidence.start_ms),()=>play(evidence.recording_id,evidence.start_ms)))}summaryBox.append(row)}
}
$('#case-search').oninput=renderCaseLibrary;
$('#case-refresh').onclick=async()=>{const b=$('#case-refresh');b.disabled=true;try{await loadCaseLibrary({initial:!selectedCase});message('口播列表已刷新')}catch(e){message(e.message)}finally{b.disabled=false}};
window.addEventListener('hashchange',()=>{const id=requestedCase();if(id&&csrf&&id!==selectedCase)openCase(id,{scroll:true,fromHash:true}).catch(e=>message(e.message))});
function renderAutomation(){
  const status=$('#auto-status'),toggle=$('#auto-toggle'),state=automationState;
  if(!state){status.textContent='自动处理状态暂不可用 · 已有口播可继续查看';toggle.disabled=true;toggle.textContent='自动处理状态暂不可用';return}
  const waiting=Number(state.waiting_budget)||0,pending=Number(state.pending)||0,transcribing=Number(state.transcribing)||0,daily=state.daily;
  $('#automation-status').dataset.state=waiting?'waiting':state.enabled?'enabled':'paused';
  if(daily?.enabled){
    status.textContent=`北京时间每天 ${daily.processing_time||'09:00'} 整理新录音 · ${daily.send_time||'11:00'} 推送企业微信群`;
    if(daily.transport==='bot'){
      if(!daily.bot_configured)status.textContent+='（智能机器人待配置）';
      else if(!daily.bot_connected)status.textContent+='（机器人未连接）';
      else if(!daily.group_bound)status.textContent+='（机器人已连接，待绑定群）';
      else status.textContent+='（机器人已连接，日报群已绑定）';
    }
    else if(!daily.webhook_configured)status.textContent+='（群机器人待配置）';
    else if(!daily.delivery_enabled)status.textContent+='（群推送已暂停）';
    if(!state.enabled)status.textContent+=' · 自动处理已暂停';
    else if(waiting)status.textContent+=' · 部分任务等待预算';
    else if(transcribing)status.textContent+=` · ${transcribing} 项转写中`;
    const working=dashboardJobs.filter(j=>j.stage==='summarizing'&&j.state==='running').length;
    if(working)status.textContent+=` · ${working} 项案例整理中`;
    if(daily.uncertain_messages)status.textContent+=' · 有群消息需要确认发送结果';
    else if(daily.last_delivery?.state==='failed')status.textContent+=' · 最近群推送失败';
    else if(daily.last_delivery?.state==='sending')status.textContent+=' · 正在发送今日成果';
    const delivery=daily.last_delivery,deliveryLabels={missing_webhook:'待配置机器人',missing_bot:'待配置智能机器人',bot_disconnected:'机器人未连接',awaiting_group:'待绑定日报群',disabled:'推送已暂停',ready:'待发送',sending:'发送中',sent:'已发送',failed:'发送失败',uncertain:'发送结果待确认'};
    const robotInfo=daily.transport==='bot'
      ?(daily.group_bound?'日报群已绑定。':'请将智能机器人加入接收日报的群，并发送绑定指令。')+(daily.bot_error?daily.bot_error+'。':'')
      :(daily.webhook_configured?'群机器人已配置。':'群机器人尚未配置，配置后才会发送。');
    $('#auto-scope').textContent=`每天北京时间 ${daily.processing_time||'09:00'} 开始处理尚未完成的录音，${daily.send_time||'11:00'} 汇总已完成口播发送到企业微信群。未完成内容保留进度，完成后进入后续推送。${robotInfo}${daily.first_processing_day?'定时处理从 '+daily.first_processing_day+' 开始。':''}${daily.first_send_day?'定时推送从 '+daily.first_send_day+' 开始。':''}${delivery?'推送状态：'+(delivery.day?delivery.day+' · ':'')+(deliveryLabels[delivery.state]||'待处理')+' · 已发送 '+(delivery.sent_messages||0)+'/'+(delivery.total_messages||0)+' 条。':''}${Number(daily.pending_cases)>0?'待推送 '+daily.pending_cases+' 篇。':''}暂停自动处理只停止派发新任务，已入队任务继续处理，录音仍会保存。`;
  }else{
    status.textContent=!state.enabled?'自动派发已暂停 · 已入队任务继续处理':waiting?'部分内容等待预算 · 录音继续保存':transcribing?`自动转写中 · ${transcribing} 项正在处理${pending?'，'+pending+' 项待处理':''}`:'自动处理已开启 · 新录音完成后自动整理';
    const history=state.history;
    if(history?.total)status.textContent+=` · 历史录音已提炼 ${Number(history.generated??history.analyzed)||0}/${history.total} 段`;
    $('#auto-scope').textContent='新录音由后台自动整理；暂停仅停止派发新任务，已入队任务继续处理，录音仍会保存。';
  }
  if(state.last_error)status.textContent+=' · 有异常待处理';
  toggle.disabled=false;toggle.textContent=state.enabled?'暂停新任务派发':'恢复自动处理';toggle.setAttribute('aria-pressed',String(!!state.enabled));
}
async function loadFeed(){
  try{
    const feed=await api('feed');feedEntries=Array.isArray(feed.entries)?feed.entries:[];
    if(feed.automation)automationState=feed.automation;
    if(!feed.automation)automationState=await api('automation');
    feedSupported=true;
  }catch(e){
    if(e.status===401)throw e;
    if(e.status===404||e.status===405){feedSupported=false;feedEntries=[];try{automationState=await api('automation')}catch{automationState=null}}
    else{automationState=null;$('#auto-status').textContent='自动处理状态暂不可用'}
  }
  renderAutomation();
}
$('#show-update').onclick=async()=>{if(!pendingCaseRefresh||!selectedCase)return;const b=$('#show-update');b.disabled=true;try{await openCase(selectedCase,{scroll:false})}finally{b.disabled=false}};
$('#materials-toggle').onclick=()=>{$('#materials').open=!$('#materials').open;if($('#materials').open)$('#materials').scrollIntoView({behavior:'smooth',block:'start'})};
$('#auto-toggle').onclick=async()=>{if(!automationState)return;const b=$('#auto-toggle');b.disabled=true;try{const result=await api('automation',{enabled:!automationState.enabled});automationState=result.enabled!==undefined?result:await api('automation');renderAutomation()}catch(e){message(e.message)}finally{b.disabled=!automationState}};
