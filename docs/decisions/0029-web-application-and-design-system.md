# ADR-0029: Web application and design system

- Status: Accepted (M6)
- Date: 2026-11-01

## Context

Operators need to see what runs are doing, decide approvals and manage agents, models, tools, knowledge, memory and access, without a terminal (FR-17). The interface must use the real API, work in light and dark themes, be accessible and responsive, and express the Hollis identity rather than a generic AI aesthetic.

## Decision

**Architecture.**
- A single-page app in `web/` (React 19, TypeScript, Vite, React Router, TanStack Query), served by `hollis serve` from the same origin as the API.
- Its build is committed to `src/hollis/server/static`, so installing Hollis never needs Node.
- It calls only the REST API, with types generated from the OpenAPI document. Live runs use Server-Sent Events; lists poll while runs are active.
- No inline scripts or styles in markup (the CSP forbids them); fonts are self-hosted (IBM Plex, OFL).

**Design system** (from the brand, `brand/README.md`):
- Warm ink and paper neutrals; brass only for attention (approvals waiting, the active section, focus), never decoration.
- Typography and hairline rules carry hierarchy: tables, definition lists and timelines rather than card grids. No gradients, translucency or decorative depth.
- The run inspector draws the mark: a ring where a run starts, a right-angle path through model calls and tool calls, a square where it ends.
- Light and dark themes are designed separately, with every text color measured at WCAG AA (4.5:1) and form borders and focus at 3:1 on every surface; the theme follows the system unless chosen.

**Accessibility and responsiveness:** semantic landmarks and headings, a skip link, visible focus, keyboard-reachable scroll regions, native dialogs, status never by color alone (glyph and word), live regions for notifications, reduced motion respected, layouts down to phone width.

**Honesty in the interface:** empty, loading and error states everywhere; the demo model labelled wherever it appears; controls shown only when the server would allow them, which still decides; deletion and "forget" dialogs state which copies remain.

## Alternatives considered

- **Server-rendered pages:** live run views and approvals need client state; the API already exists.
- **A component library:** its visual language would replace Hollis's; the needed components are few.
- **Building the app at install time:** would require Node for every user.

## Trade-offs

Committing build output adds generated files to the repository; a test checks the build references only files it contains and that its API types match the server. Node is needed only to change the app.

## Consequences

Tests: ESLint with strict accessibility rules, TypeScript strict, Vitest component tests, and Playwright journeys in the installed Chrome against a real server (opt-in locally, a CI job). Visual review in both themes and at phone width is part of changing the app.
