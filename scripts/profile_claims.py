"""Exploration aid: profile harvested claims (SaaS name frequency, repo hosts)."""
import collections
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from source_pipeline import common, harvest  # noqa: E402

if "--harvest" in sys.argv:
    print(json.dumps(harvest.run(), indent=1))
claims = common.read_jsonl(common.STAGING / "claims.jsonl")
names = collections.Counter()
for c in claims:
    for s in c["saas_names"]:
        names[s] += 1
print("distinct raw SaaS names:", len(names))
for name, n in sorted(names.items(), key=lambda kv: (-kv[1], kv[0])):
    print(f"{n}\t{name}")
hosts = collections.Counter(
    re.sub(r"https?://([^/]+).*", r"\1", c["repo_url"]) for c in claims if c["saas_names"]
)
print("hosts:", hosts.most_common(15))
