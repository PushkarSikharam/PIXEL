# Pixel Design System (foundation)

Source: `saas/console/app/globals.css` (tokens and component styles) and `saas/console/components`.
Live gallery: `/preview` in the console prototype.

## Principles

An instrument, not an AI landing page: neutral surfaces, colour only where it means something, dense
but calm layouts, no gradients, glass, glow or decorative motion (plan section 13).

## Tokens

| Group | Values |
| --- | --- |
| Structure | `--px-bg`, `--px-surface`, `--px-surface-2`, `--px-border`, `--px-border-strong`, three text levels |
| Meaning | Cobalt `--px-accent` (selection), emerald `--px-ok`, amber `--px-warn`, crimson `--px-danger`, each with a soft background |
| Type | Geist Sans and Geist Mono from the `geist` package (no network font loading), 12/13/14/16/20/24 px, letter spacing 0 |
| Space | 4 px base: 4, 8, 12, 16, 20, 24, 32, 40 |
| Shape | Radius 6 px (4 px small), control height 32 px |

Light and dark themes follow the system setting, with an explicit override (`data-theme`).

## Components

Button (primary, default, ghost, danger, small, loading), Field with label, hint and error, Input,
Select, Textarea, Badge and StatusBadge, StatusRaster (Pixel's square status motif: one cell per real
stage), Alert, Panel, PageHead, Table, Dialog (Radix), Menu (Radix), Toast, and the four states:
loading, empty, permission denied and failure.

## Accessibility gate

- Contrast is tested from the stylesheet itself: text ≥ 4.5:1 and UI boundaries ≥ 3:1, both themes
  (`contrast.test.ts`). The test caught the first input-border colour at 1.9:1; it was fixed.
- Status is never colour alone: every badge has its word, and the status raster has a text label.
- Keyboard: skip link, visible focus ring, Radix focus trapping and return for dialogs and menus,
  Escape closes overlays, focus moves to the heading of each onboarding step.
- Forms: labels bound to inputs, hints and errors linked with `aria-describedby`, errors announced.
- Live regions for loading and notifications; `prefers-reduced-motion` removes movement.
- Responsive: the shell collapses to one column under 900 px.

Still to do before Phase 6 sign-off: screenshot baselines, automated axe checks, a manual
screen-reader pass, and a 200% zoom review.
