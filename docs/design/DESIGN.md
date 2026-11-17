# Pinpage design system: answer booklet

Source of truth: the Claude Design canvas "Pinpage Answer Booklet" (6 artboards). This file records the built world so the React + Tailwind frontend (Milestone 5) matches it. Mode: Operate.

## Idea

Pinpage is marked like an exam answer booklet. The user's question is written in blue ballpoint, and every answer sentence is marked in the margin with the page it came from. No chat bubbles, no centred hero, no cream paper or serif.

Signature move: **the margin column.** A ruled margin sits to the left of every answer. Each sentence's citation sits in it, right-aligned against the rose margin rule, written as an examiner's mark: `p. 10` with the section (`4.1`) under it. Citations are never shown as a row of chips under the answer.

## Colour tokens

| Token | Hex | Use |
|---|---|---|
| cover | #0E5446 | Top bar, sign-in cover panel |
| cover-ink | #F1F5F2 | Text on cover |
| cover-muted | #A9C7BD | Secondary text on cover (4.9:1) |
| desk | #EEF0EE | App background |
| sheet | #FFFFFF | Answer sheet, panels, PDF page |
| ink | #1A1D21 | Body text |
| ink-muted | #59606A | Secondary text |
| rule | #D9DDDA | Dividers, panel borders |
| field | #7C847F | Input borders (3.9:1, meets 1.4.11) |
| margin-rule | #E8B4B0 | The margin line only |
| ballpoint | #1F3FB0 | User's questions, links, primary buttons, focus ring |
| ballpoint-tint | #E8ECFA | Selected section or conversation, text selection |
| examiner | #C2372F | Margin page marks, PDF passage bracket, destructive button |
| examiner-tint | #FBEAE8 | Hovered mark plus its sentence, marked passage, delete confirm |
| waiting | #8A5A00 | Processing and "reading" states |

Rules: ballpoint is used only for things the user wrote or can act on. Examiner red is used only for citations and the destructive confirm, never for decoration.

## Type

Public Sans for everything, with Archivo Narrow Bold uppercase (4% tracking) for the wordmark only.
Page title 28/34 SemiBold. Document title 20/26 SemiBold. Heading 16/22 SemiBold. Question 17/26 Medium in ballpoint (16/24 on phone). Answer 16/26 Regular, capped at 68ch (15/24 on phone). Body and label 14/20. Small 13/18. Margin mark 13/16 SemiBold, with the section in 12/16 ink-muted.
Numbers in lists and tables use tabular figures.

## Layout

- Desktop workspace: cover bar 56 px. Below it is a wrapping row: section rail 264 px, answer sheet (flex 999 1 560px), PDF viewer (flex 1 1 420px, max 520). 20 px gaps on the desk.
- Answer sheet grid: `88px | 1fr` with a 24 px gap. The margin column has `border-right: 1px margin-rule`. Marks are right-aligned, with 10 px of padding to the rule. On phone the grid is `56px | 1fr` with a 14 px gap.
- Composer at the foot of the sheet, aligned to the answer column (112 px left pad). The usage count and the wait note are two separate lines.
- Phone: cover bar with back, title and menu, then tabs (Answers | Page N) at 44 px each. Tapping a margin mark switches to the Page tab with the passage marked.
- Radius 3 px everywhere. Shadows are soft and offset (0 1px 2px plus 0 4–6px 12–18px, low alpha). No coloured left borders.

## Components and states

- Margin mark: a button (a link on phone) with an aria-label like "Open page 10, section 4.1". On hover or focus, the mark and its sentence both take examiner-tint and the page number is underlined. "No page" in ink-muted means not found. "…" in waiting means in progress.
- Page number: boxed digit cells, 24×30, 1 px ink border, one digit per cell.
- PDF passage mark: an examiner-red bracket drawn beside the cited passage, plus an examiner-tint wash.
- Buttons: primary is solid ballpoint, 44 px. Secondary is ballpoint outline on white. Text buttons are underlined ballpoint. Destructive is solid examiner, used only in the inline delete confirm. Disabled is 40% opacity.
- Status: Ready shows a green tick. Processing shows waiting text plus a 2 px progress line. Failed shows examiner text plus an icon and a recovery line.
- States drawn: empty library (teaches the product, offers the sample handbook), new conversation (suggested questions as ballpoint text buttons), 503 (question kept, not counted, Ask again), 429 (input disabled, says when questions return and what still works), inline delete confirm (no modal).
- Focus: 2 px ballpoint outline, offset 2 px. Text selection uses ballpoint-tint.

## Copy

Sentence case. Controls name their action ("Upload a PDF", "Ask again", "Delete document", "Keep it"). The not-found answer is always: "I couldn't find this in the document. Try rephrasing, or ask about a specific section."

## Finish review (in-thread, degraded)

The skill's separate reviewer could not run here, so I reviewed the source against the craft floor myself.
- Fixed: input borders at #9AA19D were 2.6:1, below 3:1. They are now #7C847F.
- Passes: text contrast (lowest is cover-muted at 4.9:1); 44 px touch targets on phone; real buttons, links, labels and inputs; icon-only buttons have aria-labels; no eyebrows, no middle-dot strings, no gradients, no emoji.
- Open: the small grey running header on the simulated PDF page (#7A8088 at 10 px) is document content, not UI. Dark mode and motion beyond the waiting line are unresolved, as stated in the brief.
- Verdict: ready to build from. Recheck contrast and focus in the browser once the React screens exist.

Rasters: none shipped. All marks are inline SVG drawn for Pinpage; the fonts are Google Fonts (OFL).
