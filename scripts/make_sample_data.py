"""Generate data/sample/sales_facts.csv (synthetic, seeded, deterministic).

The data has organic month-to-month drift plus a few random shocks chosen by the
seeded RNG. Nothing here knows or stores an answer: the agents have to discover
what moved by querying the data.
"""
import csv
import pathlib
import random

SEED = 20261024
MONTHS = ["2026-05", "2026-06", "2026-07", "2026-08", "2026-09"]
DAYS = ["03", "10", "17", "24"]
REGIONS = {"Ontario": 1.5, "Quebec": 1.1, "British Columbia": 0.9, "Alberta": 0.7}
CHANNELS = {"Online": 1.3, "Retail": 1.0, "Wholesale": 0.6}
PRODUCTS = {
    "Electronics": {"Noise-Cancel Headphones": (180.0, 0.62), "Smart Speaker": (90.0, 0.58)},
    "Home": {"Air Purifier": (240.0, 0.66), "Coffee Maker": (120.0, 0.60)},
    "Apparel": {"Rain Jacket": (140.0, 0.52), "Running Shoes": (110.0, 0.55)},
}


def main() -> None:
    rng = random.Random(SEED)
    all_products = [(c, p) for c, ps in PRODUCTS.items() for p in ps]
    shock_product = rng.choice(all_products)[1]
    shock_region = rng.choice(list(REGIONS))
    shock_channel = rng.choice(list(CHANNELS))

    region_drift = {r: 1.0 for r in REGIONS}
    cost_drift = {p: 1.0 for _, p in all_products}
    rows = []
    for mi, month in enumerate(MONTHS):
        for r in REGIONS:
            region_drift[r] *= rng.uniform(0.96, 1.05)
        for _, p in all_products:
            cost_drift[p] *= rng.uniform(0.985, 1.02)
        for day in DAYS:
            for region, rw in REGIONS.items():
                for channel, cw in CHANNELS.items():
                    for category, items in PRODUCTS.items():
                        for product, (price, cost_ratio) in items.items():
                            units = 40 * rw * cw * region_drift[region] * rng.uniform(0.85, 1.15)
                            unit_price = price * rng.uniform(0.97, 1.03)
                            unit_cost = price * cost_ratio * cost_drift[product]
                            if month == MONTHS[-1]:
                                if product == shock_product:
                                    unit_cost *= 1.18
                                if region == shock_region and channel == shock_channel:
                                    units *= 0.8
                            u = max(int(round(units)), 1)
                            rows.append({
                                "order_date": f"{month}-{day}",
                                "region": region,
                                "channel": channel,
                                "product_category": category,
                                "product": product,
                                "units": u,
                                "revenue": round(u * unit_price, 2),
                                "cost": round(u * unit_cost, 2),
                            })
    out = pathlib.Path(__file__).resolve().parent.parent / "data" / "sample" / "sales_facts.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {len(rows)} rows to {out}")


def make_fx() -> None:
    """SYNTHETIC stand-in for the public exchange-rate web source (not real rates). In live mode this table is a REST
    source registered in Zetaris (scripts/zetaris_register_fx_rates.sql). Independent RNG: sales_facts.csv is unaffected."""
    rng = random.Random(SEED + 1)
    rate, rows = 1.36, []
    for month in ["2026-04"] + MONTHS:
        for day in range(1, 29):
            if (day % 7) in (0, 6):     # skip weekend-like days
                continue
            rate *= rng.uniform(0.9965, 1.0035)
            rows.append({"rate_date": f"{month}-{day:02d}", "base_currency": "USD", "quote_currency": "CAD",
                         "rate": round(rate, 4)})
    out = pathlib.Path(__file__).resolve().parent.parent / "data" / "sample" / "fx_rates.csv"
    with out.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {len(rows)} SYNTHETIC rows to {out}")


if __name__ == "__main__":
    main()
    make_fx()
