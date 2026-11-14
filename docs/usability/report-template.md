# Raport z testów użyteczności Ucho: runda [1 albo 2]

> **English summary:** report template (in Polish) for an Ucho usability round: participant overview, per-task metrics, SUS summary, findings table with Nielsen severity 0 to 4, frequency and recommendation, and the A vs B decision for round 2.

- **Wersja aplikacji:** [np. v0.1.0, commit]
- **Terminy sesji:** [od kiedy do kiedy]
- **Moderator / osoba notująca:** [inicjały]
- **Uczestnicy:** [liczba], kody U1 do U6 (bez imion)
- **Wariant (runda 2):** kolejność A/B według `protocol.md`

## 1. Najważniejsze wnioski

Trzy do pięciu zdań dla kogoś, kto przeczyta tylko tę sekcję: co działa, co trzeba poprawić przed wydaniem, jaka decyzja wynika z rundy.

1. [...]
2. [...]
3. [...]

## 2. Uczestnicy

| Kod | Profil | Windows | Sprzęt (CPU, GPU, RAM) | Uwagi |
|---|---|---|---|---|
| U1 | zespół | | | |
| U2 | zespół | | | |
| U3 | spoza IT | | | |
| U4 | spoza IT | | | |
| U5 | programista | | | |
| U6 | programista | | | |

## 3. Miary zadań

Sukces: **T** tak, **C** częściowo, **N** nie. Czas w minutach i sekundach. Błędy: liczba działań, które uczestnik musiał cofnąć.

W komórce: sukces, czas, liczba błędów (np. „C, 4:20, 2”).

| Zadanie | U1 | U2 | U3 | U4 | U5 | U6 | Sukces (T/C/N) | Mediana czasu | Średnio błędów |
|---|---|---|---|---|---|---|---|---|---|
| 1. Instalacja | | | | | | | | | |
| 2. Nagranie | | | | | | | | | |
| 3. Transkrypt i fragment | | | | | | | | | |
| 4. Dyktowanie maila | | | | | | | | | |
| 5. Język interfejsu | | | | | | | | | |
| 6. MCP (U5, U6) | | | | | | | | | |

Czas pobierania modelu w zadaniu 1 (nie wlicza się do limitu): [...].

## 4. SUS

| Kod | Wynik SUS | Brakujące odpowiedzi |
|---|---|---|
| U1 | | |
| U2 | | |
| U3 | | |
| U4 | | |
| U5 | | |
| U6 | | |
| **Średnia** | | |
| Najniższy / najwyższy | | |

Cel: średnio **75 lub więcej**. Runda 1 jest punktem odniesienia dla rundy 2. W rundzie 2 wpisujemy dwie kolumny wyników (wariant A i wariant B) oraz różnicę dla każdej osoby.

### Co było najtrudniejsze (pytanie otwarte)

| Kod | Odpowiedź (dosłownie) |
|---|---|
| U1 | |

## 5. Problemy

Waga według skali Nielsena:

| Waga | Znaczenie |
|---|---|
| 0 | to nie jest problem użyteczności |
| 1 | kosmetyczny, poprawić, jeśli zostanie czas |
| 2 | drobny, niski priorytet |
| 3 | poważny, wysoki priorytet, poprawić przed wydaniem |
| 4 | katastrofa, uniemożliwia wykonanie zadania, poprawić koniecznie |

Częstość: ilu uczestników trafiło na problem (np. 3/5). Przy rozbieżnych ocenach wagi rozstrzyga moderator po odsłuchaniu nagrania.

| Nr | Problem (co się stało) | Waga 0 do 4 | Częstość | Zadanie | Dowód (kody, cytat, minuta nagrania) | Zalecenie | Zgłoszenie |
|---|---|---|---|---|---|---|---|
| P1 | *Przykład:* uczestnik zamyka okno krzyżykiem i myśli, że wyłączył aplikację, a Ucho działa dalej w zasobniku, więc nowy język interfejsu się nie włącza. | 3 | 3/5 | 5 | U1, U3, U4: „Zamknąłem, a dalej jest po polsku” | Przy zmianie języka zaproponować ponowne uruchomienie jednym przyciskiem. | #… |
| P2 | | | | | | | |

Kolejność w tabeli: od najwyższej wagi, przy tej samej wadze od najczęstszych.

## 6. Co działało dobrze

Krótka lista rzeczy, których nie należy psuć przy poprawkach.

- [...]

## 7. Porównanie wariantów (tylko runda 2)

| Miara | Wariant A (jedno okno) | Wariant B (zasobnik i osobne okna) |
|---|---|---|
| Problemy o wadze 3 i 4 | | |
| Średni SUS | | |
| Sukcesy w zadaniach 2 do 4 (T/C/N) | | |
| Mediana czasu zadań 2 do 4 | | |
| Preferencja uczestników | | |

Zasada rozstrzygnięcia (z `protocol.md`): mniej problemów o wadze 3 i 4, przy remisie wyższy SUS, potem preferencja.

**Decyzja:** domyślny wariant [A albo B], bo [...].

## 8. Dalsze kroki

| Co | Kto | Do kiedy |
|---|---|---|
| | | |
