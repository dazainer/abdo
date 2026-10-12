// Renders Abdo's app icons (an enamel street-sign plate with a white ع) to PNG.
// Run once after changing the design: `npm run icons`. The PNGs are committed, so
// the production build doesn't depend on this machine's fonts.
import sharp from "sharp";
import { mkdirSync } from "node:fs";

const BLUE = "#1D4E89";
const WHITE = "#F6F7F4";
const out = new URL("../public/icons/", import.meta.url).pathname;
mkdirSync(out, { recursive: true });

// `inset` is the plate's margin inside the canvas: maskable icons need their content
// inside the central ~80% safe zone, so the plate shrinks and the canvas goes full-bleed.
function plate({ size, inset, bleed }) {
  const s = size;
  const p = s * inset;
  const w = s - 2 * p;
  const r = w * 0.2;
  const ring = w * 0.045;
  const ringInset = w * 0.07;
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${s}" height="${s}" viewBox="0 0 ${s} ${s}">
  ${bleed ? `<rect width="${s}" height="${s}" fill="${BLUE}"/>` : ""}
  <rect x="${p}" y="${p}" width="${w}" height="${w}" rx="${r}" fill="${BLUE}"/>
  <rect x="${p + ringInset}" y="${p + ringInset}" width="${w - 2 * ringInset}" height="${w - 2 * ringInset}"
        rx="${r * 0.7}" fill="none" stroke="${WHITE}" stroke-width="${ring}"/>
  <text x="50%" y="${p + w * 0.595}" text-anchor="middle" font-family="Geeza Pro, Noto Sans Arabic, sans-serif"
        font-weight="700" font-size="${w * 0.5}" fill="${WHITE}">ع</text>
</svg>`;
}

const jobs = [
  ["icon-192.png", { size: 192, inset: 0.04, bleed: false }],
  ["icon-512.png", { size: 512, inset: 0.04, bleed: false }],
  ["icon-maskable-512.png", { size: 512, inset: 0.12, bleed: true }],
  // iOS draws its own rounded mask and ignores transparency, so: full-bleed blue.
  ["apple-touch-icon.png", { size: 180, inset: 0.0, bleed: true }],
];
for (const [name, opts] of jobs) {
  await sharp(Buffer.from(plate(opts))).png().toFile(out + name);
  console.log("wrote", name);
}
