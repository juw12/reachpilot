# ReachPilot design system

ReachPilot is a local dashboard that shows one person what their job-search and LinkedIn automations did today
and what still needs them. The UI reads like a well-set daily report: plain, dense, calm. `dashboard.html`
follows this file; change both together.

## References

| Reference | What we take |
|---|---|
| Financial Times / printed annual reports | Warm paper background, serif headlines, thin rules between sections instead of boxed cards |
| GitHub Primer (Insights, Actions) | Underlined current tab, muted gridlines, right-aligned tabular numbers, status as text plus colour |
| Stripe Dashboard tables | Dense data tables, uppercase column labels, one obvious primary button per row |
| LinkedIn (the platform the tool posts to) | The single blue accent, so the product reads as LinkedIn-adjacent without copying its UI |

## Palette (one accent)

| Token | Hex | Use |
|---|---|---|
| `--bg` / `--card` | `#faf9f6` | Page and panels (panels are separated by rules, not fills) |
| `--ink` | `#1f1d1a` | Text, section rules, header rule |
| `--muted` | `#5f5a52` | Labels, secondary text |
| `--line` | `#e4e0d8` | Hairline dividers |
| `--blue` | `#0a5aa8` | The only accent: links, primary buttons, selected tab underline, charts |
| `--blue-soft` | `#eaf1f8` | Secondary (ghost) buttons |
| `--red` / `--amber` / `--green` | `#b42318` / `#915907` / `#057642` | Status only (urgent, waiting, ok), always paired with a word |

AA contrast (checked): ink on bg 16.0:1, muted on bg 6.5:1, blue on bg 6.6:1, white on blue 6.9:1,
blue on blue-soft 6.1:1, red/amber/green on their soft fills 5.8 / 5.3 / 5.0:1.

## Type

- Display: `Georgia, Cambria, "Times New Roman", ui-serif, serif` for h1-h4, KPI values and the big module numbers.
- Body: `"Segoe UI Variable Text", "Segoe UI", system-ui, -apple-system, Roboto, sans-serif`, 15px / 1.6.
- Numbers in tables: `font-variant-numeric: tabular-nums`, right-aligned.
- Labels: 12px uppercase, letter-spacing .03-.07em, muted. Nothing below 12px.
- No web fonts: the app runs offline on a work PC.

## Space, radius, shadow, motion

- Spacing scale: 4, 8, 12, 16, 24 px. Section gap 18-24 px.
- Radius: 2px on buttons and badges, 0 on panels. Pills only for the to-do count.
- Shadow: none, except the toast and the help dialog (they float above the page).
- Motion: none beyond hover colour. Nothing animates on load.
- Phone (<= 760px): tabs stay on one row (primary tabs + "More"), touch targets >= 44px.
- Between 761px and about 1100px the tabs wrap to a second row; they never run under the clock.
- A table whose cells hold sentences (the Patent screen) does not scroll sideways on a phone: at <= 760px each
  row becomes a block with the column name above each value. Its rule sits next to the phone rules in
  `dashboard.html`; change both breakpoints together.

## Voice

Plain and specific. Say what happened and what to do: "7 jobs ready to apply today", "Start apply session".
Banned words: supercharge, seamless, unlock, next-level, effortless, magic, AI-powered.

## Signature

The editorial ledger: a 2px ink rule under the header, a 1px ink rule over every section, serif numbers, and an
underlined current tab. It looks like a printed daily report, not a SaaS template.

## Banned defaults

- Purple, violet or indigo anywhere; purple-to-blue gradients; gradient text
- Emoji used as icons, in headings, buttons, tabs or list bullets
- Inter (or any single default sans) as the only typeface
- Identical rounded cards with drop shadows in a grid
- Glow blobs, glassmorphism, decorative status dots without a label
- Centred-everything layouts, fake testimonials or logos, stock 3D shapes
- Hype copy and "Get started" as the only call to action
