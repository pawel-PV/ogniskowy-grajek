# Specyfikacja MVP

## Cel i wynik

Publiczna aplikacja Streamlit przyjmuje pojedynczy link YouTube i zwraca BPM, metrum 3/4 lub 4/4,
capo 0–7, jeden schemat bicia, sekcje harmoniczne A/B/C oraz timeline łatwych chwytów.
Interfejs działa z bazową ścieżką `/ogniskowy-grajek` i jest publikowany pod istniejącym hostem
`api.klikfirma.pl`; obliczenia pozostają na NUC-u.

Dozwolona paleta: `A, Am, C, D, Dm, E, Em, Fmaj7, G`. Sekcje nie udają rozpoznania
„zwrotki” lub „refrenu”. Wynik ma wersję schematu `1.0` i można go pobrać jako JSON.

## Pipeline

1. Walidacja HTTPS/hosta, oświadczenia o prawach, długości 10 minut i rozmiaru 100 MB.
2. yt-dlp pobiera pojedynczy film; FFmpeg tworzy stereo WAV 44,1 kHz.
3. `htdemucs` oblicza cztery stem-y; `vocals` jest usuwany po separacji. Przypięty Demucs 4.0.1
   przyjmuje `--segment` wyłącznie jako liczbę całkowitą, dlatego używamy bezpiecznego `7` s
   (poniżej architektonicznego limitu 7,8 s).
4. Bass+other trafiają do Chordino, drums do librosa. Dopiero te wejścia są mono.
5. Zmiany krótsze niż `max(1 s, pół taktu)` są wygładzane.
6. Backend wyznacza capo, bicie, sekcje i timestampy deterministycznie.
7. Ollama lub Gemini mogą jedynie zweryfikować ścisły JSON; awaria daje wynik deterministyczny.

## Fallbacki

- audio: Demucs CUDA → Demucs CPU → przybliżony HPSS/miks;
- harmonia: Chordino → szablony chroma librosa;
- transformacja: Ollama `llama3:8b` → `gemini-3.1-flash-lite` → algorytm.

## Limity i retencja

Jeden job globalnie, kolejka 8, jeden aktywny job na klienta, 3/h, 10/dzień i 50 nowych analiz
globalnie/dzień. Pliki audio są usuwane w `finally`; wynik wygasa po 24 godzinach. Gemini ma
budżet aplikacyjny 1 USD/dzień i nigdy nie otrzymuje audio.
