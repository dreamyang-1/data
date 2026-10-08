"""Explicit read-only audit authorized for scope 81/[205].
No raw business values, credentials or service locators are printed. Writes only
aggregate/metadata receipts to the supplied private evidence directory.
The identity audit expects private_catalog_snapshot.json in that directory.
"""
from pathlib import Path
import argparse


def run(evidence_dir, service_root):
    import contextlib,io,json,sys,hashlib,logging,os
    from pathlib import Path
    from datetime import datetime,timezone
    from urllib.parse import urlsplit
    sys.path.insert(0,str(service_root))
    logging.disable(logging.CRITICAL)
    from app.config import Settings
    import redis
    root=evidence_dir
    digest=lambda v: hashlib.sha256(json.dumps(v,sort_keys=True).encode()).hexdigest()
    with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
     s=Settings(); url=s.effective_redis_url()
     parts=urlsplit(url or '')
     kwargs=redis.connection.parse_url(url) if url else {}
     result={'observed_at':datetime.now(timezone.utc).isoformat(),'service':'DataAnalysis_Agent',
      'settings_env':s.env,'settings_source':'Current process environment plus Settings env_file precedence; not running-process introspection',
      'mode':s.session_store_mode,'effective_mode':'memory' if s.env=='test' else s.session_store_mode,
      'host_hash':digest(parts.hostname),'port':kwargs.get('port',6379),'db':kwargs.get('db',0),
      'tls':parts.scheme=='rediss','credential_present':bool(parts.password),'explicit_url_present':bool(s.redis_url),
      'session_key_prefix':s.session_key_prefix,'session_ttl':s.session_ttl_seconds,'response_ttl':s.response_cache_ttl_seconds,
      'target_identity_hash':digest([parts.hostname,kwargs.get('port',6379),kwargs.get('db',0)]),'production_writes':0}
     if url:
      c=redis.Redis.from_url(url,socket_connect_timeout=8,socket_timeout=8,decode_responses=True)
      try:
       result['persistence']={k:v for k,v in c.info('persistence').items() if k in ('aof_enabled','aof_last_write_status','rdb_last_bgsave_status','rdb_last_save_time','loading','rdb_changes_since_last_save')}
       result['replication']={k:v for k,v in c.info('replication').items() if k in ('role','connected_slaves','master_link_status','master_sync_in_progress','slave_read_only')}
       result['config']={}
       for k in ('appendonly','appendfsync','save','dir','dbfilename'):
        try:
         v=c.config_get(k).get(k)
         result['config'][k]={'value_hash':digest(v),'present':bool(v)} if k=='dir' else v
        except Exception as exc: result['config'][k]={'error_type':type(exc).__name__}
       result['status']='READ_ONLY_RUNTIME_VERIFIED'
      except Exception as exc:result.update(status='READ_UNAVAILABLE',error_type=type(exc).__name__)
      finally:c.close()
     else:result['status']='NO_EFFECTIVE_URL'
    result['production_process_revision_verified']=False
    result['deployment_volume_or_restore_drill_verified']=False
    (root/'redis_session_audit.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result))


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--evidence-dir',type=Path,required=True)
    parser.add_argument('--service-root',type=Path,required=True)
    args=parser.parse_args()
    if not args.evidence_dir.is_dir():
        parser.error('Private evidence directory must already exist')
    run(args.evidence_dir,args.service_root)
