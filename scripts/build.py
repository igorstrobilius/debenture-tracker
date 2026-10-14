"""Monta data/latest.json para o Debenture Tracker.

Fontes (todas públicas):
  - ANBIMA: arquivo diário de debêntures (taxa indicativa, PU, %PU par, duration)
  - B3 / Boletim Diário: "Negócio a negócio" (taxa de cada negócio de DEB/CRI/CRA)
  - B3 / Boletim Diário: "Cadastro de instrumentos" (remuneração, vencimento, 12.431, volume emitido)

Regras:
  - Universo = papéis com taxa ANBIMA no dia  ∪  papéis com negócio B3 válido na janela de 1 semana
  - Negócio B3 válido: situação != Cancelado, volume >= R$ 20 mil, taxa preenchida
  - Taxas B3 = média ponderada por volume (último pregão e últimos 5 pregões)
  - O que não existir/não der para calcular fica null (n/a no dashboard)
"""
import csv, io, json, os, sys, time, datetime as dt, urllib.request, urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data")
MIN_VOL = 20_000
WINDOW_DAYS = 5
UA = {"User-Agent": "Mozilla/5.0 (debenture-tracker)"}
B3_EXPORT = "https://arquivos.b3.com.br/bdi/table/export/csv?lang=pt-BR"
ANBIMA_URL = "https://www.anbima.com.br/informacoes/merc-sec-debentures/arqs/db{:%y%m%d}.txt"
TYPES = ("DEB", "CRI", "CRA")


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def http(url, data=None, timeout=300, tries=4):
    err = None
    for i in range(tries):
        try:
            h = dict(UA)
            if data is not None:
                h["Content-Type"] = "application/json"
            req = urllib.request.Request(url, data=data, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return 404, b""
            err = e
        except Exception as e:  # noqa
            err = e
        log(f"  retry {i+1} {url[:90]}: {err}")
        time.sleep(5 * (i + 1))
    raise RuntimeError(f"falhou: {url}: {err}")


def num(s):
    if s is None:
        return None
    s = str(s).strip()
    if s in ("", "-", "--", "N/D", "null"):
        return None
    try:
        return float(s.replace(".", "").replace(",", ".")) if "," in s else float(s)
    except ValueError:
        return None


def now_brt():
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(hours=3)


def today_brt():
    return now_brt().date()


# ---------------------------------------------------------------- ANBIMA
def anbima_file(d):
    st, body = http(ANBIMA_URL.format(d), timeout=60)
    if st != 200 or len(body) < 1000:
        return None
    rows = {}
    for line in body.decode("latin1").splitlines():
        p = line.split("@")
        if len(p) < 14 or p[0] == "Código" or not p[0].strip():
            continue
        name = p[1].replace("(*)", "").replace("(**)", "").strip()
        rows[p[0].strip()] = {
            "nome": name,
            "venc": p[2].strip(),
            "rem": p[3].strip(),
            "taxa": num(p[6]),
            "pu": num(p[10]),
            "ppar": num(p[11]),
            "dur": num(p[12]),
        }
    return rows


def anbima_latest(start, back=12):
    for k in range(back):
        d = start - dt.timedelta(days=k)
        if d.weekday() >= 5:
            continue
        f = anbima_file(d)
        if f:
            return d, f
    return None, {}


# ---------------------------------------------------------------- B3
def b3_csv(name, d0, d1):
    body = json.dumps({"Name": name, "Date": f"{d0:%Y-%m-%d}", "FinalDate": f"{d1:%Y-%m-%d}",
                       "ClientId": "", "Filters": {}}).encode()
    st, raw = http(B3_EXPORT, data=body, timeout=600)
    text = raw.decode("utf-8-sig", errors="replace")
    lines = list(csv.reader(io.StringIO(text), delimiter=";"))
    hdr_i = next((i for i, r in enumerate(lines) if len(r) > 5 and ("Código IF" in r or "Instrumento financeiro" in r)), None)
    if hdr_i is None:
        return []
    h = lines[hdr_i]
    return [dict(zip(h, r)) for r in lines[hdr_i + 1:] if len(r) == len(h)]


def b3_trades(end):
    start = end - dt.timedelta(days=12)
    rows = b3_csv("Trade", start, end)
    out = []
    for r in rows:
        if r.get("Instrumento financeiro") not in TYPES:
            continue
        try:
            d = dt.datetime.strptime(r["Data negócio"][:10], "%d/%m/%Y").date()
        except Exception:
            continue
        out.append({
            "c": r["Código IF"].strip(), "t": r["Instrumento financeiro"], "emissor": r.get("Emissor", "").strip(),
            "d": d, "q": num(r.get("Quantidade negociada")), "p": num(r.get("Preço negócio")),
            "v": num(r.get("Volume financeiro (R$)")), "r": num(r.get("Taxa negócio")),
            "sit": r.get("Situação negócio", ""),
        })
    return out


def b3_registry(d):
    for k in range(6):
        dd = d - dt.timedelta(days=k)
        if dd.weekday() >= 5:
            continue
        rows = b3_csv("InstrumentRegistration", dd, dd)
        if rows:
            reg = {}
            for r in rows:
                if r.get("Instrumento financeiro") in TYPES:
                    reg[r["Código IF"].strip()] = r
            log(f"cadastro B3 {dd}: {len(reg)} papéis DEB/CRI/CRA")
            return reg
    return {}


# ---------------------------------------------------------------- classificação
def classify(reg, anb):
    if reg:
        idx = (reg.get("Indexador") or "").upper()
        pct = num(reg.get("Percentual indexador"))
        fee = num(reg.get("Taxa adicional")) or 0
        if idx == "DI":
            # DI + spread só quando há spread; 100% DI sem spread é negociado em % do DI
            if pct in (None, 100) and fee > 0:
                return "DI+"
            return "%DI"
        if idx == "IPCA":
            return "IPCA+"
        if idx == "PREFIXADO":
            return "PRE"
        return "OUTROS"
    rem = (anb or {}).get("rem", "").upper()
    if rem.startswith("DI +"):
        return "DI+"
    if "DO DI" in rem:
        return "%DI"
    if rem.startswith("IPCA"):
        return "IPCA+"
    if rem.startswith("PREFIX"):
        return "PRE"
    return "OUTROS"


def fmt_pct(x):
    s = f"{x:.4f}".rstrip("0").rstrip(".")
    return s.replace(".", ",")


def rem_from_reg(reg):
    idx = (reg.get("Indexador") or "").strip()
    pct = num(reg.get("Percentual indexador"))
    fee = num(reg.get("Taxa adicional"))
    if idx.upper() == "DI":
        if pct not in (None, 100) or not fee:
            return f"{fmt_pct(pct or 100)}% DI" + (f" + {fmt_pct(fee)}%" if fee else "")
        return f"DI + {fmt_pct(fee or 0)}%"
    if idx.upper() == "PREFIXADO":
        return f"{fmt_pct(fee)}%" if fee is not None else "Prefixado"
    return f"{idx} + {fmt_pct(fee)}%" if fee else idx


def to_iso(s):
    try:
        return dt.datetime.strptime(s.strip()[:10], "%d/%m/%Y").date().isoformat()
    except Exception:
        return None


def wavg(ts):
    v = sum(t["v"] for t in ts)
    return (sum(t["r"] * t["v"] for t in ts) / v) if v else None


# ---------------------------------------------------------------- main
def main():
    ref = today_brt()
    if len(sys.argv) > 1:
        ref = dt.date.fromisoformat(sys.argv[1])

    log("ANBIMA...")
    d_anb, anb = anbima_latest(ref)
    d_prev, anb_prev = anbima_latest((d_anb or ref) - dt.timedelta(days=7))
    log(f"  {d_anb}: {len(anb)} papéis; semana anterior {d_prev}: {len(anb_prev)}")

    log("B3 negócios...")
    trades = b3_trades(ref)
    # pregão de hoje só entra depois das 20h (B3 publica o arquivo completo ~18h)
    cutoff = ref if (len(sys.argv) > 1 or now_brt().hour >= 20) else ref - dt.timedelta(days=1)
    dates = sorted({t["d"] for t in trades if t["d"] <= cutoff})[-WINDOW_DAYS:]
    trades = [t for t in trades if t["d"] in dates]
    valid = [t for t in trades if t["sit"] != "Cancelado" and t["r"] is not None
             and t["v"] is not None and t["v"] >= MIN_VOL and abs(t["r"]) < 100]
    log(f"  pregões {dates}: {len(trades)} negócios, {len(valid)} válidos")
    d_b3 = dates[-1] if dates else None

    log("B3 cadastro...")
    reg = b3_registry(ref)

    by = {}
    for t in valid:
        by.setdefault(t["c"], []).append(t)
    issuer_b3 = {t["c"]: (t["t"], t["emissor"]) for t in trades}

    codes = set(k for k, v in anb.items() if v["taxa"] is not None or v["ppar"] is not None) | set(by)
    rows = []
    for c in sorted(codes):
        a, ap, r, ts = anb.get(c), anb_prev.get(c), reg.get(c), by.get(c, [])
        tipo = (r or {}).get("Instrumento financeiro") or issuer_b3.get(c, ("DEB",))[0]
        emissor = (a or {}).get("nome") or ((r or {}).get("Emissor") or issuer_b3.get(c, ("", ""))[1]).strip()
        venc = to_iso(a["venc"]) if a else (to_iso(r["Vencimento"]) if r else None)
        rem = a["rem"] if a else (rem_from_reg(r) if r else None)
        inc = None
        if r:
            inc = (r.get("Incentivada") or "").strip().lower() == "true"
        vol_em = None
        if r:
            q, pu = num(r.get("Quantidade emitida")), num(r.get("Preço unitário de emissão"))
            if q and pu:
                vol_em = round(q * pu / 1e6, 1)
        # par para converter preço B3 em % do par (só quando a ANBIMA tem PU e %PU par)
        par = (a["pu"] / a["ppar"] * 100) if a and a["pu"] and a["ppar"] else None
        t1 = [t for t in ts if t["d"] == d_b3]
        last = max((t["d"] for t in ts), default=None)
        def ppar_of(sub):
            if not par or not sub:
                return None
            v = sum(t["v"] for t in sub)
            q = sum(t["q"] or 0 for t in sub)
            return round(v / q / par * 100, 2) if q else None
        row = {
            "c": c, "t": tipo, "e": emissor, "idx": classify(r, a), "rem": rem, "venc": venc,
            "inc": inc, "vem": vol_em,
            "a": a["taxa"] if a else None,
            "a_w": round((a["taxa"] - ap["taxa"]) * 100, 1) if a and ap and a["taxa"] is not None and ap["taxa"] is not None else None,
            "cts": round(a["ppar"], 2) if a and a["ppar"] is not None else None,
            "cts_w": round(a["ppar"] - ap["ppar"], 2) if a and ap and a["ppar"] is not None and ap["ppar"] is not None else None,
            "dur": round(a["dur"] / 252, 2) if a and a["dur"] else None,
            "b1": round(wavg(t1), 4) if t1 else None,
            "b1v": round(sum(t["v"] for t in t1) / 1e6, 2) if t1 else None,
            "b1n": len(t1) or None,
            "bw": round(wavg(ts), 4) if ts else None,
            "bwv": round(sum(t["v"] for t in ts) / 1e6, 2) if ts else None,
            "bwn": len(ts) or None,
            "bwp": ppar_of(ts),
            "last": last.isoformat() if last else None,
        }
        rows.append(row)

    meta = {
        "gerado_em": dt.datetime.now(dt.timezone.utc).replace(microsecond=0, tzinfo=None).isoformat() + "Z",
        "anbima_data": d_anb.isoformat() if d_anb else None,
        "anbima_semana_anterior": d_prev.isoformat() if d_prev else None,
        "b3_ultimo_pregao": d_b3.isoformat() if d_b3 else None,
        "b3_janela": [d.isoformat() for d in dates],
        "corte_volume_rs": MIN_VOL,
        "n_papeis": len(rows),
    }
    os.makedirs(os.path.join(OUT, "history"), exist_ok=True)
    payload = {"meta": meta, "rows": rows}
    with open(os.path.join(OUT, "latest.json"), "w") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    if d_b3:
        with open(os.path.join(OUT, "history", f"{d_b3.isoformat()}.json"), "w") as f:
            json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    log(json.dumps(meta, ensure_ascii=False))
    if not anb or not dates:
        sys.exit("dados incompletos (ANBIMA ou B3 vazios)")


if __name__ == "__main__":
    main()
