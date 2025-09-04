#!/usr/bin/env python3
# Minimal ecommerce simulator → posts events to a webhook (your /ingest)
import argparse, asyncio, json, random, time, uuid
from datetime import datetime, timezone
import urllib.request

# ---- probabilities you can tweak later ----
P_ADD_TO_CART = 0.35
P_BEGIN_CHECKOUT = 0.7
P_PURCHASE = 0.7

DEVICES   = ["mobile","mobile","desktop","tablet"]
SOURCES   = ["direct","seo","sem","email","social","referral"]
COUNTRIES = ["US","US","US","CA","GB","DE","AU"]
CURRENCIES= ["USD","USD","USD","EUR","GBP","AUD","CAD"]

def now_iso():
    return datetime.now(tz=timezone.utc).isoformat()

def pick(seq): return random.choice(seq)
def uid(): return str(uuid.uuid4())

def send_event(evt, target):
    data = json.dumps(evt).encode("utf-8")
    req = urllib.request.Request(target, data=data,
                                 headers={"Content-Type":"application/json"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            _ = resp.read()
    except Exception as e:
        print("[warn] POST failed:", e)

def make_session():
    return {
        "user_id": f"u_{random.randint(1, 9_999_999)}",
        "session_id": uid(),
        "device": pick(DEVICES),
        "country": pick(COUNTRIES),
        "source": pick(SOURCES),
        "utm_campaign": pick(["brand","retargeting","newsletter","new_arrivals",""]),
        "currency": pick(CURRENCIES),
        "store_id": "store-001",
    }

def make_product():
    # silly fake catalog
    n = random.randint(10000, 10199)
    price = round(random.uniform(10, 120), 2)
    cat = pick(["Tops","Bottoms","Shoes","Accessories","Home","Outerwear"])
    return {"product_id": f"SKU-{n}", "name": f"{cat} {n}", "category": cat, "price": price}

def event_base(session, **extra):
    return {
        "event_id": uid(),
        "timestamp": now_iso(),
        "user": {
            "user_id": session["user_id"],
            "session_id": session["session_id"],
            "device": session["device"],
            "country": session["country"],
            "source": session["source"],
            "utm_campaign": session["utm_campaign"],
        },
        "context": {"currency": session["currency"], "store_id": session["store_id"]},
        **extra
    }

async def simulate_one_session(target):
    s = make_session()

    # page_view (landing)
    send_event(event_base(s, event_type="page_view", page=pick(["/","/home","/sale"])), target)

    # product_view (most sessions)
    product = make_product()
    await asyncio.sleep(random.uniform(0.05, 0.2))
    send_event(event_base(s, event_type="product_view", product=product), target)

    cart = []
    # add_to_cart?
    if random.random() < P_ADD_TO_CART:
        qty = random.choice([1,1,1,2])
        line = {"product_id": product["product_id"], "qty": qty, "price": product["price"]}
        cart.append(line)
        await asyncio.sleep(random.uniform(0.05, 0.2))
        send_event(event_base(s, event_type="add_to_cart", line_item=line, cart_size=len(cart)), target)

    # begin_checkout?
    if cart and random.random() < P_BEGIN_CHECKOUT:
        subtotal = sum(li["qty"]*li["price"] for li in cart)
        shipping = 0.0 if subtotal >= 75 else random.choice([4.99, 6.99, 9.99])
        tax = round(0.08*subtotal, 2) if s["country"] in ("US","CA") else 0.0
        total = round(subtotal+shipping+tax, 2)
        await asyncio.sleep(random.uniform(0.05, 0.2))
        send_event(event_base(s, event_type="begin_checkout",
                              cart=cart, subtotal=round(subtotal,2),
                              shipping=shipping, tax=tax, total=total), target)

        # purchase?
        if random.random() < P_PURCHASE:
            await asyncio.sleep(random.uniform(0.05, 0.2))
            send_event(event_base(s, event_type="purchase", items=cart,
                                  subtotal=round(subtotal,2), shipping=shipping,
                                  tax=tax, total=total,
                                  order_id="o_"+uid()[:12],
                                  payment_method=pick(["card","paypal","apple_pay","google_pay","klarna"])
                                  ), target)

async def main():
    ap = argparse.ArgumentParser(description="Minimal ecommerce event simulator")
    ap.add_argument("--target", required=True, help="Webhook URL (e.g., http://127.0.0.1:5000/ingest)")
    ap.add_argument("--rps", type=float, default=1.0, help="sessions per second")
    ap.add_argument("--duration", type=int, default=60, help="seconds to run")
    args = ap.parse_args()

    print(f"Sending ~{args.rps} sessions/sec for {args.duration}s → {args.target}")
    start = time.time()
    tasks = []
    while time.time() - start < args.duration:
        tasks.append(asyncio.create_task(simulate_one_session(args.target)))
        await asyncio.sleep(max(0.01, 1.0/args.rps))
        if len(tasks) > 500:
            tasks = [t for t in tasks if not t.done()]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

if __name__ == "__main__":
    asyncio.run(main())
