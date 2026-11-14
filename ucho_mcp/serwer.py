# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Serwer MCP (stdio) udostepniajacy archiwum nagran Ucho agentom AI.

Celowo bez zewnetrznych bibliotek: tylko biblioteka standardowa Pythona,
tylko odczyt, tylko stdio. Serwer nie otwiera portow, nie laczy sie z siecia
i nie zmienia plikow - czyta `meta.json`, `transkrypt.md` i `podsumowanie.md`
z katalogu nagran. Dzieki temu caly kod da sie przejrzec w kilka minut.

Protokol: JSON-RPC 2.0, jedna wiadomosc w linii (transport stdio MCP).
Na stdout trafiaja wylacznie odpowiedzi protokolu; logi ida na stderr.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

log = logging.getLogger("ucho_mcp")

WERSJE_PROTOKOLU = ("2025-06-18", "2025-03-26", "2024-11-05")
NAZWA = "ucho"
WERSJA = "0.1.0"

INSTRUKCJE = (
    "Archiwum lokalnych nagran spotkan i ich transkrypcji z aplikacji Ucho. "
    "Zacznij od list_meetings albo search_transcripts, potem pobierz get_transcript "
    "lub get_summary dla wybranego id. Transkrypty moga zawierac poufne rozmowy: "
    "cytuj tylko to, co potrzebne do odpowiedzi. / Archive of local meeting "
    "recordings and transcripts from the Ucho app."
)

NARZEDZIA = [
    {
        "name": "list_meetings",
        "description": (
            "Lista nagranych spotkan, od najnowszych. Filtry opcjonalne: zakres dat "
            "(RRRR-MM-DD) i fraza w nazwie spotkania. / List recorded meetings, newest first."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "od": {"type": "string", "description": "Data od (RRRR-MM-DD), wlacznie."},
                "do": {"type": "string", "description": "Data do (RRRR-MM-DD), wlacznie."},
                "fraza": {"type": "string", "description": "Fragment nazwy spotkania."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 50},
            },
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "get_transcript",
        "description": (
            "Transkrypt spotkania z czasem i mowca ('Ja' = mikrofon, 'Spotkanie' = "
            "dzwiek systemowy). Opcjonalnie tylko zakres minut. / Meeting transcript."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "id z list_meetings (nazwa folderu)."},
                "od_min": {"type": "number", "description": "Od ktorej minuty."},
                "do_min": {"type": "number", "description": "Do ktorej minuty."},
            },
            "required": ["id"],
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "get_summary",
        "description": (
            "Podsumowanie spotkania (podsumowanie.md), jesli zostalo wygenerowane. "
            "Gdy go nie ma, pobierz transkrypt i podsumuj sam. / Meeting summary, if any."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "search_transcripts",
        "description": (
            "Wyszukiwanie pelnotekstowe we wszystkich transkryptach. Zwraca spotkanie, "
            "czas, mowce i fragment wypowiedzi. / Full-text search across transcripts."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "fraza": {"type": "string", "description": "Slowa do znalezienia."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 20},
            },
            "required": ["fraza"],
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
]


class BladNarzedzia(Exception):
    """Blad po stronie wywolania narzedzia - wraca do agenta jako isError."""


# --- archiwum ---------------------------------------------------------------


def _czas(sekundy: float) -> str:
    sekundy = max(0, int(sekundy))
    g, reszta = divmod(sekundy, 3600)
    m, s = divmod(reszta, 60)
    return f"{g:02d}:{m:02d}:{s:02d}"


def _data(tekst: str | None) -> datetime | None:
    if not tekst:
        return None
    for format_ in ("%Y-%m-%d", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(tekst.strip(), format_)
        except ValueError:
            continue
    return None


class Archiwum:
    def __init__(self, katalog: Path):
        self.katalog = katalog
        self._indeks: sqlite3.Connection | None = None
        self._stan_indeksu: tuple | None = None

    # Bezpieczenstwo: id to wylacznie nazwa folderu bezposrednio w archiwum.
    def sesja(self, id_: str) -> Path:
        if not isinstance(id_, str) or not id_ or re.search(r"[\\/:]|\.\.", id_):
            raise BladNarzedzia(f"Niepoprawne id: {id_!r}")
        sciezka = self.katalog / id_
        if not sciezka.is_dir():
            raise BladNarzedzia(f"Nie ma spotkania o id {id_!r}. Uzyj list_meetings.")
        return sciezka

    def sesje(self) -> list[Path]:
        if not self.katalog.is_dir():
            return []
        return sorted((s for s in self.katalog.iterdir() if s.is_dir()), reverse=True)

    @staticmethod
    def meta(sesja: Path) -> dict:
        try:
            dane = json.loads((sesja / "meta.json").read_text(encoding="utf-8"))
            return dane if isinstance(dane, dict) else {}
        except (OSError, ValueError):
            return {}

    def opis(self, sesja: Path) -> dict:
        meta = self.meta(sesja)
        data = meta.get("data") or ""
        if not data:
            czesci = sesja.name.split("_")
            if len(czesci) >= 2:
                data = f"{czesci[0]} {czesci[1].replace('-', ':')}"
        opis = {
            "id": sesja.name,
            "tytul": meta.get("temat") or meta.get("nazwa") or sesja.name,
            "data": data,
            "czas_trwania": _czas(meta.get("trwanie_s", 0)) if meta.get("trwanie_s") else None,
            "transkrypt": (sesja / "transkrypt.md").exists(),
            "podsumowanie": (sesja / "podsumowanie.md").exists(),
        }
        if meta.get("uczestnicy"):
            opis["uczestnicy"] = meta["uczestnicy"]
        if meta.get("tldr"):
            opis["w_skrocie"] = meta["tldr"]
        return opis

    def segmenty(self, sesja: Path) -> list[dict]:
        segmenty = self.meta(sesja).get("segmenty") or []
        return [s for s in segmenty if isinstance(s, dict) and s.get("tekst")]

    # --- indeks pelnotekstowy --------------------------------------------

    def _stan(self) -> tuple:
        stan = []
        for sesja in self.sesje():
            plik = sesja / "meta.json"
            try:
                stan.append((sesja.name, plik.stat().st_mtime_ns))
            except OSError:
                continue
        return tuple(stan)

    def indeks(self) -> sqlite3.Connection:
        """Indeks FTS5 w pamieci, przebudowywany, gdy zmieni sie archiwum."""
        stan = self._stan()
        if self._indeks is not None and stan == self._stan_indeksu:
            return self._indeks
        baza = sqlite3.connect(":memory:")
        try:
            baza.execute("CREATE VIRTUAL TABLE w USING fts5(sesja UNINDEXED, start UNINDEXED,"
                         " mowca UNINDEXED, tekst, tokenize='unicode61 remove_diacritics 2')")
            self.fts = True
        except sqlite3.OperationalError:
            # SQLite bez FTS5 - zwykla tabela i wyszukiwanie LIKE.
            baza.execute("CREATE TABLE w (sesja, start, mowca, tekst)")
            self.fts = False
        for nazwa, _ in stan:
            for seg in self.segmenty(self.katalog / nazwa):
                baza.execute("INSERT INTO w VALUES (?, ?, ?, ?)",
                             (nazwa, float(seg.get("start", 0)), seg.get("mowca", ""),
                              str(seg["tekst"])))
        baza.commit()
        if self._indeks is not None:
            self._indeks.close()
        self._indeks, self._stan_indeksu = baza, stan
        return baza

    def szukaj(self, fraza: str, limit: int) -> list[dict]:
        baza = self.indeks()
        slowa = re.findall(r"\w+", fraza, flags=re.UNICODE)
        if not slowa:
            raise BladNarzedzia("Pusta fraza.")
        if self.fts:
            zapytanie = " ".join(f'"{s}"*' for s in slowa)
            wiersze = baza.execute(
                "SELECT sesja, start, mowca, tekst FROM w WHERE w MATCH ? "
                "ORDER BY rank LIMIT ?", (zapytanie, limit)).fetchall()
        else:
            warunek = " AND ".join("tekst LIKE ?" for _ in slowa)
            wiersze = baza.execute(
                f"SELECT sesja, start, mowca, tekst FROM w WHERE {warunek} LIMIT ?",
                [f"%{s}%" for s in slowa] + [limit]).fetchall()
        return [{"id": s, "czas": _czas(st), "mowca": m, "fragment": t[:400]}
                for s, st, m, t in wiersze]


# --- narzedzia --------------------------------------------------------------


def _liczba(wartosc, nazwa: str, domyslna: int, maks: int) -> int:
    if wartosc is None:
        return domyslna
    try:
        return max(1, min(maks, int(wartosc)))
    except (TypeError, ValueError):
        raise BladNarzedzia(f"{nazwa} musi byc liczba") from None


def wywolaj(archiwum: Archiwum, nazwa: str, arg: dict) -> str:
    if nazwa == "list_meetings":
        od, do = _data(arg.get("od")), _data(arg.get("do"))
        fraza = (arg.get("fraza") or "").lower()
        limit = _liczba(arg.get("limit"), "limit", 50, 500)
        wynik = []
        for sesja in archiwum.sesje():
            opis = archiwum.opis(sesja)
            data = _data(opis["data"][:10]) if opis["data"] else None
            if od and (data is None or data < od):
                continue
            if do and (data is None or data > do):
                continue
            if fraza and fraza not in f"{opis['tytul']} {sesja.name}".lower():
                continue
            wynik.append(opis)
            if len(wynik) >= limit:
                break
        if not wynik:
            return f"Brak spotkan spelniajacych kryteria w {archiwum.katalog}."
        return json.dumps(wynik, ensure_ascii=False, indent=1)

    if nazwa == "get_transcript":
        sesja = archiwum.sesja(arg.get("id"))
        od = float(arg["od_min"]) * 60 if arg.get("od_min") is not None else None
        do = float(arg["do_min"]) * 60 if arg.get("do_min") is not None else None
        segmenty = archiwum.segmenty(sesja)
        if not segmenty:
            plik = sesja / "transkrypt.md"
            if plik.exists() and od is None and do is None:
                return plik.read_text(encoding="utf-8")
            return "To spotkanie nie ma jeszcze transkryptu."
        opis = archiwum.opis(sesja)
        linie = [f"# {opis['tytul']} ({opis['data']})", ""]
        for seg in segmenty:
            start = float(seg.get("start", 0))
            if (od is not None and start < od) or (do is not None and start > do):
                continue
            linie.append(f"[{_czas(start)}] {seg.get('mowca', '')}: {seg['tekst']}")
        return "\n".join(linie)

    if nazwa == "get_summary":
        sesja = archiwum.sesja(arg.get("id"))
        plik = sesja / "podsumowanie.md"
        if plik.exists():
            return plik.read_text(encoding="utf-8")
        return ("To spotkanie nie ma podsumowania (podsumowania w Ucho sa opcjonalne). "
                "Pobierz get_transcript i podsumuj sam.")

    if nazwa == "search_transcripts":
        fraza = arg.get("fraza")
        if not isinstance(fraza, str):
            raise BladNarzedzia("Podaj fraze.")
        trafienia = archiwum.szukaj(fraza, _liczba(arg.get("limit"), "limit", 20, 200))
        if not trafienia:
            return f"Nie znaleziono: {fraza!r}"
        return json.dumps(trafienia, ensure_ascii=False, indent=1)

    raise BladNarzedzia(f"Nieznane narzedzie: {nazwa}")


# --- protokol ---------------------------------------------------------------


def obsluz(archiwum: Archiwum, wiadomosc: dict) -> dict | None:
    """Jedna wiadomosc JSON-RPC -> odpowiedz (albo None dla powiadomien)."""
    metoda = wiadomosc.get("method")
    id_ = wiadomosc.get("id")
    parametry = wiadomosc.get("params") or {}

    if id_ is None:
        return None  # powiadomienie, np. notifications/initialized

    def wynik(dane: dict) -> dict:
        return {"jsonrpc": "2.0", "id": id_, "result": dane}

    def blad(kod: int, tresc: str) -> dict:
        return {"jsonrpc": "2.0", "id": id_, "error": {"code": kod, "message": tresc}}

    if metoda == "initialize":
        prosba = parametry.get("protocolVersion")
        wersja = prosba if prosba in WERSJE_PROTOKOLU else WERSJE_PROTOKOLU[0]
        return wynik({
            "protocolVersion": wersja,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": NAZWA, "version": WERSJA},
            "instructions": INSTRUKCJE,
        })
    if metoda == "ping":
        return wynik({})
    if metoda == "tools/list":
        return wynik({"tools": NARZEDZIA})
    if metoda == "tools/call":
        nazwa = parametry.get("name")
        argumenty = parametry.get("arguments") or {}
        try:
            tekst = wywolaj(archiwum, nazwa, argumenty)
            return wynik({"content": [{"type": "text", "text": tekst}], "isError": False})
        except BladNarzedzia as problem:
            return wynik({"content": [{"type": "text", "text": str(problem)}], "isError": True})
        except Exception as problem:  # noqa: BLE001 - agent ma dostac tresc bledu
            log.exception("Blad narzedzia %s", nazwa)
            return wynik({"content": [{"type": "text", "text": f"Blad: {problem}"}],
                          "isError": True})
    if metoda in ("resources/list", "prompts/list"):
        return wynik({metoda.split("/")[0]: []})
    return blad(-32601, f"Metoda nieobslugiwana: {metoda}")


def katalog_nagran(argv: list[str]) -> Path:
    """--katalog <sciezka> > zmienna UCHO_NAGRANIA > ustawienia.json > domyslny."""
    import os

    if "--katalog" in argv:
        i = argv.index("--katalog")
        if i + 1 < len(argv):
            return Path(argv[i + 1]).expanduser()
    if os.environ.get("UCHO_NAGRANIA"):
        return Path(os.environ["UCHO_NAGRANIA"]).expanduser()
    from ucho import config

    return config.Ustawienia().katalog_nagran


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    archiwum = Archiwum(katalog_nagran(argv))
    log.info("Archiwum: %s", archiwum.katalog)

    wejscie = sys.stdin.buffer
    wyjscie = sys.stdout.buffer
    for linia in wejscie:
        linia = linia.strip()
        if not linia:
            continue
        try:
            wiadomosc = json.loads(linia.decode("utf-8"))
        except ValueError:
            odpowiedz = {"jsonrpc": "2.0", "id": None,
                         "error": {"code": -32700, "message": "Parse error"}}
        else:
            if isinstance(wiadomosc, list):  # paczka JSON-RPC
                odpowiedz = [o for o in (obsluz(archiwum, w) for w in wiadomosc) if o]
                odpowiedz = odpowiedz or None
            else:
                odpowiedz = obsluz(archiwum, wiadomosc)
        if odpowiedz is not None:
            wyjscie.write(json.dumps(odpowiedz, ensure_ascii=False).encode("utf-8") + b"\n")
            wyjscie.flush()
    return 0
