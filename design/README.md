# Ucho design system

A small, calm system for a tool people keep open all day. Warm skin-toned pink ("blush") on warm neutrals: friendly, not loud, easy on the eyes during long calls. Tokens live in [`tokens.css`](tokens.css) and [`tokens.json`](tokens.json); a visual preview is in [`preview.html`](preview.html) (open it in a browser).

## Principles

1. **Quiet by default.** The app runs during meetings. Nothing blinks or demands attention except the recording state.
2. **Recording is unmistakable.** One color (clay red) means "recording now" and is used for nothing else.
3. **Private shows.** No third-party fonts, scripts or images anywhere, including the website. System fonts only.
4. **Readable first.** Every text color passes WCAG AA (4.5:1) on its background, in both themes.
5. **Polish first, English always.** Layouts leave room for Polish, which runs about 20% longer than English.

## Color

| Token | Light | Dark | Use |
|---|---|---|---|
| `--color-bg` | `#FDF7F4` blush-50 | `#171312` | page and window background |
| `--color-surface` | `#FFFFFF` | `#221C1A` | cards, panels |
| `--color-text` | `#2A211E` (14.8:1) | `#F4EAE6` (15.6:1) | body text |
| `--color-text-muted` | `#675C55` (6.1:1) | `#A99C93` (6.9:1) | secondary text |
| `--color-brand` | `#DF9C86` blush-400 | `#EBBBAA` | logo, illustrations, decoration only |
| `--color-action` | `#8E4E3D` blush-700 (6.0:1) | `#EBBBAA` (10.7:1) | buttons, links, focus |
| `--color-recording` | `#B4452F` clay | `#EE8A72` | recording state only |
| `--color-success` / `warning` / `info` | sage / amber / dusk | lighter variants | status messages |

Blush-400 is the brand color but fails contrast for text, so it is never used for text or as a button fill behind white text. Actions use blush-700 in light mode and blush-300 in dark mode.

Full scales: blush 50 to 900, stone (warm neutrals) 50 to 950.

## Type

| Role | Stack | Size |
|---|---|---|
| Display (logo, big headings) | Iowan Old Style, Charter, Sitka Heading, Georgia | `--text-3xl` / `--text-4xl` |
| UI and body | Segoe UI Variable, Segoe UI, system-ui | `--text-base` (16 px), line height 1.55 |
| Code, timestamps | Cascadia Mono, Consolas | `--text-sm` |

Timestamps in transcripts (`[00:12:31]`) always use the mono font so columns line up.

## Space, shape, depth

- 4 px grid: `--space-1` (4) to `--space-9` (96).
- Radii: 8 (inputs, small buttons), 12 (cards), 20 (large panels), full (pills, the record button).
- Shadows are low and warm-tinted; in dark mode surfaces are separated by color, not shadow.

## Motion

- Fast 140 ms for hover and press, base 240 ms for panels, slow 2.4 s for the ear's sound waves.
- `prefers-reduced-motion` turns animation off; the logo then shows static waves.

## Components

Implemented in the app (v0.2): the theme is built from `tokens.json` in `ucho/theme.py`, the components below live in `ucho/ui.py`. Screenshots: [`docs/screens`](../docs/screens).

| Component | Rules |
|---|---|
| Record button | Circle, 120 to 148 px. Idle: `--color-action`. Recording: `--color-recording` with a slow pulse ring. Label REC / STOP plus the shortcut under it. |
| Primary button | `--color-action` fill, `--color-on-action` text, radius 12, min height 40. |
| Secondary button | Surface fill, 1 px border, text color. |
| Status pill | Radius full, soft background + status color text: Ready (sage), Transcribing (dusk), Recording (clay). |
| Meeting card | Surface, radius 12, title + date line in muted text + one-line summary, actions as text buttons. |
| Transcript line | Mono timestamp, speaker in semibold (Me in action color, Meeting in muted), text in body color. |
| Focus | 2 px outline in `--color-focus` with 2 px offset, never removed. |

## Logo

`assets/ucho-mark.svg`: an ear drawn as two strokes with three sound waves arriving from the left. Brand blush for the outer ear, blush-600 for the inner curl. Minimum size 24 px; below 32 px drop the waves. README banners (PL/EN, light/dark, animated): `assets/banner-*.svg`, built by `make_banner.py` with the headline in Bricolage Grotesque converted to outlines.

## Voice

Short, concrete, calm. Say what happens and where data goes ("The recording stays on this laptop"), never "AI-powered" or "revolutionary". Polish texts follow the same rules: no calques, no dashes as punctuation in marketing copy.
