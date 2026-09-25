# RoboAlarms identity

The integration uses the same teal R/house mark as the RoboAlarms documentation and
main website. It replaces the early shield/check placeholder.

- Source: `docs/brand/roboalarms-mark.png`, copied unchanged from the documentation
  project's `docs/public/brand/roboalarms-mark.png` on 2026-09-24.
- Original: transparent 1254 × 1254 PNG, generated for RoboAlarms on 2026-09-23.
- Source SHA-256: `c43c06a044f11f27e029bc6c78064bde4f799848ec013617b76927e57d5a33f4`.
- Integration exports: `custom_components/roboalarms/brand/icon.png` (256 × 256)
  and `icon@2x.png` (512 × 512). Both preserve the original colors, proportions and alpha.
- Export method: Sharp 0.35.4, Lanczos3 resizing, PNG with compression level 9.
- README: uses `icon@2x.png` at 120 × 120 CSS pixels with the project name as live text.

Keep the source mark intact. Do not reinstate the retired placeholder generator.
The same transparent mark is used on light and dark backgrounds.

Home Assistant 2026.3+ [loads custom integration branding from the local brand directory](https://developers.home-assistant.io/blog/2026/02/24/brands-proxy-api/).
No manifest or integration-version change is needed to update these local assets;
publishing an updated release remains a separate action.
