"""Snapshot the cumulative damage counter of every MU member and append it to damage_log.jsonl.

Meant to run every 15 minutes from Windows Task Scheduler. The differences between two
consecutive snapshots give the damage each player dealt in that interval.

Usage: pythonw -I warera_damage_tracker.py [--user Timmynerve]
"""
import argparse
import json
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import warera_mu_report as wr  # noqa: E402

LOG = HERE / "damage_log.jsonl"
ERRORS = HERE / "damage_tracker_errors.log"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--user", default="Timmynerve")
    args = parser.parse_args()

    mu = wr.find_mu(args.user)
    profiles = wr.api_many([("user.getUserLite", {"userId": uid}) for uid in mu["members"]])
    counters, names, buffs = {}, {}, {}
    for uid, u in zip(mu["members"], profiles):
        if not u:
            continue
        count = (u.get("stats") or {}).get("damagesCount")
        if count is not None:
            counters[uid] = count
            names[uid] = u.get("username", "?")
        if u.get("buffs"):
            buffs[uid] = u["buffs"]
    entry = {"t": datetime.now(timezone.utc).isoformat(timespec="seconds"), "mu": mu["_id"], "c": counters, "n": names, "b": buffs}
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        with ERRORS.open("a", encoding="utf-8") as f:
            f.write(datetime.now().isoformat(timespec="seconds") + "\n" + traceback.format_exc() + "\n")
        raise
