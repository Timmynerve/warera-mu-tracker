"""Find the WarEra skill allocation, equipment and consumables that maximize daily damage
while keeping the daily balance (income minus spending) at or above a target.

Needs equipment_market_summary.csv (run warera_equipment_market.py first). The combat model was
reverse-engineered from the game client and checked against ~1,400 live hits:
  - attack = (skill + weapon + precision overflow*4) * (1+ammo%) * (1+pill%) * (1+military rank%)
  - a hit misses with probability 1-precision/100 (half damage, no crit), otherwise crits with
    probability critChance/100 for x(1+critDamages/100)
  - armor and dodge go through x -> round(100x/(x+40)); a dodged hit costs no health and no armor wear,
    otherwise a hit costs healthCost*(1-armor/100) health
  - each hit uses 1 ammo and 1 weapon durability; armor pieces lose 1 durability per non-dodged hit
  - bars regenerate 10%/h; one food uses 1 hunger and restores healthRegenPercent% of max health
  - each lootChance % gives 1% (case1) and 0.01% (case2) per hit to loot a case (wooden map cases are ignored)
Fighting also pays bounties and battle loot pool items (handed out by damage rank), both counted per 1k
damage. Battle bonuses (orders, alliance, patriotic...) only scale those earnings and the damage shown.

Usage: python warera_build_optimizer.py [--user Timmynerve] [--companies 4,6,8] [--max-daily-cost 0]
"""
import argparse
import csv
import random
import sys
from pathlib import Path

from warera_mu_report import api

SLOTS = ("weapon", "helmet", "chest", "pants", "boots", "gloves")
FOODS = ("bread", "steak", "cookedFish")
AMMOS = (None, "lightAmmo", "ammo", "heavyAmmo")
PILL = "cocain"
# Skills the optimizer may move points between ("companies" is fixed by --companies, management is left out)
FREE_SKILLS = ("attack", "precision", "criticalChance", "criticalDamages", "armor", "dodge",
               "health", "hunger", "lootChance", "energy", "production", "entrepreneurship")
SOFT_CAP_DIVISOR = 40
MISS_DAMAGE = 0.5
DURABILITY = 100
ROLL_POSITIONS = {"low": 0.1, "mid": 0.5, "high": 0.9}
ROLL_PRICE_COLUMNS = {"low": "median_low_roll", "mid": "median_full_state", "high": "median_high_roll"}
SEARCH_RESTARTS = 6
# Crafting recipe and scraps returned when dismantling a fully worn item, per rarity (from the game client;
# dismantling gives baseScrap + bonusScrap * state/maxState)
CRAFT = {"common": (6, 1, 2), "uncommon": (18, 2, 6), "rare": (54, 4, 18),
         "epic": (162, 8, 54), "legendary": (486, 16, 162), "mythic": (1458, 32, 486)}  # (scraps, steel, salvage)


def soft_cap(value):
    return round(100 * value / (value + SOFT_CAP_DIVISOR)) if value > 0 else 0


def load_equipment(csv_path, items, prices):
    """Equipment options per slot: (label, stats, lifetime cost), keeping only options not beaten on stats and cost.

    Lifetime cost = buying (market) or crafting price, minus the scraps from dismantling the worn-out item."""
    if not Path(csv_path).exists():
        sys.exit(f"{csv_path} not found: run warera_equipment_market.py first")
    scrap, steel = prices["scraps"], prices["steel"]
    options = {slot: [("none", {}, 0.0)] for slot in SLOTS}
    with open(csv_path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            item = items[row["item"]]
            craft_scraps, craft_steel, salvage = CRAFT[item["rarity"]]
            for roll, position in ROLL_POSITIONS.items():
                price = float(row[ROLL_PRICE_COLUMNS[roll]] or row["median_price"])
                stats = {stat: low + position * (high - low) for stat, (low, high) in item["dynamicStats"].items()}
                options[item["usage"]].append((f"{row['item']} bought ({roll} roll)", stats, price - salvage * scrap))
                if roll == "mid":  # a craft rolls at random, so count on the average stats
                    options[item["usage"]].append((f"{row['item']} crafted", stats,
                                                   (craft_scraps - salvage) * scrap + craft_steel * steel))
    for slot, opts in options.items():
        options[slot] = sorted((o for o in opts if not any(dominates(p, o) for p in opts)), key=lambda o: o[2])
    return options


def dominates(a, b):
    """True when option a is at least as cheap and as strong as b on every stat, and better somewhere."""
    if a is b or a[2] > b[2] or any(a[1].get(s, 0) < v for s, v in b[1].items()):
        return False
    return a[2] < b[2] or any(a[1].get(s, 0) > v for s, v in b[1].items())


class Model:
    def __init__(self, cfg, user, prices, wage, args):
        self.skills_cfg = cfg["skills"]
        self.items = cfg["items"]
        self.prices = prices
        self.health_cost = cfg["battle"]["healthCost"]
        self.full_regen_hours = cfg["user"]["regenDividedBy"]  # bars regenerate 1/regenDividedBy of their max per hour
        self.regen_per_day = 24 / self.full_regen_hours
        self.energy_per_action = cfg["user"]["energyCostPerAction"]
        self.rank_percent = user["skills"]["attack"].get("militaryRankPercent") or 0
        self.case_values = (args.case_price or prices.get("case1", 0), prices.get("case2", 0))
        self.bounty_per_1k = args.bounty_per_1k
        self.pool_loot_per_1k = args.pool_loot_per_1k
        self.battle_multiplier = args.battle_multiplier
        self.wage = wage
        self.pp_value = args.pp_value
        self.company_income = args.company_income
        pill = self.items[PILL]["flatStats"]
        self.pill_percent = pill["percentAttack"]
        self.pill_buff_hours = pill["buffDurationHours"]
        self.pill_cycle_hours = pill["buffDurationHours"] + pill["debuffDurationHours"]

    def value(self, skill, level):
        return self.skills_cfg[skill]["levels"][str(level)]["value"]

    def cost(self, skill, level):
        return self.skills_cfg[skill]["levels"][str(level)]["totalCost"]

    def max_level(self, skill):
        return max(int(level) for level in self.skills_cfg[skill]["levels"])

    def income(self, levels, companies):
        """Daily income that does not depend on fighting: automated companies, own-company work, job."""
        production = self.value("production", levels["production"])
        own_work = self.regen_per_day * self.value("entrepreneurship", levels["entrepreneurship"]) / self.energy_per_action
        job_work = self.regen_per_day * self.value("energy", levels["energy"]) / self.energy_per_action
        return {"companies": companies * self.company_income,
                "own company work": own_work * production * self.pp_value,
                "job": job_work * production * self.wage}

    def evaluate(self, levels, gear, food, ammo, pill, income, budget):
        """Damage per day and spending breakdown for one full setup, fighting as much as the budget allows."""
        stats = {s: sum(opt[1].get(s, 0) for opt in gear.values()) for s in
                 ("attack", "criticalChance", "criticalDamages", "precision", "armor", "dodge")}
        precision = self.value("precision", levels["precision"]) + stats["precision"]
        crit_chance = self.value("criticalChance", levels["criticalChance"]) + stats["criticalChance"]
        attack = (self.value("attack", levels["attack"]) + stats["attack"] + 4 * max(0, precision - 100))
        crit_damage = self.value("criticalDamages", levels["criticalDamages"]) + stats["criticalDamages"] \
            + 4 * max(0, crit_chance - 100)
        p, c = min(precision, 100) / 100, min(crit_chance, 100) / 100
        hit_factor = p * (1 + c * crit_damage / 100) + (1 - p) * MISS_DAMAGE
        ammo_percent = self.items[ammo]["flatStats"]["percentAttack"] if ammo else 0
        base_damage = attack * (1 + ammo_percent / 100) * (1 + self.rank_percent / 100) * hit_factor

        armor = soft_cap(self.value("armor", levels["armor"]) + stats["armor"])
        dodge = soft_cap(self.value("dodge", levels["dodge"]) + stats["dodge"])
        health_per_hit = self.health_cost * (1 - armor / 100) * (1 - dodge / 100)
        loot = self.value("lootChance", levels["lootChance"]) / 100
        per_hit = {"gear wear": gear["weapon"][2] / DURABILITY
                   + (1 - dodge / 100) * sum(gear[s][2] for s in SLOTS[1:]) / DURABILITY,
                   "ammo": self.prices.get(ammo, 0) if ammo else 0,
                   "cases": -loot * (self.case_values[0] + self.case_values[1] / 100)}
        # bounty and loot pool pay per damage actually dealt, battle bonuses included
        earnings_per_damage = self.battle_multiplier * (self.bounty_per_1k + self.pool_loot_per_1k) / 1000

        max_health = self.value("health", levels["health"])
        max_hunger = self.value("hunger", levels["hunger"])
        food_health = self.items[food]["flatStats"]["healthRegenPercent"] / 100 * max_health
        food_price = self.prices[food]
        # (bar refills available per day, attack multiplier) for each fighting window
        if pill:
            # start the buff with full bars, fight through it, then use what regenerates during the debuff
            scale = 24 / self.pill_cycle_hours
            buff_refills = 1 + self.pill_buff_hours / self.full_regen_hours
            debuff_refills = self.pill_cycle_hours / self.full_regen_hours - buff_refills
            windows = [(buff_refills * scale, 1 + self.pill_percent / 100),
                       (debuff_refills * scale, 1 - self.pill_percent / 100)]
            fixed = self.prices[PILL] * scale
        else:
            windows, fixed = [(self.regen_per_day, 1)], 0

        # Hit batches (hits, damage per hit, money flows per hit), filled best damage-per-net-cost first
        batches = []
        for refills, multiplier in windows:
            damage = base_damage * multiplier
            flows = {**per_hit, "bounty + loot pool": -damage * earnings_per_damage}
            batches.append((refills * max_health / health_per_hit, damage, {**flows, "food": 0}))
            batches.append((refills * max_hunger * food_health / health_per_hit, damage,
                            {**flows, "food": food_price * health_per_hit / food_health}))
        batches = [(count, dmg, flows, sum(flows.values())) for count, dmg, flows in batches]
        batches.sort(key=lambda b: -b[1] / b[3] if b[3] > 0 else -float("inf"))
        money = sum(income.values()) - fixed + budget
        if money < 0:
            return None
        hits = damage = spent = 0.0
        totals = dict.fromkeys(list(per_hit) + ["bounty + loot pool", "food"], 0.0)
        for count, dmg, flows, cost in batches:
            take = count if cost <= 0 else min(count, (money - spent) / cost)
            if take <= 0:
                continue
            hits, damage, spent = hits + take, damage + take * dmg, spent + take * cost
            for key, value in flows.items():
                totals[key] += take * value
        return {"damage": damage, "hits": hits, "net": sum(income.values()) - fixed - spent, "flows": totals,
                "attack": attack, "precision": precision, "crit_chance": crit_chance, "crit_damage": crit_damage,
                "armor": armor, "dodge": dodge, "health_per_hit": health_per_hit, "pill_per_day": fixed}

    def best_setup(self, levels, companies, budget, equipment, start=None):
        """Best gear + consumables for fixed skills (coordinate descent on gear for each consumable combo)."""
        income = self.income(levels, companies)
        best = None
        for food in FOODS:
            for ammo in AMMOS:
                for pill in (False, True):
                    gear = dict(start) if start else {slot: equipment[slot][0] for slot in SLOTS}
                    result = self.evaluate(levels, gear, food, ammo, pill, income, budget)
                    if result is None:
                        continue
                    improved = True
                    while improved:
                        improved = False
                        for slot in SLOTS:
                            for option in equipment[slot]:
                                trial = {**gear, slot: option}
                                r = self.evaluate(levels, trial, food, ammo, pill, income, budget)
                                if r and r["damage"] > result["damage"] + 1e-6:
                                    gear, result, improved = trial, r, True
                    if best is None or result["damage"] > best["result"]["damage"]:
                        best = {"gear": gear, "food": food, "ammo": ammo, "pill": pill,
                                "result": result, "income": income}
        return best

    def optimize(self, points, companies, budget, equipment, seed_levels=None):
        """Hill climbing on skill levels: move one point-step between two skills while damage improves."""
        rng = random.Random(1)
        overall = None
        for restart in range(SEARCH_RESTARTS):
            levels = dict(seed_levels) if restart == 0 and seed_levels else self.random_levels(points, rng)
            levels = self.fit_budget(levels, points)
            setup = self.best_setup(levels, companies, budget, equipment)
            while True:
                candidates = []
                for up in FREE_SKILLS:
                    for down in (None,) + FREE_SKILLS:
                        if up == down or levels[up] >= self.max_level(up) or (down and levels[down] == 0):
                            continue
                        trial = {**levels, up: levels[up] + 1, **({down: levels[down] - 1} if down else {})}
                        if self.spent(trial) > points:
                            continue
                        r = self.evaluate(trial, setup["gear"], setup["food"], setup["ammo"], setup["pill"],
                                          self.income(trial, companies), budget)
                        if r and r["damage"] > setup["result"]["damage"] + 1e-6:
                            candidates.append((r["damage"], trial))
                if not candidates:
                    # re-optimize gear/consumables, keep climbing only if that found something better
                    refreshed = self.best_setup(levels, companies, budget, equipment, setup["gear"])
                    if refreshed["result"]["damage"] <= setup["result"]["damage"] + 1e-6:
                        break
                    setup = refreshed
                    continue
                levels = max(candidates, key=lambda c: c[0])[1]
                setup = self.best_setup(levels, companies, budget, equipment, setup["gear"])
            if overall is None or setup["result"]["damage"] > overall["result"]["damage"]:
                overall = {**setup, "levels": levels}
        return overall

    def spent(self, levels):
        return sum(self.cost(skill, level) for skill, level in levels.items())

    def random_levels(self, points, rng):
        levels = {skill: 0 for skill in FREE_SKILLS}
        while True:
            options = [s for s in FREE_SKILLS if levels[s] < self.max_level(s)
                       and self.spent({**levels, s: levels[s] + 1}) <= points]
            if not options:
                return levels
            skill = rng.choice(options)
            levels[skill] += 1

    def fit_budget(self, levels, points):
        levels = {skill: levels.get(skill, 0) for skill in FREE_SKILLS}
        while self.spent(levels) > points:
            skill = max(levels, key=lambda s: levels[s])
            levels[skill] -= 1
        return levels


def print_setup(model, title, setup, points):
    r, levels = setup["result"], setup["levels"]
    print(f"\n=== {title} ===")
    print("Skills: " + ", ".join(f"{s} {levels[s]}" for s in FREE_SKILLS if levels[s])
          + f"  ({model.spent(levels)}/{points} points)")
    print("Gear:   " + ", ".join(f"{slot}: {setup['gear'][slot][0]} @ {setup['gear'][slot][2]:.1f}" for slot in SLOTS))
    print(f"Food: {setup['food']}, ammo: {setup['ammo'] or 'none'}, pill: {'yes' if setup['pill'] else 'no'}")
    print(f"Stats:  attack {r['attack']:.0f}, precision {r['precision']:.0f}, crit {r['crit_chance']:.0f}% "
          f"x{1 + r['crit_damage'] / 100:.2f}, armor {r['armor']}%, dodge {r['dodge']}%, "
          f"{r['health_per_hit']:.2f} health/hit")
    print(f"Per day: {r['hits']:.0f} hits, {r['damage'] / 1e6:.2f}M damage before battle bonuses "
          f"(~{r['damage'] * model.battle_multiplier / 1e6:.2f}M with them), balance {r['net']:+.0f}")
    earned = {**setup["income"], **{k: -v for k, v in r["flows"].items() if v < 0}}
    spent = {**{k: v for k, v in r["flows"].items() if v > 0}, "pill": r["pill_per_day"]}
    print("  earned: " + ", ".join(f"{k} {v:.0f}" for k, v in earned.items() if v >= 0.5))
    print("  spent:  " + ", ".join(f"{k} {v:.0f}" for k, v in spent.items() if v >= 0.5))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--user", default="Timmynerve")
    parser.add_argument("--companies", default="6", help="number of active companies, or a comma-separated list to compare")
    parser.add_argument("--max-daily-cost", type=float, default=0, help="allowed daily loss (0 = self-financed)")
    parser.add_argument("--company-income", type=float, default=24, help="daily profit of one automated company")
    parser.add_argument("--pp-value", type=float, default=0.11, help="profit per production point in your own company")
    parser.add_argument("--bounty-per-1k", type=float, default=0.04, help="bounty/contract money per 1k damage")
    parser.add_argument("--pool-loot-per-1k", type=float, default=0.13,
                        help="market value of battle loot pool items per 1k damage (0.13 = your Oct 2026 history)")
    parser.add_argument("--case-price", type=float, help="value of one case1 (default: current market price)")
    parser.add_argument("--battle-multiplier", type=float, default=1.4,
                        help="average battle bonus multiplier (orders, alliance...), used for bounty and loot pool")
    parser.add_argument("--wage-tax", type=float, default=10, help="income tax on job wages, in percent")
    parser.add_argument("--equipment-csv", default="equipment_market_summary.csv")
    args = parser.parse_args()

    print("Loading game data and prices...")
    cfg = api("gameConfig.getGameConfig", {})
    found = api("search.searchAnything", {"searchText": args.user})
    if not found.get("userIds"):
        sys.exit(f"No player found for '{args.user}'")
    user = api("user.getUserLite", {"userId": found["userIds"][0]})
    prices = api("itemTrading.getPrices", {})
    wage = api("workOffer.getWageStats", {})["allowedRange"]["average"] * (1 - args.wage_tax / 100)
    equipment = load_equipment(args.equipment_csv, cfg["items"], prices)
    model = Model(cfg, user, prices, wage, args)

    total_points = user["leveling"]["totalSkillPoints"]
    current = {skill: user["skills"][skill]["level"] for skill in FREE_SKILLS}
    print(f"{user['username']}: level {user['leveling']['level']}, {total_points} skill points, "
          f"military rank bonus {model.rank_percent}%")

    for companies in (int(c) for c in args.companies.split(",")):
        level = companies - model.value("companies", 0)
        if not 0 <= level <= model.max_level("companies"):
            print(f"\nSkipping {companies} companies: out of the skill's range")
            continue
        points = total_points - model.cost("companies", level)
        print(f"\n##### {companies} active companies: {model.cost('companies', level)} points in companies, "
              f"{points} left #####")
        if companies == user["skills"]["companies"]["value"]:
            now = model.best_setup(current, companies, args.max_daily_cost, equipment)
            print_setup(model, "Current skills, best gear and consumables", {**now, "levels": current}, points)
        best = model.optimize(points, companies, args.max_daily_cost, equipment, current)
        print_setup(model, "Optimized", best, points)


if __name__ == "__main__":
    main()
