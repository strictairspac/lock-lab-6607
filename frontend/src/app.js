import {api,download} from './api/client.js';
import {samplesStore} from './stores/samples.js';import {transfersStore} from './stores/transfers.js';import {trialsStore} from './stores/trials.js';
import {backupsApi} from './api/backups.js';
import {session} from './hooks/session.js';import {feedback} from './hooks/feedback.js';import {formValues} from './utils/forms.js';import {esc,date} from './utils/html.js';
import {frame} from './components/common/frame.js';import {table} from './components/common/table.js';import {loginPage} from './pages/login.js';import {samplesPage} from './pages/samples.js';import {transfersPage} from './pages/transfers.js';import {trialsPage} from './pages/trials.js';import {evidencePage} from './pages/evidence.js';import {backupsPage} from './pages/backups.js';import {titles} from './router/routes.js';
let timer=null;const app=document.querySelector('#app');
async function submitForm(id,path,numbers,after){document.querySelector(id)?.addEventListener('submit',async e=>{e.preventDefault();const button=e.target.querySelector('button');button.disabled=true;try{const data=await api(path,'POST',formValues(e.target,numbers));await after(data);feedback('操作完成，记录已保存。');}catch(err){feedback(err.message,true);}finally{button.disabled=false;}})}
function bindBackupActions(refresh){
  document.querySelector('#create-backup')?.addEventListener('click',async e=>{
    e.target.disabled=true;
    try{await backupsApi.create();feedback('备份完成，快照已生成。');await refresh();}
    catch(err){feedback(err.message,true);e.target.disabled=false;}
  });
  document.querySelectorAll('[data-action="restore"]').forEach(btn=>btn.addEventListener('click',async()=>{
    btn.disabled=true;
    try{const r=await backupsApi.startRestore(btn.dataset.id);feedback(r.status==='available'?'恢复完成，三级对账通过，隔离库可用。':'恢复任务已启动。');await refresh();}
    catch(err){feedback(err.message,true);btn.disabled=false;}
  }));
  document.querySelectorAll('[data-action="verify"]').forEach(btn=>btn.addEventListener('click',async()=>{
    btn.disabled=true;
    try{const r=await backupsApi.verify(btn.dataset.id);feedback(r.verification?.passed?'验证通过，恢复库可用。':'验证未通过，请查看失败原因。');await refresh();}
    catch(err){feedback(err.message,true);btn.disabled=false;}
  }));
}
async function render(){clearTimeout(timer);if(!session.token){app.innerHTML=loginPage();submitForm('#login','/auth/login',[],async data=>{session.save(data);location.hash='samples';await render()});return;}
try{await api('/auth/me');await Promise.all([samplesStore.refresh(),transfersStore.refresh(),trialsStore.refresh()]);const [page='samples',id]=location.hash.slice(1).split('/');let content;const s=samplesStore.items,t=transfersStore.items,r=trialsStore.items;let trial=null;let backupData=null;
if(page==='transfers')content=transfersPage(t,s);else if(page==='trials')content=trialsPage(r,t);else if(page==='evidence'){trial=id?await api('/trials/'+id):r[0];content=evidencePage(trial)}else if(page==='backups'){const [backups,restores]=await Promise.all([backupsApi.listBackups(),backupsApi.listRestores()]);backupData={backups,restores};content=backupsPage(backups,restores);}else if(page==='audit'){const logs=await api('/audit');content=table(['时间','操作人','动作'],logs.map(x=>[date(x.created_at),esc(x.actor),esc(x.action)]))}else content=samplesPage(s);
const [title,subtitle]=titles[page]||titles.samples;app.innerHTML=frame(page,title,subtitle,content);document.querySelector('#logout').onclick=()=>{session.clear();render()};submitForm('#sample-form','/samples',['quantity'],render);submitForm('#transfer-form','/transfers',['sample_id','quantity'],render);submitForm('#trial-form','/trials',['transfer_id','timeout_ms'],async data=>{location.hash='evidence/'+data.id;await render()});document.querySelector('#export')?.addEventListener('click',async e=>{try{await download(e.target.dataset.id);feedback('证据 JSON 已下载')}catch(err){feedback(err.message,true)}});if(page==='backups')bindBackupActions(render);
const trialBusy=(page==='evidence'&&trial&&['queued','running'].includes(trial.status))||(page==='trials'&&r.some(x=>['queued','running'].includes(x.status)));
const backupBusy=backupData&&(backupData.backups.some(b=>b.status==='running')||backupData.restores.some(x=>['running','verifying','interrupted'].includes(x.status)));
if(trialBusy||backupBusy)timer=setTimeout(render,1500);
}catch(err){if(err.message.includes('登录')||err.message.includes('Not authenticated')){session.clear();await render()}else {if(!document.querySelector('#feedback'))app.innerHTML='<main><div id="feedback"></div><button onclick="location.reload()">重新加载</button></main>';feedback(err.message,true)}}}
window.addEventListener('hashchange',render);render();
