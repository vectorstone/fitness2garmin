"""f2g — CLI for the Apple Health → Garmin Connect pipeline."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from . import __version__
from .apple_export import (
    bodymass_from_json,
    bodymass_to_json,
    parse_export,
    workouts_from_json,
    workouts_to_json,
)
from .fit_builder import write_fit
from .garmin_client import GarminGateway, upload_many
from .keep_export import (
    KeepRecord,
    parse_keep_export,
    records_from_json,
    records_to_json,
)
from .keep_plan import build_keep_plan, summarize as keep_summarize
from .plan import build_plan, summarize
from .state import Ledger

DATA_DIR = Path("data")
DEFAULT_ZIP = DATA_DIR / "export.zip"
WORKOUTS_JSON = "workouts.json"
BODYMASS_JSON = "bodymass.json"
FIT_DIR = "fit"
INVENTORY_JSON = "garmin_activities.json"
PLAN_JSON = "plan.json"
STATE_DB = "state.db"
TOKENS = "garmin_tokens.json"
KEEP_WORKOUTS_JSON = "keep_workouts.json"
KEEP_FIT_DIR = "keep_fit"
KEEP_PLAN_JSON = "keep_plan.json"


def _d(args) -> Path:
    return Path(args.data)


# --- commands ----------------------------------------------------------------


def cmd_login(args):
    import getpass

    gateway = GarminGateway(_d(args) / TOKENS)
    email = args.email or input("Garmin email: ")
    password = args.password or getpass.getpass("Garmin password: ")
    # force: drop any stale tokens (e.g. wrong region) before credential login
    gateway.login(email=email, password=password, is_cn=args.cn, force=True)
    print("[ok] login succeeded, tokens cached")


def cmd_parse(args):
    zip_path = Path(args.zip)
    if not zip_path.exists():
        raise SystemExit(f"export archive not found: {zip_path}")
    workouts, body = parse_export(zip_path)
    out_dir = _d(args)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / WORKOUTS_JSON).write_text(
        json.dumps(workouts_to_json(workouts), ensure_ascii=False, indent=1)
    )
    (out_dir / BODYMASS_JSON).write_text(
        json.dumps(bodymass_to_json(body), ensure_ascii=False, indent=1)
    )
    with_gps = sum(1 for w in workouts if w.gps)
    with_hr = sum(1 for w in workouts if w.hr)
    print(f"[ok] {len(workouts)} workouts parsed")
    print(f"     GPS track: {with_gps} | HR series: {with_hr}")
    print(f"     body-mass days: {len(body)}")
    for w in workouts:
        if not w.hr and not w.gps:
            print(f"     [warn] no HR and no GPS: {w.id}")


def _load_workouts(args) -> list:
    path = _d(args) / WORKOUTS_JSON
    if not path.exists():
        raise SystemExit("run 'f2g parse' first")
    return workouts_from_json(json.loads(path.read_text()))


def cmd_convert(args):
    workouts = _load_workouts(args)
    out_dir = _d(args) / FIT_DIR
    ok = skipped = 0
    for w in workouts:
        if not w.hr and not w.gps:
            skipped += 1
            continue
        try:
            path = write_fit(w, out_dir)
            ok += 1
        except Exception as e:  # noqa: BLE001
            print(f"[fail] {w.id}: {e}")
    print(f"[ok] {ok} FIT files written to {out_dir} ({skipped} skipped, no data)")


def _garmin_years(workouts: list) -> tuple[int, int]:
    if not workouts:
        now = datetime.now()
        return now.year, now.year
    return workouts[0].start.year, workouts[-1].start.year + 1


def cmd_inventory(args):
    workouts = _load_workouts(args)
    y_from, y_to = _garmin_years(workouts)
    print(f"[garmin] fetching inventory {y_from}..{y_to - 1}")
    gateway = GarminGateway(_d(args) / TOKENS)
    items = gateway.inventory(y_from, y_to)
    out = _d(args) / INVENTORY_JSON
    out.write_text(json.dumps(items, ensure_ascii=False, indent=1))
    print(f"[ok] {len(items)} activities -> {out}")


def _load_inventory(args) -> list:
    path = _d(args) / INVENTORY_JSON
    if not path.exists():
        raise SystemExit("run 'f2g inventory' first")
    return json.loads(path.read_text())


def cmd_plan(args):
    workouts = _load_workouts(args)
    inventory = _load_inventory(args)
    plan = build_plan(workouts, inventory)
    counts = summarize(plan)
    out = _d(args) / PLAN_JSON
    out.write_text(
        json.dumps(
            [
                {
                    "action": p.action,
                    "workout_id": p.workout.id if p.workout else None,
                    "garmin_id": p.garmin.id if p.garmin else None,
                    "reason": p.reason,
                }
                for p in plan
            ],
            ensure_ascii=False,
            indent=1,
        )
    )
    print(f"[plan] upload={counts['upload']} replace={counts['replace']} "
          f"skip={counts['skip']} garmin-only={counts['garmin_only']} "
          f"apple-dup={counts.get('apple_dup', 0)} no-data={counts.get('no_data', 0)}")
    for p in plan:
        if p.action == "replace":
            print(f"  replace: {p.workout.id} ({p.reason})")
    print(f"[ok] plan written to {out}")


def cmd_clean(args):
    plan = json.loads((_d(args) / PLAN_JSON).read_text())
    targets = [p for p in plan if p["action"] == "replace"]
    if not targets:
        print("[clean] nothing to delete (no 'replace' items in plan)")
        return
    print(f"[clean] {len(targets)} Garmin activities marked for deletion:")
    for p in targets:
        print(f"  - id={p['garmin_id']} ({p['reason']})")
    if not args.apply:
        print("[clean] dry run — re-run with --apply to actually delete")
        return
    gateway = GarminGateway(_d(args) / TOKENS)
    ledger = Ledger(_d(args) / STATE_DB)
    done = failed = 0
    for p in targets:
        try:
            gateway.delete(p["garmin_id"])
            done += 1
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"[fail] delete {p['garmin_id']}: {e}")
    print(f"[ok] deleted {done}, failed {failed} (ledger not updated; "
          f"re-run 'f2g plan' after deletion)")


def _fragment_targets(args) -> list[tuple]:
    """garmin-only + before 2026 + type 'other' — old broken-import fragments.

    Native Garmin-watch activities keep real types (running/walking/…);
    import fragments land as auto-named 'other'. Returns (plan_item, activity).
    """
    plan = json.loads((_d(args) / PLAN_JSON).read_text())
    acts = {
        a.get("activityId"): a
        for a in json.loads((_d(args) / INVENTORY_JSON).read_text())
    }
    targets = []
    for p in plan:
        if p["action"] != "garmin_only":
            continue
        a = acts.get(p["garmin_id"])
        if not a:
            continue
        start = a.get("startTimeLocal") or ""
        type_key = (a.get("activityType") or a.get("activityTypeDTO") or {}).get(
            "typeKey", ""
        )
        if start[:4] < "2026" and type_key == "other":
            targets.append((p, a))
    return targets


def cmd_clean_fragments(args):
    targets = _fragment_targets(args)
    if not targets:
        print("[clean-fragments] no fragments matched (garmin-only, <2026, type 'other')")
        return
    print(f"[clean-fragments] {len(targets)} fragment activities to delete:")
    for p, a in targets:
        print(
            f"  - id={p['garmin_id']}  {a.get('activityName', '')[:28]:28} "
            f"{(a.get('startTimeLocal') or '')[:16]}  "
            f"{int(a.get('duration') or 0)}s {int(a.get('distance') or 0)}m"
        )
    if not args.apply:
        print("[clean-fragments] dry run — re-run with --apply to actually delete")
        return
    gateway = GarminGateway(_d(args) / TOKENS)
    ledger = Ledger(_d(args) / STATE_DB)
    done = failed = 0
    for p, a in targets:
        try:
            gateway.delete(p["garmin_id"])
            ledger.mark(
                f"garmin@{p['garmin_id']}",
                "deleted",
                detail=a.get("activityName", ""),
            )
            done += 1
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"[fail] delete {p['garmin_id']}: {e}")
    print(f"[ok] deleted {done}, failed {failed}")


def cmd_upload(args):
    plan = json.loads((_d(args) / PLAN_JSON).read_text())
    wanted = {"upload"} | ({"replace"} if args.include_replace else set())
    items = [p for p in plan if p["action"] in wanted]
    if not items:
        print("[upload] nothing to upload per plan")
        return
    workouts = {w.id: w for w in _load_workouts(args)}
    from .fit_builder import fit_filename

    fit_dir = _d(args) / FIT_DIR
    files: list[Path] = []
    ids: dict[str, str] = {}  # fit filename -> workout id
    for p in items:
        w = workouts.get(p["workout_id"])
        if w is None:
            continue
        f = fit_dir / fit_filename(w)
        if f.exists():
            files.append(f)
            ids[f.name] = w.id
        else:
            print(f"[skip] FIT missing for {p['workout_id']} (run 'f2g convert')")
    ledger = Ledger(_d(args) / STATE_DB)
    already = ledger.uploaded_ids()
    keep = [f for f in files if ids[f.name] not in already]
    if len(keep) < len(files):
        print(f"[upload] skipping {len(files) - len(keep)} already-uploaded (ledger)")
    if not keep:
        print("[upload] nothing to do")
        return
    print(f"[upload] uploading {len(keep)} FIT files (silent, 2s pacing)…")
    gateway = GarminGateway(_d(args) / TOKENS)

    def on_result(path, result):
        ledger.mark(ids[path.name], result["status"], fit_path=str(path))
        print(f"  {path.name}: {result['status']}")

    summary = upload_many(gateway, keep, on_result=on_result)
    print(f"[ok] uploaded={summary['uploaded']} duplicate={summary['duplicate']} "
          f"failed={summary['failed']}")
    for err in summary["errors"][:10]:
        print(f"  [err] {err}")


def cmd_weight(args):
    path = _d(args) / BODYMASS_JSON
    if not path.exists():
        raise SystemExit("run 'f2g parse' first")
    body = bodymass_from_json(json.loads(path.read_text()))
    if not body:
        print("[weight] no body-mass records in export")
        return
    gateway = GarminGateway(_d(args) / TOKENS)
    ledger = Ledger(_d(args) / STATE_DB)
    sent = skipped = 0
    for b in body:
        key = f"weight@{b.ts.date().isoformat()}"
        if ledger.status_of(key) in ("uploaded", "duplicate"):
            skipped += 1
            continue
        try:
            gateway.add_weight(b.ts.isoformat(), b.kg, b.fat_percent)
            ledger.mark(key, "uploaded", detail=f"{b.kg}kg")
            sent += 1
        except Exception as e:  # noqa: BLE001
            print(f"[fail] {key}: {e}")
    print(f"[ok] weigh-ins sent={sent} skipped={skipped} total={len(body)}")


def cmd_status(args):
    d = _d(args)
    print(f"data dir: {d.resolve()}")
    for name, path in [
        ("export.zip", d / "export.zip"),
        ("workouts", d / WORKOUTS_JSON),
        ("bodymass", d / BODYMASS_JSON),
        ("fit files", d / FIT_DIR),
        ("inventory", d / INVENTORY_JSON),
        ("plan", d / PLAN_JSON),
        ("tokens", d / TOKENS),
    ]:
        if path.is_dir():
            print(f"  {name}: {len(list(path.glob('*')))} files")
        elif path.exists():
            print(f"  {name}: exists")
        else:
            print(f"  {name}: —")
    if (d / STATE_DB).exists():
        print(f"  ledger: {Ledger(d / STATE_DB).summary()}")


def cmd_sync(args):
    cmd_parse(args)
    cmd_convert(args)
    cmd_inventory(args)
    cmd_plan(args)
    print("[sync] plan ready. Review data/plan.json, then: f2g clean --apply && f2g upload")


# --- Keep export commands -----------------------------------------------------


def _keep_dir(args) -> Path:
    return Path(args.dir)


def cmd_keep_parse(args):
    src = _keep_dir(args)
    if not src.exists():
        raise SystemExit(f"Keep export dir not found: {src}")
    records = parse_keep_export(src)
    out = _d(args) / KEEP_WORKOUTS_JSON
    out.write_text(json.dumps(records_to_json(records), ensure_ascii=False, indent=1))
    # Persist each bundle summary FIT now: keep-convert later overwrites the
    # ones that have an HR series with richer built FITs.
    fit_dir = _d(args) / KEEP_FIT_DIR
    fit_dir.mkdir(parents=True, exist_ok=True)
    for r in records:
        if r.fit_bytes:
            keep_fit_path(args, r.n).write_bytes(r.fit_bytes)
    hr = sum(1 for r in records if r.hr)
    print(f"[ok] {len(records)} Keep workouts parsed -> {out}")
    print(f"     HR series: {hr} | span {records[-1].start_local.date()} … "
          f"{records[0].start_local.date()}")
    print(f"     {KEEP_FIT_DIR}: {len(list(fit_dir.glob('*.fit')))} summary FITs")


def _load_keep_records(args) -> list[KeepRecord]:
    path = _d(args) / KEEP_WORKOUTS_JSON
    if not path.exists():
        raise SystemExit("run 'f2g keep-parse' first")
    return records_from_json(json.loads(path.read_text()))


def keep_fit_path(args, n: int) -> Path:
    return _d(args) / KEEP_FIT_DIR / f"keep_{n:04d}.fit"


def cmd_keep_convert(args):
    from .fit_builder import build_fit

    records = {r.n: r for r in _load_keep_records(args)}
    plan = json.loads((_d(args) / KEEP_PLAN_JSON).read_text())
    out_dir = _d(args) / KEEP_FIT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    built = missing = 0
    for p in plan:
        if p["action"] not in ("upload", "replace"):
            continue
        r = records[p["n"]]
        path = keep_fit_path(args, r.n)
        if r.hr:
            path.write_bytes(build_fit(r.to_workout()))
            built += 1
        elif not path.exists():
            missing += 1
            print(f"[skip] #{r.n} {r.type} has neither HR nor bundle FIT")
    print(f"[ok] {built} FIT rebuilt with HR series, missing={missing} -> {out_dir}")


def cmd_keep_plan(args):
    records = _load_keep_records(args)
    inventory = _load_inventory(args)
    items = build_keep_plan(records, inventory)
    counts = keep_summarize(items)
    out = _d(args) / KEEP_PLAN_JSON
    out.write_text(json.dumps([it.to_json() for it in items], ensure_ascii=False, indent=1))
    print(f"[keep-plan] upload={counts.get('upload', 0)} "
          f"replace={counts.get('replace', 0)} skip={counts.get('skip', 0)} "
          f"junk={counts.get('junk', 0)} twin-ghost={counts.get('twin_ghost', 0)}")
    for it in items:
        if it.action == "replace":
            print(f"  replace: #{it.n} -> garmin {it.garmin_id} ({it.reason})")
    print(f"[ok] plan written to {out}")


def cmd_keep_clean(args):
    plan = json.loads((_d(args) / KEEP_PLAN_JSON).read_text())
    targets = [p for p in plan if p["action"] == "replace"]
    if not targets:
        print("[keep-clean] nothing to delete (no 'replace' items)")
        return
    print(f"[keep-clean] {len(targets)} garbage-duration Garmin activities:")
    for p in targets:
        print(f"  - id={p['garmin_id']}  #{p['n']}  {p['reason']}")
    if not args.apply:
        print("[keep-clean] dry run — re-run with --apply to actually delete")
        return
    gateway = GarminGateway(_d(args) / TOKENS)
    ledger = Ledger(_d(args) / STATE_DB)
    done = failed = 0
    for p in targets:
        try:
            gateway.delete(p["garmin_id"])
            ledger.mark(f"garmin@{p['garmin_id']}", "deleted", detail=p["reason"])
            done += 1
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"[fail] delete {p['garmin_id']}: {e}")
    print(f"[ok] deleted {done}, failed {failed}")


def cmd_keep_upload(args):
    plan = json.loads((_d(args) / KEEP_PLAN_JSON).read_text())
    wanted = {"upload"} | ({"replace"} if args.include_replace else set())
    items = [p for p in plan if p["action"] in wanted]
    if not items:
        print("[keep-upload] nothing to upload per keep-plan")
        return
    ledger = Ledger(_d(args) / STATE_DB)
    files: list[Path] = []
    ids: dict[str, str] = {}
    for p in items:
        key = f"keep@{p['n']:04d}"
        if ledger.status_of(key) in ("uploaded", "duplicate"):
            continue
        f = keep_fit_path(args, p["n"])
        if f.exists():
            files.append(f)
            ids[f.name] = key
        else:
            print(f"[skip] FIT missing for #{p['n']} (run 'f2g keep-convert')")
    if not files:
        print("[keep-upload] nothing to do (ledger clean / no FIT files)")
        return
    print(f"[keep-upload] uploading {len(files)} FIT files (silent, 2s pacing)…")
    gateway = GarminGateway(_d(args) / TOKENS)

    def on_result(path, result):
        ledger.mark(ids[path.name], result["status"], fit_path=str(path))
        print(f"  {path.name}: {result['status']}")

    summary = upload_many(gateway, files, on_result=on_result)
    print(f"[ok] uploaded={summary['uploaded']} duplicate={summary['duplicate']} "
          f"failed={summary['failed']}")
    for err in summary["errors"][:10]:
        print(f"  [err] {err}")


def cmd_keep_sync(args):
    cmd_keep_parse(args)
    cmd_keep_plan(args)
    cmd_keep_convert(args)
    print("[keep-sync] plan ready. Review data/keep_plan.json, then: "
          "f2g keep-clean --apply && f2g keep-upload --include-replace")


# --- argparse -----------------------------------------------------------------


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="f2g",
        description="Apple Health (Apple Watch) → Garmin Connect migration pipeline",
    )
    parser.add_argument("--version", action="version", version=__version__)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--data", default=str(DATA_DIR), help="data directory")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add(name: str, help: str) -> argparse.ArgumentParser:
        return sub.add_parser(name, help=help, parents=[common])

    p = add("login", "interactive Garmin login (saves tokens)")
    p.add_argument("--email", default=None)
    p.add_argument("--password", default=None)
    p.add_argument("--cn", action="store_true", help="use garmin.cn (China)")
    p.set_defaults(fn=cmd_login)

    p = add("parse", help="parse export.zip → workouts.json")
    p.add_argument("--zip", default=str(DEFAULT_ZIP))
    p.set_defaults(fn=cmd_parse)

    p = add("convert", help="workouts.json → FIT files")
    p.set_defaults(fn=cmd_convert)

    p = add("inventory", help="fetch Garmin activity inventory")
    p.set_defaults(fn=cmd_inventory)

    p = add("plan", help="compare Apple vs Garmin → plan.json")
    p.set_defaults(fn=cmd_plan)

    p = add("clean", help="delete Garmin activities marked 'replace'")
    p.add_argument("--apply", action="store_true", help="actually delete (default dry-run)")
    p.set_defaults(fn=cmd_clean)

    p = add("clean-fragments", help="delete old import fragments (garmin-only, <2026, type 'other')")
    p.add_argument("--apply", action="store_true", help="actually delete (default dry-run)")
    p.set_defaults(fn=cmd_clean_fragments)

    p = add("upload", help="upload planned FIT files to Garmin")
    p.add_argument("--include-replace", action="store_true",
                   help="also upload 'replace' items (run clean --apply first)")
    p.set_defaults(fn=cmd_upload)

    p = add("weight", help="push Apple Health weigh-ins to Garmin")
    p.set_defaults(fn=cmd_weight)

    p = add("status", help="show pipeline state")
    p.set_defaults(fn=cmd_status)

    p = add("sync", help="parse + convert + inventory + plan")
    p.add_argument("--zip", default=str(DEFAULT_ZIP))
    p.set_defaults(fn=cmd_sync)

    p = add("keep-parse", help="parse Keep export dir → keep_workouts.json")
    p.add_argument("--dir", default=str(DATA_DIR / "565994"), help="Keep export directory")
    p.set_defaults(fn=cmd_keep_parse)

    p = add("keep-plan", help="match Keep workouts vs Garmin → keep_plan.json")
    p.set_defaults(fn=cmd_keep_plan)

    p = add("keep-convert", help="build FIT files for keep-plan upload/replace items")
    p.set_defaults(fn=cmd_keep_convert)

    p = add("keep-clean", help="delete garbage-duration Garmin activities ('replace')")
    p.add_argument("--apply", action="store_true", help="actually delete (default dry-run)")
    p.set_defaults(fn=cmd_keep_clean)

    p = add("keep-upload", help="upload planned Keep FIT files to Garmin")
    p.add_argument("--include-replace", action="store_true",
                   help="also upload 'replace' items (run keep-clean --apply first)")
    p.set_defaults(fn=cmd_keep_upload)

    p = add("keep-sync", help="keep-parse + keep-plan + keep-convert")
    p.add_argument("--dir", default=str(DATA_DIR / "565994"), help="Keep export directory")
    p.set_defaults(fn=cmd_keep_sync)

    args = parser.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
