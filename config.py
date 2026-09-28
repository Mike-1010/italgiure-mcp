"""Configurazione del connettore SentenzeWeb (italgiure.giustizia.it/sncass).
Modifica qui, non in server.py."""
import os

BASE = "https://www.italgiure.giustizia.it"

# Pagina di ricerca: una GET qui stabilisce la sessione (cookie "cookiesession1")
# richiesta dalle chiamate successive all'endpoint Solr.
SEARCH_PAGE_URL = f"{BASE}/sncass/"

# Endpoint reale delle ricerche: un core Apache Solr esposto dietro un
# gateway IIS/ISAPI. Risponde in JSON (wt=json).
SELECT_URL = f"{BASE}/sncass/isapi/hc.dll/sn.solr/sn-collection/select?app.query"

# Pattern per il PDF "pulito" (OCR) di un provvedimento. {db} è "snciv"/"snpen"
# (dal campo "kind"), {path} è il valore del campo "filename" del documento.
PDF_URL_TEMPLATE = BASE + "/xway/application/nif/clean/hc.dll?verbo=attach&db={db}&id={path}"

# Identificati in modo onesto: metti un contatto reale.
USER_AGENT = os.getenv(
    "ITALGIURE_UA",
    "italgiure-mcp/0.1 (uso professionale personale; contatto: TUA_EMAIL)",
)

# Il sito ha un limitatore di frequenza lato server molto aggressivo
# ("NIF Arbiter Service" -> risponde "rate overflow." se disturbato troppo
# spesso). Restiamo molto più prudenti che con altri connettori.
MIN_INTERVAL = 4.0     # secondi minimi tra due richieste al sito
SESSION_TTL = 1800     # secondi di validità presunta della sessione (cookie)
CACHE_TTL = 1800       # secondi di cache in memoria dei risultati
TIMEOUT = 25
MAX_RETRIES_ON_OVERFLOW = 2   # tentativi aggiuntivi se il sito risponde "rate overflow"
RETRY_BACKOFF = 10.0          # secondi di attesa prima di ritentare

# Marcatore usato dal gateway per segnalare che ha bloccato la richiesta per
# eccesso di frequenza (risposta HTML, non JSON, anche se abbiamo chiesto wt=json).
RATE_OVERFLOW_MARKER = "rate overflow"

# Campi richiesti a Solr (fl). "ocr"/"testoocr" sono i campi testuali usati
# anche per gli estratti evidenziati (highlighting); il testo integrale resta
# comunque nel PDF collegato a ciascun provvedimento.
FL_FIELDS = (
    "id,filename,szdec,kind,ssz,tipoprov,numcard,numdec,numdep,datdep,"
    "ecli,anno,datdec,presidente,relatore,requisitoria,testoocr,ocr"
)

DEFAULT_ROWS = 10
MAX_ROWS = 20  # non esageriamo, vista la sensibilità del rate limiter

# "kind" possibili: snciv (Cassazione civile), snpen (Cassazione penale).
KIND_FILTERS = {
    "civile": 'kind:"snciv"',
    "penale": 'kind:"snpen"',
    "entrambi": '(kind:"snciv" OR kind:"snpen")',
}

HL_PARAMS = {
    "hl": "true",
    "hl.snippets": "4",
    "hl.fragsize": "180",
    "hl.fl": "ocr",
    "hl.maxAnalyzedChars": "1000000",
    "hl.simple.pre": '<em class="hit">',
    "hl.simple.post": "</em>",
}
