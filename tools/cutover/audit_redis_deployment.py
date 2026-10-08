"""Explicit read-only audit authorized for scope 81/[205].
No raw business values, credentials or service locators are printed. Writes only
aggregate/metadata receipts to the supplied private evidence directory.
The identity audit expects private_catalog_snapshot.json in that directory.
"""
from pathlib import Path
import argparse


def run(evidence_dir, service_root):
    import json,re,subprocess,hashlib,socket,sys
    from pathlib import Path
    from datetime import datetime,timezone
    sys.path.insert(0,str(service_root))
    from app.config import Settings
    from urllib.parse import urlsplit
    root=evidence_dir
    cmd=['rg','--files','--hidden','E:/YouoAgent','E:/yy','-g','*.yml','-g','*.yaml','-g','*.conf','-g','*.ps1','-g','*.sh','-g','Dockerfile*','-g','!node_modules','-g','!.git','-g','!.venv','-g','!venv','-g','!beifen','-g','!__pycache__','-g','!target']
    paths=subprocess.check_output(cmd,text=True,encoding='utf-8').splitlines()
    patterns={'redis':'redis','volume':'volume|/data','aof':'appendonly|appendfsync|appendonlydir','rdb':'dbfilename|dump.rdb|save [0-9]','restart':'restart:|restart=|Restart=','backup_restore':'backup|restore|BGSAVE|redis-cli.*SAVE'}
    records=[]
    for name in paths:
     p=Path(name)
     if p.stat().st_size>2000000:continue
     data=p.read_text(encoding='utf-8-sig',errors='replace')
     if not re.search(r'\bredis\b',data,re.I):continue
     found={k:[i for i,line in enumerate(data.splitlines(),1) if re.search(pattern,line,re.I)] for k,pattern in patterns.items()}
     records.append({'path':str(p),'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'keyword_line_evidence':found,'proves_target_deployment':False})
    host=urlsplit(Settings().effective_redis_url()).hostname
    target_addresses=sorted({i[4][0] for i in socket.getaddrinfo(host,None)})
    raw=subprocess.check_output(['powershell','-NoProfile','-Command','Get-NetIPAddress | Select-Object -ExpandProperty IPAddress'],text=True).splitlines()
    out={'observed_at':datetime.now(timezone.utc).isoformat(),'searched_files':len(paths),'redis_related_files':records,'configured_agent_target_is_local_interface':bool(set(target_addresses)&set(raw)),
     'scope':'Repository and accessible workspace deployment configuration only; keyword presence is not deployed-volume evidence',
     'target_volume_verified':False,'target_restart_policy_verified':False,'backup_restore_drill_verified':False,'writes':0}
    (root/'redis_deployment_audit.json').write_text(json.dumps(out,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(out))


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--evidence-dir',type=Path,required=True)
    parser.add_argument('--service-root',type=Path,required=True)
    args=parser.parse_args()
    if not args.evidence_dir.is_dir():
        parser.error('Private evidence directory must already exist')
    run(args.evidence_dir,args.service_root)
