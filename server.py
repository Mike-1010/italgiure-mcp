"""Connettore MCP per SentenzeWeb (italgiure.giustizia.it/sncass) — ricerca
gratuita e in tempo reale delle sentenze della Corte di Cassazione (civile e
penale, dal 2012). Nessun login richiesto per questa sezione del sito.

Il sito espone un core Apache Solr dietro un gateway ISAPI: interroghiamo
direttamente quell'endpoint e otteniamo JSON, niente scraping HTML. Il sito
però limita molto la frequenza delle richieste (vedi RATE_OVERFLOW_MARKER in
config.py): restiamo deliberatamente lenti e cortesi.
"""
import argparse
import asyncio
import os
import re
import sys
import time
from typing import Optional

import httpx
from mcp.server.fastmcp import FastMCP

import config
import tls_fix

_HOSTNAME = config.BASE.split("//", 1)[1].split("/", 1)[0]

INSTRUCTIONS = """\
Accesso in tempo reale a SentenzeWeb, il motore di ricerca gratuito della
Corte di Cassazione (italgiure.giustizia.it/sncass): sentenze e ordinanze
della Cassazione civile e penale dal 2012 a oggi. Nessun contenuto a
pagamento: tutto ciò che restituisce questo connettore è pubblico.

Usa `cerca_cassazione` per ricerche testuali (anche con più parole: vengono
cercate come prossimità, non richiede virgolette). Usa `ultime_cassazione`
per gli ultimi provvedimenti pubblicati, senza una query specifica.

Ogni risultato include gli estremi del provvedimento, un estratto con i
termini di ricerca evidenziati, e il link al PDF integrale (versione OCR
"pulita"): il testo integrale va sempre letto lì, l'estratto è solo un
indizio di pertinenza. Cita sempre numero, sezione e data della decisione.

Il sito ha un limitatore di frequenza severo: se una ricerca fallisce con un
messaggio di sovraccarico, aspetta prima di riprovare invece di ripetere la
chiamata subito.
"""

mcp = FastMCP(
    "italgiure",
    instructions=INSTRUCTIONS,
    host=os.getenv("MCP_HOST", "0.0.0.0"),
    port=int(os.getenv("PORT", "8000")),
    streamable_http_path=os.getenv("MCP_PATH", "/mcp"),
)

_client: Optional[httpx.AsyncClient] = None
_client_lock = asyncio.Lock()
_lock = asyncio.Lock()
_last_request = 0.0
_cache: dict[str, tuple[float, dict]] = {}
_session_lock = asyncio.Lock()
_session_ready_at = 0.0


class RateOverflow(Exception):
    """Il sito ha rifiutato la richiesta per eccesso di frequenza."""


_tls_fix_error: Optional[str] = None
_tls_fix_hops: int = -1


async def get_client() -> httpx.AsyncClient:
    """Client HTTP condiviso, creato al primo utilizzo con un SSLContext che
    include gli eventuali certificati intermedi mancanti (vedi tls_fix.py)."""
    global _client, _tls_fix_error, _tls_fix_hops
    if _client is not None:
        return _client
    async with _client_lock:
        if _client is not None:
            return _client
        try:
            ssl_ctx, hops = await tls_fix.build_ssl_context_diag(_HOSTNAME)
            verify = ssl_ctx
            _tls_fix_hops = hops
        except Exception as e:
            # Non siamo riusciti a ricostruire la catena: ripieghiamo sulla
            # verifica standard (certifi), ma teniamo traccia del motivo per
            # poterlo diagnosticare (vedi diagnostica_tls()).
            _tls_fix_error = f"{type(e).__name__}: {e}"
            verify = True
        _client = httpx.AsyncClient(
            headers={"User-Agent": config.USER_AGENT},
            timeout=config.TIMEOUT,
            follow_redirects=True,
            verify=verify,
        )
        return _client


async def _throttle() -> None:
    global _last_request
    wait = config.MIN_INTERVAL - (time.monotonic() - _last_request)
    if wait > 0:
        await asyncio.sleep(wait)
    _last_request = time.monotonic()


async def ensure_session() -> None:
    """Visita la pagina di ricerca per ottenere il cookie di sessione."""
    global _session_ready_at
    if time.monotonic() - _session_ready_at < config.SESSION_TTL:
        return
    async with _session_lock:
        if time.monotonic() - _session_ready_at < config.SESSION_TTL:
            return
        client = await get_client()
        async with _lock:
            await _throttle()
            try:
                r = await client.get(config.SEARCH_PAGE_URL)
            except Exception as e:
                detail = f" [tls_fix: {_tls_fix_error}]" if _tls_fix_error else ""
                raise RuntimeError(
                    f"Impossibile connettersi a italgiure.giustizia.it: {e}{detail}"
                ) from e
        if r.status_code == 200:
            _session_ready_at = time.monotonic()


def _escape(term: str) -> str:
    return re.sub(r'(["\\])', r"\\\1", term.strip())


def build_query(query: str, tipo: str, sezione: Optional[str],
                 anno_da: Optional[int], anno_a: Optional[int]) -> str:
    query = (query or "").strip()
    if not query:
        text_clause = "*:*"
    else:
        words = query.split()
        escaped = _escape(query)
        if len(words) > 1:
            text_clause = f'("{escaped}"~7)'
        else:
            text_clause = escaped

    kind_clause = config.KIND_FILTERS.get(tipo, config.KIND_FILTERS["entrambi"])
    clauses = [text_clause, kind_clause]

    if sezione:
        clauses.append(f'ssz:"{_escape(sezione)}"')
    if anno_da or anno_a:
        lo = anno_da or "*"
        hi = anno_a or "*"
        clauses.append(f"anno:[{lo} TO {hi}]")

    return " AND ".join(f"({c})" for c in clauses)


async def fetch_solr(q: str, rows: int, _attempt: int = 0) -> dict:
    rows = max(1, min(rows, config.MAX_ROWS))
    cache_key = f"{q}|{rows}"
    now = time.monotonic()
    if cache_key in _cache:
        ts, data = _cache[cache_key]
        if now - ts < config.CACHE_TTL:
            return data

    await ensure_session()

    data = {
        "start": "0",
        "rows": str(rows),
        "q": q,
        "wt": "json",
        "indent": "off",
        "sort": "pd desc,numdec desc",
        "fl": config.FL_FIELDS,
        **config.HL_PARAMS,
        "hl.q": q,
    }

    client = await get_client()
    async with _lock:
        await _throttle()
        try:
            r = await client.post(
                config.SELECT_URL,
                data=data,
                headers={"Referer": config.SEARCH_PAGE_URL},
            )
        except httpx.HTTPError as e:
            raise RuntimeError(f"Impossibile raggiungere italgiure.giustizia.it: {e}") from e

    text = r.text
    if config.RATE_OVERFLOW_MARKER in text:
        if _attempt < config.MAX_RETRIES_ON_OVERFLOW:
            await asyncio.sleep(config.RETRY_BACKOFF * (_attempt + 1))
            return await fetch_solr(q, rows, _attempt + 1)
        raise RateOverflow(
            "Il sito ha temporaneamente rifiutato la richiesta per eccesso di "
            "frequenza (limitatore del servizio). Riprova tra qualche minuto."
        )

    try:
        parsed = r.json()
    except ValueError as e:
        raise RuntimeError(
            f"Risposta inattesa da italgiure.giustizia.it (non JSON): {text[:200]!r}"
        ) from e

    _cache[cache_key] = (now, parsed)
    return parsed


def _pdf_url(doc: dict) -> Optional[str]:
    filename = doc.get("filename")
    kind = doc.get("kind")
    if not filename or not kind:
        return None
    # Il campo "filename" può essere una lista di percorsi (es. originale +
    # versione OCR "pulita"): preferiamo quello con ".clean.pdf" se presente,
    # altrimenti il primo disponibile.
    if isinstance(filename, list):
        if not filename:
            return None
        path = next((p for p in filename if "clean" in p), filename[0])
    else:
        path = filename
    return config.PDF_URL_TEMPLATE.format(db=kind, path=path)


def _format_doc(doc: dict, highlighting: dict) -> dict:
    doc_id = doc.get("id", "")
    snippets = highlighting.get(doc_id, {}).get("ocr", [])
    out = {
        "numero_decisione": doc.get("numdec"),
        "numero_deposito": doc.get("numdep"),
        "data_deposito": doc.get("datdep"),
        "data_decisione": doc.get("datdec"),
        "anno": doc.get("anno"),
        "tipo": "civile" if doc.get("kind") == "snciv" else (
            "penale" if doc.get("kind") == "snpen" else doc.get("kind")
        ),
        "sezione": doc.get("ssz"),
        "tipo_provvedimento": doc.get("tipoprov"),
        "presidente": doc.get("presidente"),
        "relatore": doc.get("relatore"),
        "ecli": doc.get("ecli"),
        "estratto": snippets,
        "url_pdf": _pdf_url(doc),
    }
    return {k: v for k, v in out.items() if v not in (None, "", [])}


async def _search(query: str, tipo: str, sezione: Optional[str],
                   anno_da: Optional[int], anno_a: Optional[int], n: int) -> dict:
    q = build_query(query, tipo, sezione, anno_da, anno_a)
    try:
        parsed = await fetch_solr(q, n)
    except RateOverflow as e:
        return {"errore": str(e)}
    except RuntimeError as e:
        return {"errore": str(e)}
    except Exception as e:
        detail = f" [tls_fix: {_tls_fix_error}]" if _tls_fix_error else ""
        return {"errore": f"{type(e).__name__}: {e}{detail}"}

    response = parsed.get("response", {})
    highlighting = parsed.get("highlighting", {})
    docs = response.get("docs", [])
    return {
        "totale_trovati": response.get("numFound", len(docs)),
        "risultati": [_format_doc(d, highlighting) for d in docs],
        "nota": "Il testo integrale è nel PDF collegato; l'estratto è solo un'anteprima.",
    }


@mcp.tool()
async def diagnostica_tls() -> dict:
    """Strumento di servizio: verifica se il problema di certificato TLS del
    sito è stato risolto e, in caso contrario, spiega perché. Da usare solo
    per debug del connettore, non per ricerche vere."""
    try:
        info = await tls_fix.diagnose(_HOSTNAME)
    except Exception as e:
        return {"errore_diagnostica": f"{type(e).__name__}: {e}"}
    return info


@mcp.tool()
async def cerca_cassazione(
    query: str,
    tipo: str = "entrambi",
    sezione: Optional[str] = None,
    anno_da: Optional[int] = None,
    anno_a: Optional[int] = None,
    n: int = 10,
) -> dict:
    """Cerca sentenze/ordinanze della Corte di Cassazione (dal 2012) per
    testo libero, su SentenzeWeb (italgiure.giustizia.it/sncass), gratuito e
    senza login.

    Args:
        query: testo da cercare (es. "licenziamento illegittimo"). Più
            parole vengono cercate come prossimità, non serve mettere le
            virgolette.
        tipo: "civile", "penale" o "entrambi" (default "entrambi").
        sezione: filtro opzionale per sezione (best-effort, es. "L" per
            Lavoro), se noto.
        anno_da: anno minimo della decisione (opzionale).
        anno_a: anno massimo della decisione (opzionale).
        n: numero massimo di risultati (default 10, max 20).
    """
    return await _search(query, tipo, sezione, anno_da, anno_a, n)


@mcp.tool()
async def ultime_cassazione(
    tipo: str = "entrambi",
    sezione: Optional[str] = None,
    n: int = 10,
) -> dict:
    """Restituisce gli ultimi provvedimenti pubblicati dalla Corte di
    Cassazione su SentenzeWeb, senza una query testuale specifica.

    Args:
        tipo: "civile", "penale" o "entrambi" (default "entrambi").
        sezione: filtro opzionale per sezione, se noto.
        n: numero massimo di risultati (default 10, max 20).
    """
    return await _search("", tipo, sezione, None, None, n)


async def _probe(query: str) -> None:
    res = await _search(query, "entrambi", None, None, None, 5)
    import json
    print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--probe", help="Esegue una ricerca di prova da riga di comando")
    args = parser.parse_args()

    if args.probe:
        asyncio.run(_probe(args.probe))
        sys.exit(0)

    mcp.run(transport=os.getenv("MCP_TRANSPORT", "streamable-http"))
