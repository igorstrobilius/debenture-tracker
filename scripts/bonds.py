"""Bonds offshore de emissores brasileiros (HY/distressed) a partir do TradingView.

Universo = (1) screener de bonds do TradingView com país de risco Brasil
         + (2) busca por cada termo de data/bonds_emissores.txt (o screener não indexa todos os bonds).
Filtro: emissor corporativo e (YTW >= 7,5%  ou  sem YTW publicado e preço < 90).
Preço e YTW são os do TradingView, sem cálculo próprio.
"""
import json, os, re, time, datetime as dt, urllib.parse, urllib.request

SCAN = "https://scanner.tradingview.com/bond/scan"
SYM = "https://scanner.tradingview.com/symbol"
SEARCH = "https://symbol-search.tradingview.com/symbol_search/v3/"
HDR = {"User-Agent": "Mozilla/5.0", "Origin": "https://www.tradingview.com", "Referer": "https://www.tradingview.com/"}
FIELDS = ["description", "bond_issuer_cr_parent", "yield_to_worst", "close", "current_coupon",
          "issue_amount", "maturity_date", "coupon_currency", "currency_id", "country", "bond_issuer_type"]
BASE = [{"left": "country", "operation": "equal", "right": "Brazil"},
        {"left": "bond_issuer_type", "operation": "equal", "right": "corporate"}]
VENUE_RANK = {"FINRA": 0, "BONDSCOM": 1, "LUXSE": 2}
MIN_YTW, DISTRESSED_PX = 7.5, 90
TODAY = int(dt.date.today().strftime("%Y%m%d"))


def _http(url, body=None, tries=3):
    err = None
    for i in range(tries):
        try:
            h = dict(HDR)
            if body is not None:
                h["Content-Type"] = "application/json"
            with urllib.request.urlopen(urllib.request.Request(url, data=body, headers=h), timeout=60) as r:
                return json.load(r)
        except Exception as e:  # noqa
            err = e
            time.sleep(2 * (i + 1))
    print(f"  TradingView falhou {url[:90]}: {err}", flush=True)
    return None


# ---------------------------------------------------------------- (1) screener
def _scan(filters):
    body = json.dumps({"filter": BASE + filters, "columns": ["description"], "range": [0, 10]}).encode()
    return _http(SCAN, body) or {}


def _collect(field, lo, hi, extra, out, depth=0):
    f = extra + [{"left": field, "operation": "in_range", "right": [lo, hi]}]
    d = _scan(f)
    n = d.get("totalCount", 0)
    if n == 0:
        return
    if n <= 10 or depth > 18:
        out.extend(x["s"] for x in d.get("data", []))
        return
    mid = (lo + hi) / 2
    _collect(field, lo, mid, extra, out, depth + 1)
    _collect(field, mid + 1e-9, hi, extra, out, depth + 1)


def _screener_symbols():
    out = []
    _collect("yield_to_worst", MIN_YTW, 2000, [], out)
    _collect("close", 0, DISTRESSED_PX, [{"left": "yield_to_worst", "operation": "empty"}], out)
    return out


# ---------------------------------------------------------------- (2) busca por emissor
def _clean(s):
    return re.sub(r"</?em>", "", s or "").strip()


def _search(term, max_pages=2):
    res = []
    for p in range(max_pages):
        q = urllib.parse.urlencode({"text": term, "search_type": "bond", "start": p * 50})
        d = _http(f"{SEARCH}?{q}")
        if not d:
            break
        syms = d.get("symbols", [])
        res += syms
        if not d.get("symbols_remaining") or not syms:
            break
    t = term.lower().split()[0]
    return [(f'{x.get("source_id") or x["exchange"]}:{_clean(x["symbol"])}', _clean(x.get("description")))
            for x in res if t in _clean(x.get("description")).lower()]


def _matured(desc):
    m = re.search(r"(\d{2})-([A-Z]{3})-(\d{4})", desc or "")
    if not m:
        return False
    try:
        return int(dt.datetime.strptime(m.group(0), "%d-%b-%Y").strftime("%Y%m%d")) < TODAY
    except ValueError:
        return False


def _fetch(sym):
    q = urllib.parse.urlencode({"symbol": sym, "fields": ",".join(FIELDS), "no_404": "true"})
    return _http(f"{SYM}?{q}", tries=2)


def load(terms_path=None):
    # candidatos agrupados por bond (descrição)
    cands = {}
    for s in _screener_symbols():
        cands.setdefault(None, []).append(s)
    screener = cands.pop(None, [])
    terms = []
    if terms_path and os.path.exists(terms_path):
        terms = [l.strip() for l in open(terms_path, encoding="utf-8") if l.strip() and not l.startswith("#")]
    by_desc = {}
    for t in terms:
        for sym, desc in _search(t):
            if not _matured(desc):
                by_desc.setdefault(desc, ([], t))[0].append(sym)
        time.sleep(0.2)
    print(f"  TradingView: {len(screener)} listagens no screener, {len(by_desc)} bonds pela busca ({len(terms)} termos)", flush=True)

    best = {}

    def consider(sym, d, term=None):
        if not d or d.get("bond_issuer_type") not in (None, "corporate"):
            return
        # resultado de busca fora do Brasil só entra se o nome do bond começa pelo termo (ex.: Vamos Europe, Abra Global)
        if term and d.get("country") != "Brazil" and not (d.get("description") or "").lower().startswith(term.lower()):
            return
        m = d.get("maturity_date")
        if m and m < TODAY:
            return
        ytw, px = d.get("yield_to_worst"), d.get("close")
        if not px or px <= 0.5:  # preço zerado = dado ruim do TradingView
            return
        if not ((ytw is not None and ytw >= MIN_YTW) or (ytw is None and px is not None and px < DISTRESSED_PX)):
            return
        venue = sym.split(":")[0]
        key = d.get("description")
        rank = (ytw is None, VENUE_RANK.get(venue, 9))
        if key not in best or rank < best[key][0]:
            best[key] = (rank, venue, sym, d)

    for s in screener:
        consider(s, _fetch(s))
    for desc, (syms, term) in by_desc.items():
        syms = sorted(set(syms), key=lambda s: VENUE_RANK.get(s.split(":")[0], 9))
        for s in syms[:3]:
            d = _fetch(s)
            if d and d.get("close") is not None:
                consider(s, d, term)
                if d.get("yield_to_worst") is not None:
                    break

    rows = []
    for rank, venue, sym, d in best.values():
        m = d.get("maturity_date")
        desc = d.get("description") or ""
        rows.append({
            "bond": desc,
            "e": d.get("bond_issuer_cr_parent") or desc.split(" ")[0],
            "ytw": round(d["yield_to_worst"], 3) if d.get("yield_to_worst") is not None else None,
            "px": round(d["close"], 3) if d.get("close") is not None else None,
            "cpn": d.get("current_coupon"),
            "amt": round(d["issue_amount"] / 1e6) if d.get("issue_amount") else None,
            "venc": f"{str(m)[:4]}-{str(m)[4:6]}-{str(m)[6:]}" if m and m < 29991231 else None,
            "perp": bool(m and m >= 29991231) or "PERP" in desc,
            "ccy": d.get("coupon_currency") or d.get("currency_id") or None,
            "venue": venue, "sym": sym,
        })
    print(f"  TradingView: {len(rows)} bonds no filtro", flush=True)
    return rows
