# Specyfikacja MVP

## Cel i wynik

Publiczna aplikacja Streamlit przyjmuje pojedynczy link YouTube i zwraca BPM, metrum 3/4 lub 4/4,
capo 0–7, jeden schemat bicia, sekcje harmoniczne A/B/C, timeline łatwych chwytów oraz opcjonalny
statyczny śpiewnik polski lub angielski.
Interfejs działa w głównej ścieżce publicznej subdomeny `ogniskowy-grajek.klikfirma.pl`; Cloudflare
Tunnel przekazuje ruch do Streamlit na NUC-u, gdzie pozostają wszystkie obliczenia.

Dozwolona paleta: `A, Am, C, D, Dm, E, Em, Fmaj7, G`. Sekcje nie udają rozpoznania
„zwrotki” lub „refrenu”. Wynik ma wersję schematu `2.0` i można go pobrać jako JSON oraz — gdy
śpiewnik powstał — jako UTF-8 ChordPro. UI zachowuje zgodność odczytu niewygasłych wyników `1.0`.

## Pipeline

1. Walidacja HTTPS/hosta, oświadczenia o prawach, długości 10 minut i rozmiaru 100 MB.
2. yt-dlp pobiera pojedynczy film; FFmpeg tworzy stereo WAV 44,1 kHz.
3. `htdemucs` oblicza cztery stem-y; `vocals` jest zachowywany tylko do lokalnej transkrypcji.
   Przypięty Demucs 4.0.1
   przyjmuje `--segment` wyłącznie jako liczbę całkowitą, dlatego używamy bezpiecznego `7` s
   (poniżej architektonicznego limitu 7,8 s).
4. Bass+other trafiają do Chordino, drums do librosa. Dopiero te wejścia są mono.
5. Zmiany krótsze niż `max(1 s, pół taktu)` są wygładzane.
6. Backend wyznacza capo, bicie, sekcje i timestampy deterministycznie.
7. Ollama lub Gemini mogą jedynie zweryfikować ścisły JSON akordów; nigdy nie otrzymują audio ani
   tekstu. Awaria daje wynik deterministyczny.
8. Tekst pochodzi kolejno z oryginalnych ręcznych napisów YouTube, oryginalnych napisów
   automatycznych `pl/en`, a następnie lokalnego `faster-whisper medium`. Tłumaczenia są pomijane.
9. ASR działa lokalnie przez osobny subprocess, CPU INT8, 4 wątki, VAD i timestampy słów. Limit to
   600 s z rezerwą 240 s na resztę pipeline. Wynik wymaga języka `pl/en`, prawdopodobieństwa języka
   co najmniej 0,60, średniej pewności słów co najmniej 0,40 i minimum 8 słów.
10. Pyphen dzieli słowa w przybliżeniu na sylaby. Akord trafia do sylaby obejmującej timestamp albo do
    następnej sylaby oddalonej najwyżej o 1,5 s. Pozostałe akordy tworzą linie instrumentalne.
11. Linie powstają z cue, interpunkcji, pauzy co najmniej 1,2 s lub limitu 48 znaków/10 słów.

## Fallbacki

- audio: Demucs CUDA → Demucs CPU → przybliżony HPSS/miks;
- harmonia: Chordino → szablony chroma librosa;
- transformacja: Ollama `llama3:8b` → `gemini-3.1-flash-lite` → algorytm.
- tekst: ręczne napisy YT → automatyczne napisy YT → lokalny ASR → same akordy i link UG.

## Kontrakt śpiewnika

Opcjonalne `arrangement.songbook` zawiera `source`, `language`, `confidence`, stałe
`alignment_mode=APPROXIMATE_SYLLABLE` oraz chronologiczne linie `LYRIC`/`INSTRUMENTAL`. Sylaby mają
tekst i zakres czasu; anchory wskazują istniejący `event_id`, uproszczony chwyt oraz indeks sylaby.
Timeline backendu, capo i timestampy pozostają autorytatywne. Ultimate Guitar jest tylko zewnętrznym
linkiem wyszukiwania; backend nie wysyła do niego żadnego żądania i nie kopiuje treści.

## Limity i retencja

Jeden job globalnie, kolejka 8, jeden aktywny job na klienta, 3/h, 10/dzień i 50 nowych analiz
globalnie/dzień. Audio, wokal i napisy robocze są usuwane w `finally`; wynik wraz z tekstem wygasa po
24 godzinach. Gemini ma budżet aplikacyjny 1 USD/dzień i nigdy nie otrzymuje audio ani tekstu.
