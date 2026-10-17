# Comic to EPUB (Bleubirddy.io)

Turns CBZ, CBR and PDF comics into fixed-layout EPUBs, with foggy speech-bubble
lettering cleaned up. Everything runs in the visitor's browser: no server, no uploads.

## Add it to the site

1. Copy this whole `comic-to-epub` folder into your GitHub Pages repo, for example
   at `tools/comic-to-epub/`.
2. Commit and push. The tool is then live at
   `https://bleubirddy.io/tools/comic-to-epub/`.
3. Link to it from your nav or a blog post:
   ```html
   <a href="/tools/comic-to-epub/">Comic to EPUB</a>
   ```

No build step and nothing to install. All libraries are inside `lib/`.

## Files

| File | What it does |
|---|---|
| `index.html`, `style.css`, `ui.js` | The page: drop zone, options, progress, downloads |
| `app.js` | Reads CBZ/CBR/PDF, runs pages through the workers, writes the EPUB |
| `page-worker.js` | Splits two-page spreads and cleans up lettering (off the main thread) |
| `lib/jszip/` | JSZip 3.10.1: reads CBZ, writes EPUB (MIT) |
| `lib/libarchive/` | libarchive.js 2.0.2: reads CBR/RAR via WebAssembly (MIT) |
| `lib/pdfjs/` | pdf.js 4.10.38: renders PDF pages (Apache-2.0) |

## Restyling

The colors are CSS variables at the top of `style.css` (light and dark sets). To match
the rest of the site, change those, or replace the `<header>`/`<footer>` in `index.html`
with your site's own.

## Notes

- **Speed:** about 15-35 seconds per issue on a laptop; slower on a phone.
- **Output size:** close to the original. "Maximum image quality" roughly doubles it.
- **Browsers:** current Chrome, Edge, Firefox and Safari (needs OffscreenCanvas and
  module workers: Safari 16.4 or newer).
- **Not in this version:** OCR re-lettering (the Python tool's `--lettering reletter`).
  Planned as version 2.
