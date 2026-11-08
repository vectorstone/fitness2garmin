"""Garmin Connect gateway: login (token-cached, MFA-aware), inventory, upload, delete."""

from __future__ import annotations

import getpass
import json
import time
from pathlib import Path

from garminconnect import Garmin


class GarminGateway:
    def __init__(self, tokenstore: Path):
        self.tokenstore = tokenstore
        self.config_path = tokenstore.parent / "config.json"
        self._client: Garmin | None = None

    def _is_cn(self) -> bool:
        try:
            return bool(json.loads(self.config_path.read_text()).get("is_cn"))
        except Exception:  # noqa: BLE001 — missing/corrupt config means global
            return False

    # -- login ---------------------------------------------------------------

    def login(
        self,
        email: str | None = None,
        password: str | None = None,
        is_cn: bool | None = None,
        force: bool = False,
    ) -> Garmin:
        if self._client is not None:
            return self._client
        if is_cn is None:
            is_cn = self._is_cn()
        if force:
            self.tokenstore.unlink(missing_ok=True)
        elif self.tokenstore.exists():
            g = Garmin("", "", is_cn=is_cn)
            try:
                g.login(tokenstore=str(self.tokenstore))
                self._client = g
                return g
            except Exception as e:  # noqa: BLE001 — surface a clear message
                msg = str(e)
                hint = ""
                if "doesn't exist in DB" in msg or "Invalid Username" in msg:
                    hint = (
                        "\nHint: your account may be on Garmin China (garmin.cn). "
                        "Run 'f2g login --cn'."
                    )
                raise SystemExit(
                    f"[garmin] token login failed: {msg}{hint}\n"
                    f"Run 'f2g login' to re-authenticate."
                )
        if not (email and password):
            raise SystemExit(
                "No valid tokenstore. Run 'f2g login' once (interactive)."
            )
        g = Garmin(email, password, is_cn=is_cn, prompt_mfa=self._prompt_mfa)
        # login(tokenstore=...) persists tokens itself on credential login.
        g.login(tokenstore=str(self.tokenstore))
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(json.dumps({"is_cn": is_cn}))
        print(f"[garmin] tokens saved to {self.tokenstore} (is_cn={is_cn})")
        self._client = g
        return g

    @staticmethod
    def _prompt_mfa() -> str:
        return input("MFA code: ")

    # -- inventory -------------------------------------------------------------

    def inventory(self, year_from: int, year_to: int) -> list[dict]:
        """Fetch every activity between years (inclusive) from Garmin."""
        g = self.login()
        all_items: list[dict] = []
        for year in range(year_from, year_to + 1):
            items = g.get_activities_by_date(
                f"{year}-01-01", f"{year}-12-31", None, "asc"
            )
            all_items.extend(items or [])
            print(f"[garmin] {year}: {len(items or [])} activities")
        return all_items

    # -- upload ----------------------------------------------------------------

    def upload(self, fit_path: Path) -> dict:
        """Upload one activity file. Returns {'status': 'uploaded'|'duplicate', ...}"""
        g = self.login()
        try:
            resp = g.upload_activity(str(fit_path))
            return {"status": "uploaded", "response": _short(resp)}
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            if "duplicate" in msg.lower() or "409" in msg:
                return {"status": "duplicate", "detail": msg[:200]}
            if "429" in msg or "TooManyRequests" in type(e).__name__:
                # rate limited: back off and retry once before giving up
                print(f"[garmin] rate limited on {fit_path.name}, waiting 60s…")
                time.sleep(60)
                try:
                    resp = g.upload_activity(str(fit_path))
                    return {"status": "uploaded", "response": _short(resp)}
                except Exception as e2:  # noqa: BLE001
                    msg = str(e2)
                    if "duplicate" in msg.lower() or "409" in msg:
                        return {"status": "duplicate", "detail": msg[:200]}
                    raise
            raise

    # -- delete ------------------------------------------------------------------

    def delete(self, activity_id: int | str) -> None:
        g = self.login()
        g.delete_activity(str(activity_id))

    # -- weigh-ins ----------------------------------------------------------------

    def add_weight(self, ts_iso: str, kg: float, fat_percent: float | None) -> dict:
        g = self.login()
        return g.add_body_composition(
            timestamp=ts_iso,
            weight=kg,
            percent_fat=fat_percent,
        )


def _short(obj, limit: int = 200) -> str:
    try:
        s = json.dumps(obj)
    except Exception:
        s = str(obj)
    return s[:limit]


def upload_many(
    gateway: GarminGateway,
    files: list[Path],
    on_result=None,
    pause_s: float = 2.0,
) -> dict:
    """Sequential silent upload with pacing. Returns summary counters."""
    summary = {"uploaded": 0, "duplicate": 0, "failed": 0, "errors": []}
    for i, path in enumerate(files):
        try:
            result = gateway.upload(path)
            summary[result["status"]] += 1
            if on_result:
                on_result(path, result)
        except Exception as e:  # noqa: BLE001
            summary["failed"] += 1
            summary["errors"].append(f"{path.name}: {e}")
        if i < len(files) - 1:
            time.sleep(pause_s)
    return summary
