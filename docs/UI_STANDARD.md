# QA Center UI Standard

QA Center is an engineering operations console. Its interface prioritizes target safety, run state, failures, evidence, and the next available action. It is compact, neutral, and explicit; decoration never competes with operational meaning.

## Information hierarchy

Every major page follows: page header, target/context band, primary workflow, secondary detail, then evidence/logs. P1 information is target, environment, connection/run state, failures, blockers, and result. P2 is scenario metadata, time, duration, version, and evidence count. P3 is IDs, hashes, and implementation metadata; place it last or behind disclosure.

Execution screens must show target name, endpoint, environment as text, MESFlow version, QA Center version, and connection status. A run action must remain visually adjacent to this identity. LOCAL and DEV are informational, TEST is warning, and PRODUCTION is danger. Never encode environment or status by color alone.

## Tokens

The canonical web tokens live in `current/static/app.css`. Other page styles may alias them, but must not redefine their meaning.

| Purpose | Token / scale |
| --- | --- |
| Spacing | `--space-1` through `--space-7`: 4, 8, 12, 16, 24, 32, 48px |
| Type | 12px meta, 13–14px table/body, 16px card title, 18–20px section title, 24–28px page title |
| Controls | `--control-height`: 40px; large primary: 44px |
| Radius | `--radius-sm`: 6px, `--radius-md`: 8px, `--radius-lg`: 12px |
| Layering | nav 20, sticky content 30, drawer 50, modal 70, toast 90 |
| Surfaces | canvas, surface, subtle, border, text, muted |
| Semantics | info, success, warning, danger; use only for meaning |

Use the spacing scale for gaps, margins, and padding. Card padding is 16–24px, section separation 24–32px, desktop page padding 24–32px, and mobile page padding 16px.

## Components

- Page header: one 24–28px title, one short context line, optional status, and one primary action.
- Context band: a compact grid of labeled facts. Endpoint and hashes use monospace and wrap anywhere.
- Buttons: primary for the single forward action in a group; secondary for supporting actions; ghost for refresh/navigation; danger only for stop/delete/reset. Cancel is secondary. Disabled controls retain their label and have visibly reduced contrast.
- Status badge: uppercase text plus semantic color. PASS/READY green; RUNNING blue; WARNING/STALE amber; FAIL/BLOCKED red; SKIPPED/CANCELLED neutral.
- Cards: one meaningful group, 1px border, 8–12px radius, minimal shadow. Avoid nested cards.
- Tables: text left, useful numbers right, stable action column, 40px rows, and a `.table-wrap` horizontal boundary. On narrow screens preserve page width and scroll only the table.
- Filters: search, status, suite/category, target, date, reset, refresh. Controls are equal height and wrap.
- Empty/error states: name the missing or failed object, explain why when known, and provide a concrete next action.
- Dialogs: `max-height: 88vh`, internal scrolling, visible action footer, modal layer 70. Drawers use layer 50 and a consistent header/body/footer.
- Logs: monospace, at least 1.55 line height, quiet timestamps, visible severity, wrapping by default, and no page-wide horizontal overflow.
- Icons: use the existing product icon family. Do not introduce emoji or ambiguous icon-only controls. Every icon-only control requires an accessible name and tooltip.

## Accessibility and responsive rules

All interactive elements have a visible `:focus-visible` ring. Inputs have programmatic labels. Status text remains present without color. Click targets are at least 36px on desktop and 44px on mobile where space permits.

Required review viewports: 1920x1080, 1440x900, 1366x768, 1024x768, 390x844, and 360x800. At 1366x768, target identity, status, failures, progress, and primary actions stay above secondary metadata. At mobile widths, toolbars wrap, page-wide overflow is forbidden, dialogs fit the viewport, and tables scroll inside their own wrapper.

## Change checklist

For non-trivial UI changes: review all affected states, capture 1366x768 and mobile screenshots, inspect them visually, and check DOM geometry for page overflow, off-screen interactive elements, overlay collision, and clipped dialog actions. Store audit screenshots beneath `artifacts/ui-audit/qa-center/`; final accepted baselines belong in `artifacts/ui-audit/qa-center/final/` with a manifest containing screen, route, state, viewport, screenshot, and verdict.
