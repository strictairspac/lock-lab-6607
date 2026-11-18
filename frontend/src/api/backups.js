import {api} from './client.js';
export const backupsApi={
  listBackups:()=>api('/backups'),
  create:()=>api('/backups','POST'),
  listRestores:()=>api('/restores'),
  startRestore:backupId=>api(`/backups/${backupId}/restore`,'POST'),
  verify:restoreId=>api(`/restores/${restoreId}/verify`,'POST'),
};
