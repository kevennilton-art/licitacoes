#!/usr/bin/env python3
"""Coleta as licitações com propostas abertas no PNCP e grava dois arquivos:

  licitacoes.json.gz  lista enxuta de licitações (usada pelo site)
  meta.json           data da coleta, totais e estatísticas

Se qualquer modalidade falhar, nada é gravado (o site continua com os dados anteriores).
"""
import gzip
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import threading
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

BASE = os.environ.get("PNCP_BASE", "https://pncp.gov.br/api/consulta/v1")
SAIDA = os.environ.get("OUT", "saida")
PAUSA = float(os.environ.get("PAUSA", "6"))  # intervalo mínimo entre chamadas, somando todos os trabalhadores (~10 por minuto; limite visto: ~12)
TRABALHADORES = int(os.environ.get("TRABALHADORES", "3"))  # chamadas simultâneas (cada uma leva ~10s no PNCP)
ESPERA_ERRO = float(os.environ.get("ESPERA_ERRO", "30"))
MAX_FALHAS = int(os.environ.get("MAX_FALHAS", "5"))  # páginas parcialmente perdidas que ainda aceitamos
ESPERA_RODADA = float(os.environ.get("ESPERA_RODADA", "180"))  # pausa antes de tentar de novo uma modalidade que falhou
ANTERIORES = os.environ.get("ANTERIORES_URL") or "https://raw.githubusercontent.com/{}/dados/licitacoes.json.gz".format(os.environ.get("GITHUB_REPOSITORY", "kevennilton-art/licitacoes"))
MAX_MODALIDADES_DESATUALIZADAS = int(os.environ.get("MAX_DESATUALIZADAS", "4"))
MAX_PAGINAS = int(os.environ.get("MAX_PAGINAS", "0"))  # 0 = todas (só para testes usar outro valor)
MODALIDADES = range(1, 14)
HEADERS = {"User-Agent": "Mozilla/5.0 (buscador de licitacoes)", "Accept": "application/json"}


def log(msg):
    print(msg, flush=True)


_trava = threading.Lock()
_proxima = [0.0]


def esperar_vez():
    """Garante um intervalo mínimo entre as chamadas de todos os trabalhadores."""
    with _trava:
        quando = max(time.time(), _proxima[0])
        _proxima[0] = quando + PAUSA
    espera = quando - time.time()
    if espera > 0:
        time.sleep(espera)


def segurar_todos(segundos):
    """Em caso de erro, todos os trabalhadores esperam."""
    with _trava:
        _proxima[0] = max(_proxima[0], time.time() + segundos)


def chamar(path, params, tentativas=6):
    url = f"{BASE}{path}?{urllib.parse.urlencode(params)}"
    ultimo = "sem resposta"
    for i in range(tentativas):
        esperar_vez()
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=90) as r:
                corpo = r.read()
                if r.status == 204 or not corpo:
                    return 204, None
                return r.status, json.loads(corpo)
        except urllib.error.HTTPError as e:
            ultimo = f"HTTP {e.code}"
            limite = tentativas if e.code == 429 else min(tentativas, 3)
            if e.code in (429, 500, 502, 503, 504) and i < limite - 1:
                log(f"  {ultimo}; nova tentativa em {int(ESPERA_ERRO * (i + 1))}s")
                segurar_todos(ESPERA_ERRO * (i + 1))
                continue
            return e.code, ultimo
        except Exception as e:  # rede, JSON inválido
            ultimo = repr(e)[:120]
            if i < tentativas - 1:
                log(f"  falha de rede ({ultimo}); nova tentativa em {int(ESPERA_ERRO * (i + 1))}s")
                segurar_todos(ESPERA_ERRO * (i + 1))
                continue
    return 0, ultimo


def texto(v, n=None):
    if v is None:
        return None
    v = str(v).strip()
    if not v:
        return None
    return v[:n] if n else v


def enxuto(r):
    org = r.get("orgaoEntidade") or {}
    un = r.get("unidadeOrgao") or {}
    am = r.get("amparoLegal") or {}
    d = {
        "id": r.get("numeroControlePNCP"),
        "obj": texto(r.get("objetoCompra"), 1500),
        "mi": r.get("modalidadeId"),
        "mn": texto(r.get("modalidadeNome")),
        "dp": texto(r.get("modoDisputaNome")),
        "st": texto(r.get("situacaoCompraNome")),
        "vl": r.get("valorTotalEstimado"),
        "pub": texto(r.get("dataPublicacaoPncp")),
        "ab": texto(r.get("dataAberturaProposta")),
        "enc": texto(r.get("dataEncerramentoProposta")),
        "org": texto(org.get("razaoSocial")),
        "cnpj": texto(org.get("cnpj")),
        "esf": texto(org.get("esferaId")),
        "pod": texto(org.get("poderId")),
        "und": texto(un.get("nomeUnidade")),
        "uf": texto(un.get("ufSigla")),
        "mun": texto(un.get("municipioNome")),
        "num": texto(r.get("numeroCompra")),
        "proc": texto(r.get("processo")),
        "ano": r.get("anoCompra"),
        "seq": r.get("sequencialCompra"),
        "lnk": texto(r.get("linkSistemaOrigem") or r.get("linkProcessoEletronico"), 400),
        "usr": texto(r.get("usuarioNome")),
        "srp": True if r.get("srp") else None,
        "amp": texto(am.get("nome")),
    }
    return {k: v for k, v in d.items() if v is not None}


def main():
    inicio = time.time()
    data_final = (datetime.now(timezone.utc) + timedelta(days=365)).strftime("%Y%m%d")
    itens = {}
    por_modalidade = {}
    erros = []

    def pegar(m, pagina):
        st, corpo = chamar(
            "/contratacoes/proposta",
            {"dataFinal": data_final, "codigoModalidadeContratacao": m, "pagina": pagina, "tamanhoPagina": 50},
        )
        return pagina, st, corpo

    def absorver(m, corpo):
        for r in corpo.get("data") or []:
            e = enxuto(r)
            if e.get("id"):
                itens[e["id"]] = e
                nome = e.get("mn") or str(m)
                por_modalidade[nome] = por_modalidade.get(nome, 0) + 1

    sem_dados = []

    def recuperar(m, pagina):
        """Uma página de 50 falhou: busca o mesmo trecho em 5 páginas de 10 para salvar o que for possível."""
        dados, falhas = [], 0
        for k in range(5):
            st, c = chamar(
                "/contratacoes/proposta",
                {"dataFinal": data_final, "codigoModalidadeContratacao": m,
                 "pagina": (pagina - 1) * 5 + 1 + k, "tamanhoPagina": 10},
                tentativas=2,
            )
            if st == 200 and isinstance(c, dict):
                dados += c.get("data") or []
            elif st != 204:
                falhas += 1
        return dados, falhas

    def coletar_modalidade(m):
        """Devolve None se deu certo, ou o motivo da falha. O que já foi baixado fica guardado."""
        _, st, corpo = pegar(m, 1)
        if st == 204:
            return None
        if st != 200 or not isinstance(corpo, dict):
            return f"pagina 1: {st} {str(corpo)[:60]}"
        total_pag = int(corpo.get("totalPaginas") or 1)
        if MAX_PAGINAS:
            total_pag = min(total_pag, MAX_PAGINAS)
        absorver(m, corpo)
        paginas = list(range(2, total_pag + 1))
        feitas = 1
        perdidas = []
        lote = max(1, TRABALHADORES * 4)
        with ThreadPoolExecutor(max_workers=TRABALHADORES) as ex:
            for i in range(0, len(paginas), lote):
                grupo = paginas[i:i + lote]
                for pagina, st, corpo in ex.map(lambda p: pegar(m, p), grupo):
                    if st == 204:
                        continue
                    if st != 200 or not isinstance(corpo, dict):
                        dados, falhas = recuperar(m, pagina)
                        absorver(m, {"data": dados})
                        if falhas:
                            perdidas.append(f"modalidade {m} pagina {pagina}: {st} ({falhas} de 5 trechos perdidos)")
                        log(f"  pagina {pagina} da modalidade {m} deu erro {st}; recuperados {len(dados)} registros")
                feitas += len(grupo)
                if len(perdidas) > MAX_FALHAS:
                    return f"muitas paginas com falha ({len(perdidas)})"
                if feitas % 24 < lote or feitas >= total_pag:
                    log(f"modalidade {m}: pagina {min(feitas, total_pag)}/{total_pag} | total acumulado {len(itens)} | {int(time.time() - inicio)}s")
        sem_dados.extend(perdidas)
        return None

    # 1a rodada: se as 3 primeiras que falharem forem seguidas, o PNCP está fora do ar: para logo
    pendentes = []
    for m in MODALIDADES:
        motivo = coletar_modalidade(m)
        if motivo:
            log(f"modalidade {m} falhou: {motivo}")
            pendentes.append((m, motivo))
            if len(pendentes) >= 3:
                erros.append("o PNCP parece fora do ar (3 modalidades falharam)")
                break

    # rodadas extras: espera e tenta de novo só as que falharam
    for rodada in range(2):
        if not pendentes or erros:
            break
        log(f"Aguardando {int(ESPERA_RODADA)}s para tentar de novo as modalidades {[m for m, _ in pendentes]}")
        time.sleep(ESPERA_RODADA)
        novas = []
        for m, _ in pendentes:
            motivo = coletar_modalidade(m)
            if motivo:
                log(f"modalidade {m} falhou de novo: {motivo}")
                novas.append((m, motivo))
        pendentes = novas

    # as que continuam falhando: mantém os registros da coleta anterior (ainda não encerrados)
    desatualizadas = []
    if pendentes and not erros:
        if len(pendentes) > MAX_MODALIDADES_DESATUALIZADAS:
            erros.append(f"{len(pendentes)} modalidades falharam")
        else:
            try:
                req = urllib.request.Request(ANTERIORES, headers=HEADERS)
                with urllib.request.urlopen(req, timeout=120) as r:
                    anteriores = json.loads(gzip.decompress(r.read()))
            except Exception as e:
                anteriores = None
                erros.append(f"modalidades {[m for m, _ in pendentes]} falharam e nao foi possivel ler a coleta anterior ({repr(e)[:80]})")
            if anteriores is not None:
                agora_br = (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S")
                for m, motivo in pendentes:
                    mantidos = 0
                    for x in anteriores:
                        if x.get("mi") == m and x.get("id") and (x.get("enc") or "9") >= agora_br and x["id"] not in itens:
                            itens[x["id"]] = x
                            mantidos += 1
                    desatualizadas.append({"modalidade": m, "motivo": motivo, "registrosMantidos": mantidos})
                    log(f"modalidade {m}: mantidos {mantidos} registros da coleta anterior ({motivo})")

    if erros:
        log("ERROS NA COLETA (nada foi gravado):")
        for e in erros:
            log("  " + e)
        return 1
    if not itens:
        log("Nenhuma licitação coletada. Nada foi gravado.")
        return 1

    lista = sorted(itens.values(), key=lambda x: x.get("enc") or "")
    os.makedirs(SAIDA, exist_ok=True)
    bruto = json.dumps(lista, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    with open(os.path.join(SAIDA, "licitacoes.json.gz"), "wb") as f:
        f.write(gzip.compress(bruto, 9, mtime=0))

    dominios = Counter()
    for x in lista:
        link = x.get("lnk")
        if link:
            try:
                dominios[urlparse(link).netloc.lower() or "(vazio)"] += 1
            except Exception:
                dominios["(invalido)"] += 1
    usuarios = Counter(x.get("usr") or "(sem usuario)" for x in lista)
    agora = datetime.now(timezone.utc)
    meta = {
        "geradoEm": agora.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "geradoEmBrasilia": (agora - timedelta(hours=3)).strftime("%d/%m/%Y %H:%M"),
        "total": len(lista),
        "tamanhoJsonBytes": len(bruto),
        "duracaoSegundos": int(time.time() - inicio),
        "porModalidade": por_modalidade,
        "semLink": sum(1 for x in lista if not x.get("lnk")),
        "paginasComFalha": sem_dados,
        "modalidadesDesatualizadas": desatualizadas,
        "topUsuarios": usuarios.most_common(80),
        "topDominios": dominios.most_common(80),
    }
    with open(os.path.join(SAIDA, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    log(f"OK: {len(lista)} licitações | JSON {len(bruto) / 1e6:.1f} MB | {meta['duracaoSegundos']}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
