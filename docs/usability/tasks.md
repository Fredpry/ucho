# Zadania dla uczestników

> **English summary:** the six task scenarios for Ucho usability sessions (install, record, find, dictate, change language, MCP), each with start state, success criteria, what to observe and a time limit. Content is in Polish because the participants are Polish.

Moderator czyta każde zadanie osobno, dopiero gdy poprzednie się skończy. Tekst w ramce czytamy dosłownie. Uczestnik może poprosić o ponowne przeczytanie, ale nie dostaje dodatkowych wskazówek.

Ocena każdego zadania:

- **tak**: cel osiągnięty bez pomocy, w limicie czasu;
- **częściowo**: cel osiągnięty z jedną podpowiedzią albo z pominięciem jednego kroku z kryteriów;
- **nie**: brak celu, więcej niż jedna podpowiedź, rezygnacja albo przekroczony limit.

Liczymy też **błędy**: każde działanie, które oddala od celu i które uczestnik musi cofnąć (zła zakładka, zły folder, zamknięcie okna zamiast ukrycia, ponowne kliknięcie REC).

---

## Zadanie 1. Instalacja z archiwum ZIP

> Na czacie wysłałem Ci link do paczki z aplikacją Ucho. Zainstaluj ją na swoim komputerze tak, żeby dało się ją uruchomić z menu Start.

- **Stan początkowy:** Ucho nie jest zainstalowane. Uczestnik ma link do `Ucho-Windows.zip` (Releases albo plik z czatu). Python może, ale nie musi być zainstalowany.
- **Kryteria sukcesu:**
  - archiwum rozpakowane **poza OneDrive** (nie na Pulpicie ani w Dokumentach, jeśli są synchronizowane);
  - `instaluj.ps1` (od v0.2 `install.ps1`) uruchomiony i zakończony bez błędu;
  - w menu Start są skróty Ucho (w wersji 0.1 także Ucho Dyktuj).
- **Na co patrzymy:**
  - czy uczestnik czyta README albo instrukcję, czy od razu klika;
  - gdzie rozpakowuje archiwum i czy zauważa ostrzeżenie o OneDrive;
  - reakcja na ostrzeżenie Windows SmartScreen i na okno PowerShell;
  - czy rozumie, na co czeka podczas pobierania modelu (ok. 1,6 GB) i czy nie przerywa instalacji;
  - pytania typu „czy to już?”, „czy coś się zawiesiło?”.
- **Limit czasu:** 10 minut aktywnej pracy. Czas pobierania modelu nie wlicza się do limitu, osoba notująca zapisuje go osobno. Jeśli łącze jest wolne, moderator może w tym czasie zadać pytania wstępne.

---

## Zadanie 2. Nagranie krótkiej rozmowy

> Zaraz porozmawiamy przez dwie minuty o planowaniu firmowego wyjazdu. Nagraj tę rozmowę w Ucho tak, żeby potem dostać jej tekst.

- **Stan początkowy:** Ucho zainstalowane, nieuruchomione. Trwa rozmowa w Teams z moderatorem. Uczestnik ma słuchawki.
- **Przebieg rozmowy (moderator):** wymyślona rozmowa, bez prawdziwych danych. Moderator mówi o terminie i miejscu wyjazdu, a mniej więcej w połowie wspomina: **„Musimy jeszcze zamówić autokar na czterdzieści osób”**. To zdanie będzie potrzebne w zadaniu 3.
- **Kryteria sukcesu:**
  - nagrywanie rozpoczęte przed rozmową (przycisk REC albo Ctrl+Alt+R), najlepiej z nazwą spotkania;
  - obie ścieżki aktywne (mikrofon i dźwięk systemowy);
  - nagranie zatrzymane po rozmowie i transkrypcja zakończona.
- **Na co patrzymy:**
  - czy uczestnik znajduje aplikację w menu Start;
  - czy wie, który przycisk zaczyna nagrywanie, i czy widzi, że nagrywanie trwa;
  - czy rozumie wybór ścieżek i języka (czy w ogóle go zauważa);
  - czy po STOP rozumie, że trwa transkrypcja, i czy na nią czeka;
  - czy zamyka okno, myśląc, że wyłącza aplikację (Ucho chowa się do zasobnika).
- **Limit czasu:** 5 minut ponad czas rozmowy.

---

## Zadanie 3. Znalezienie transkryptu i fragmentu

> Znajdź tekst rozmowy, którą przed chwilą nagraliśmy, i pokaż mi miejsce, w którym była mowa o autokarze. O której minucie rozmowy to padło?

- **Stan początkowy:** transkrypcja z zadania 2 zakończona. Okno Ucho może być otwarte albo schowane.
- **Kryteria sukcesu:**
  - uczestnik otwiera transkrypt (w aplikacji albo plik `transkrypt.md`, od v0.2 `transcript.md`, w folderze nagrania);
  - wskazuje zdanie o autokarze i podaje jego znacznik czasu.
- **Na co patrzymy:**
  - czy szuka na liście nagrań w aplikacji, czy w Eksploratorze plików;
  - czy rozumie etykiety mówców „Ja” i „Spotkanie”;
  - czy używa wyszukiwania (w wersji 0.2) albo Ctrl+F;
  - czy rozumie znaczniki czasu;
  - jak ocenia jakość tekstu (zapisz dosłowne komentarze o błędach rozpoznawania).
- **Limit czasu:** 4 minuty.

---

## Zadanie 4. Podyktowanie maila

> Napisz w Outlooku nowego maila do siebie, ale zamiast pisać na klawiaturze, podyktuj jego treść, na przykład: „Przypominam o jutrzejszym spotkaniu o dziesiątej. Proszę o potwierdzenie obecności.”

- **Stan początkowy:** Outlook otwarty (albo Notatnik, jeśli uczestnik nie ma Outlooka). W wersji 0.1 Ucho Dyktuj nieuruchomione; w wersji 0.2 Ucho działa w tle.
- **Kryteria sukcesu:**
  - dyktowanie uruchomione (w wersji 0.1 trzeba najpierw uruchomić Ucho Dyktuj, w obu wersjach skrót Ctrl+Shift+Spacja);
  - tekst wkleił się w treść maila, w miejscu kursora;
  - uczestnik sam poprawił ewentualne błędy albo uznał tekst za gotowy.
- **Na co patrzymy:**
  - czy uczestnik wie, że dyktowanie to osobna funkcja (w wersji 0.1 osobna aplikacja);
  - czy odkrywa skrót klawiszowy sam, czy szuka przycisku;
  - czy wie, że trzeba nacisnąć skrót drugi raz, żeby zakończyć;
  - czy kursor był w dobrym miejscu i co się dzieje, gdy nie był;
  - jak reaguje na czas oczekiwania między końcem mówienia a pojawieniem się tekstu.
- **Limit czasu:** 4 minuty.

---

## Zadanie 5. Zmiana języka interfejsu

> Twój kolega z zagranicy chce skorzystać z Ucho na Twoim komputerze. Przełącz aplikację na język angielski, a potem z powrotem na polski.

- **Stan początkowy:** Ucho uruchomione, interfejs po polsku.
- **Kryteria sukcesu:**
  - uczestnik znajduje Ustawienia i opcję „Język interfejsu”;
  - interfejs jest po angielsku; w wersji 0.1 zmiana działa dopiero po ponownym uruchomieniu, więc uczestnik musi zamknąć Ucho (z menu ikony w zasobniku: „Zakończ”, bo krzyżyk tylko chowa okno) i uruchomić je znowu;
  - interfejs wraca do polskiego.
- **Na co patrzymy:**
  - czy myli język interfejsu z językiem transkrypcji (to dwa różne ustawienia);
  - czy zauważa dopisek „po ponownym uruchomieniu” i czy umie naprawdę zamknąć aplikację, skoro krzyżyk chowa ją do zasobnika;
  - czy po angielsku zostały jakieś teksty po polsku (zanotuj, to błąd tłumaczenia).
- **Limit czasu:** 3 minuty.

---

## Zadanie 6. Podłączenie MCP w Claude Code (tylko U5, U6)

> Podłącz archiwum nagrań Ucho do Claude Code, a potem zapytaj Claude'a, o czym rozmawialiśmy w nagraniu sprzed chwili i co trzeba zamówić.

- **Stan początkowy:** Claude Code zainstalowany i zalogowany. Ucho zainstalowane, w archiwum jest nagranie z zadania 2. Uczestnik ma dostęp do README (strona repozytorium albo plik w folderze aplikacji).
- **Ważne:** Claude Code wyśle treść transkryptu do Anthropic. Dlatego używamy wyłącznie wymyślonej rozmowy z zadania 2, a uczestnik zaznacza osobną zgodę w `consent.md`.
- **Kryteria sukcesu:**
  - `claude mcp add ucho -- ...` z poprawnymi ścieżkami do `python.exe` i `mcp_serwer.py` (od v0.2 `mcp_server.py`);
  - serwer `ucho` widoczny w Claude Code (np. `/mcp`);
  - Claude odpowiada na podstawie transkryptu i wymienia autokar.
- **Na co patrzymy:**
  - czy uczestnik znajduje polecenie w README i czy zamienia `<you>` na właściwą ścieżkę;
  - kłopoty ze ścieżkami ze spacjami i cudzysłowami w PowerShell;
  - czy rozumie, że serwer tylko czyta i nie otwiera portów;
  - czy Claude sam wybiera właściwe narzędzie (`list_meetings`, `search_transcripts`, `get_transcript`).
- **Limit czasu:** 7 minut.
