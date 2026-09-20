# YMZX brand asset

`ymzx-29675b01.png` is the user-supplied transparent black logo, preserved byte for byte.

- Dimensions: 2018 × 783 pixels, RGBA.
- SHA-256: `29675b01791f048779dbb6be6dba84860082a788d4dd0ff959e4edcd66ea7313`.
- Full wordmark viewport: `63 208 1905 421`.
- Compact cat-shaped M viewport: `515 208 528 421`.

`components/brand-logo.tsx` frames the original image with SVG; it does not redraw it. The dark navigation rail inverts the displayed black mark with CSS. The favicon embeds the same original PNG inside a white tile so it remains visible on light and dark browser chrome and makes no external image request.

The application name remains **1CatDL**. Include the `brand` directory when exporting static assets. The PNG filename and favicon URL include an asset version to avoid stale browser caches.
