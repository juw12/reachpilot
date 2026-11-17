# ReachPilot design system

ReachPilot is a local dashboard that shows one person what their job-search and LinkedIn automations did today
and what still needs them. The UI reads like a field notebook: paper, hairline rules, a serif for names and
numbers, a mono for labels. `dashboard.html` follows this file; change both together.

## References

| Reference | What we take |
|---|---|
| Cairn (the author's own notes app) | The layout model and the materials: a left sidebar with grouped pages, a paper background, hairline rules instead of boxed cards, Instrument Serif for display, Geist for text, Geist Mono for labels |
| GitHub Primer (Insights, Actions) | Muted gridlines, right-aligned tabular numbers, status as a word plus colour |
| Stripe Dashboard tables | Dense data tables, uppercase column labels, one obvious primary button per row |
| LinkedIn (the platform the tool posts to) | The single blue accent. ReachPilot keeps its own blue; it does not take Cairn's accent |

## Palette (one accent)

| Token | Hex | Use |
|---|---|---|
| `--bg` / `--card` | `#f4f1ea` | Page, sidebar and blocks (blocks are separated by rules, not fills) |
| `--surface` | `#fffdf8` | The selected page in the sidebar, secondary buttons, quoted text (a post, an e-mail) |
| `--sunken` | `#ebe6db` | Hover in the sidebar, grey badges, code, chart tracks |
| `--ink` | `#1b1a17` | Text and the rule under every block heading |
| `--ink-2` | `#4a463e` | Sidebar items, the line under a page title |
| `--muted` | `#5f5a52` | Labels, notes, secondary text |
| `--line` / `--line-strong` | `#e1dbcf` / `#cbc3b3` | Hairline dividers / button and dashed-box borders |
| `--blue` | `#0a5aa8` | The only accent: links, primary buttons, the selected-page marker, the label above a page title, charts |
| `--blue-soft` | `#dfe8f2` | Blue badges |
| `--red` / `--amber` / `--green` | `#b42318` / `#7d4c05` / `#05693b` | Status only (urgent, waiting, ok), always paired with a word |
| `--red-soft` / `--amber-soft` / `--green-soft` | `#f6dedb` / `#f6e8c9` / `#dcebdf` | The fill behind a status word |

AA contrast, computed from the hex values above (WCAG 2.x, 4.5:1 needed for text):
ink on bg 15.4:1, ink-2 on bg 8.3:1, muted on bg 6.1:1,
muted on sunken 5.5:1, blue on bg 6.1:1, blue on blue-soft 5.6:1,
white on blue 6.9:1, red / amber / green on their soft fills
5.1 / 6.0 / 5.5:1. The lowest pair is red on red-soft.
Change a colour and these numbers must be computed again, not estimated.

## Type

- Display: `"Instrument Serif"` for the product name, page titles (44px, 34px on a phone), numbers in the number
  strips (38px) and module headlines (30px). Regular weight only.
- Text: `"Geist"`, 14.5px / 1.55. Block headings are Geist 600 at 14px over an ink rule.
- Labels and metadata: `"Geist Mono"` 12px, uppercase with letter-spacing .05-.06em for labels; badges, run times and
  the log use it in normal case.
- Numbers in tables: `font-variant-numeric: tabular-nums`, right-aligned.
- Nothing below 12px.
- The three fonts are bundled in `fonts/` (SIL Open Font License, see `fonts/LICENSE.md`) and served by the app from
  `/fonts/...`. No font, script or style is loaded from another host: the app runs offline on a work PC. If a font file
  is missing the stacks fall back to Georgia, Segoe UI and Consolas.

## Layout

- Shell: a 232px sidebar on the left (product name, "This PC" with the update time, the pages in groups, "How it
  works" at the bottom) and the page on the right, at most 1180px wide with 40px padding.
- Sidebar groups: Today (Overview, To do), Posting, Projects, Job search, Other. A page is listed only when its module
  is installed. The selected page has a surface fill, a hairline outline and a 3px blue marker at the sidebar edge.
- Every page starts with a page head: a mono label (the group, or today's date on Overview), the page title in serif,
  and one line saying what the page shows.
- A block is a heading with an ink rule and what belongs to it. No boxes, no shadows, no fills behind blocks.
- One rule system: an ink rule under a block heading, a hairline between rows and between cells. Each block brings
  its own top rule; nothing draws a rule for its neighbour.
- Number strips: figures sit in one joined strip with hairlines between cells. The column count follows the number of
  cells so no row ends in a hole: 1 to 4 cells in one row, 5 or 6 in rows of three, 7 or 8 in rows of four; with 5 or 7
  the first cell is double width. On a phone the strip has two columns and the first cell is full width when the count
  is odd.
- To-do rows: text on the left, buttons in a fixed column on the right; urgent rows carry a red rule on the left edge
  and the word "urgent". On Overview a row shows at most four links with short notes; the To do page shows all of it.

## Space, radius, shadow, motion

- Spacing scale: 4, 8, 12, 16, 24, 36, 40 px. 36px between blocks, 28px under a page head.
- Radius: 6px on buttons, inputs and quoted text, 4px on badges, 5px on sidebar items. Blocks have none.
- Shadow: none, except the toast and the help dialog (they float above the page).
- Motion: none beyond hover colour. Nothing animates on load.

## Breakpoints

- <= 900px: the sidebar becomes a drawer. A top bar shows the menu button, the product name and the update time; the
  drawer closes on the scrim, on Escape and when a page is picked. Touch targets are at least 44px.
- <= 760px: 16px page padding, two-column number strips, to-do buttons under the text. A table whose cells hold
  sentences (the Patent screen, the Patent desk, the podcast directories) does not scroll sideways: each row becomes a
  block. These rules sit together in the phone block of `dashboard.html`; change them together.
- <= 600px: the per-day automation table becomes one small grid per row.
- Other tables scroll sideways inside their own box; the page itself never scrolls sideways (checked at 320px).

## Voice

Plain and specific. Say what happened and what to do: "7 jobs ready to apply today", "Start apply session".
Banned words: supercharge, seamless, unlock, next-level, effortless, magic, AI-powered.

## Signature

The field notebook: paper, an ink rule under every block heading, serif numbers in a joined strip, mono labels, and
one blue marker for where you are. It looks like a kept notebook, not a SaaS template.

## Banned defaults

- Purple, violet or indigo anywhere; purple-to-blue gradients; gradient text
- Emoji used as icons, in headings, buttons, sidebar items or list bullets
- Inter (or any single default sans) as the only typeface
- Identical rounded cards with drop shadows in a grid
- A coloured left border on every card (the red rule is for urgent to-do rows only)
- Glow blobs, glassmorphism, decorative status dots without a label
- Centred-everything layouts, fake testimonials or logos, stock 3D shapes
- Hype copy and "Get started" as the only call to action
- Hard-coded colours in templates: use the tokens
