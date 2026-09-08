"""Operator-only catalog publication; existing HTTP/Agent contracts are unchanged.

verify opens an already initialized isolated catalog index read-only. initialize,
publish and reactivate are explicit operations for an approved runtime target.
They must never run as part of ordinary requests, imports or offline evaluation.
"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from catalog_publication import CatalogPublication
from catalog_generation import configured_embedding_contract
from catalog_registry import RedisCatalogReleaseRegistry
from catalog_release import CatalogEvidenceError, catalog_scope
from catalog_store import open_catalog_store


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["verify","initialize","publish","reactivate"])
    parser.add_argument("--semantic-model-id", required=True, type=int)
    parser.add_argument("--business-domain-id", type=int, action="append", default=[])
    parser.add_argument("--publication-id")
    parser.add_argument("--producer-revision")
    parser.add_argument("--vector-index-version")
    args=parser.parse_args(argv)
    try:
        scope=catalog_scope(args.semantic_model_id,args.business_domain_id)
        if args.action=="publish" and (not args.publication_id or not args.producer_revision):
            raise CatalogEvidenceError("CATALOG_RELEASE_IDENTITY_MISSING")
        if args.action=="reactivate" and not args.vector_index_version:
            raise CatalogEvidenceError("CATALOG_RELEASE_IDENTITY_MISSING")
        store=open_catalog_store(initialize=args.action=="initialize")
        registry=RedisCatalogReleaseRegistry(store.catalog_target_identity)
        publication=CatalogPublication(store,registry)
        if args.action=="initialize":
            receipt={"target_identity_hash":registry.target_identity_hash}
        elif args.action=="publish":
            from embedding import embed_documents
            contract=configured_embedding_contract()
            receipt=publication.publish(args.semantic_model_id,scope["business_domain_ids"],embed_fn=embed_documents,
                publication_id=args.publication_id,producer_revision=args.producer_revision,embedding_contract=contract)
        elif args.action=="reactivate":
            receipt=publication.reactivate(args.semantic_model_id,scope["business_domain_ids"],args.vector_index_version)
        else:
            receipt=publication.pin(args.semantic_model_id,scope["business_domain_ids"]).finish()
        report={"status":"VERIFIED_INDEX_GENERATION" if args.action=="verify" else "OPERATION_COMPLETED",
                "action":args.action,**receipt,"cutover_readiness":"REQUIRES_DEPLOYMENT_AND_CAPABILITY_EVIDENCE"}
    except CatalogEvidenceError as exc:
        print(json.dumps({"status":"BLOCKED","reason_code":str(exc)}))
        return 2
    except Exception:
        # Do not print connection credentials, source SQL or private metadata.
        print(json.dumps({"status":"BLOCKED","reason_code":"CATALOG_OPERATION_FAILED"}))
        return 2
    print(json.dumps(report,sort_keys=True))
    return 0


if __name__=="__main__":
    raise SystemExit(main())
