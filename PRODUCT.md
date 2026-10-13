# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

The four roommates of a student suite. Each uses the app on their own phone (installed to the
Home Screen as a PWA), mostly in quick glances between classes, in the morning, and when heading
out. They speak and write mixed Egyptian Arabic and English, often in the same sentence.

## Product Purpose

Abdo is a household assistant that acts like a roommate: you talk to him (text or hold-to-talk
voice) and he keeps the suite's shared state for everyone: groceries, the fridge, packages,
shared money, statuses and travel, classes and exams, polls, prayer times, and buses.

Success: open the app, know within seconds what today holds and what needs you, and act in one
tap.

## Positioning

One assistant shared by the whole suite, with each person's own view on top: the same lists,
money, and plans for everyone, answered in the suite's own mix of Egyptian Arabic and English,
and reachable from the web app or Telegram with one shared conversation.

## Operating Context

- Phones first: an installed PWA on iPhone and Android, often one-handed, often outdoors or on
  campus; also opened on a laptop now and then.
- The morning briefing (also sent as a notification) is the daily ritual; prayer notifications
  and reminders bring people back during the day.
- Anything the app shows can also be done by asking Abdo in chat or on Telegram.

## Capabilities and Constraints

- Screens: Today (briefing, hourly weather, free time, statuses, exams, prayer times, buses,
  reminders, the week's calendar), Chat, Lists (polls, groceries, going shopping, fridge,
  packages), Money (balances, settle-up, expenses), Classes (Quest import), Settings.
- Text is bidirectional: Arabic and English mix on one line, so every text surface must handle
  both directions (`unicode-bidi: plaintext` / `dir="auto"`).
- Login is a 6-digit code sent by Telegram. Push notifications only work from the installed
  Home Screen app on iPhone.
- Dark theme only for now.
- The repo is public: no real names, residence, or ids in the product or its docs.

## Brand Commitments

- The name Abdo (عبده) and his voice: a warm, witty Cairene roommate.
- Screen titles are bilingual, Arabic first with English beneath.
- The established visual identity (Cairo enamel street-sign plates) stays; this is a
  restructure and polish, not a new look.

## Evidence on Hand

Real content only comes from the suite's own data. Demo mode (`tests/demo_server.py`) seeds
clearly fake data (made-up roommates "Mo" and "Sam", sample items) for screenshots; never present
it as real.

## Product Principles

1. Today first: the app opens on what today holds and what needs you.
2. Say it once: a fact appears in one place per screen; no section repeats the briefing.
3. Glanceable, then actionable: the answer before the details, and the action one tap from the
   fact it acts on.
4. Abdo can do it too: views stay simple because anything complex can be asked in chat.
5. Both languages are first-class: Arabic and English read correctly wherever they appear.
