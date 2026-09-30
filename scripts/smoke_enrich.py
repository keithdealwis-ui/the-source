"""Exploration aid: reconcile staged claims and live-check a few repositories."""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from source_pipeline import activity, common, enrich, normalise  # noqa: E402

claims = common.read_jsonl(common.STAGING / "claims.jsonl")
rec = normalise.reconcile(claims, common.load_sources())
print("oss", len(rec["oss"]), "edges", len(rec["edges"]), "saas", len(rec["saas"]),
      "rejections", len(rec["rejections"]), "uncatalogued", len(rec["uncatalogued"]))
keys = sys.argv[1:] or ["github.com/mattermost/mattermost", "gitlab.com/bramw/baserow"]
policy = common.load_policy()
rules = activity.Rules(policy)
as_of = datetime.now(timezone.utc).replace(microsecond=0)
common.LIVE = common.DATA / "live-smoke"
snap = enrich.run(keys, as_of=as_of, workers=4)
for key, live in snap["projects"].items():
    verdict = activity.evaluate(live, rules, as_of) if live.get("found") else None
    slim = {k: v for k, v in live.items() if k != "observations"}
    print(json.dumps({"key": key, "live": slim, "maintenance": verdict}, indent=1)[:2500])
