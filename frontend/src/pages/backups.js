import {table} from '../components/common/table.js';
import {esc,date} from '../utils/html.js';
import {BACKUP_STATES,RESTORE_STATES,RUN_STATES} from '../constants/enums.js';

const stateBadge=(map,value)=>`<span class="badge ${esc(value)}">${esc(map[value]||value)}</span>`;

const manifestSummary=m=>!m?'<span class="muted">—</span>':
  `<small>${m.tables_total} 张表 · 快照 ${date(m.snapshot_at)}<br>校验和 ${esc(m.checksum.slice(0,16))}…</small>`;

const restoreRows=restores=>restores.map(r=>[
  `<strong>恢复 #${r.id}</strong><small>备份 #${r.backup_id} · ${date(r.created_at)}</small>`,
  stateBadge(RESTORE_STATES,r.status),
  `<small>${Object.entries(r.progress||{}).map(([k,v])=>`${esc(k)}:${v.rows}行`).join(' · ')||'尚未载入'}</small>`,
  r.verification
    ? (r.verification.passed
      ? `<small class="ok">对账通过：${r.verification.checks.length} 项检查<br>${r.verification.checked_at?date(r.verification.checked_at):''}</small>`
      : `<small class="error">对账失败：${esc((r.verification.failures||[]).join('；'))}</small>`)
    : (r.error?`<small class="error">${esc(r.error)}</small>`:'<small>等待对账</small>'),
  `<button class="mini" data-action="verify" data-id="${r.id}" ${r.status==='verifying'?'disabled':''}>重新验证</button>`
]);

export function backupsPage(backups,restores){
  const busyBackups=backups.some(b=>b.status==='running');
  return `<div class="split">
<section>
  <div class="section-top"><h2>备份记录</h2><span>POINT-IN-TIME SNAPSHOT</span></div>
  ${backups.length?table(['备份 / 时间','状态','快照与校验','操作'],backups.map(b=>[
    `<strong>${esc(b.backup_code)}</strong><small>${date(b.created_at)} · ${esc(b.triggered_by)}</small>`,
    stateBadge(BACKUP_STATES,b.status),
    b.status==='failed'?`<small class="error">${esc(b.error)}</small>`:manifestSummary(b.manifest),
    `<button class="mini" data-action="restore" data-id="${b.id}" ${b.status!=='completed'?'disabled':''}>恢复到隔离库</button>`
  ])):'<div class="empty">还没有备份。点击右侧按钮发起一次同提交时点快照。</div>'}
  <div class="section-top" style="margin-top:34px"><h2>恢复与对账</h2><span>ISOLATED RESTORE</span></div>
  ${restores.length?table(['恢复任务','状态','续跑进度（断点）','对账结果','操作'],restoreRows(restores))
    :'<div class="empty">尚无恢复任务。对一份可用备份发起恢复后，在此跟踪断点续跑与对账。</div>'}
</section>
<section class="form-panel">
  <span class="eyebrow">BACKUP &amp; RECOVERY</span>
  <h2>样本库可恢复性验证</h2>
  <p class="help">备份在单个快照事务内导出样本台账、流转申请、演练、镜像账与回执，所有数据取自同一提交时点；备份期间登记和演练照常并发。</p>
  <button class="primary" id="create-backup" ${busyBackups?'disabled':''}>发起一次备份 ↗</button>
  <p class="help">恢复目标始终是独立隔离库：已完成演练保留全部回执；未完成演练标记为「${esc(RUN_STATES.interrupted)}」且不再执行。恢复中断后可对同一备份续跑，事件经 outbox 幂等发布，重试不重复通知。</p>
  <div class="protocol"><h3>可用性判定（三级对账）</h3><ol>
    <li>备份文件 SHA-256 与 manifest 一致</li>
    <li>各表行数与快照内容逐项对账</li>
    <li>数量守恒、回执完整、中断状态合规</li>
  </ol><p class="help">只有三级对账全部通过，恢复库才显示「${esc(RESTORE_STATES.available)}」；任一失败都会列出具体原因。</p></div>
</section></div>`;
}
