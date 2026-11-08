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
          f"skip={counts['skip']} garmin-only={counts['garmin_only']}")
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

    args = parser.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
