"""Opt-in, previewed collection-membership removal; never delete library items."""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import time

import ingest as ing
import local_state
import zotero as z

VERSION = "zotero-input.removal-plan/1"


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def source_snapshot(paths):
    return [{"path": str(Path(p).resolve()), "sha256": hashlib.sha256(Path(p).read_bytes()).hexdigest()} for p in paths]


def check_plan(plan, *, check_sources=True):
    if plan.get("schema_version") != VERSION or plan.get("integrity") != digest({k: v for k, v in plan.items() if k != "integrity"}):
        raise ValueError("Removal plan changed; regenerate and review it")
    if not plan.get("complete") or not plan.get("source_count"):
        raise ValueError("Resolve source extraction/matching gaps and import additions before removal")
    if check_sources:
        for source in plan["sources"]:
            if hashlib.sha256(Path(source["path"]).read_bytes()).hexdigest() != source["sha256"]:
                raise ValueError("Source changed after preview; regenerate the removal plan")


def plan_removals(references, backend, collection, *, sources=()):
    backend.collection(collection)
    library = backend.library
    target = backend.target_items(collection)
    retained, unresolved = set(), []
    for ref in references:
        found, detail = ing.match_reference(ref, target, library)
        if ref.get("extraction_errors") or not found or detail["ambiguous"]:
            unresolved.append({"document_id": ref.get("document_id"), "reference_id": ref.get("reference_id"),
                               "reason": ref.get("extraction_errors") or detail.get("reason") or "Reference not verified in target; import/review it first"})
        else:
            retained.add(found["key"])
    candidates = []
    for item in target:
        if collection not in item["data"].get("collections", []) or not ing.scoped(item, library):
            raise ing.Uncertain("Target snapshot has inconsistent membership/library")
        if item["key"] not in retained:
            candidates.append({"item": ing.identity(item, library), "data": deepcopy(item["data"]),
                               "data_hash": digest(item["data"]), "version": item.get("version")})
    plan = {"schema_version": VERSION, "library": library.json(), "target_collection": collection,
            "source_count": len(references), "sources": source_snapshot(sources), "unresolved": unresolved,
            "target_keys": sorted(i["key"] for i in target), "candidates": candidates,
            "large_change": len(candidates) > max(5, len(target) * 0.25),
            "complete": bool(references) and not unresolved, "operation": "remove_target_membership_only"}
    plan["integrity"] = digest(plan)
    return plan


def verify_removed(candidate, backend, collection):
    current = backend.get_item(candidate["item"]["item_key"])
    if ing.identity(current, backend.library) != candidate["item"] or current["data"].get("deleted"):
        raise ing.Uncertain("Reference identity changed or item was deleted")
    members = set(current["data"].get("collections", []))
    old = set(candidate["data"].get("collections", []))
    if not (old - {collection}).issubset(members):
        raise ing.Uncertain("Other collection memberships were changed")
    keys = {i["key"] for i in backend.target_items(collection)}
    return collection not in members and candidate["item"]["item_key"] not in keys


def apply_removals(plan, backend, *, approved=False, source_complete=False, acknowledge_large_change=False,
                   checks=3, delay=0.3, verify_only=False):
    check_plan(plan, check_sources=not verify_only)
    if not 1 <= checks <= 5:
        raise ValueError("Verification checks must be between 1 and 5")
    if not verify_only and (approved is not True or source_complete is not True):
        raise PermissionError("Review the preview and explicitly confirm removal and source completeness")
    if not verify_only and plan["large_change"] and acknowledge_large_change is not True:
        raise PermissionError("Large membership change requires explicit acknowledgment after preview")
    collection = plan["target_collection"]
    backend.collection(collection)
    if backend.library.json() != plan["library"]:
        raise ing.Uncertain("Removal library identity changed")
    present = {i["key"] for i in backend.target_items(collection)}
    candidates = {c["item"]["item_key"] for c in plan["candidates"]}
    if present - set(plan["target_keys"]) or set(plan["target_keys"]) - present - candidates:
        raise ing.Uncertain("Target changed since preview; regenerate before writing")
    # Validate every candidate before making any change.
    for candidate in plan["candidates"]:
        if not verify_removed(candidate, backend, collection):
            current = backend.get_item(candidate["item"]["item_key"])
            if digest(current["data"]) != candidate["data_hash"]:
                raise ing.Uncertain("Candidate changed since preview; regenerate before writing")
    results, denied = [], False
    for candidate in plan["candidates"]:
        row = {"item": candidate["item"], "outcome": "pending", "membership_removed_verified": False, "reason": None}
        try:
            if verify_removed(candidate, backend, collection):
                row.update(outcome="already_removed", membership_removed_verified=True)
            elif not verify_only:
                if denied:
                    raise ing.PermissionDenied("An earlier write was denied; remaining writes stopped")
                if not backend.can_remove():
                    raise ing.Unavailable("No supported removal writer; use the reviewed native script/UI route")
                error = None
                try:
                    backend.remove(candidate["item"]["item_key"], collection)
                except Exception as exc:
                    error = exc
                for attempt in range(checks):
                    if verify_removed(candidate, backend, collection):
                        row.update(outcome="removed", membership_removed_verified=True, reason=str(error) if error else None)
                        break
                    if attempt + 1 < checks:
                        time.sleep(delay)
                if not row["membership_removed_verified"]:
                    raise error or ing.Uncertain("Removal not verified; no automatic repeat")
        except Exception as exc:
            denied = denied or isinstance(exc, ing.PermissionDenied)
            row.update(outcome="failed" if isinstance(exc, ing.PermissionDenied) else "pending", reason=str(exc))
        results.append(row)
    for candidate, row in zip(plan["candidates"], results):
        if row["membership_removed_verified"]:
            try:
                if not verify_removed(candidate, backend, collection):
                    raise ing.Uncertain("Final membership verification failed")
            except Exception as exc:
                row.update(outcome="pending", membership_removed_verified=False, reason=str(exc))
    return {"schema_version": "zotero-input.removal-results/1", "operation": plan["operation"],
            "complete": all(r["membership_removed_verified"] for r in results), "items": results}


def native_script(plan, *, approved=False, source_complete=False, acknowledge_large_change=False):
    check_plan(plan)
    if not approved or not source_complete or (plan["large_change"] and not acknowledge_large_change):
        raise PermissionError("Reviewed removal/source-completeness approval required")
    template = Path(__file__).with_name("zotero_remove_membership.js").read_text(encoding="utf-8")
    return template.replace("__PLAN_JSON__", json.dumps(plan, ensure_ascii=True))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    preview = subs.add_parser("plan")
    inputs = preview.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--source", action="append")
    inputs.add_argument("--references-json")
    preview.add_argument("--collection", required=True)
    preview.add_argument("--library-type", choices=["user", "group"], default="user")
    preview.add_argument("--library-id", type=int, default=0)
    preview.add_argument("--encoding")
    for name in ("apply", "verify", "native-script"):
        command = subs.add_parser(name)
        command.add_argument("--plan", required=True)
        if name != "verify":
            command.add_argument("--approve-removals", action="store_true")
            command.add_argument("--confirm-source-complete", action="store_true")
            command.add_argument("--acknowledge-large-change", action="store_true")
        if name == "apply":
            command.add_argument("--plugin-url", default="http://127.0.0.1:23120/mcp")
    for command in subs.choices.values():
        command.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    local_state.preflight_output(args.out, sources=[getattr(args, "plan", None), getattr(args, "references_json", None), *(getattr(args, "source", None) or [])])
    if args.command == "plan":
        sources = args.source or [args.references_json]
        if args.references_json:
            value = json.loads(Path(args.references_json).read_text(encoding="utf-8-sig"))
            if value.get("schema_version") != "zotero-input.references/1":
                raise ValueError("Unsupported structured source schema")
            refs = ing.normalize_sources(value["documents"])
        else:
            refs = ing.file_sources(args.source, encoding=args.encoding)
        import provenance_notes
        backend = ing.LocalBackend(ing.Library(args.library_type, args.library_id), provenance_notes.NoWriter())
        result = plan_removals(refs, backend, args.collection, sources=sources)
    else:
        plan = json.loads(Path(args.plan).read_text(encoding="utf-8-sig"))
        flags = {} if args.command == "verify" else {"approved": args.approve_removals, "source_complete": args.confirm_source_complete,
                                                        "acknowledge_large_change": args.acknowledge_large_change}
        if args.command == "native-script":
            text = native_script(plan, **flags)
            with Path(args.out).open("x", encoding="utf-8") as handle:
                handle.write(text)
            print(json.dumps({"script": args.out, "written_to_zotero": False}))
            return 0
        import provenance_notes
        writer = ing.Plugin(args.plugin_url) if args.command == "apply" else provenance_notes.NoWriter()
        backend = ing.LocalBackend(ing.Library(plan["library"]["type"], plan["library"]["id"]), writer)
        result = apply_removals(plan, backend, verify_only=args.command == "verify", **flags)
    local_state.atomic_json(args.out, result, replace=False)
    print(json.dumps({"complete": result["complete"], "result": args.out}))
    return 0 if result["complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
