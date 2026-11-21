
#!/usr/bin/env python3
"""Teste da API do PNCP (contratações com propostas abertas).

Só lê dados públicos e imprime um resumo. Nada é gravado no repositório.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import date, timedelta
from urllib.parse import urlparse

BASE = os.environ.get("PNCP_BASE", "https://pncp.gov.br/api/consulta/v1")
PAUSA = float(os.environ.get("PAUSA", "6"))  # segundos entre chamadas (limite visto: ~12/min)
ESPERA_ERRO = float(os.environ.get("ESPERA_ERRO", "20"))  # espera antes de tentar de novo após erro 429/5xx
HEADERS = {"User-Agent": "Mozilla/5.0 (teste buscador de licitacoes)", "Accept": "application/json"}
MODALIDADES = range(1, 14)

linhas = []


def say(txt=""):
    print(txt, flush=True)
    linhas.append(txt)


def get(path, params, tentativas=4):
    url = f"{BASE}{path}?{urllib.parse.urlencode(params)}"
    for i in range(tentativas):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=40) as r:
                corpo = r.read()
                if r.status == 204 or not corpo:
                    return 204, None, url
                return r.status, json.loads(corpo), url
        except urllib.error.HTTPError as e:
            trecho = e.read()[:200].decode("utf-8", "replace").replace("\n", " ")
            if e.code in (429, 500, 502, 503, 504) and i < tentativas - 1:
                time.sleep(ESPERA_ERRO * (i + 1))
                continue
            return e.code, trecho, url
        except Exception as e:  # rede, JSON inválido etc.
            if i < tentativas - 1:
                time.sleep(min(5, ESPERA_ERRO))
                continue
            return 0, repr(e), url
    return 0, "sem resposta", url


def main():
    data_final = (date.today() + timedelta(days=365)).strftime("%Y%m%d")
    say(f"Teste iniciado em {date.today().isoformat()} | base: {BASE} | dataFinal usada: {data_final}")
    say()
    say("1) Primeira página de cada modalidade")
    say("modalidade | status | registros | paginas | itens_na_pagina")

    amostra = []
    total_paginas = 0
    total_registros = 0
    maior = (0, None)
    erros = []
    for m in MODALIDADES:
        st, corpo, url = get(
            "/contratacoes/proposta",
            {"dataFinal": data_final, "codigoModalidadeContratacao": m, "pagina": 1, "tamanhoPagina": 50},
        )
        if st == 200 and isinstance(corpo, dict):
            itens = corpo.get("data") or []
            reg = corpo.get("totalRegistros", 0) or 0
            pag = corpo.get("totalPaginas", 0) or 0
            say(f"{m} | {st} | {reg} | {pag} | {len(itens)}")
            amostra.extend(itens)
            total_registros += reg
            total_paginas += pag
            if reg > maior[0]:
                maior = (reg, m)
        elif st == 204:
            say(f"{m} | 204 | 0 | 0 | 0")
        else:
            say(f"{m} | ERRO {st} | {str(corpo)[:120]}")
            erros.append((m, st))
        time.sleep(PAUSA)

    say()
    say(f"Total de registros somando as modalidades: {total_registros}")
    say(f"Total de paginas (50 por pagina) a coletar: {total_paginas}")
    say(f"Chamadas por minuto sugeridas: 10 -> tempo estimado de coleta: {total_paginas / 10:.0f} min")

    say()
    say("2) Campos do primeiro registro (nomes)")
    if amostra:
        r = amostra[0]
        say("campos: " + ", ".join(sorted(r.keys())))
        for sub in ("orgaoEntidade", "unidadeOrgao", "amparoLegal"):
            if isinstance(r.get(sub), dict):
                say(f"{sub}: " + ", ".join(sorted(r[sub].keys())))
        say("exemplo (cortado): " + json.dumps(r, ensure_ascii=False)[:1200])
    else:
        say("nenhum registro recebido")

    say()
    say("3) Preenchimento dos campos nos registros da amostra (primeira pagina de cada modalidade)")
    campos = ["objetoCompra", "valorTotalEstimado", "dataAberturaProposta", "dataEncerramentoProposta",
              "dataPublicacaoPncp", "linkSistemaOrigem", "linkProcessoEletronico", "numeroCompra", "processo"]
    for c in campos:
        cheio = sum(1 for r in amostra if r.get(c) not in (None, "", 0))
        say(f"{c}: {cheio}/{len(amostra)}")
    for sub, c in (("unidadeOrgao", "ufSigla"), ("unidadeOrgao", "municipioNome"),
                   ("unidadeOrgao", "codigoUnidade"), ("orgaoEntidade", "esferaId"),
                   ("orgaoEntidade", "poderId"), ("orgaoEntidade", "razaoSocial")):
        cheio = sum(1 for r in amostra if isinstance(r.get(sub), dict) and r[sub].get(c) not in (None, ""))
        say(f"{sub}.{c}: {cheio}/{len(amostra)}")

    say()
    say("4) Sistemas de origem (dominio de linkSistemaOrigem), amostra")
    hosts = Counter()
    for r in amostra:
        link = r.get("linkSistemaOrigem") or ""
        host = urlparse(link).netloc.lower() if link else "(sem link)"
        hosts[host] += 1
    for h, n in hosts.most_common(40):
        say(f"{n:5d}  {h}")

    say()
    say("5) Teste de filtro por UF e de paginação")
    if maior[1]:
        m = maior[1]
        st, corpo, _ = get(
            "/contratacoes/proposta",
            {"dataFinal": data_final, "codigoModalidadeContratacao": m, "uf": "CE", "pagina": 1, "tamanhoPagina": 50},
        )
        if st == 200 and isinstance(corpo, dict):
            ufs = Counter((r.get("unidadeOrgao") or {}).get("ufSigla") for r in corpo.get("data") or [])
            say(f"modalidade {m} com uf=CE: registros={corpo.get('totalRegistros')} | UFs vistas: {dict(ufs)}")
        else:
            say(f"modalidade {m} com uf=CE: status {st} {str(corpo)[:120]}")
        time.sleep(PAUSA)
        st, corpo, _ = get(
            "/contratacoes/proposta",
            {"dataFinal": data_final, "codigoModalidadeContratacao": m, "pagina": 2, "tamanhoPagina": 50},
        )
        if st == 200 and isinstance(corpo, dict):
            say(f"modalidade {m} pagina 2: itens={len(corpo.get('data') or [])} numeroPagina={corpo.get('numeroPagina')}")
        else:
            say(f"modalidade {m} pagina 2: status {st} {str(corpo)[:120]}")

    say()
    say("FIM DO TESTE" + (" (com erros em: " + str(erros) + ")" if erros else ""))
    with open("resultado.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(linhas) + "\n")
    return 1 if len(erros) == len(list(MODALIDADES)) else 0


if __name__ == "__main__":
    sys.exit(main())
