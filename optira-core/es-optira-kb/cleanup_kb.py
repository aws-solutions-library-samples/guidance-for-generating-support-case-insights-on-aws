#!/usr/bin/env python3
"""Clean up orphaned Optira Knowledge Base resources from failed deploys.

Each failed run of ``bedrock_kb_core.py`` can leave behind resources that are
NOT managed by CloudFormation (they are created via boto3 with a random
suffix): an OpenSearch Serverless collection ``bedrock-kb-<suffix>`` and its
policies ``kb-enc-<suffix>`` (encryption), ``kb-net-<suffix>`` (network) and
``kb-data-<suffix>`` (data access), plus possibly a FAILED Bedrock Knowledge
Base named ``optira-support-case-kb`` and a stale KB id in the
``optira/knowledge-base-id`` secret.

This helper removes those orphans. It is **dry-run by default** -- it only
prints what it would delete. Pass ``--confirm`` to actually delete.

It never deletes a collection that is still referenced by a Knowledge Base you
are keeping, and by default it only deletes Knowledge Bases in a FAILED state.

Usage:
    python3 cleanup_kb.py --region us-east-1                 # dry run
    python3 cleanup_kb.py --region us-east-1 --confirm       # delete FAILED KB + orphans
    python3 cleanup_kb.py --region us-east-1 --all-kbs --confirm      # also delete matching ACTIVE KBs
    python3 cleanup_kb.py --region us-east-1 --delete-secret --confirm
"""
from __future__ import annotations

import argparse
import time

import boto3

DEFAULT_KB_NAME = "optira-support-case-kb"
COLLECTION_PREFIX = "bedrock-kb-"
POLICY_PREFIXES = ("kb-enc-", "kb-net-", "kb-data-")
KB_SECRET_NAME = "optira/knowledge-base-id"


def _suffix_from_collection(name: str) -> str:
    return name[len(COLLECTION_PREFIX):] if name.startswith(COLLECTION_PREFIX) else ""


def _suffix_from_policy(name: str) -> str:
    for p in POLICY_PREFIXES:
        if name.startswith(p):
            return name[len(p):]
    return ""


# --------------------------- Knowledge Bases ------------------------------ #
def _list_knowledge_bases(bedrock):
    kbs = []
    token = None
    while True:
        kwargs = {"maxResults": 100}
        if token:
            kwargs["nextToken"] = token
        resp = bedrock.list_knowledge_bases(**kwargs)
        kbs.extend(resp.get("knowledgeBaseSummaries", []))
        token = resp.get("nextToken")
        if not token:
            return kbs


def _collection_arn_for_kb(bedrock, kb_id):
    try:
        kb = bedrock.get_knowledge_base(knowledgeBaseId=kb_id)["knowledgeBase"]
        cfg = kb.get("storageConfiguration", {}).get(
            "opensearchServerlessConfiguration", {}
        )
        return cfg.get("collectionArn")
    except Exception:
        return None


def _delete_knowledge_base(bedrock, kb_id, confirm):
    # Delete data sources first, then the KB.
    try:
        for ds in bedrock.list_data_sources(knowledgeBaseId=kb_id).get(
            "dataSourceSummaries", []
        ):
            print(f"    - data source {ds['dataSourceId']}")
            if confirm:
                bedrock.delete_data_source(
                    knowledgeBaseId=kb_id, dataSourceId=ds["dataSourceId"]
                )
    except Exception as e:
        print(f"    (could not list/delete data sources: {e})")
    if confirm:
        bedrock.delete_knowledge_base(knowledgeBaseId=kb_id)


# --------------------------- OpenSearch Serverless ------------------------ #
def _collection_name_from_arn(arn):
    # arn:aws:aoss:region:acct:collection/<id> -> we can't get the name from arn;
    # resolve via batch_get_collection by id.
    return arn.rsplit("/", 1)[-1] if arn else None


def _list_collections(aoss):
    cols = []
    token = None
    while True:
        kwargs = {"maxResults": 100}
        if token:
            kwargs["nextToken"] = token
        resp = aoss.list_collections(**kwargs)
        cols.extend(resp.get("collectionSummaries", []))
        token = resp.get("nextToken")
        if not token:
            return cols


def _in_use_collection_ids(bedrock, aoss):
    """Collection ids referenced by remaining Knowledge Bases (to keep)."""
    ids = set()
    for kb in _list_knowledge_bases(bedrock):
        arn = _collection_arn_for_kb(bedrock, kb["knowledgeBaseId"])
        cid = _collection_name_from_arn(arn)
        if cid:
            ids.add(cid)
    return ids


def _wait_collection_deleted(aoss, collection_id, timeout=180):
    start = time.time()
    while time.time() - start < timeout:
        try:
            resp = aoss.batch_get_collection(ids=[collection_id])
            if not resp.get("collectionDetails"):
                return True
        except Exception:
            return True
        time.sleep(10)
    return False


def _delete_policies(aoss, kept_suffixes, confirm):
    """Delete kb-enc/net/data-<suffix> policies whose suffix is orphaned."""
    deleted = []
    # encryption + network policies
    for ptype in ("encryption", "network"):
        token = None
        while True:
            kwargs = {"type": ptype, "maxResults": 100}
            if token:
                kwargs["nextToken"] = token
            resp = aoss.list_security_policies(**kwargs)
            for p in resp.get("securityPolicySummaries", []):
                name = p["name"]
                suf = _suffix_from_policy(name)
                if suf and suf not in kept_suffixes:
                    deleted.append((ptype, name))
                    print(f"  - {ptype} policy {name}")
                    if confirm:
                        try:
                            aoss.delete_security_policy(name=name, type=ptype)
                        except Exception as e:
                            print(f"    (failed: {e})")
            token = resp.get("nextToken")
            if not token:
                break
    # data access policies
    token = None
    while True:
        kwargs = {"type": "data", "maxResults": 100}
        if token:
            kwargs["nextToken"] = token
        resp = aoss.list_access_policies(**kwargs)
        for p in resp.get("accessPolicySummaries", []):
            name = p["name"]
            suf = _suffix_from_policy(name)
            if suf and suf not in kept_suffixes:
                deleted.append(("data", name))
                print(f"  - data access policy {name}")
                if confirm:
                    try:
                        aoss.delete_access_policy(name=name, type="data")
                    except Exception as e:
                        print(f"    (failed: {e})")
        token = resp.get("nextToken")
        if not token:
            break
    return deleted


def main() -> None:
    parser = argparse.ArgumentParser(description="Clean up orphaned Optira KB resources")
    parser.add_argument("--region", required=True)
    parser.add_argument("--kb-name", default=DEFAULT_KB_NAME)
    parser.add_argument(
        "--all-kbs",
        action="store_true",
        help="Delete ALL Knowledge Bases matching --kb-name (incl. ACTIVE), "
        "not just FAILED ones.",
    )
    parser.add_argument(
        "--delete-secret",
        action="store_true",
        help=f"Also delete the {KB_SECRET_NAME} secret.",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Actually delete. Without this flag the script only prints (dry run).",
    )
    args = parser.parse_args()

    mode = "DELETE" if args.confirm else "DRY RUN (nothing will be deleted)"
    print(f"=== Optira KB cleanup [{mode}] region={args.region} ===\n")

    bedrock = boto3.client("bedrock-agent", region_name=args.region)
    aoss = boto3.client("opensearchserverless", region_name=args.region)

    # 1) Knowledge Bases matching the name.
    print(f"Knowledge Bases named '{args.kb_name}':")
    to_delete = []
    for kb in _list_knowledge_bases(bedrock):
        if kb.get("name") != args.kb_name:
            continue
        status = kb.get("status")
        delete_it = args.all_kbs or status == "FAILED"
        flag = "-> delete" if delete_it else "keep"
        print(f"  {kb['knowledgeBaseId']} status={status} [{flag}]")
        if delete_it:
            to_delete.append(kb["knowledgeBaseId"])
    for kb_id in to_delete:
        print(f"  deleting KB {kb_id}:")
        _delete_knowledge_base(bedrock, kb_id, args.confirm)
    if not to_delete:
        print("  (no KBs selected for deletion)")

    # 2) Determine which collections are still in use (by KBs we keep).
    #    In dry-run the target KBs still exist, so exclude them explicitly.
    print("\nResolving in-use collections...")
    in_use = _in_use_collection_ids(bedrock, aoss)
    if not args.confirm:
        for kb_id in to_delete:
            arn = _collection_arn_for_kb(bedrock, kb_id)
            cid = _collection_name_from_arn(arn)
            if cid:
                in_use.discard(cid)
    print(f"  in-use collection ids kept: {sorted(in_use) or '[]'}")

    # 3) Orphaned collections (prefix match, not in use).
    print("\nOrphaned OpenSearch Serverless collections:")
    kept_suffixes = set()
    orphan_suffixes = set()
    any_orphan = False
    for col in _list_collections(aoss):
        name = col.get("name", "")
        if not name.startswith(COLLECTION_PREFIX):
            continue
        suffix = _suffix_from_collection(name)
        if col["id"] in in_use:
            kept_suffixes.add(suffix)
            print(f"  {name} (id={col['id']}) status={col.get('status')} [keep, in use]")
            continue
        any_orphan = True
        orphan_suffixes.add(suffix)
        print(f"  {name} (id={col['id']}) status={col.get('status')} -> delete")
        if args.confirm:
            try:
                aoss.delete_collection(id=col["id"])
                _wait_collection_deleted(aoss, col["id"])
            except Exception as e:
                print(f"    (failed: {e})")
    if not any_orphan:
        print("  (none)")

    # 4) Orphaned policies (suffix not among kept collections).
    print("\nOrphaned policies (encryption / network / data):")
    _delete_policies(aoss, kept_suffixes, args.confirm)

    # 5) Optionally delete the KB id secret.
    if args.delete_secret:
        print(f"\nSecret {KB_SECRET_NAME}: -> delete")
        if args.confirm:
            sm = boto3.client("secretsmanager", region_name=args.region)
            try:
                sm.delete_secret(
                    SecretId=KB_SECRET_NAME, ForceDeleteWithoutRecovery=True
                )
            except Exception as e:
                print(f"  (failed: {e})")

    print("\nDone." + ("" if args.confirm else "  (dry run -- re-run with --confirm to delete)"))


if __name__ == "__main__":
    main()
