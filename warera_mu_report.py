"""Fetch WarEra military unit data from the public API and build an HTML damage report.

Usage: python warera_mu_report.py [--user Timmynerve] [--days 7] [--out mu_report.html]
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    REPORT_TZ = ZoneInfo("Europe/Paris")
except Exception:  # tzdata missing (common on Windows): fall back to the machine's local time
    REPORT_TZ = None

API = "https://api2.warera.io/trpc/"
HEADERS = {"User-Agent": "curl/8.0"}


BATCH_SIZE = 20


def _get(url):
    """GET with automatic wait on rate limiting (100 requests/minute for anonymous users)."""
    for attempt in range(8):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                wait = int(exc.headers.get("retry-after") or 30) + 1
                print(f"  rate limited, waiting {wait}s...")
                time.sleep(wait)
                continue
            if attempt >= 2:
                raise
        except Exception:
            if attempt >= 2:
                raise
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Request kept failing: {url[:120]}")


def api(endpoint, payload):
    return api_many([(endpoint, payload)])[0]


def api_many(calls):
    """Run many tRPC calls, grouped into batched HTTP requests."""
    results = []
    for i in range(0, len(calls), BATCH_SIZE):
        chunk = calls[i:i + BATCH_SIZE]
        names = ",".join(c[0] for c in chunk)
        inputs = {str(j): c[1] for j, c in enumerate(chunk)}
        url = (API + names + "?batch=1&input=" + urllib.parse.quote(json.dumps(inputs, separators=(",", ":"))))
        for entry in _get(url):
            results.append(entry.get("result", {}).get("data") if "result" in entry else None)
    return results


def paginate(endpoint, payload, max_pages=50):
    items, cursor = [], None
    for _ in range(max_pages):
        page = api(endpoint, {**payload, **({"cursor": cursor} if cursor else {})}) or {}
        items += page.get("items", [])
        cursor = page.get("nextCursor")
        if not cursor:
            break
    return items


def paginate_many(endpoint, payloads, max_pages=20):
    """Paginate several queries in parallel batches; returns one item list per payload."""
    items = [[] for _ in payloads]
    pending = [(i, None) for i in range(len(payloads))]
    for _ in range(max_pages):
        if not pending:
            break
        pages = api_many([(endpoint, {**payloads[i], **({"cursor": c} if c else {})}) for i, c in pending])
        nxt = []
        for (i, _), page in zip(pending, pages):
            page = page or {}
            items[i] += page.get("items", [])
            if page.get("nextCursor"):
                nxt.append((i, page["nextCursor"]))
        pending = nxt
    return items


def parse_date(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def find_mu(username):
    found = api("search.searchAnything", {"searchText": username})
    if not found.get("userIds"):
        sys.exit(f"No player found for '{username}'")
    user_id = found["userIds"][0]
    mus = api("mu.getManyPaginated", {"memberId": user_id, "limit": 5}).get("items", [])
    if not mus:
        sys.exit(f"Player '{username}' is not in a military unit")
    return mus[0]


def slim_skill(skill):
    if not skill:
        return {}
    keep = ("level", "value", "weapon", "equipment", "total", "totalAfterSoftCap",
            "currentBarValue", "hourlyBarRegen", "ammoPercent", "buffsPercent",
            "debuffsPercent", "militaryRankPercent")
    return {k: skill.get(k) for k in keep if k in skill}


def fetch_member(user_id, u):
    u = u or {}
    skills = u.get("skills", {})
    return {
        "id": user_id,
        "name": u.get("username", "?"),
        "level": u.get("leveling", {}).get("level"),
        "militaryRank": u.get("militaryRank"),
        "avatar": u.get("avatarUrl"),
        "isActive": u.get("isActive"),
        "lastSeen": u.get("dates", {}).get("lastConnectionAt"),
        "buffs": u.get("buffs") or {},
        "totalDamage": u.get("rankings", {}).get("userDamages", {}).get("value"),
        "weeklyDamage": u.get("rankings", {}).get("weeklyUserDamages", {}).get("value"),
        "skills": {k: slim_skill(skills.get(k)) for k in (
            "attack", "precision", "criticalChance", "criticalDamages",
            "armor", "dodge", "health", "hunger")},
        "skillLevels": {k: s.get("level", 0) for k, s in skills.items()},
        "skillPoints": u.get("leveling", {}).get("totalSkillPoints"),
    }


def optimizer_data(config):
    """Game rules, prices and equipment market data used by the OpTimmyZer tab.

    Equipment prices come from recent market sales, which need an API token (WARERA_API_TOKEN);
    without one the tab explains that it is unavailable."""
    import os
    sys.path.insert(0, str(Path(__file__).parent))  # isolated mode (python -I, as in CI) leaves the script dir out
    from warera_build_optimizer import CRAFT, FREE_SKILLS, manager_rate
    from warera_equipment_market import fetch_sales, summarize

    items = config["items"]
    prices = api("itemTrading.getPrices", {})
    equipment = {code: item for code, item in items.items() if item.get("type") in ("weapon", "equipment")}
    market = {}
    token = os.environ.get("WARERA_API_TOKEN")
    if token:
        headers = {**HEADERS, "X-API-Key": token}
        since = datetime.now(timezone.utc) - timedelta(days=3)
        for code, item in equipment.items():
            sales = fetch_sales(code, since, 500, headers)
            if sales:
                row = summarize(code, item, sales)
                market[code] = {"low": row["median_low_roll"], "mid": row["median_full_state"],
                                "high": row["median_high_roll"], "median": row["median_price"]}
    skills = {k: {"unlockAt": s["levels"]["0"].get("unlockAtLevel", 1),
                  "levels": [[lv["value"], lv["totalCost"]] for _, lv in sorted(s["levels"].items(), key=lambda x: int(x[0]))]}
              for k, s in config["skills"].items() if k in FREE_SKILLS + ("companies",)}
    return {
        "skills": skills,
        "freeSkills": FREE_SKILLS,
        "equipment": {code: {"usage": it["usage"], "rarity": it["rarity"], "stats": it["dynamicStats"],
                             "icon": it.get("iconImg") or f"{code}.png", "market": market.get(code)}
                      for code, it in equipment.items()},
        "consumables": {code: {**items[code]["flatStats"], "icon": items[code].get("iconImg") or f"{code}.png"}
                        for code in ("bread", "steak", "cookedFish", "lightAmmo", "ammo", "heavyAmmo", "cocain")},
        "prices": {k: prices.get(k) for k in ("bread", "steak", "cookedFish", "lightAmmo", "ammo", "heavyAmmo",
                                             "cocain", "case1", "case2", "scraps", "steel")},
        "managerRate": manager_rate(config),
        "craft": CRAFT,
        "rules": {"healthCost": config["battle"]["healthCost"], "regenHours": config["user"]["regenDividedBy"],
                  "energyPerAction": config["user"]["energyCostPerAction"],
                  "resetCostPerPoint": config["user"]["resetSkillsCostPerPoint"]},
        # same defaults as warera_build_optimizer.py
        "defaults": {"companyIncome": 24, "ppValue": 0.11, "wageTax": 10, "bounty": 0.04, "poolLoot": 0.13,
                     "battleMultiplier": 1.4},
    }


def recent_battles(days):
    since = datetime.now(timezone.utc) - timedelta(days=days)
    active = paginate("battle.getBattles", {"isActive": True, "limit": 100})
    ended, cursor = [], None
    for _ in range(40):
        page = api("battle.getBattles", {"isActive": False, "limit": 100,
                                         **({"cursor": cursor} if cursor else {})})
        batch = page.get("items", [])
        ended += [b for b in batch if parse_date(b.get("endedAt") or b["createdAt"]) >= since]
        cursor = page.get("nextCursor")
        oldest = min((parse_date(b["createdAt"]) for b in batch), default=since)
        if not cursor or oldest < since - timedelta(days=2):
            break
    return active + ended


def ranking(battle_id, kind, side):
    return {"battleId": battle_id, "dataType": "damage", "type": kind, "side": side, "limit": 100}


def battles_with_mu(battles, mu_id):
    rows = paginate_many("battleRanking.getRanking", [ranking(b["_id"], "mu", "merged") for b in battles], max_pages=5)
    return [b for b, r in zip(battles, rows) if any(x.get("mu") == mu_id for x in r)]


def battle_details(battles, mu_id, member_ids):
    keys = [(b, side) for b in battles for side in ("attacker", "defender")]
    mu_ranks = paginate_many("battleRanking.getRanking", [ranking(b["_id"], "mu", s) for b, s in keys], max_pages=5)
    present = [(k, next((r for r in mr if r.get("mu") == mu_id), None), len(mr)) for k, mr in zip(keys, mu_ranks)]
    present = [p for p in present if p[1]]
    users = paginate_many("battleRanking.getRanking", [ranking(b["_id"], "user", s) for (b, s), _, _ in present])
    details = {b["_id"]: {} for b in battles}
    for ((b, side), mine, count), urows in zip(present, users):
        details[b["_id"]][side] = {
            "muDamage": mine["value"],
            "muRank": mine["rank"],
            "muCount": count,
            "sideDamage": sum(r["value"] for r in urows),
            "players": [{"id": r["user"], "damage": r["value"], "rank": r["rank"]}
                        for r in urows if r["user"] in member_ids],
        }
    return details


MAX_GAP_MINUTES = 75
DEFAULT_PILL = "cocain"


def pill_periods(buff_states, items):
    """Rebuild buff and debuff time ranges from the end dates seen on a player.

    A pill gives a buff, then a debuff that starts when the buff ends; each
    duration comes from the item's flatStats in the game config.
    """
    def hours(code, key):
        stats = (items.get(code) or items.get(DEFAULT_PILL) or {}).get("flatStats", {})
        return timedelta(hours=stats.get(key) or 0)

    periods = set()
    for b in buff_states:
        if b.get("buffEndAt"):
            end = parse_date(b["buffEndAt"]).replace(second=0, microsecond=0)
            code = (b.get("buffCodes") or [DEFAULT_PILL])[0]
            periods.add(("buff", end - hours(code, "buffDurationHours"), end))
        if b.get("debuffEndAt"):
            end = parse_date(b["debuffEndAt"]).replace(second=0, microsecond=0)
            code = (b.get("debuffCodes") or [DEFAULT_PILL])[0]
            start = end - hours(code, "debuffDurationHours")
            periods.add(("debuff", start, end))
            periods.add(("buff", start - hours(code, "buffDurationHours"), start))
    # The same pill is seen many times with slightly different end dates: merge overlaps.
    merged = []
    for kind, start, end in sorted(periods):
        if merged and merged[-1][0] == kind and start <= merged[-1][2]:
            merged[-1][2] = max(merged[-1][2], end)
        else:
            merged.append([kind, start, end])
    return merged


def build_activity(log_path, member_ids, current_buffs=None, items=None):
    """Turn cumulative damage snapshots into damage per local hour of day.

    Also counts, per hour, the measured minutes each player spent under a pill
    buff or debuff.
    """
    if not log_path.exists():
        return None
    snaps = []
    seen_buffs = {uid: [b] for uid, b in (current_buffs or {}).items() if b}
    for line in log_path.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
            snaps.append((parse_date(e["t"]), e["c"]))
        except (ValueError, KeyError):
            continue
        for uid, b in (e.get("b") or {}).items():
            seen_buffs.setdefault(uid, []).append(b)
    snaps.sort(key=lambda s: s[0])
    if not snaps:
        return None
    periods = {uid: pill_periods(states, items or {}) for uid, states in seen_buffs.items() if uid in member_ids}
    coverage = [0.0] * 24
    users = {uid: [0.0] * 24 for uid in member_ids}
    pills = {kind: {uid: [0.0] * 24 for uid in periods} for kind in ("buff", "debuff")}
    for (t0, c0), (t1, c1) in zip(snaps, snaps[1:]):
        total_min = (t1 - t0).total_seconds() / 60
        if total_min <= 0 or total_min > MAX_GAP_MINUTES:
            continue
        # Split the interval at local hour boundaries.
        pieces, cur = [], t0
        while cur < t1:
            local = cur.astimezone(REPORT_TZ)
            next_hour = (local.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)).astimezone(timezone.utc)
            nxt = min(t1, next_hour)
            pieces.append((local.hour, cur, nxt))
            cur = nxt
        for hour, a, b in pieces:
            coverage[hour] += (b - a).total_seconds() / 60
        for uid in member_ids:
            if uid in c0 and uid in c1 and c1[uid] >= c0[uid]:
                delta = c1[uid] - c0[uid]
                for hour, a, b in pieces:
                    users[uid][hour] += delta * (b - a).total_seconds() / 60 / total_min
        for uid, spans in periods.items():
            for kind, start, end in spans:
                for hour, a, b in pieces:
                    overlap = (min(b, end) - max(a, start)).total_seconds() / 60
                    if overlap > 0:
                        pills[kind][uid][hour] += overlap
    return {
        "start": snaps[0][0].isoformat(), "end": snaps[-1][0].isoformat(), "snapshots": len(snaps),
        "coverage": [round(c, 1) for c in coverage],
        "users": {uid: [round(v) for v in vals] for uid, vals in users.items()},
        **{kind + "s": {uid: [round(v, 1) for v in vals] for uid, vals in per_user.items() if any(vals)}
           for kind, per_user in pills.items()},
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--user", default="Timmynerve")
    parser.add_argument("--days", type=float, default=7)
    parser.add_argument("--out", default="mu_report.html")
    args = parser.parse_args()

    print("Looking up military unit...")
    mu = find_mu(args.user)
    member_ids = set(mu["members"])
    print(f"MU: {mu['name']} ({len(member_ids)} members)")

    print("Loading game reference data...")
    countries = {c["_id"]: {"name": c.get("name"), "code": c.get("code")}
                 for c in api("country.getAllCountries", {})}
    regions = {rid: r.get("name") or r.get("code") for rid, r in api("region.getRegionsObject", {}).items()}
    config = api("gameConfig.getGameConfig", {})
    foods = {k: config["items"][k]["flatStats"]["healthRegenPercent"]
             for k in ("bread", "steak", "cookedFish") if k in config["items"]}

    print("Loading member profiles...")
    members = [fetch_member(uid, u) for uid, u in zip(
        mu["members"], api_many([("user.getUserLite", {"userId": uid}) for uid in mu["members"]]))]

    print(f"Scanning battles from the last {args.days:g} days...")
    battles = recent_battles(args.days)
    print(f"  {len(battles)} battles to check")
    hits = battles_with_mu(battles, mu["_id"])
    print(f"  MU fought in {len(hits)} battles, loading player damage...")
    details = battle_details(hits, mu["_id"], member_ids)

    battle_rows = []
    for b in hits:
        sides = details[b["_id"]]
        if not sides:
            continue
        battle_rows.append({
            "id": b["_id"],
            "isActive": b.get("isActive"),
            "createdAt": b.get("createdAt"),
            "endedAt": b.get("endedAt"),
            "type": b.get("type"),
            "region": regions.get(b.get("defender", {}).get("region"), "?"),
            "attacker": countries.get(b["attacker"]["country"], {}),
            "defender": countries.get(b["defender"]["country"], {}),
            "score": [b["attacker"].get("wonRoundsCount", 0), b["defender"].get("wonRoundsCount", 0)],
            "wonBy": b.get("wonBy"),
            "sides": sides,
        })
    battle_rows.sort(key=lambda r: (r["isActive"], r.get("endedAt") or r["createdAt"]), reverse=True)

    print("Loading optimizer prices...")
    optimizer = optimizer_data(config)

    data = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "days": args.days,
        "mu": {
            "id": mu["_id"], "name": mu["name"], "avatar": mu.get("avatarUrl"),
            "country": countries.get(mu["country"], {}),
            "level": mu.get("leveling", {}).get("level"),
            "rankings": mu.get("rankings", {}),
        },
        "foods": foods,
        "healthCost": config["battle"].get("healthCost", 10),
        "members": members,
        "battles": battle_rows,
        "optimizer": optimizer,
        "activity": build_activity(Path(__file__).parent / "damage_log.jsonl", list(member_ids),
                                   {m["id"]: m["buffs"] for m in members}, config["items"]),
    }
    template = (Path(__file__).parent / "mu_report_template.html").read_text(encoding="utf-8")
    html = template.replace("/*__DATA__*/null", json.dumps(data, ensure_ascii=False).replace("</", "<\\/"))
    # Standalone page skeleton (standards mode, base reset) for hosting outside claude.ai.
    html = ('<!doctype html><html lang="fr"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
            '<style>:root{padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px)}'
            'body{margin:0;font:14px/1.4 system-ui,sans-serif}img{max-width:100%}[hidden]{display:none!important}</style>'
            '</head><body>' + html + '</body></html>')
    Path(args.out).write_text(html, encoding="utf-8")
    Path(args.out).with_suffix(".json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Report written to {args.out} ({len(battle_rows)} battles, {len(members)} members)")


if __name__ == "__main__":
    main()
