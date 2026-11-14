# Protokół testów użyteczności Ucho

> **English summary:** moderator's protocol for remote think-aloud usability tests of Ucho (5 or 6 Polish participants, Teams, 30 to 40 min, two rounds: v0.1 baseline, then variant A vs B). Content is in Polish because the participants are Polish.

Pliki w tym folderze:

| Plik | Do czego |
|---|---|
| `protocol.md` | ten dokument: cel, uczestnicy, przebieg sesji, obie rundy |
| `tasks.md` | sześć zadań z kryteriami sukcesu i limitem czasu |
| `sus.md` | ankieta SUS (10 stwierdzeń) i sposób liczenia wyniku |
| `consent.md` | zgoda na udział i nagranie, do wysłania przed sesją |
| `report-template.md` | szablon raportu z ustaleniami i miarami |

## 1. Cel

Chcemy sprawdzić, czy osoba, która pierwszy raz widzi Ucho, potrafi bez pomocy zainstalować aplikację, nagrać rozmowę, znaleźć w transkrypcie potrzebny fragment i podyktować tekst. Szukamy miejsc, w których ludzie się gubią, mylą albo rezygnują.

Konkretne pytania badawcze:

1. Czy instalacja z archiwum ZIP kończy się sukcesem bez naszej pomocy?
2. Czy uczestnik rozumie, co nagrywa Ucho (mikrofon i dźwięk systemowy) i gdzie potem szuka wyników?
3. Czy dwie osobne aplikacje (Ucho i Ucho Dyktuj w wersji 0.1) są zrozumiałe, czy mylą?
4. W rundzie 2: który układ okien jest łatwiejszy, wariant A (jedno okno z dwoma trybami) czy wariant B (ikona w zasobniku jako centrum, osobne okna)?

Wynik rundy 2 rozstrzyga, który wariant będzie domyślny w wersji 0.2.

## 2. Uczestnicy

Pięć lub sześć osób. Według reguły Nielsena pięć osób wykrywa około 85% problemów użyteczności, więc więcej osób w jednej rundzie niewiele daje. Lepiej zrobić dwie małe rundy niż jedną dużą.

| Kod | Profil | Liczba | Zadania |
|---|---|---|---|
| U1, U2 | osoby z zespołu, które nie budowały Ucho | 2 | 1 do 5 |
| U3, U4 | klienci albo znajomi spoza branży IT, na co dzień mają spotkania w Teams | 2 | 1 do 5 |
| U5 (U6) | programiści, którzy używają Claude Code albo innego klienta MCP | 1 lub 2 | 1 do 6 |

Warunki udziału:

- laptop z Windows 10 lub 11, na którym uczestnik może sam zainstalować program (bez uprawnień administratora wystarczy);
- słuchawki z mikrofonem (bez nich Ucho nagra rozmowę dwa razy, patrz README);
- Microsoft Teams, a przy zadaniu 4 program pocztowy Outlook (jeśli go nie ma, wystarczy Notatnik albo okno czatu Teams);
- przy zadaniu 6: zainstalowany Claude Code.

W raporcie i notatkach używamy tylko kodów U1 do U6. Imiona i nazwiska zna wyłącznie moderator, lista nie trafia do repozytorium.

**Do decyzji założyciela:** kto konkretnie bierze udział (patrz sekcja 8).

## 3. Role

- **Moderator** prowadzi rozmowę, czyta zadania, nie podpowiada.
- **Osoba notująca** (może być ta sama osoba, ale wtedy notatki są uboższe) zapisuje czasy, błędy i cytaty w arkuszu z `report-template.md`.

## 4. Forma sesji

- Zdalnie przez Microsoft Teams. Uczestnik udostępnia **cały ekran** (nie jedno okno), bo zadania przechodzą między przeglądarką, Eksploratorem plików, zasobnikiem systemowym i aplikacją.
- Czas: od 30 do 40 minut (runda 2 do 45 minut).
- Metoda głośnego myślenia: uczestnik mówi na bieżąco, co widzi, czego szuka i czego się spodziewa.
- Nagranie wyłącznie dźwięku, lokalnie, aplikacją Ucho na laptopie moderatora, i tylko po podpisaniu zgody (`consent.md`). **Nie włączamy nagrywania w Teams**, bo trafia ono do chmury Microsoftu. Folder z nagraniem nie może leżeć w OneDrive.
- Przed sesją prosimy uczestnika o włączenie trybu „Nie przeszkadzać” w Windows i zamknięcie prywatnych okien, bo będzie udostępniał ekran.

## 5. Przebieg sesji

| Minuta | Etap | Co robi moderator |
|---|---|---|
| 0 do 3 | Powitanie | Przedstawia się, potwierdza zgodę (ustnie, gdy jest już podpisana na piśmie), włącza nagrywanie w Ucho na swoim laptopie. |
| 3 do 5 | Wprowadzenie | Czyta tekst wprowadzenia (niżej). |
| 5 do 7 | Pytania wstępne | Jak nagrywasz lub notujesz spotkania dzisiaj? Czy dyktujesz teksty? (U5, U6: jakiego agenta AI używasz?) |
| 7 do 32 | Zadania | Czyta zadania z `tasks.md` po kolei, każde osobno. Osoba notująca mierzy czas. |
| 32 do 36 | SUS | Wysyła ankietę z `sus.md` na czacie Teams albo czyta stwierdzenia na głos. |
| 36 do 40 | Rozmowa końcowa | „Co było najtrudniejsze?”, „Co było zaskakujące?”, „Czy użyłbyś tego w pracy, a jeśli nie, to dlaczego?” Dziękuje, zatrzymuje nagranie. |

### Tekst wprowadzenia (do przeczytania)

> Dziękuję, że znalazłeś czas. Dzisiaj testujemy aplikację, a nie Ciebie. Nie ma złych odpowiedzi, a każda trudność, na którą trafisz, to dla nas cenna informacja o aplikacji. Poproszę Cię o wykonanie kilku zadań. Mów proszę na głos, co widzisz, czego szukasz i co zamierzasz kliknąć. Nie będę podpowiadać, bo chcemy zobaczyć, jak radzisz sobie sam. Jeśli utkniesz, powiedz, i po kilku minutach przejdziemy dalej. W każdej chwili możesz przerwać.

(Formy męskie są tu skrótem, moderator mówi w formie właściwej dla rozmówcy.)

## 6. Zasady dla moderatora

- **Nie podpowiadaj.** Na pytanie „gdzie to jest?” odpowiadaj pytaniem: „Gdzie byś tego szukał?”
- Gdy uczestnik milczy dłużej niż 10 sekund: „Co teraz widzisz?”, „Czego szukasz?”
- Pomóż dopiero po przekroczeniu limitu czasu z `tasks.md` albo gdy uczestnik sam chce zrezygnować. Wtedy zadanie oznaczamy jako „nie” (albo „częściowo”, jeśli brakowało jednego kroku) i notujemy, w czym pomogliśmy.
- Nie tłumacz projektu ani decyzji. Pytania o to, dlaczego coś działa w dany sposób, zapisz i wróć do nich w rozmowie końcowej.
- Zapisuj dosłowne cytaty, są najcenniejsze w raporcie.

## 7. Dwie rundy

### Runda 1: punkt odniesienia (wersja 0.1)

- Termin: przed pracami nad nowym wyglądem i połączeniem aplikacji (W2, W3).
- Wersja: paczka `Ucho-Windows.zip` v0.1, czyli dwie aplikacje (Ucho i Ucho Dyktuj).
- Zadania 1 do 5 (U5 i U6 także zadanie 6), SUS raz.
- Cel: lista problemów obecnej wersji i wynik SUS, do którego porównamy rundę 2.

### Runda 2: porównanie wariantów A i B (wersja 0.2)

- Termin: po scaleniu W2 i W3.
- **Wariant A:** jedno okno z dwoma trybami (zakładki „Spotkanie” i „Dyktowanie”), jedna ikona w zasobniku.
- **Wariant B:** ikona w zasobniku jako centrum, z menu otwiera się małe okno dyktowania albo pełne okno nagrywania.
- Każdy uczestnik poznaje oba warianty. Kolejność zmieniamy na przemian, żeby wynik nie zależał od tego, który wariant był pierwszy:

| Uczestnik | Pierwszy | Drugi |
|---|---|---|
| U1, U3, U5 | A | B |
| U2, U4, U6 | B | A |

- Przebieg: zadanie 1 (instalacja) raz, potem zadania 2 do 4 w pierwszym wariancie, SUS, przełączenie układu okien w Ustawieniach (robi moderator albo uczestnik według polecenia), zadania 2 do 4 w drugim wariancie (zadanie 2 skrócone do 1 minuty rozmowy), drugi SUS. Na końcu pytanie: „Który układ wolisz i dlaczego?”
- Zadanie 5 i 6 tylko raz, w wariancie, który uczestnik miał jako drugi.
- Rozstrzygnięcie: wygrywa wariant z mniejszą liczbą problemów o wadze 3 i 4, przy remisie wyższy średni SUS, potem deklarowana preferencja.

## 8. Do decyzji założyciela przed rundą 1

1. Lista uczestników U1 do U6 (imiona zna tylko moderator) i terminy.
2. Kto jest moderatorem, a kto notuje.
3. Kto jest administratorem danych w formularzu zgody (osoba prywatna czy spółka) i adres do kontaktu.
4. Czy runda 2 to te same osoby (łatwiej porównać), czy przynajmniej trzy nowe (świeże spojrzenie, bez efektu nauki). Zalecenie: mieszany skład, 3 osoby z rundy 1 i 2 lub 3 nowe.
5. Czy uczestnicy dostają drobne podziękowanie (np. bon) i kto je wysyła.

## 9. Po sesji

- Tego samego dnia: osoba notująca uzupełnia arkusz, moderator dopisuje wnioski.
- Po wszystkich sesjach: raport według `report-template.md`, problemy jako zgłoszenia w repozytorium (bez danych uczestników, tylko kody U1 do U6).
- Nagrania sesji usuwamy po zamknięciu raportu, najpóźniej po 3 miesiącach (tak jak obiecuje `consent.md`).
