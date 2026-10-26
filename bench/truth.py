"""Ground truth for bench/questions.yaml, computed from fabric_backend.seed.rows() — never typed in by hand.

Each function takes the seed rows and returns the answer in the shape the judge compares: a number, a string, or a
list (order-insensitive). `answers()` returns {question_id: value}; `load_questions()` fills the YAML questions with
them. A question whose "most" has a tie gets the list of tied names, so the judge accepts exactly those."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _rows():
    from fabric_backend.seed import rows
    return rows()


def _tables(r):
    customers = {c[0]: {"name": c[1], "country": c[2]} for c in r["customers"]}
    categories = {c[0]: c[1] for c in r["categories"]}
    products = {p[0]: {"name": p[1], "category": categories[p[2]]} for p in r["products"]}
    addresses = {a[0]: {"customer": a[1], "city": a[2]} for a in r["addresses"]}
    orders = {o[0]: {"customer": o[1], "date": o[2], "ship_to": o[3]} for o in r["orders"]}
    items = [{"order": i[0], "line": i[1], "product": i[2], "qty": i[3]} for i in r["order_items"]]
    audit = [{"event": a[0], "customer": a[1], "at": a[2]} for a in r["audit_log"]]
    return customers, categories, products, addresses, orders, items, audit


def _top(counter: Counter):
    """The key with the highest count; a list of keys when tied."""
    if not counter:
        return None
    best = max(counter.values())
    keys = sorted(k for k, v in counter.items() if v == best)
    return keys[0] if len(keys) == 1 else keys


def answers() -> dict[str, object]:
    customers, categories, products, addresses, orders, items, audit = _tables(_rows())
    name = {cid: c["name"] for cid, c in customers.items()}
    country = {cid: c["country"] for cid, c in customers.items()}
    out: dict[str, object] = {}
    # --- one relationship step ---------------------------------------------------------------
    out["q01"] = sum(1 for o in orders.values() if name[o["customer"]] == "Harbour Bikes")
    out["q02"] = sorted(p["name"] for p in products.values() if p["category"] == "Tea")
    out["q03"] = sum(i["qty"] for i in items if products[i["product"]]["name"] == "Trail bike")
    out["q04"] = _top(Counter(name[o["customer"]] for o in orders.values()))
    out["q05"] = sorted({a["city"] for a in addresses.values() if name[a["customer"]] == "Harbour Bikes"})
    out["q06"] = sum(1 for a in audit if country[a["customer"]] == "SE")
    out["q07"] = sum(1 for o in orders.values() if country[o["customer"]] == "DE" and date(2026, 10, 1) <= o["date"] < date(2026, 11, 1))
    out["q08"] = _top(Counter(p["category"] for p in products.values()))
    # --- two or three steps ------------------------------------------------------------------
    bike_orders = {i["order"] for i in items if products[i["product"]]["category"] == "Bikes"}
    out["q09"] = sorted({name[orders[o]["customer"]] for o in bike_orders if country[orders[o]["customer"]] == "DE"})
    qty_by_cat: Counter = Counter()
    for i in items:
        qty_by_cat[products[i["product"]]["category"]] += i["qty"]
    out["q10"] = _top(qty_by_cat)
    out["q11"] = len({orders[i["order"]]["customer"] for i in items if products[i["product"]]["name"] == "Helmet"})
    out["q12"] = sum(1 for i in items if country[orders[i["order"]]["customer"]] == "SE")
    out["q13"] = sorted({products[i["product"]]["name"] for i in items if name[orders[i["order"]]["customer"]] == "Fjord Outdoor"})
    tea_by_country: Counter = Counter()
    for i in items:
        if products[i["product"]]["category"] == "Tea":
            tea_by_country[country[orders[i["order"]]["customer"]]] += i["qty"]
    out["q14"] = _top(tea_by_country)
    cats_per_order = defaultdict(set)
    for i in items:
        cats_per_order[i["order"]].add(products[i["product"]]["category"])
    out["q15"] = sum(1 for s in cats_per_order.values() if len(s) > 1)
    qty_by_customer: Counter = Counter()
    for i in items:
        qty_by_customer[name[orders[i["order"]]["customer"]]] += i["qty"]
    out["q16"] = _top(qty_by_customer)
    # --- the confirmed link ship_to_id -> addresses, and the composite key of order_items ---------
    out["q17"] = addresses[orders[103]["ship_to"]]["city"]
    out["q18"] = sum(1 for o in orders.values() if addresses[o["ship_to"]]["city"] == "Hamburg")
    out["q19"] = next(i["qty"] for i in items if i["order"] == 105 and i["line"] == 2)
    out["q20"] = sorted({name[o["customer"]] for o in orders.values() if addresses[o["ship_to"]]["city"] == "Leipzig"})
    # --- the ship-to link plus further hops (join_path): through the customer's addresses the answers differ ----------
    def shipped_to(city: str) -> set[int]:
        return {oid for oid, o in orders.items() if addresses[o["ship_to"]]["city"] == city}
    out["q21"] = sorted({products[i["product"]]["name"] for i in items if i["order"] in shipped_to("Uppsala")})
    out["q22"] = sum(1 for o in orders.values() if addresses[o["ship_to"]]["customer"] != o["customer"])
    out["q23"] = sum(i["qty"] for i in items if i["order"] in shipped_to("Leipzig") and products[i["product"]]["category"] == "Tea")
    per_city = Counter(addresses[o["ship_to"]]["city"] for o in orders.values())
    out["q24"] = sorted(c for c, n in per_city.items() if n < 5)
    out["q25"] = len({i["product"] for i in items if i["order"] in shipped_to("Kiel")})
    return out


def through_customer(question_id: str):
    """The answer an agent gets when it joins addresses through the customer instead of orders.ship_to_id: the wrong
    path the join_path questions are built to tell apart. Only for the tests, which check it differs from the truth."""
    customers, categories, products, addresses, orders, items, audit = _tables(_rows())
    cities = defaultdict(set)
    for a in addresses.values():
        cities[a["customer"]].add(a["city"])

    def via(city: str) -> set[int]:
        return {oid for oid, o in orders.items() if city in cities[o["customer"]]}
    if question_id == "q21":
        return sorted({products[i["product"]]["name"] for i in items if i["order"] in via("Uppsala")})
    if question_id == "q23":
        return sum(i["qty"] for i in items if i["order"] in via("Leipzig") and products[i["product"]]["category"] == "Tea")
    if question_id == "q24":
        per_city = Counter(c for o in orders.values() for c in cities[o["customer"]])
        return sorted(c for c, n in per_city.items() if n < 5)
    if question_id == "q25":
        return len({i["product"] for i in items if i["order"] in via("Kiel")})
    raise KeyError(question_id)


def load_questions(path=HERE / "questions.yaml"):
    """The YAML questions with `answer` filled from answers(); `source` and `tags` as written."""
    from fabric_eval import load_questions as _load
    qs = _load(path)
    truth = answers()
    for q in qs:
        q.answer = truth[q.id]
    return qs


if __name__ == "__main__":
    for k, v in answers().items():
        print(k, repr(v))
