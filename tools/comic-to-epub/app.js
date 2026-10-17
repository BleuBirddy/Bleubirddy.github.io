/* Comic to EPUB - Bleubirddy.io
 * Everything runs in the visitor's browser: files are never uploaded anywhere.
 *
 * Inputs:  .cbz / .zip (JSZip), .cbr / .rar (libarchive.js, WebAssembly), .pdf (pdf.js)
 * Output:  fixed-layout EPUB 3 (one page image per page, EPUB 2 table of contents too)
 */
import { Archive } from "./lib/libarchive/libarchive.js";

const here = (p) => new URL(p, import.meta.url).href;
Archive.init({ workerUrl: here("./lib/libarchive/worker-bundle.js") });

const IMAGE_EXT = /\.(jpe?g|png|webp|gif|bmp)$/i;
const CREDIT_PAGE = /^z/i; // scan-group credit pages like "zWater.jpg" / "ZZZZZ.jpg"
const JPEG_QUALITY = 0.95; // 1.0 = near-lossless with full color resolution, ~2x bigger
const PDF_DPI = 200, PDF_MAX_H = 3000;

/* ------------------------------------------------------------------ reading */

const naturalKey = (s) => s.toLowerCase().split(/(\d+)/).map((t, i) => (i % 2 ? t.padStart(12, "0") : t));
function naturalSort(a, b) {
  const ka = naturalKey(a), kb = naturalKey(b);
  for (let i = 0; i < Math.min(ka.length, kb.length); i++) if (ka[i] !== kb[i]) return ka[i] < kb[i] ? -1 : 1;
  return ka.length - kb.length;
}
const baseName = (p) => p.split("/").pop();
const isPage = (path) => IMAGE_EXT.test(path) && !baseName(path).startsWith(".") && !path.includes("__MACOSX")
  && !CREDIT_PAGE.test(baseName(path));

async function readComic(file, onStatus) {
  const head = new Uint8Array(await file.slice(0, 5).arrayBuffer());
  const magic = String.fromCharCode(...head);
  if (magic.startsWith("%PDF")) return readPdf(file, onStatus);
  if (head[0] === 0x50 && head[1] === 0x4b) return readZip(file);
  return readRar(file);
}

async function readZip(file) {
  const zip = await JSZip.loadAsync(file);
  const names = Object.keys(zip.files).filter((n) => !zip.files[n].dir);
  const pages = [];
  for (const n of names.filter(isPage).sort(naturalSort)) pages.push({ name: n, buf: await zip.files[n].async("arraybuffer") });
  const info = names.find((n) => baseName(n).toLowerCase() === "comicinfo.xml");
  const meta = info ? parseComicInfo(await zip.files[info].async("string")) : {};
  return { pages, meta };
}

async function readRar(file) {
  const archive = await Archive.open(file);
  const tree = await archive.extractFiles();
  const flat = [];
  (function walk(node, prefix) {
    for (const [k, v] of Object.entries(node)) {
      if (v instanceof File) flat.push([prefix + k, v]);
      else if (v && typeof v === "object") walk(v, prefix + k + "/");
    }
  })(tree, "");
  const pages = [];
  for (const [n, f] of flat.filter(([n]) => isPage(n)).sort((a, b) => naturalSort(a[0], b[0])))
    pages.push({ name: n, buf: await f.arrayBuffer() });
  const info = flat.find(([n]) => baseName(n).toLowerCase() === "comicinfo.xml");
  const meta = info ? parseComicInfo(await info[1].text()) : {};
  return { pages, meta };
}

let pdfjs = null;
async function readPdf(file, onStatus) {
  if (!pdfjs) {
    pdfjs = await import("./lib/pdfjs/pdf.min.mjs");
    pdfjs.GlobalWorkerOptions.workerSrc = here("./lib/pdfjs/pdf.worker.min.mjs");
  }
  const doc = await pdfjs.getDocument({ data: new Uint8Array(await file.arrayBuffer()) }).promise;
  const meta = {};
  try {
    const { info } = await doc.getMetadata();
    if (info?.Title) meta.title = String(info.Title).trim();
    if (info?.Author) meta.writer = String(info.Author).trim();
  } catch { /* no metadata */ }
  const pages = [];
  for (let i = 1; i <= doc.numPages; i++) {
    onStatus(`Rendering PDF page ${i} of ${doc.numPages}`);
    const page = await doc.getPage(i);
    const base = page.getViewport({ scale: 1 });
    const scale = Math.min(PDF_DPI / 72, PDF_MAX_H / base.height);
    const vp = page.getViewport({ scale });
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(vp.width); canvas.height = Math.round(vp.height);
    const ctx = canvas.getContext("2d");
    ctx.fillStyle = "#fff"; ctx.fillRect(0, 0, canvas.width, canvas.height);
    await page.render({ canvasContext: ctx, viewport: vp }).promise;
    const blob = await new Promise((r) => canvas.toBlob(r, "image/jpeg", JPEG_QUALITY));
    pages.push({ name: `${String(i).padStart(5, "0")}.jpg`, buf: await blob.arrayBuffer() });
    page.cleanup();
  }
  await doc.destroy();
  return { pages, meta };
}

function parseComicInfo(xml) {
  try {
    const doc = new DOMParser().parseFromString(xml, "application/xml");
    const get = (t) => (doc.getElementsByTagName(t)[0]?.textContent || "").trim();
    const m = { title: get("Title"), series: get("Series"), number: get("Number"), writer: get("Writer"),
      publisher: get("Publisher"), summary: get("Summary"), language: get("LanguageISO"), manga: get("Manga") };
    return Object.fromEntries(Object.entries(m).filter(([, v]) => v));
  } catch { return {}; }
}

/* ------------------------------------------------------------- page workers */

class Pool {
  constructor(n) {
    this.workers = Array.from({ length: n }, () => new Worker(here("./page-worker.js")));
    this.idle = [...this.workers]; this.queue = []; this.nextId = 0; this.pending = new Map();
    for (const w of this.workers) w.onmessage = (e) => this.done(w, e.data);
  }
  run(msg, transfer) {
    return new Promise((resolve, reject) => { this.queue.push({ msg, transfer, resolve, reject }); this.pump(); });
  }
  pump() {
    while (this.idle.length && this.queue.length) {
      const w = this.idle.pop(), job = this.queue.shift(), id = this.nextId++;
      this.pending.set(id, job);
      w.postMessage({ ...job.msg, id }, job.transfer);
    }
  }
  done(w, data) {
    const job = this.pending.get(data.id); this.pending.delete(data.id);
    this.idle.push(w);
    data.ok ? job.resolve(data.pages) : job.reject(new Error(data.error));
    this.pump();
  }
  terminate() { this.workers.forEach((w) => w.terminate()); }
}

/* ---------------------------------------------------------------- EPUB */

const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

async function uuid5ish(text) { // stable id from the title, formatted as a UUID
  const h = new Uint8Array(await crypto.subtle.digest("SHA-1", new TextEncoder().encode("comic-to-epub:" + text)));
  h[6] = (h[6] & 0x0f) | 0x50; h[8] = (h[8] & 0x3f) | 0x80;
  const x = [...h.slice(0, 16)].map((b) => b.toString(16).padStart(2, "0")).join("");
  return `${x.slice(0, 8)}-${x.slice(8, 12)}-${x.slice(12, 16)}-${x.slice(16, 20)}-${x.slice(20)}`;
}

async function buildEpub(pages, meta, rtl) {
  const n = pages.length, digits = Math.max(3, String(n).length);
  const num = (i) => String(i + 1).padStart(digits, "0");
  const { width: W, height: H } = pages[0];
  const title = esc(meta.title);
  const bookId = `urn:uuid:${await uuid5ish(meta.title)}`;
  const modified = new Date().toISOString().replace(/\.\d+Z$/, "Z");
  const extra = [];
  if (meta.writer) extra.push(`<dc:creator>${esc(meta.writer)}</dc:creator>`);
  if (meta.publisher) extra.push(`<dc:publisher>${esc(meta.publisher)}</dc:publisher>`);
  if (meta.summary) extra.push(`<dc:description>${esc(meta.summary)}</dc:description>`);
  if (meta.series) {
    extra.push(`<meta property="belongs-to-collection" id="series">${esc(meta.series)}</meta>`);
    extra.push('<meta refines="#series" property="collection-type">series</meta>');
    if (meta.number) extra.push(`<meta refines="#series" property="group-position">${esc(meta.number)}</meta>`);
  }
  const manifest = ['<item id="cover-img" href="images/cover.jpg" media-type="image/jpeg" properties="cover-image"/>'];
  const spine = [], nav = [], ncx = [];
  pages.forEach((_, i) => {
    manifest.push(`<item id="i${num(i)}" href="images/${num(i)}.jpg" media-type="image/jpeg"/>`);
    manifest.push(`<item id="p${num(i)}" href="pages/${num(i)}.xhtml" media-type="application/xhtml+xml"/>`);
    spine.push(`<itemref idref="p${num(i)}"/>`);
  });
  for (const i of [0, ...Array.from({ length: Math.max(0, Math.ceil((n - 10) / 10)) }, (_, k) => 10 * (k + 1))].filter((i) => i < n)) {
    const label = i === 0 ? "Start" : `Page ${i + 1}`;
    nav.push(`<li><a href="pages/${num(i)}.xhtml">${label}</a></li>`);
    ncx.push(`<navPoint id="n${num(i)}" playOrder="${ncx.length + 1}"><navLabel><text>${label}</text></navLabel><content src="pages/${num(i)}.xhtml"/></navPoint>`);
  }
  const nl = "\n    ";
  const opf = `<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid" prefix="rendition: http://www.idpf.org/vocab/rendition/#">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="bookid">${bookId}</dc:identifier>
    <dc:title>${title}</dc:title>
    <dc:language>${esc(meta.language || "en")}</dc:language>
    ${extra.join(nl)}
    <meta property="dcterms:modified">${modified}</meta>
    <meta property="rendition:layout">pre-paginated</meta>
    <meta property="rendition:orientation">portrait</meta>
    <meta property="rendition:spread">none</meta>
    <meta name="cover" content="cover-img"/>
    <meta name="fixed-layout" content="true"/>
    <meta name="original-resolution" content="${W}x${H}"/>
    <meta name="book-type" content="comic"/>
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
    <item id="css" href="style.css" media-type="text/css"/>
    ${manifest.join(nl)}
  </manifest>
  <spine toc="ncx" page-progression-direction="${rtl ? "rtl" : "ltr"}">
    ${spine.join(nl)}
  </spine>
</package>
`;
  const navDoc = `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head><title>${title}</title></head>
<body><nav epub:type="toc" id="toc"><h1>${title}</h1><ol>
${nav.join("\n")}
</ol></nav></body>
</html>
`;
  const ncxDoc = `<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
<head><meta name="dtb:uid" content="${bookId}"/><meta name="dtb:depth" content="1"/>
<meta name="dtb:totalPageCount" content="0"/><meta name="dtb:maxPageNumber" content="0"/></head>
<docTitle><text>${title}</text></docTitle>
<navMap>
${ncx.join("\n")}
</navMap>
</ncx>
`;
  const css = "@page { margin: 0; }\n"
    + "html, body { margin: 0; padding: 0; width: 100%; height: 100%; background: #fff; }\n"
    + "div.page { margin: 0; padding: 0; width: 100%; height: 100%; text-align: center; }\n"
    + "img { display: block; margin: 0 auto; width: 100%; height: 100%; object-fit: contain; }\n";
  const container = `<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>
`;
  const zip = new JSZip();
  const STORE = { compression: "STORE" }, DEFL = { compression: "DEFLATE" };
  zip.file("mimetype", "application/epub+zip", STORE); // must be first and uncompressed
  zip.file("META-INF/container.xml", container, DEFL);
  zip.file("OEBPS/content.opf", opf, DEFL);
  zip.file("OEBPS/nav.xhtml", navDoc, DEFL);
  zip.file("OEBPS/toc.ncx", ncxDoc, DEFL);
  zip.file("OEBPS/style.css", css, DEFL);
  zip.file("OEBPS/images/cover.jpg", pages[0].data, STORE);
  pages.forEach((p, i) => {
    const page = `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>${title} - ${i + 1}</title><meta name="viewport" content="width=${p.width}, height=${p.height}"/>
<link rel="stylesheet" type="text/css" href="../style.css"/></head>
<body><div class="page"><img src="../images/${num(i)}.jpg" alt="Page ${i + 1}" width="${p.width}" height="${p.height}"/></div></body>
</html>
`;
    zip.file(`OEBPS/pages/${num(i)}.xhtml`, page, DEFL);
    zip.file(`OEBPS/images/${num(i)}.jpg`, p.data, STORE); // already compressed
  });
  return zip.generateAsync({ type: "blob", mimeType: "application/epub+zip" });
}

/* -------------------------------------------------------------- conversion */

export async function convert(file, opts, onProgress) {
  onProgress({ phase: "Reading", done: 0, total: 1 });
  const { pages, meta } = await readComic(file, (msg) => onProgress({ phase: msg, done: 0, total: 1 }));
  if (!pages.length) throw new Error("No page images found in this file.");
  const rtl = opts.rtl || (meta.manga || "").toLowerCase() === "yesandrighttoleft";
  const pool = new Pool(Math.max(1, Math.min(navigator.hardwareConcurrency || 2, 4)));
  let done = 0, cleaned = 0;
  try {
    const results = await Promise.all(pages.map((p) =>
      pool.run({ buf: p.buf, split: opts.split, rtl, clean: opts.clean, quality: opts.maxQuality ? 1.0 : JPEG_QUALITY }, [p.buf])
        .then((out) => { done++; onProgress({ phase: opts.clean ? "Cleaning lettering" : "Preparing pages", done, total: pages.length }); return out; })));
    const outPages = results.flat();
    cleaned = outPages.reduce((s, p) => s + (p.cleaned || 0), 0);
    const raw = file.name.replace(/\.[^.]+$/, "");
    if (!meta.title) {
      meta.title = raw.replace(/_+/g, " ").trim() || "Comic";
      if (meta.series) meta.title = meta.number ? `${meta.series} #${meta.number}` : meta.series;
    }
    onProgress({ phase: "Building EPUB", done: pages.length, total: pages.length });
    const blob = await buildEpub(outPages, meta, rtl);
    const name = (raw.replace(/[^\w\-. ]+/g, "").trim() || "comic") + ".epub";
    return { blob, name, pages: outPages.length, cleaned };
  } finally {
    pool.terminate();
  }
}
