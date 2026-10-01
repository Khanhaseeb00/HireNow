# Hire Now Brand Source of Truth

Production branding must use only the files in `static/brand/`.

## Locked master assets
- `hirenow-mark.svg`: symbol-only launcher/app mark. Use for PWA launcher, favicon-style square placements and compact identity.
- `hirenow-lockup.svg`: full HireNow lockup. Use inside the app, login/register and splash.
- `final-brand.css`: shared proportions, colors and responsive splash rules.

## Locked colors
- Navy: `#0B1E3F`
- Orange: `#FF9F1C`
- White: `#FFFFFF`

## Non-negotiable rules
1. Never redraw the H/handshake mark in page HTML/CSS.
2. Never generate a separate AI logo for a screen.
3. Never stretch a logo. Set width only; height remains automatic. Preserve the SVG viewBox.
4. Launcher icons use the symbol-only master; never put the wordmark/tagline inside the launcher icon.
5. Splash screens are responsive HTML/CSS + the exact master SVG, not screenshots/raster mockups.
6. Do not include fake phone status bars, time, battery, Wi-Fi, browser chrome or device frames in splash assets.
7. Hirer and Worker may have different role copy, but must reference the same master lockup.
8. A brand change is made in the master asset once, then propagated by reference. Do not fork the logo.
