"""Freeze evaluation inputs without exporting private captures or configuration."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from tools.cutover.evaluation_contract import digest

ROOT=Path(__file__).resolve().parents[2]
EVALUATOR_FILES=tuple('tools/cutover/'+name for name in (
    'harness_contract.py','harness_corpus.py','harness_fixtures.py','harness_observation.py',
    'harness_runtime.py','harness_replay.py','harness_splits.py','harness_manifest.py','harness_safety.py',
    'harness_cli.py','harness_dryplan.py','harness_transport_replay.py','harness_replay_corpus.py',
    'run_raw_transition_benchmark.py','transition_observations.py',
    'frozen_source_values.py','transition_evaluator.py'))


def files_hash(names):
    return {name:hashlib.sha256((ROOT/name).read_bytes().replace(b'\r\n',b'\n')).hexdigest() for name in names}


def evaluator_identity(commit):
    from tools.cutover.harness_contract import VERSION
    files=files_hash(EVALUATOR_FILES)
    return {'evaluator_version':VERSION,'evaluator_git_commit':commit,
            'evaluator_hash':digest(files),'evaluator_files':files}


def freeze_versions(catalog, *, commit,settings,candidate_snapshot,split_manifest=None):
    from app.semantic_v2.recognition import PARSE_PROMPT,DRAFT_PROMPT,PROMPT_VERSION,current_turn_schema,SemanticTaskDraft
    from app.semantic_v2.source_value_probe import PROBE_PROMPT,PROBE_PROMPT_VERSION
    from app.semantic_v2.pipeline import CandidateSelectionDecision
    import re
    prompts={name:{'version':PROBE_PROMPT_VERSION if name=='PROBE' else PROMPT_VERSION,
                    'hash':digest(value),'line_count':len(value.splitlines()),
                    'sentence_units':len([s for s in re.split(r'(?<=[.!?])\s+',' '.join(value.split())) if s])}
             for name,value in [('PARSE',PARSE_PROMPT),('DRAFT',DRAFT_PROMPT),('PROBE',PROBE_PROMPT)]}
    schemas={k:digest(v) for k,v in {'current_turn':current_turn_schema(),
        'semantic_draft_template':SemanticTaskDraft.model_json_schema(),
        'value_choice_template':CandidateSelectionDecision.model_json_schema()}.items()}
    source_paths=sorted(str(p.relative_to(ROOT)).replace('\\','/') for p in (ROOT/'app/semantic_v2').glob('*.py'))
    source_hashes=files_hash(source_paths)
    return {'manifest_version':'evaluation-input-freeze-v1','git_commit':commit,
        **evaluator_identity(commit),'prompt_freeze':'FROZEN_NO_SEMANTIC_EDITS','prompts':prompts,
        'schema_freeze':'FROZEN_TEMPLATES_WITH_RUNTIME_OFFERED_HANDLE_INSTANTIATION','schema_hashes':schemas,
        'catalog_version':catalog['catalog_version'],'catalog_hash':catalog['artifact_hash'],
        'scope':catalog['scope'],'candidate_snapshot_version':candidate_snapshot['version'],
        'candidate_snapshot_hash':candidate_snapshot['snapshot_hash'],
        'candidate_mode':'ALL_PINNED_ROLE_COMPATIBLE_ROWS_NOT_RANKED_LIVE_RETRIEVAL',
        'split_manifest_hash':split_manifest.get('manifest_hash') if split_manifest else None,
        'runtime_source_hashes':source_hashes,'runtime_source_hash':digest(source_hashes),
        'as_of':'2026-09-09T09:00:00+08:00','timezone':'Asia/Shanghai','calendar_version':'gregorian-business-calendar-v1',
        'model':settings.intent_model_name,'thinking_mode':settings.intent_model_enable_thinking,
        'temperature':0,'top_p':'NOT_SENT_PROVIDER_DEFAULT','seed':'NOT_SENT_SUPPORT_UNVERIFIED',
        'randomness':'PROVIDER_DETERMINISM_NOT_ESTABLISHED','retry_policy':{'max_retries':0},
        'timeout_seconds':settings.intent_model_timeout_seconds,'max_tokens':'NOT_SENT_PROVIDER_DEFAULT',
        'api_provider':'CONFIGURED_COMPATIBLE_GATEWAY','endpoint_hash':digest(settings.intent_model_base_url),
        'formal_model_benchmark_allowed':False,'semantic_candidate_baseline_id':None,
        'baseline_status':'HARNESS_CANDIDATE_NOT_SEMANTIC_FREEZE_CANDIDATE'}


def freeze_candidates(publication,case):
    from app.domain.models import TrustedIdentity
    from app.semantic_v2.catalog_bridge import ScopedPlanSession,RECORD_TYPES
    from tools.cutover.harness_fixtures import request_for
    session=ScopedPlanSession(request_for(case),TrustedIdentity(tenant_id='evaluation',user_id='evaluation'),publication)
    values={}
    for kind in sorted({v[0] for v in RECORD_TYPES.values()}):
        values[str(kind)]=session.candidates(kind)
    session.accept_catalog()
    material={'version':'frozen-native-candidate-universe-v1','scope':case['scope'],
        'catalog_version':session.context.catalog_pin.catalog_version,'catalog_hash':case['catalog_ref'],
        'candidates':values,'selection_contract':'RawTurnPlanner._candidates current mention role filter and opaque handles',
        'live_retrieval_quality':'NOT_EVALUATED'}
    return {**material,'snapshot_hash':digest(material)}


def verify_frozen(manifest):
    current=evaluator_identity(manifest['evaluator_git_commit'])
    if current['evaluator_hash']!=manifest['evaluator_hash']:
        raise ValueError('EVALUATOR_CHANGED_REOPEN_BASELINE')
    if files_hash(manifest['runtime_source_hashes'])!=manifest['runtime_source_hashes']:
        raise ValueError('RUNTIME_OR_PROMPT_CHANGED_REOPEN_BASELINE')


def write_json(path,value):
    Path(path).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
