"""Execute Claude's decisions that Kaileb approved.

How approval works:
  Claude opens a PR from branch  decision/YYYY-MM-DD  adding  decisions/YYYY-MM-DD.json.
  Kaileb merges it (= YES) or closes it (= NO).
  A decision is executed only if ALL of these hold:
    - it is a buy/sell (holds are just logged)
    - GitHub says its PR was merged, by the configured approver
    - it's within proposal_expiry_hours of being proposed
    - BTC hasn't moved more than proposal_max_price_move since the proposal
    - target_exposure is one of allowed_exposures
    - the account isn't paused/shut down (bot.cycle enforces)
Every decision gets exactly one outcome recorded in state/claude/executions.json.

  python execute.py                     (in GitHub Actions)
  python execute.py --offline CSV --trust-local   (local testing, skips GitHub check)
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import urllib.request
from datetime import datetime, timedelta, timezone

import bot

HERE = os.path.dirname(os.path.abspath(__file__))
DECISIONS = os.path.join(HERE, "decisions")


def load_decisions() -> list[tuple[str, dict]]:
    out = []
    for p in sorted(glob.glob(os.path.join(DECISIONS, "*.json"))):
        try:
            with open(p) as f:
                out.append((os.path.basename(p), json.load(f)))
        except Exception as e:
            bot.log(f"bad decision file {p}: {e}")
    return out


def load_exec(P: bot.Paths) -> dict:
    if os.path.exists(P.executions):
        with open(P.executions) as f:
            return json.load(f)
    return {}


def save_exec(P: bot.Paths, d: dict) -> None:
    os.makedirs(P.dir, exist_ok=True)
    with open(P.executions, "w") as f:
        json.dump(d, f, indent=2)


def merged_pr(name: str) -> dict | None:
    """Return {'number', 'merged_by', 'merged_at', 'url'} for the PR that added this decision."""
    repo, token = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_TOKEN")
    if not (repo and token):
        return None
    branch = "decision/" + name.removesuffix(".json")
    owner = repo.split("/")[0]
    url = f"https://api.github.com/repos/{repo}/pulls?state=closed&head={owner}:{branch}&per_page=5"
    hdr = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    with urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=20) as r:
        prs = json.load(r)
    for pr in prs:
        if pr.get("merged_at"):
            with urllib.request.urlopen(urllib.request.Request(pr["url"], headers=hdr), timeout=20) as r:
                full = json.load(r)
            return {"number": pr["number"], "merged_at": pr["merged_at"], "url": pr["html_url"],
                    "merged_by": (full.get("merged_by") or {}).get("login", "")}
    return None


def validate(d: dict, cfg: dict) -> str | None:
    if d.get("action") not in ("buy", "sell", "hold"):
        return "invalid action"
    if d.get("target_exposure") not in cfg["allowed_exposures"]:
        return f"target_exposure {d.get('target_exposure')} not in {cfg['allowed_exposures']}"
    try:
        float(d["price_at_proposal"])
        datetime.fromisoformat(d["created_at"])
    except Exception:
        return "missing/invalid price_at_proposal or created_at"
    return None


def execute_pending(cfg: dict, mkt: bot.Market, trust_local: bool = False) -> list[dict]:
    P = bot.Paths("claude")
    done = load_exec(P)
    results = []
    for name, d in load_decisions():
        if name in done:
            continue
        rec = {"decision": name, "action": d.get("action"), "target": d.get("target_exposure"),
               "checked_at": bot.now_utc().isoformat(), "price_now": mkt.price}
        err = validate(d, cfg)
        if err:
            rec["outcome"] = f"rejected_invalid: {err}"
        elif d["action"] == "hold":
            rec["outcome"] = "hold_logged"
        else:
            pr = None if trust_local else merged_pr(name)
            if not trust_local and not pr:
                import time
                time.sleep(10)            # merge event can beat the API by a few seconds
                pr = merged_pr(name)
            approver = cfg.get("approver")
            if not trust_local and not pr:
                # a buy/sell on main without a merged PR = it skipped Kaileb. Never execute it.
                rec["outcome"] = "rejected_no_approval: no merged PR for this decision"
            elif not trust_local and approver and pr["merged_by"].lower() != approver.lower():
                rec["outcome"] = f"rejected: merged by {pr['merged_by']}, not {approver}"
            else:
                created = datetime.fromisoformat(d["created_at"])
                move = mkt.price / float(d["price_at_proposal"]) - 1
                rec["pr"] = pr
                if bot.now_utc() > created + timedelta(hours=cfg["proposal_expiry_hours"]):
                    rec["outcome"] = "expired_approved_too_late"
                elif abs(move) > cfg["proposal_max_price_move"]:
                    rec["outcome"] = f"skipped_price_moved {move:+.1%}"
                else:
                    r = bot.cycle(cfg, "claude", mkt, float(d["target_exposure"]),
                                  f"Claude {name} (approved)", force=True)
                    rec["outcome"] = "executed" if r.get("action") in ("buy", "sell") else \
                        f"no_trade ({r.get('status')}, {r.get('action', '')})"
                    rec["fill"] = r.get("fill")
        done[name] = rec
        results.append(rec)
        bot.log(f"decision {name}: {rec['outcome']}")
    save_exec(P, done)
    return results


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline")
    ap.add_argument("--trust-local", action="store_true")
    a = ap.parse_args()
    c = bot.load_cfg()
    m = bot.Market(c, a.offline, live=(bot.acct_mode(c, "claude") == "live"))
    execute_pending(c, m, a.trust_local)
