"""Devedor de CRI/CRA a partir dos dados abertos da CVM (informe mensal das securitizadoras).

Ordem de preferência para o nome exibido:
  1. Devedor com maior % no informe mensal (nome via cadastro CVM de cias abertas ou BrasilAPI, com cache)
  2. "Nome_Emissao" do informe, quando parece um nome (ex.: "CORP HAPVIDA II")
  3. nada (o dashboard mostra só a securitizadora)
"""
import csv, io, json, os, re, sys, time, zipfile, datetime as dt, urllib.request

csv.field_size_limit(sys.maxsize)  # o informe da CVM tem campos de texto enormes

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


def _cnpj_ok(c):
    if len(c) != 14 or len(set(c)) == 1:
        return False
    def dv(base, w):
        t = sum(int(a) * b for a, b in zip(base, w)) % 11
        return "0" if t < 2 else str(11 - t)
    w1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    return c[12] == dv(c[:12], w1) and c[13] == dv(c[:13], [6] + w1)


def _digits(c):
    return re.sub(r"\D", "", c or "")


BAD_NOME = re.compile(r"(s[ée]rie|emiss[ãa]o|s[êe]nior|subordinad|mezanino|^cri\b|^cra\b|^\W*$|^[A-Z]{2,4}\d{2,})", re.I)


def _clean_nome(n):
    n = re.sub(r"^\s*(CORP|CRI|CRA)[\s\-_]+", "", (n or "").strip(), flags=re.I).strip(" -_")
    if not n or BAD_NOME.search(n) or len(n) < 3:
        return None
    return n.upper()


def load(cache_path, years=None):
    y0 = dt.date.today().year
    years = years or [y0 - 2, y0 - 1, y0]
    nome, dev, cetip2cert, par = {}, {}, {}, {}
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
                code = (r.get("Codigo_CETIP") or "").strip()
                if code:
                    cetip2cert[code] = r["Codigo_Identificacao_Certificado"]
                    # valor unitário da série no fim do mês de referência (saldo atualizado / quantidade)
                    q, v = _fl(r.get("Quantidade_Certificados")), _fl(r.get("Valor_Certificados"))
                    ref = r.get("Data_Referencia", "")
                    if q > 0 and v > 0 and (code not in par or ref >= par[code][0]):
                        par[code] = (ref, v / q)
            # por certificado, no informe mais recente: maior devedor e maior cedente (CNPJ e %)
            last = {}
            for r in _csv(zf, f"_cedente_devedor_{y}.csv"):
                tipo, cnpj = r.get("Tipo"), _digits(r.get("CNPJ"))
                if tipo not in ("Devedor", "Cedente") or not _cnpj_ok(cnpj):
                    continue
                k, ref = r["Codigo_Identificacao_Certificado"], r.get("Data_Referencia", "")
                if k not in last or ref > last[k][0]:
                    last[k] = (ref, {})
                if ref == last[k][0]:
                    cur = last[k][1].get(tipo)
                    pct = _fl(r.get("Percentual"))
                    if cur is None or pct > cur[0]:
                        last[k][1][tipo] = (pct, cnpj)
            for k, (ref, t) in last.items():
                dv, cd = t.get("Devedor"), t.get("Cedente")
                # devedor relevante (>= 20%); senão cedente concentrado (>= 50%), típico de CRA/CRI corporativo
                if dv and dv[0] >= 0.2:
                    dev[k] = dv[1]
                elif cd and cd[0] >= 0.5:
                    dev[k] = cd[1]
                elif k in dev:
                    pass
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
    missing = [c for c in set(dev.values()) if c not in cache][:600]
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
    lookup.par = lambda code: par.get(code)
    return lookup
