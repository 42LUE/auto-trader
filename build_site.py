"""Builds site/data.json, the single file the dashboard (index.html) reads.
Runs in GitHub Actions after every bot run and every proposal PR open/close."""
from __future__ import annotations

import base64
import glob
import json
import os
import urllib.request

import pandas as pd

import bot

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "site", "data.json")


def gh(path: str):
    repo, token = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_TOKEN")
    if not (repo and token):
        return None
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/{path}",
                                 headers={"Authorization": f"Bearer {token}",
                                          "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except Exception as e:
        bot.log(f"github api {path}: {e}")
        return None


def account_data(name: str) -> dict | None:
    P = bot.Paths(name)
    if not os.path.exists(P.equity):
        return None
    eq = pd.read_csv(P.equity)
    eq = eq.groupby("candle").tail(1)  # one point per day for the chart
    st = json.load(open(P.state))
    trades = pd.read_csv(P.trades).to_dict("records") if os.path.exists(P.trades) else []
    last = eq.iloc[-1]
    return {
        "mode": st["mode"], "start_capital": st["start_capital"], "fees_paid": st["fees_paid"],
        "paused": os.path.exists(P.paused), "shutdown": os.path.exists(P.shutdown),
        "equity": float(last["equity"]), "exposure": float(last["btc"] * last["price"] / max(last["equity"], 1e-9)),
        "series": [{"d": r.candle, "eq": r.equity, "bh": r.buy_hold_equity} for r in eq.itertuples()],
        "trades": trades[-50:],
    }


def decisions() -> list[dict]:
    execs = {}
    p = bot.Paths("claude").executions
    if os.path.exists(p):
        execs = json.load(open(p))
    out = {}
    for f in glob.glob(os.path.join(HERE, "decisions", "*.json")):
        name = os.path.basename(f)
        try:
            d = json.load(open(f))
        except Exception:
            continue
        d.update({"file": name, "status": "on_main", "execution": execs.get(name)})
        out[name] = d
    # proposal PRs (open = waiting for Kaileb, closed unmerged = rejected)
    prs = gh("pulls?state=all&per_page=40&sort=created&direction=desc") or []
    for pr in prs:
        ref = pr["head"]["ref"]
        if not ref.startswith("decision/"):
            continue
        name = ref.split("/", 1)[1] + ".json"
        state = "pending" if pr["state"] == "open" else ("approved" if pr.get("merged_at") else "rejected")
        d = out.get(name)
        if d is None:
            c = gh(f"contents/decisions/{name}?ref={ref}")
            try:
                d = json.loads(base64.b64decode(c["content"])) if c else {"file": name}
            except Exception:
                d = {"file": name}
            d["file"] = name
            d["execution"] = execs.get(name)
        d.update({"status": state, "pr_url": pr["html_url"], "pr_number": pr["number"],
                  "pr_title": pr["title"]})
        out[name] = d
    for d in out.values():
        oc = ((d.get("execution") or {}).get("outcome") or "")
        if d.get("action") == "hold" and d["status"] in ("on_main", "approved"):
            d["status"] = "hold"
        elif d["status"] in ("on_main", "approved"):
            if oc.startswith(("executed", "no_trade")):
                d["status"] = "executed"
            elif oc:
                d["status"] = "not_executed"
            else:
                d["status"] = "approved"     # merged, executes on the next run
    return sorted(out.values(), key=lambda d: d.get("created_at", d["file"]), reverse=True)


def reports() -> dict:
    out = {}
    for kind, keep in (("daily", 21), ("weekly", 30), ("claude-review", 30)):
        files = sorted(glob.glob(os.path.join(HERE, "reports", kind, "*.md")), reverse=True)[:keep]
        out[kind] = [{"name": os.path.basename(f)[:-3], "md": open(f).read()} for f in files]
    return out


def build() -> dict:
    snap = json.load(open(os.path.join(HERE, "data", "snapshot.json"))) \
        if os.path.exists(os.path.join(HERE, "data", "snapshot.json")) else None
    data = {
        "generated_at": bot.now_utc().isoformat(),
        "repo": os.environ.get("GITHUB_REPOSITORY", "42LUE/auto-trader"),
        "config": {k: v for k, v in bot.load_cfg().items() if k != "ntfy_topic"},
        "snapshot": snap,
        "accounts": {a: account_data(a) for a in bot.ACCOUNTS},
        "decisions": decisions(),
        "reports": reports(),
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(data, f, indent=1, default=str)
    return data


if __name__ == "__main__":
    d = build()
    print(f"site/data.json: {len(d['decisions'])} decisions, "
          f"{sum(len(v) for v in d['reports'].values())} reports")
