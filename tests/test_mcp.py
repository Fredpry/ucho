# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
"""Serwer MCP na sztucznym archiwum: protokol, narzedzia i bezpieczenstwo id."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from ucho_mcp.serwer import Archiwum, obsluz

KORZEN = Path(__file__).resolve().parent.parent


def _sesja(katalog: Path, nazwa: str, data: str, segmenty: list[tuple]) -> None:
    sesja = katalog / nazwa
    sesja.mkdir(parents=True)
    meta = {
        "nazwa": nazwa.split("_")[2],
        "data": data,
        "trwanie_s": 600,
        "segmenty": [{"start": s, "koniec": s + 5, "tekst": t, "mowca": m}
                     for s, m, t in segmenty],
    }
    (sesja / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    (sesja / "transkrypt.md").write_text("# transkrypt", encoding="utf-8")


@pytest.fixture
def archiwum(tmp_path: Path) -> Archiwum:
    _sesja(tmp_path, "2026-11-10_10-00_Budzet", "2026-11-10 10:00", [
        (5, "Ja", "Zaczynamy od budżetu na czwarty kwartał."),
        (130, "Spotkanie", "Dostawca stali podniósł cenę o dziesięć procent."),
    ])
    _sesja(tmp_path, "2026-11-12_14-30_Rekrutacja", "2026-11-12 14:30", [
        (10, "Spotkanie", "Mamy trzech kandydatów na stanowisko."),
    ])
    (tmp_path / "2026-11-12_14-30_Rekrutacja" / "podsumowanie.md").write_text(
        "# Podsumowanie\nTrzech kandydatów.", encoding="utf-8")
    return Archiwum(tmp_path)


def _wywolaj(archiwum, nazwa, **argumenty):
    odpowiedz = obsluz(archiwum, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                  "params": {"name": nazwa, "arguments": argumenty}})
    wynik = odpowiedz["result"]
    return wynik["content"][0]["text"], wynik["isError"]


def test_initialize_i_lista_narzedzi(archiwum):
    init = obsluz(archiwum, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                             "params": {"protocolVersion": "2025-06-18"}})
    assert init["result"]["protocolVersion"] == "2025-06-18"
    assert init["result"]["serverInfo"]["name"] == "ucho"
    narzedzia = obsluz(archiwum, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    nazwy = {n["name"] for n in narzedzia["result"]["tools"]}
    assert nazwy == {"list_meetings", "get_transcript", "get_summary", "search_transcripts"}
    assert obsluz(archiwum, {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_list_meetings_od_najnowszych_i_filtr_dat(archiwum):
    tekst, blad = _wywolaj(archiwum, "list_meetings")
    assert not blad
    lista = json.loads(tekst)
    assert [s["id"] for s in lista] == ["2026-11-12_14-30_Rekrutacja", "2026-11-10_10-00_Budzet"]
    tekst, _ = _wywolaj(archiwum, "list_meetings", od="2026-11-11")
    assert [s["id"] for s in json.loads(tekst)] == ["2026-11-12_14-30_Rekrutacja"]


def test_get_transcript_z_zakresem_minut(archiwum):
    tekst, blad = _wywolaj(archiwum, "get_transcript", id="2026-11-10_10-00_Budzet", od_min=2)
    assert not blad
    assert "Dostawca stali" in tekst
    assert "budżetu" not in tekst


def test_get_summary(archiwum):
    tekst, _ = _wywolaj(archiwum, "get_summary", id="2026-11-12_14-30_Rekrutacja")
    assert "Trzech kandydatów" in tekst
    tekst, blad = _wywolaj(archiwum, "get_summary", id="2026-11-10_10-00_Budzet")
    assert not blad and "nie ma podsumowania" in tekst


def test_wyszukiwanie_bez_polskich_znakow_i_po_poczatku_slowa(archiwum):
    tekst, blad = _wywolaj(archiwum, "search_transcripts", fraza="budzet")
    assert not blad
    trafienia = json.loads(tekst)
    assert trafienia[0]["id"] == "2026-11-10_10-00_Budzet"
    assert trafienia[0]["czas"] == "00:00:05"
    tekst, _ = _wywolaj(archiwum, "search_transcripts", fraza="kandyd")
    assert json.loads(tekst)[0]["id"] == "2026-11-12_14-30_Rekrutacja"


@pytest.mark.parametrize("zle_id", ["..", "../poza", "a/b", "C:\\Windows", "nie-ma-takiego"])
def test_id_nie_wychodzi_poza_archiwum(archiwum, zle_id):
    tekst, blad = _wywolaj(archiwum, "get_transcript", id=zle_id)
    assert blad


def test_serwer_przez_stdio(archiwum):
    wejscie = "\n".join(json.dumps(w) for w in [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "search_transcripts", "arguments": {"fraza": "stali"}}},
    ]) + "\n"
    proces = subprocess.run(
        [sys.executable, str(KORZEN / "mcp_serwer.py"), "--katalog", str(archiwum.katalog)],
        input=wejscie.encode("utf-8"), capture_output=True, timeout=30, check=True)
    linie = [json.loads(linia) for linia in proces.stdout.decode("utf-8").splitlines()]
    assert [o["id"] for o in linie] == [1, 2]
    assert "Dostawca stali" in linie[1]["result"]["content"][0]["text"]
