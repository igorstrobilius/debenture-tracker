"""Devedor de CRI/CRA a partir dos dados abertos da CVM (informe mensal das securitizadoras).

Ordem de preferência para o nome exibido:
  1. Devedor com maior % no informe mensal (nome via cadastro CVM de cias abertas ou BrasilAPI, com cache)
  2. "Nome_Emissao" do informe, quando parece um nome (ex.: "CORP HAPVIDA II")
  3. nada (o dashboard mostra só a securitizadora)
"""
import csv, io, json, os, re, time, zipfile, datetime as dt, urllib.request

CVM = "https://dados.cvm.gov.br/dados"
UA = {"User-Agent": "Mozilla/5.0 (debenture-tracker)"}


def _get(url, timeout=180, tries=3):
    err = None
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
                return r.read()
        except Exception as e:  # noqa
            err = e
            time.sleep(4 * (i + 1))
    print(f"  CVM falhou {url}: {err}", flush=True)
    return None


def _csv(zf, suffix):
    name = next((n for n in zf.namelist() if n.endswith(suffix)), None)
    if not name:
        return []
    text = zf.read(name).decode("latin1")
    return list(csv.DictReader(io.StringIO(text), delimiter=";"))


def _fl(s):
    try:
        return float(s)
    except Exception:
        return 0.0


def _digits(c):
    return re.sub(r"\D", "", c or "")


BAD_NOME = re.compile(r"(s[ée]rie|emiss[ãa]o|s[êe]nior|subordinad|mezanino|^cri\b|^cra\b|^\W*$|^[A-Z]{2,4}\d{2,})", re.I)


def _clean_nome(n):
    n = (n or "").strip()
    if not n or BAD_NOME.search(n) or len(n) < 3:
        return None
    n = re.sub(r"^(CORP|CRI|CRA)\s+", "", n, flags=re.I).strip()
    return n.upper()


def load(cache_path, years=None):
    years = years or [dt.date.today().year, dt.date.today().year - 1]
    nome, dev, cetip2cert = {}, {}, {}
    for kind in ("cri", "cra"):
        for y in sorted(years):  # ano mais recente por último, sobrescreve
            raw = _get(f"{CVM}/SECURIT/DOC/INF_MENSAL_{kind.upper()}/DADOS/inf_mensal_{kind}_{y}.zip")
            if not raw:
                continue
            zf = zipfile.ZipFile(io.BytesIO(raw))
            for r in sorted(_csv(zf, f"_geral_{y}.csv"), key=lambda r: r.get("Data_Referencia", "")):
                n = _clean_nome(r.get("Nome_Emissao"))
                if n:
                    nome[r["Codigo_Identificacao_Certificado"]] = n
            for r in _csv(zf, f"_classe_{y}.csv"):
                if r.get("Codigo_CETIP"):
                    cetip2cert[r["Codigo_CETIP"].strip()] = r["Codigo_Identificacao_Certificado"]
            best = {}
            for r in _csv(zf, f"_cedente_devedor_{y}.csv"):
                if r.get("Tipo") != "Devedor" or not _digits(r.get("CNPJ")):
                    continue
                k = r["Codigo_Identificacao_Certificado"]
                key = (r.get("Data_Referencia", ""), _fl(r.get("Percentual")))
                if k not in best or key > best[k][0]:
                    best[k] = (key, _digits(r["CNPJ"]))
            for k, (key, cnpj) in best.items():
                if key[1] >= 0.5:
                    dev[k] = cnpj
    print(f"  CVM: {len(nome)} nomes de emissão, {len(dev)} devedores, {len(cetip2cert)} códigos CETIP", flush=True)

    # nomes por CNPJ: cache → cadastro CVM → BrasilAPI
    cache = {}
    if os.path.exists(cache_path):
        cache = json.load(open(cache_path))
    raw = _get(f"{CVM}/CIA_ABERTA/CAD/DADOS/cad_cia_aberta.csv")
    if raw:
        for r in csv.DictReader(io.StringIO(raw.decode("latin1")), delimiter=";"):
            d = _digits(r.get("CNPJ_CIA"))
            if d and d not in cache and r.get("DENOM_SOCIAL"):
                cache[d] = r["DENOM_SOCIAL"].strip()
    missing = [c for c in set(dev.values()) if c not in cache][:400]
    for c in missing:
        b = _get(f"https://brasilapi.com.br/api/cnpj/v1/{c}", timeout=15, tries=1)
        if not b:
            cache[c] = None  # não tenta de novo todo dia
        else:
            try:
                j = json.loads(b)
                cache[c] = (j.get("razao_social") or "").strip() or None
            except Exception:
                pass
        time.sleep(0.3)
    json.dump(cache, open(cache_path, "w"), ensure_ascii=False, indent=0, sort_keys=True)

    def lookup(code, isin):
        cert = isin if (isin in dev or isin in nome) else cetip2cert.get(code, isin)
        if cert in dev and cache.get(dev[cert]):
            return cache[dev[cert]]
        return nome.get(cert)
    return lookup
