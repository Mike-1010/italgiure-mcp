# Connettore MCP per SentenzeWeb (Corte di Cassazione)

Strumenti esposti a Claude: `cerca_cassazione`, `ultime_cassazione`.

Interroga in tempo reale il motore di ricerca gratuito della Corte di
Cassazione (https://www.italgiure.giustizia.it/sncass/): sentenze e
ordinanze civili e penali dal 2012 a oggi. **Nessun login richiesto** per
questa sezione (diversa dal portale ItalgiureWeb completo, che è a
pagamento/riservato agli iscritti Cassa Forense — non trattato qui).

## Come funziona
Il sito espone un motore Apache Solr dietro un gateway ISAPI
(`/sncass/isapi/hc.dll/sn.solr/sn-collection/select`): il connettore
interroga direttamente quell'endpoint e riceve JSON — non fa scraping HTML.

**Attenzione**: il sito ha un limitatore di frequenza lato server molto
severo (durante l'ispezione ha risposto "rate overflow" dopo pochissime
richieste ravvicinate). Il connettore per questo:
- aspetta almeno 4 secondi tra una richiesta e l'altra;
- tiene una cache di 30 minuti sui risultati identici;
- se il sito rifiuta comunque, aspetta e ritenta automaticamente (max 2
  volte) prima di segnalare l'errore.

Se usi il connettore molto intensamente potresti comunque incontrare rifiuti
temporanei: è il sito stesso a proteggersi, non un bug.

## 1. Installazione
    python -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt

Imposta un contatto reale in `USER_AGENT` (config.py).

## 2. Prova locale
    python server.py --probe "licenziamento illegittimo"

Se vedi risultati con numero, sezione e link al PDF, è pronto.

**Nota importante**: se provi questo comando in un ambiente con accesso a
internet ristretto (es. sandbox di sviluppo con proxy/allowlist), la
richiesta potrebbe fallire per motivi di rete che non hanno nulla a che fare
con il sito. Il test che conta è quello fatto dopo il deploy (punto 4).

## 3. Collegamento a Claude
**Remoto (web, mobile, desktop):** pubblica su un host HTTPS (Render,
Fly.io, Cloud Run...) con comando `python server.py` e variabili `PORT` e
`MCP_PATH=/un-percorso-segreto/mcp` (il server è altrimenti pubblico, senza
login: il percorso segreto è l'unico controllo d'accesso).
Poi: Impostazioni -> Connettori -> Aggiungi connettore personalizzato ->
URL `https://tuo-host/un-percorso-segreto/mcp`.

**Locale (solo Claude Desktop):** in `claude_desktop_config.json`:

    {"mcpServers": {"italgiure": {
      "command": "/percorso/.venv/bin/python",
      "args": ["/percorso/server.py"],
      "env": {"MCP_TRANSPORT": "stdio"}}}}

## 4. Verifica dopo il deploy (il test che conta davvero)
Appena il servizio è online su Render, prova subito una singola ricerca da
Claude (una sola, per non innescare il rate-limiter). Se funziona, il
servizio è raggiungibile da lì. Se il sito rifiuta sistematicamente ogni
richiesta (non solo "rate overflow" occasionale, ma sempre), potrebbe
esserci un blocco specifico sull'intervallo IP del provider cloud scelto:
in quel caso valuteremo un provider diverso o un'altra strategia.

## Note d'uso
- Restituisce estremi del provvedimento ed estratto con evidenziazione; il
  testo integrale va sempre letto nel PDF collegato (versione OCR "pulita").
- Il filtro per sezione (`sezione`) è best-effort: il campo esatto usato dal
  sito per le sezioni non è documentato pubblicamente, verificalo con
  qualche ricerca di prova.
- Rispetta la sensibilità del sito alla frequenza delle richieste: evita
  script che lo interrogano in loop.
