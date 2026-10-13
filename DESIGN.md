---
name: Abdo
description: A roommate assistant's phone app, set as Cairo enamel street signs glowing on a navy night.
colors:
  enamel-blue: "#2a62a8"
  signal-gold: "#f2b705"
  night-navy: "#0e1a2b"
  night-surface: "#16263d"
  hairline: "#22344f"
  enamel-white: "#f6f7f4"
  moon-text: "#eef1f5"
  dusk-text: "#9fb0c8"
  blue-black-ink: "#1a2233"
  recording-red: "#b3261e"
  alarm-coral: "#ff8a80"
typography:
  display:
    fontFamily: "Alexandria, IBM Plex Sans Arabic, system-ui, sans-serif"
    fontSize: "3rem"
    fontWeight: 700
    lineHeight: 1.45
  numeral:
    fontFamily: "Alexandria, IBM Plex Sans Arabic, system-ui, sans-serif"
    fontSize: "2.75rem"
    fontWeight: 700
    lineHeight: 1
    fontFeature: "tnum"
  headline:
    fontFamily: "Alexandria, IBM Plex Sans Arabic, system-ui, sans-serif"
    fontSize: "1.5625rem"
    fontWeight: 700
    lineHeight: 1.45
  title:
    fontFamily: "Alexandria, IBM Plex Sans Arabic, system-ui, sans-serif"
    fontSize: "1.25rem"
    fontWeight: 600
    lineHeight: 1.5
  time:
    fontFamily: "Alexandria, IBM Plex Sans Arabic, system-ui, sans-serif"
    fontSize: "1rem"
    fontWeight: 600
    lineHeight: 1.5
    fontFeature: "tnum"
  body:
    fontFamily: "IBM Plex Sans Arabic, system-ui, sans-serif"
    fontSize: "1rem"
    fontWeight: 400
    lineHeight: 1.5
  label:
    fontFamily: "IBM Plex Sans Arabic, system-ui, sans-serif"
    fontSize: "0.8125rem"
    fontWeight: 500
    lineHeight: 1.5
  sign-caption:
    fontFamily: "Alexandria, IBM Plex Sans Arabic, system-ui, sans-serif"
    fontSize: "0.8125rem"
    fontWeight: 600
    lineHeight: 1.15
    letterSpacing: "0.02em"
  micro:
    fontFamily: "IBM Plex Sans Arabic, system-ui, sans-serif"
    fontSize: "0.6875rem"
    fontWeight: 500
    lineHeight: 1.3
rounded:
  rule: "2px"
  tail: "6px"
  control-sm: "10px"
  control: "12px"
  plate: "14px"
  bubble: "16px"
  plate-lg: "18px"
  sign-lg: "20px"
  composer: "22px"
  pill: "999px"
spacing:
  xxs: "4px"
  xs: "6px"
  sm: "8px"
  md: "12px"
  gutter: "16px"
  section: "18px"
  lg: "24px"
components:
  sign-plate:
    backgroundColor: "{colors.enamel-blue}"
    textColor: "{colors.enamel-white}"
    rounded: "{rounded.plate}"
    padding: "10px 22px 9px"
  sign-plate-large:
    backgroundColor: "{colors.enamel-blue}"
    textColor: "{colors.enamel-white}"
    rounded: "{rounded.sign-lg}"
    padding: "22px 44px 18px"
  briefing-plate:
    backgroundColor: "{colors.enamel-blue}"
    textColor: "{colors.enamel-white}"
    rounded: "{rounded.plate-lg}"
    padding: "18px 20px 16px"
  button-primary:
    backgroundColor: "{colors.enamel-blue}"
    textColor: "{colors.enamel-white}"
    rounded: "{rounded.control}"
    padding: "0 18px"
    height: "44px"
  button-quiet:
    backgroundColor: "transparent"
    textColor: "{colors.moon-text}"
    rounded: "{rounded.control}"
    padding: "0 18px"
    height: "44px"
  button-small:
    rounded: "{rounded.control-sm}"
    padding: "0 12px"
    height: "34px"
    typography: "{typography.label}"
  field:
    backgroundColor: "{colors.night-surface}"
    textColor: "{colors.moon-text}"
    rounded: "{rounded.control}"
    padding: "10px 14px"
    height: "46px"
  chip-need:
    backgroundColor: "transparent"
    textColor: "{colors.moon-text}"
    rounded: "{rounded.pill}"
    padding: "0 14px"
    height: "40px"
    typography: "{typography.label}"
  chip-toggle:
    backgroundColor: "{colors.night-surface}"
    textColor: "{colors.moon-text}"
    rounded: "{rounded.pill}"
    padding: "0 14px"
    height: "38px"
  chip-toggle-on:
    backgroundColor: "{colors.enamel-blue}"
    textColor: "{colors.enamel-white}"
    rounded: "{rounded.pill}"
  chip-tag:
    backgroundColor: "{colors.hairline}"
    textColor: "{colors.moon-text}"
    rounded: "{rounded.pill}"
    padding: "1px 8px"
    typography: "{typography.label}"
  tab:
    backgroundColor: "{colors.night-surface}"
    textColor: "{colors.dusk-text}"
    height: "64px"
    typography: "{typography.label}"
  tab-active:
    textColor: "{colors.moon-text}"
  bubble-abdo:
    backgroundColor: "{colors.enamel-blue}"
    textColor: "{colors.enamel-white}"
    rounded: "{rounded.bubble}"
    padding: "12px 16px"
  bubble-mine:
    backgroundColor: "{colors.night-surface}"
    textColor: "{colors.moon-text}"
    rounded: "{rounded.bubble}"
    padding: "10px 14px"
  mic-button:
    backgroundColor: "{colors.signal-gold}"
    textColor: "{colors.blue-black-ink}"
    rounded: "{rounded.pill}"
    size: "46px"
  mic-button-recording:
    backgroundColor: "{colors.recording-red}"
    textColor: "{colors.enamel-white}"
  send-button:
    backgroundColor: "{colors.enamel-blue}"
    textColor: "{colors.enamel-white}"
    rounded: "{rounded.pill}"
    size: "46px"
---

# Design System: Abdo

## Overview

**Creative North Star: "Street Signs on a Night Street"**

Abdo is a Cairene roommate, so his app is dressed in the most Cairene object there is: the blue enamel street sign, with white lettering and a white ring set in from the edge. The signs hang on a navy night. Everything Abdo says is a plate: each screen's title, his chat bubbles, the morning briefing, the money balance. Everything else sits quietly on the navy (rows, hairlines, soft secondary text), so the plates carry the screen.

On top of the blue and white there is one signal colour: gold, for whatever is happening now or needs you next. That covers the "now" tick on the day's route line, the next prayer, the active tab, the needs-you chips, the hold-to-talk mic and the focus ring. The screens are dense and built for a quick glance on a phone held in one hand: a single column, rows at least 44 to 52px tall, and times and numbers set in the sign face with tabular figures so they line up like a timetable.

The app speaks two languages throughout. Titles read Arabic first in large type, with English beneath at caption size, the way a bilingual street sign does. Every line of mixed text reads in its own direction. The app is dark only for now.

**Key Characteristics:**
- Enamel-blue plates with an inset white ring mark Abdo's own voice; nothing else gets a plate.
- One navy night ground with tonal surfaces and hairlines; no drop-shadow elevation.
- Gold signals now, next and act; it is never decoration.
- Two faces: Alexandria for signs, headings, times and numbers; IBM Plex Sans Arabic for running text.
- Arabic-first bilingual titles; `unicode-bidi: plaintext` on every mixed line.
- Today is a transit route: a 4px enamel-blue line with ring stops, square bus stops, and a terminus bar.

## Colors

A navy night lit by a blue enamel plate and a gold signal.

### Primary
- **Enamel Blue** (`enamel-blue`): the plate itself. Used for screen-title signs, the briefing and money-balance plates, Abdo's chat bubbles, primary buttons, the send button, toggles that are on, the day's route line and its terminus, count badges and text links. In code this is `--plate`.

### Secondary
- **Signal Gold** (`signal-gold`): the colour for now, next and needs-you. It marks the "now" tick and label, the next prayer's filled stop and time, prayer stop rings, the active tab's icon and underline, the needs-you chip outlines, the bus "in N min" countdown, fridge items nearing expiry, weather icons on the briefing plate, the hold-to-talk mic, the "mark paid" action, the focus ring, the text caret and selection (at 35% opacity), and a chosen poll's bar (also at 35%).

### Neutral
- **Night Navy** (`night-navy`): the page ground, the composer's background, and the knockout fill inside ring stops.
- **Night Surface** (`night-surface`): one step up from the ground. Used for the tab bar, fields, my own chat bubbles, notices, poll options and quiet chips.
- **Hairline** (`hairline`): row dividers, borders on fields and quiet buttons, the tab bar's top edge, the fill for tags and unvoted poll bars, and the switch track when off. In code it is both `--line` and `--snow`.
- **Enamel White** (`enamel-white`): lettering on plates, the ring inset into each plate, the rings of route-line stops, and toast text. Rules inside a plate use it at 25% (`--plate-rule`).
- **Moon Text** (`moon-text`): primary text on the navy.
- **Dusk Text** (`dusk-text`): secondary text, meta lines, inactive tabs, placeholders and past-tense details.
- **Blue-Black Ink** (`blue-black-ink`): text on gold fills (the mic, "mark paid"), and the toast background.

### Status
- **Recording Red** (`recording-red`): the mic while it's recording, with a soft pulse. Used only there.
- **Alarm Coral** (`alarm-coral`): error text and borders (error notices, expired fridge items). It is a lighter red on purpose, so error text stays readable on the navy. Today the value is written out in the CSS rather than kept as a token.

### Named Rules
**The Enamel Voice Rule.** A plate (enamel blue, white ring, white lettering) means Abdo is speaking or a screen is naming itself: screen titles, his chat bubbles, the briefing, the money balance. Ordinary rows, lists and forms never sit on a plate.

**The Gold Means Now Rule.** Gold marks only what is current, next, active or waiting on you. If a gold element doesn't answer "what's happening now, or what should I do?", it shouldn't be gold.

## Typography

**Sign Font:** Alexandria (500, 600, 700), with IBM Plex Sans Arabic and system-ui as fallbacks
**Body Font:** IBM Plex Sans Arabic (400, 500, 600), with system-ui as the fallback

**Character:** Alexandria's geometric, wide letters carry Arabic and Latin with the same signage presence. It sets everything a sign would show: titles, headings, times, temperatures and amounts. Plex Sans Arabic is the calm, readable voice for sentences and meta lines in both scripts.

The scale is a 1.25 ratio (13 / 16 / 20 / 25 / 31) with two numeral sizes above it. Fonts load locally through @fontsource.

### Hierarchy
- **Display** (Alexandria 700): the large sign on the login screen only.
- **Numeral** (Alexandria 700, tabular figures): the briefing temperature and the money balance, one per plate. The calendar day number uses the 31px step (`--t-display`) in the same face and weight.
- **Headline** (Alexandria 700): the Arabic line of every screen-title sign and the briefing greeting. Line height stays generous (1.45 to 1.5) so the dots and tails of Arabic letters never touch the line below.
- **Title** (Alexandria 600): section headings ("Around", "Coming up"), poll questions, and the free-together windows.
- **Time** (Alexandria 600, tabular figures): times on route stops, Arabic prayer names on stops, and calendar times. am/pm sit beside the time at micro size in Dusk Text.
- **Body** (Plex Sans Arabic 400): row titles, chat, fields.
- **Label** (Plex Sans Arabic 500, 13px): meta lines, tab labels, chips, notices, toasts.
- **Sign Caption** (Alexandria 600, 13px, 0.02em tracking, sentence case): the English line beneath the Arabic on a sign.
- **Micro** (Plex Sans Arabic 500, 11px): only for units and small companions to a larger figure: am/pm, hour labels in the weather strip, rain chance, count badges.

### Named Rules
**The Arabic Leads Rule.** Every screen title is a sign: the Arabic name large on top, the English name beneath at caption size. Arabic is never the smaller line.

**The Two Faces Rule.** Alexandria for anything a sign would show (names, headings, times, numbers); Plex Sans Arabic for sentences. Don't add a third family.

**The Timetable Figures Rule.** Times, temperatures, amounts and counts use tabular figures (`font-variant-numeric: tabular-nums`), so columns of times line up like a schedule.

**The Own-Direction Rule.** Every text surface that can mix scripts uses `unicode-bidi: plaintext`. In lists, also pin `text-align: left`, so Arabic rows don't flush right. Chat bubbles keep `dir="auto"` alignment on purpose.

## Layout

The app is a single column, at most 640px wide and centred, sized for a phone held in one hand. Each screen has a fixed header holding its centred sign, then its own scrolling area with 16px side gutters and 24px at the bottom. A fixed tab bar sits at the bottom (64px plus the device's safe-area inset), and the column pads itself to clear it. The safe-area insets are honoured at the top and bottom. The login screen narrows to 420px and centres vertically.

Spacing runs mostly on steps of 4, 6, 8, 12, 16, 18 and 24px. Sections are 18px apart; rows inside a section are separated by hairlines rather than gaps. Tap targets are at least 44px (rows 52px, route stops 48px, chips 38 to 40px tall in a scrolling row).

Horizontal strips (the needs-you chips, the Lists jump row, the hourly weather) scroll sideways. They bleed out to the screen edge with a -16px margin, keep the 16px gutter as inner padding, hide their scrollbars, and fade out at the trailing edge with a mask, which shows there is more to scroll.

There are no breakpoints. On wider screens the column simply stops growing at 640px.

**The Route Grammar.** Today lays out time as a transit line. The list is indented 30px, and a 4px enamel-blue line runs down the left with rounded caps. Each stop is a three-column grid: a 62px time, then the content, then an optional action. Each stop marker is a 14px ring centred on the line. The gold "now" tick (18 by 4px) crosses the line at the current moment, and a 16px terminus bar closes the line under the last stop.

## Elevation & Depth

The system is flat, and depth comes from colour. The navy ground, a slightly lighter navy surface, and hairline borders separate the layers. Plates stand out by colour and by their **inset ring**: two inset shadows, one in plate blue and one in enamel white at 85%, draw the white ring 4 to 5px inside the edge. That is a line drawn inside the plate, not elevation, and nothing floats.

### Shadow Vocabulary
- **Enamel ring** (`box-shadow: inset 0 0 0 4px var(--plate), inset 0 0 0 6px rgba(246, 247, 244, 0.85)`): on every plate. Large plates (the briefing, the money balance) use 5px and 7px.
- **Selected outline** (`box-shadow: 0 0 0 1px var(--plate)`): on a chosen poll option or the current code slot. When the code input has focus, it becomes 2px in gold.
- **Switch knob** (`box-shadow: 0 1px 2px rgba(26, 34, 51, 0.3)`): the only true drop shadow, and only on the toggle knob.
- **Recording pulse** (keyframed from `0 0 0 0 rgba(179, 38, 30, 0.45)` out to 10px and transparent): only on the mic while it's recording.

### Named Rules
**The Ring, Not Shadow Rule.** Plates get their presence from the inset white ring and the blue. Cards, rows and plates never get drop shadows.

## Shapes

Corners are softly rounded and grow with size: 10 to 12px on controls, 14px on title signs, 16px on chat bubbles, 18px on the large plates, 20px on the login sign, and 22px on the composer. Chips, toggles, badges and the switch are full pills; the mic and send buttons are circles. A chat bubble pinches its corner nearest the speaker to 6px as a tail (`border-end-end-radius` on mine, `border-end-start-radius` on Abdo's). These are logical properties, so the tail flips correctly in right-to-left text.

The route line has its own small set of shapes: a ring for each stop, a filled gold ring for the next prayer, a thinner ring (2px) for sunrise, and a 3px-radius rounded square in plate blue with a white border for bus stops, like a bus-stop sign. The ends of rules and ticks are rounded (2px).

## Components

### Street Sign (signature)
A screen title set as a Cairo street sign.
- **Shape:** an inline plate with the enamel ring, 14px corners (20px for the large login sign), centred in the screen header.
- **Content:** the Arabic name (headline, `lang="ar" dir="rtl"`) above the English name (sign caption at 92% opacity). It is the page's `h1`.
- **Rule:** one per screen, always bilingual, always Arabic first.

### Briefing Plate (signature)
The morning briefing on Today: Abdo speaking, so it sits on enamel.
- **Shape:** a full-width plate, 18px corners, with the 5px and 7px ring, and 18px 20px 16px padding.
- **Content, top to bottom:** the date line at label size; the Arabic greeting as a headline; a 25%-white rule; the weather row (a 34px gold stroke icon, a numeral temperature with a raised half-size degree sign, the sky in bold, and a feels-like and high/low line); a 25%-white rule; then a sideways-scrolling hourly strip (hour at micro size, a 20px gold icon, temperature in the sign face, rain chance in pale blue only when it's 30% or more).
- **Loading:** a blank plate at 236px tall and 60% opacity, so the layout doesn't jump when the data arrives.
- The money balance on the Money screen is the same plate (`money-summary`), carrying a numeral amount and transfer rows separated by 25%-white rules.

### Route Line (signature)
The rest of today as stops on a single line (see Layout, The Route Grammar).
- **Stops:** a time in the time face, then a title in the row-title style, with an optional meta line beneath and an optional small quiet button on the right.
- **States:** past stops fade to 50% opacity (earlier ones fold behind an "Earlier today (n)" link); the next prayer turns gold (filled marker, time and title); exam owners are in bold gold; sunrise is in Dusk Text.
- **Bus stop:** the whole row is a toggle that expands the upcoming trips. A chevron rotates 90° over 0.2s with an `cubic-bezier(0.16, 1, 0.3, 1)` ease-out, and a gold "in N min" countdown sits beside the route name.
- **Now marker:** a gold tick with a "Now h:mm am" label in the sign face. When Today opens, the tick draws across the line from the left (0.6s, same ease-out), and only once. With reduced motion, it doesn't animate.

### Buttons
- **Shape:** 12px corners, 44px tall, 18px side padding, weight 600. The small size is 34px tall, 10px corners, label size.
- **Primary:** an enamel-blue fill with enamel-white text. It's a plate without the ring.
- **Quiet:** transparent, with a 1px hairline border and Moon Text. This is the default for secondary actions and for row actions (Cancel, Clear, Remove).
- **Link:** enamel-blue text, underlined with a 3px offset.
- **Round:** 46px circles in the chat composer. Send is enamel blue; the mic is gold with ink-coloured icon and turns recording red with a pulse while held.
- **Disabled:** 55% opacity. **Focus:** a 2px gold outline, 2px out from the element, everywhere.

### Chips
- **Needs-you chip:** a pill 40px tall with a 1px gold outline, Moon Text at label size, no wrapping. It's a link to the place where you act on it, and fills with 14% gold while pressed. These sit in the sideways-scrolling needs-you row under the briefing.
- **Toggle chip:** a pill 38px tall on Night Surface with a hairline border. When on, it fills enamel blue (used to choose who an expense splits between).
- **Jump link:** a Lists section shortcut. A pill on Night Surface at label size, weight 600, with a small enamel-blue count badge.
- **Tag:** a small pill filled with Hairline, at label size.

### Rows and Sections
- **Section:** a title-style heading, an optional note in Dusk Text at label size, then rows.
- **Row:** at least 52px tall, 12px gap between parts, a hairline divider below, the title in body style, the meta line beneath in Dusk Text at label size. Both lines read in their own direction and are pinned left.
- **Empty state:** a single line in Dusk Text.

### Inputs / Fields
- **Style:** 46px tall, 10px 14px padding, a 1px hairline border, 12px corners, Night Surface fill, Dusk Text placeholder, a gold caret, and each line read in its own direction.
- **Focus:** the global 2px gold focus ring.
- **Login code:** six 56px slots in the sign face at headline size. The current slot gets a blue outline, which turns gold when the code input has focus.
- **Composer:** the chat text box is a 22px-radius pill that grows with its text up to 140px tall.

### Navigation
- **Tab bar:** fixed at the bottom on Night Surface, with a hairline top border and five equal tabs (a 24px stroke icon over a label-size word).
- **Inactive:** Dusk Text. **Active:** the label in Moon Text, the icon in gold, and an 18 by 3px gold underline below.

### Icons
Inline stroke SVGs on a 24px viewBox, with a 1.8 stroke and round caps and joins, coloured by `currentColor`. There's no icon library or icon font; a new icon is a new path added to the set.

### Chat Bubbles
Abdo's bubbles are plates (enamel ring, 12px 16px padding) on the left. Mine are Night Surface with a hairline border on the right. Both use 16px corners with a 6px tail, and run at most 84% of the column width. A meta line underneath at label size carries the time and a replay control.

## Do's and Don'ts

### Do:
- **Do** give each screen exactly one street-sign title, Arabic on top and English beneath, centred in the fixed header.
- **Do** put Abdo's own words (his briefing, his chat bubbles, the balance he keeps) on an enamel plate with the inset white ring.
- **Do** keep gold for now, next, active and needs-you. Gold fills are only for the mic and the "mark paid" action, with Blue-Black Ink text on them.
- **Do** set times, temperatures and amounts in Alexandria with tabular figures.
- **Do** use `unicode-bidi: plaintext` on every line that can mix scripts, and `text-align: left` in lists.
- **Do** keep tap targets at least 44px, and use logical properties (`inset-inline-start`, `border-end-start-radius`) so the layout mirrors cleanly in right-to-left text.
- **Do** respect `prefers-reduced-motion`: the now-tick draw, the chevron turn, the switch slide and the recording pulse all turn off.

### Don't:
- **Don't** put drop shadows on plates, cards or rows. Depth is the ring and the navy tones.
- **Don't** put ordinary lists, forms or settings on a plate. A plate means Abdo or a screen title.
- **Don't** use gold as decoration, or in large fills beyond the mic and the settle-up action.
- **Don't** make English the larger or leading line in a title.
- **Don't** add a third typeface, an icon font, or emoji standing in for icons.
- **Don't** add a light theme or light surfaces without a deliberate system change. The world is a night street.
- **Don't** let an Arabic row flush right inside a list.
