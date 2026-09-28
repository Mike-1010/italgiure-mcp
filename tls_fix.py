"""Recupero automatico dei certificati intermedi mancanti.

Alcuni server della PA italiana (questo incluso) non inviano, durante
l'handshake TLS, la catena completa dei certificati: mandano solo il
certificato "foglia". I browser tollerano questo perché recuperano da soli
il certificato intermedio mancante seguendo l'estensione AIA (Authority
Information Access) del certificato — è un comportamento invisibile
all'utente. Le librerie Python (incluso httpx) non lo fanno, e quindi
rifiutano la connessione con CERTIFICATE_VERIFY_FAILED anche se il
certificato è in realtà valido.

Questo modulo replica lo stesso comportamento del browser: scarica il/i
certificato/i intermedio/i mancante/i e li aggiunge, insieme alle CA note
(certifi), a un SSLContext costruito ad hoc. Espone anche `diagnose()` per
capire, passo per passo, cosa succede quando qualcosa non funziona.
"""
import asyncio
import socket
import ssl
from typing import List, Tuple

import certifi
import httpx
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.x509.oid import AuthorityInformationAccessOID, ExtensionOID


def _get_leaf_cert_pem(hostname: str, port: int) -> str:
    # Connessione TLS non verificata, usata solo per LEGGERE il certificato
    # che il server presenta (non per fidarsi di esso): è il primo passo
    # necessario per capire quale catena gli manca.
    return ssl.get_server_certificate((hostname, port))


def _cert_from_pem(pem: str) -> x509.Certificate:
    return x509.load_pem_x509_certificate(pem.encode())


def _issuer_urls(cert: x509.Certificate) -> List[str]:
    try:
        ext = cert.extensions.get_extension_for_oid(ExtensionOID.AUTHORITY_INFORMATION_ACCESS)
    except x509.ExtensionNotFound:
        return []
    return [
        d.access_location.value
        for d in ext.value
        if d.access_method == AuthorityInformationAccessOID.CA_ISSUERS
    ]


async def _fetch_cert_pem(url: str) -> str:
    async with httpx.AsyncClient(verify=False, timeout=15, follow_redirects=True) as c:
        r = await c.get(url)
        r.raise_for_status()
        data = r.content
    try:
        cert = x509.load_der_x509_certificate(data)
    except ValueError:
        cert = x509.load_pem_x509_certificate(data)
    return cert.public_bytes(serialization.Encoding.PEM).decode()


def _name(n: x509.Name) -> str:
    try:
        return n.rfc4514_string()
    except Exception:
        return str(n)


async def _collect_chain(hostname: str, port: int, max_hops: int, log: list) -> List[str]:
    extra_pems: List[str] = []
    leaf_pem = await asyncio.to_thread(_get_leaf_cert_pem, hostname, port)
    cert = _cert_from_pem(leaf_pem)
    log.append({"passo": "certificato foglia", "subject": _name(cert.subject), "issuer": _name(cert.issuer)})

    for i in range(max_hops):
        urls = _issuer_urls(cert)
        if not urls:
            log.append({"passo": f"hop {i+1}", "esito": "nessun URL AIA (CA Issuers) trovato nel certificato, mi fermo qui"})
            break
        log.append({"passo": f"hop {i+1}", "url_aia": urls[0]})
        try:
            pem = await _fetch_cert_pem(urls[0])
        except Exception as e:
            log.append({"passo": f"hop {i+1}", "esito": f"scaricamento fallito: {type(e).__name__}: {e}"})
            break
        extra_pems.append(pem)
        cert = _cert_from_pem(pem)
        log.append({"passo": f"hop {i+1}", "scaricato": _name(cert.subject), "issuer": _name(cert.issuer)})
        if cert.issuer == cert.subject:  # root autofirmata: fine catena
            log.append({"passo": f"hop {i+1}", "esito": "root autofirmata raggiunta"})
            break

    return extra_pems


async def build_ssl_context(hostname: str, port: int = 443, max_hops: int = 3) -> ssl.SSLContext:
    ctx, _hops = await build_ssl_context_diag(hostname, port, max_hops)
    return ctx


async def build_ssl_context_diag(
    hostname: str, port: int = 443, max_hops: int = 3
) -> Tuple[ssl.SSLContext, int]:
    """Come build_ssl_context, ma restituisce anche il numero di certificati
    intermedi recuperati (utile per diagnostica)."""
    log: list = []
    extra_pems = await _collect_chain(hostname, port, max_hops, log)

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_verify_locations(cafile=certifi.where())
    if extra_pems:
        ctx.load_verify_locations(cadata="\n".join(extra_pems))
    return ctx, len(extra_pems)


async def diagnose(hostname: str, port: int = 443, max_hops: int = 3) -> dict:
    """Ricostruisce la catena passo-passo e prova a stabilire una vera
    connessione TLS verificata, riportando ogni fase per capire dove (e se)
    qualcosa fallisce."""
    log: list = []
    result: dict = {"host": hostname, "porta": port, "passi": log}

    try:
        extra_pems = await _collect_chain(hostname, port, max_hops, log)
    except Exception as e:
        result["errore_raccolta_catena"] = f"{type(e).__name__}: {e}"
        return result

    result["certificati_intermedi_recuperati"] = len(extra_pems)

    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.load_verify_locations(cafile=certifi.where())
        if extra_pems:
            ctx.load_verify_locations(cadata="\n".join(extra_pems))
    except Exception as e:
        result["errore_costruzione_contesto"] = f"{type(e).__name__}: {e}"
        return result

    # Prova reale: una connessione TLS verificata con il contesto costruito.
    def _try_connect() -> str:
        with socket.create_connection((hostname, port), timeout=15) as sock:
            with ctx.wrap_socket(sock, server_hostname=hostname):
                return "connessione TLS verificata riuscita"

    try:
        esito = await asyncio.to_thread(_try_connect)
        result["verifica_connessione"] = esito
        result["esito"] = "OK: la catena ricostruita è sufficiente a verificare il certificato"
    except Exception as e:
        result["verifica_connessione"] = f"{type(e).__name__}: {e}"
        result["esito"] = "FALLITO: anche con i certificati intermedi recuperati la verifica non passa"

    return result
