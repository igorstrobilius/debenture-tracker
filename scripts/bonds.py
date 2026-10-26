"""Bonds offshore de emissores brasileiros (HY/distressed) a partir do screener do TradingView.

Universo: país de risco Brasil, emissor corporativo, e
  (a) YTW >= 7,5%, ou
  (b) sem YTW publicado e preço < 80 (distressed, ex.: Raízen).
Preço e YTW são os do TradingView, sem cálculo próprio.
O screener anônimo devolve no máximo 10 linhas por consulta, então a busca é
fatiada por faixas de YTW / preço até cada fatia caber em 10.
"""
import json, time, urllib.request

URL = "https://scanner.tradingview.com/bond/scan"
COLS = ["description", "bond_issuer_cr_parent", "yield_to_worst", "close", "current_coupon",
        "issue_amount", "maturity_date", "coupon_currency", "currency_id", "update_mode"]
BASE = [{"left": "country", "operation": "equal", "right": "Brazil"},
        {"left": "bond_issuer_type", "operation": "equal", "right": "corporate"}]
VENUE_RANK = {"FINRA": 0, "BONDSCOM": 1, "LUXSE": 2}
MIN_YTW, DISTRESSED_PX = 7.5, 80


def _scan(filters):
    body = json.dumps({"filter": BASE + filters, "columns": COLS, "range": [0, 10]}).encode()
    for i in range(3):
        try:
            req = urllib.request.Request(URL, data=body, headers={"Content-Type": "application/json",
                                                                   "User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except Exception as e:  # noqa
            err = e
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"TradingView: {err}")


def _collect(field, lo, hi, extra, out, depth=0):
    f = extra + [{"left": field, "operation": "in_range", "right": [lo, hi]}]
    d = _scan(f)
    n = d.get("totalCount", 0)
    if n == 0:
        return
    if n <= 10 or depth > 18 or hi - lo < 1e-4:
        out.extend(d.get("data", []))
        if n > 10:  # fatia indivisível pelo campo: divide por vencimento
            for a, b in ((19000101, 20291231), (20300101, 20341231), (20350101, 20991231)):
                d2 = _scan(f + [{"left": "maturity_date", "operation": "in_range", "right": [a, b]}])
                out.extend(d2.get("data", []))
        return
    mid = (lo + hi) / 2
    _collect(field, lo, mid, extra, out, depth + 1)
    _collect(field, mid + 1e-9, hi, extra, out, depth + 1)
    time.sleep(0.2)


def load():
    raw = []
    _collect("yield_to_worst", MIN_YTW, 2000, [], raw)
    _collect("close", 0, DISTRESSED_PX, [{"left": "yield_to_worst", "operation": "empty"}], raw)
    best = {}
    for x in raw:
        venue = x["s"].split(":")[0]
        d = dict(zip(COLS, x["d"]))
        key = d["description"]
        rank = (d["yield_to_worst"] is None, VENUE_RANK.get(venue, 9))
        if key not in best or rank < best[key][0]:
            best[key] = (rank, venue, x["s"], d)
    rows = []
    for rank, venue, sym, d in best.values():
        m = d["maturity_date"]
        rows.append({
            "bond": d["description"],
            "e": d["bond_issuer_cr_parent"] or d["description"].split(" ")[0],
            "ytw": round(d["yield_to_worst"], 3) if d["yield_to_worst"] is not None else None,
            "px": round(d["close"], 3) if d["close"] is not None else None,
            "cpn": d["current_coupon"],
            "amt": round(d["issue_amount"] / 1e6) if d["issue_amount"] else None,
            "venc": f"{str(m)[:4]}-{str(m)[4:6]}-{str(m)[6:]}" if m and m < 29991231 else None,
            "perp": bool(m and m >= 29991231) or "PERP" in (d["description"] or ""),
            "ccy": d["coupon_currency"] or d["currency_id"] or None,
            "venue": venue, "sym": sym,
        })
    print(f"  TradingView: {len(raw)} listagens, {len(rows)} bonds únicos", flush=True)
    return rows
