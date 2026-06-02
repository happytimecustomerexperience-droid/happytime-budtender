# Subsystem 4 — No-Reload Menu Chat Widget

> Date: 2026-05-29 · Status: approved · Parent: master-design.md
> Lives in the website repo (`happytimeweed/public/` + a thin API reuse).

## Problem
The 3 store menu pages are static HTML (for edge-cache speed) and cannot mount
the React chatbot. Today "Continue with budtender" navigates/opens a new tab —
the user wants it to open the chat **in place, no reload**, resuming whatever
session was already going.

## Approach (chosen): standalone vanilla-JS widget
Ship `public/budtender-widget.js` — a slim, dependency-free chat panel injected
on the static menu pages. It is NOT a re-implementation of the React brain; it is
a thin client over the SAME server endpoints and the SAME persisted session.

- **Shared session:** reads/writes `localStorage['chatbot-state-v2']` (same key,
  same shape as the React app's persist layer). Opening the widget hydrates the
  in-progress conversation; closing persists it. Switching between the site
  (React) and the menu (widget) is seamless.
- **Same APIs:** calls same-origin `/api/chat/turn` (SSE), `/api/chat/search`,
  `/api/catalog/pairing`, `/api/track` — identical contracts, so all the
  personalization/classification work flows through unchanged.
- **In-place open:** the existing "Continue with budtender" launcher button no
  longer navigates; it toggles the widget panel open over the menu (fixed
  overlay, bottom-right), and back. No new tab, no reload.
- **UI scope:** message list, quick-reply chips, the product cards (reuse the same
  card markup/styles as `budtender-pairing.js`), the pairing card, an input box,
  and a "scroll to menu" affordance. Minimal but on-brand.

## Why not convert menu to Next routes
Rejected to preserve the static-HTML edge-cache performance the menu pages were
built for. The vanilla widget keeps that speed while giving true no-reload chat.

## Shared-shape risk + mitigation
The widget must stay schema-compatible with `lib/chat/persist.ts` /
`schema.ts`. Mitigation: a tiny shared JSON contract doc + a smoke test that
writes a state with the widget and rehydrates it in the React app (and vice
versa). Keep the widget's state read/write in one small module.

## Verification
- On a menu page, clicking "Continue with budtender" opens the chat panel with NO
  navigation/reload (URL unchanged), showing the prior conversation.
- A turn taken in the widget persists; opening the React site afterward resumes
  the same thread. Product cards + pairing render. Analytics events fire.
