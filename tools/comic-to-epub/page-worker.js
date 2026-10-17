/* Comic to EPUB - page worker.
 *
 * Runs off the main thread. For each page image it:
 *   1. splits two-page spreads into separate pages (optional, right-to-left aware),
 *   2. finds speech bubbles / caption boxes and cleans up their lettering in place:
 *      foggy gray strokes become solid ink, the bubble paper becomes flat, and thin
 *      strokes are thickened slightly so they survive being shrunk to screen size.
 * The artwork is never touched: only neutral, near-white bubble paper with text-like
 * marks on it is edited, and a page with no edits is passed back byte-for-byte.
 *
 * This is a port of the clean-up path of comic_convert.py (Bleubirddy.io).
 */

const WHITE_BG = 215;
const CLEAN_GROW = 0.035; // stroke thickening, as a fraction of letter height

self.onmessage = async (e) => {
  const { id, buf, split, rtl, clean, quality } = e.data;
  try {
    const out = await processPage(buf, { split, rtl, clean, quality });
    self.postMessage({ id, ok: true, pages: out }, out.map((p) => p.data));
  } catch (err) {
    self.postMessage({ id, ok: false, error: String(err && err.message || err) });
  }
};

async function processPage(buf, opts) {
  const bytes = new Uint8Array(buf);
  const isJpeg = bytes[0] === 0xff && bytes[1] === 0xd8;
  // colorSpaceConversion "none": by default browsers convert to the display's color
  // profile while decoding, which shifts every pixel of the artwork a little
  const bmp = await createImageBitmap(new Blob([buf]), { colorSpaceConversion: "none" });
  const W = bmp.width, H = bmp.height;
  const canvas = new OffscreenCanvas(W, H);
  const ctx = canvas.getContext("2d", { willReadFrequently: true });
  ctx.drawImage(bmp, 0, 0);
  bmp.close();

  let parts = [[0, W]];
  if (opts.split && W > H) {
    const half = Math.floor(W / 2);
    parts = opts.rtl ? [[half, W], [0, half]] : [[0, half], [half, W]];
  }
  const results = [];
  for (const [x0, x1] of parts) {
    const w = x1 - x0;
    const img = ctx.getImageData(x0, 0, w, H);
    let stats = { cleaned: 0 };
    if (opts.clean) stats = cleanLettering(img.data, w, H);
    if (stats.cleaned === 0 && parts.length === 1 && isJpeg) {
      // untouched page: keep the original file exactly
      results.push({ data: buf.slice(0), width: w, height: H, cleaned: 0 });
      continue;
    }
    const c2 = new OffscreenCanvas(w, H);
    c2.getContext("2d").putImageData(img, 0, 0);
    const blob = await c2.convertToBlob({ type: "image/jpeg", quality: opts.quality });
    results.push({ data: await blob.arrayBuffer(), width: w, height: H, cleaned: stats.cleaned });
  }
  return results;
}

/* ------------------------------------------------------------------ helpers */

function toGray(rgba, n) {
  const g = new Uint8Array(n);
  for (let i = 0, j = 0; i < n; i++, j += 4) {
    g[i] = (rgba[j] * 0.299 + rgba[j + 1] * 0.587 + rgba[j + 2] * 0.114 + 0.5) | 0;
  }
  return g;
}

function halve(g, W, H) {
  const w = W >> 1, h = H >> 1, s = new Uint8Array(w * h);
  for (let y = 0; y < h; y++) {
    const r0 = 2 * y * W, r1 = r0 + W;
    for (let x = 0; x < w; x++) {
      const a = 2 * x;
      s[y * w + x] = (g[r0 + a] + g[r0 + a + 1] + g[r1 + a] + g[r1 + a + 1] + 2) >> 2;
    }
  }
  return { s, w, h };
}

/* Connected components of a 0/1 mask. Returns labels (0 = background) and per-label
 * stats [x, y, w, h, area] (index 0 unused). */
function label(mask, w, h, conn8) {
  const lab = new Int32Array(w * h);
  const stats = [null];
  const stack = new Int32Array(w * h);
  let n = 0;
  for (let p = 0; p < w * h; p++) {
    if (!mask[p] || lab[p]) continue;
    n++;
    let sp = 0, minx = w, miny = h, maxx = 0, maxy = 0, area = 0;
    stack[sp++] = p; lab[p] = n;
    while (sp) {
      const q = stack[--sp];
      const x = q % w, y = (q / w) | 0;
      area++;
      if (x < minx) minx = x; if (x > maxx) maxx = x;
      if (y < miny) miny = y; if (y > maxy) maxy = y;
      for (let dy = -1; dy <= 1; dy++) {
        const yy = y + dy;
        if (yy < 0 || yy >= h) continue;
        for (let dx = -1; dx <= 1; dx++) {
          if (!dx && !dy) continue;
          if (!conn8 && dx && dy) continue;
          const xx = x + dx;
          if (xx < 0 || xx >= w) continue;
          const r = yy * w + xx;
          if (mask[r] && !lab[r]) { lab[r] = n; stack[sp++] = r; }
        }
      }
    }
    stats.push([minx, miny, maxx - minx + 1, maxy - miny + 1, area]);
  }
  return { lab, n, stats };
}

/* Inside of a shape: everything not reachable from the border through 0 pixels (4-conn). */
function fillHoles(comp, w, h) {
  const outside = new Uint8Array(w * h);
  const stack = [];
  const push = (x, y) => {
    const p = y * w + x;
    if (!comp[p] && !outside[p]) { outside[p] = 1; stack.push(p); }
  };
  for (let x = 0; x < w; x++) { push(x, 0); push(x, h - 1); }
  for (let y = 0; y < h; y++) { push(0, y); push(w - 1, y); }
  while (stack.length) {
    const p = stack.pop(), x = p % w, y = (p / w) | 0;
    if (x > 0) push(x - 1, y); if (x < w - 1) push(x + 1, y);
    if (y > 0) push(x, y - 1); if (y < h - 1) push(x, y + 1);
  }
  const filled = new Uint8Array(w * h);
  for (let i = 0; i < w * h; i++) filled[i] = outside[i] ? 0 : 1;
  return filled;
}

/* Exact Euclidean distance transform (Felzenszwalb & Huttenlocher), squared distances
 * to the nearest set pixel, with vertical distances scaled by sy (for elliptical dilation). */
function edt(mask, w, h, sy = 1) {
  const INF = 1e20;
  const f = new Float64Array(Math.max(w, h));
  const d = new Float64Array(w * h);
  const v = new Int32Array(Math.max(w, h));
  const z = new Float64Array(Math.max(w, h) + 1);
  const out = new Float64Array(Math.max(w, h));
  const pass = (n, get, set, scale) => {
    let k = 0;
    v[0] = 0; z[0] = -INF; z[1] = INF;
    for (let q = 1; q < n; q++) {
      let s;
      while (true) {
        const r = v[k];
        s = ((f[q] + (q * scale) ** 2) - (f[r] + (r * scale) ** 2)) / (2 * scale * (q - r));
        if (s <= z[k]) { k--; if (k < 0) { k = 0; break; } } else break;
      }
      k++; v[k] = q; z[k] = s; z[k + 1] = INF;
    }
    k = 0;
    for (let q = 0; q < n; q++) {
      while (z[k + 1] < q * scale) k++; // z is in scaled units
      const r = v[k];
      out[q] = ((q - r) * scale) ** 2 + f[r];
    }
    for (let q = 0; q < n; q++) set(q, out[q]);
  };
  for (let x = 0; x < w; x++) {
    for (let y = 0; y < h; y++) f[y] = mask[y * w + x] ? 0 : INF;
    pass(h, null, (q, val) => { d[q * w + x] = val; }, sy);
  }
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) f[x] = d[y * w + x];
    pass(w, null, (q, val) => { d[y * w + q] = val; }, 1);
  }
  return d;
}

/* Binary dilation by an ellipse with radii rx, ry (like cv2 MORPH_ELLIPSE). */
function dilateEllipse(mask, w, h, rx, ry) {
  const d = edt(mask, w, h, rx / ry);
  const out = new Uint8Array(w * h), r2 = rx * rx;
  for (let i = 0; i < w * h; i++) out[i] = d[i] <= r2 ? 1 : 0;
  return out;
}

/* Binary rectangular min/max filter (square k x k), `iters` times. */
function morphRect(mask, w, h, k, iters, erode) {
  let cur = mask;
  const r = k >> 1;
  for (let it = 0; it < iters; it++) {
    const tmp = new Uint8Array(w * h), out = new Uint8Array(w * h);
    for (let y = 0; y < h; y++) {
      for (let x = 0; x < w; x++) {
        let v = erode ? 1 : 0;
        for (let dx = -r; dx <= r; dx++) {
          const xx = x + dx;
          const val = xx < 0 || xx >= w ? (erode ? 0 : 0) : cur[y * w + xx];
          if (erode ? !val : val) { v = erode ? 0 : 1; break; }
        }
        tmp[y * w + x] = v;
      }
    }
    for (let y = 0; y < h; y++) {
      for (let x = 0; x < w; x++) {
        let v = erode ? 1 : 0;
        for (let dy = -r; dy <= r; dy++) {
          const yy = y + dy;
          const val = yy < 0 || yy >= h ? 0 : tmp[yy * w + x];
          if (erode ? !val : val) { v = erode ? 0 : 1; break; }
        }
        out[y * w + x] = v;
      }
    }
    cur = out;
  }
  return cur;
}

function percentile(arr, p) {
  if (!arr.length) return 0;
  const a = Float64Array.from(arr).sort();
  const pos = (a.length - 1) * p / 100, lo = Math.floor(pos), hi = Math.ceil(pos);
  return a[lo] + (a[hi] - a[lo]) * (pos - lo);
}
const median = (arr) => percentile(arr, 50);

/* ---------------------------------------------------------- bubble detection */

function findBubbles(rgba, gray, W, H) {
  const { s: small, w, h } = halve(gray, W, H);
  const page = w * h;
  const found = [];
  for (const thr of [215, 180]) {
    const bright = new Uint8Array(w * h);
    for (let i = 0; i < w * h; i++) bright[i] = small[i] > thr ? 1 : 0;
    const { lab, n, stats } = label(bright, w, h, false);
    for (let i = 1; i <= n; i++) {
      const [x, y, bw, bh] = stats[i];
      if (bw < 12 || bh < 10 || bw > 0.7 * w || bh > 0.5 * h) continue;
      if ((x === 0) + (y === 0) + (x + bw === w) + (y + bh === h) >= 2) continue;
      const comp = new Uint8Array(bw * bh);
      for (let yy = 0; yy < bh; yy++)
        for (let xx = 0; xx < bw; xx++) comp[yy * bw + xx] = lab[(y + yy) * w + x + xx] === i ? 1 : 0;
      const filled = fillHoles(comp, bw, bh);
      let farea = 0, hcount = 0;
      const holes = new Uint8Array(bw * bh);
      for (let k = 0; k < bw * bh; k++) {
        if (filled[k]) { farea++; if (!comp[k]) { holes[k] = 1; hcount++; } }
      }
      if (!(0.0006 * page < farea && farea < 0.1 * page)) continue;
      if (farea / (bw * bh) < 0.36) continue; // compact shapes (0.36 lets joined bubbles through)
      const hf = hcount / farea;
      if (!(0.025 < hf && hf < 0.5)) continue;
      const hl = label(holes, bw, bh, true);
      if (hl.n < 5) continue;
      let smallMarks = 0;
      const areas = [];
      for (let j = 1; j <= hl.n; j++) {
        const st = hl.stats[j];
        if (st[3] < 0.35 * bh && st[2] < 0.5 * bw) smallMarks++;
        areas.push(st[4]);
      }
      if (smallMarks < 5 || median(areas) > 0.02 * farea) continue;
      const box = [x, y, bw, bh];
      if (found.some((f) => overlap(box, f.box) > 0.3)) continue;
      found.push({ box, filled });
    }
  }
  const out = [];
  for (const { box: [x, y, bw, bh], filled } of found) {
    const X = x * 2, Y = y * 2;
    const Wd = Math.min(W - X, bw * 2), Hd = Math.min(H - Y, bh * 2);
    let interior = new Uint8Array(Wd * Hd);
    for (let yy = 0; yy < Hd; yy++)
      for (let xx = 0; xx < Wd; xx++)
        interior[yy * Wd + xx] = filled[Math.min(bh - 1, yy >> 1) * bw + Math.min(bw - 1, xx >> 1)];
    interior = morphRect(interior, Wd, Hd, 3, 2, true); // keep off the outline
    const g = new Uint8Array(Wd * Hd);
    for (let yy = 0; yy < Hd; yy++)
      for (let xx = 0; xx < Wd; xx++) g[yy * Wd + xx] = gray[(Y + yy) * W + X + xx];
    const inVals = [];
    for (let k = 0; k < Wd * Hd; k++) if (interior[k]) inVals.push(g[k]);
    if (!inVals.length) continue;
    const p60 = percentile(inVals, 60);
    const bgL = [], bgR = [], bgG = [], bgB = [];
    for (let yy = 0; yy < Hd; yy++)
      for (let xx = 0; xx < Wd; xx++) {
        const k = yy * Wd + xx;
        if (interior[k] && g[k] >= p60) {
          const j = ((Y + yy) * W + X + xx) * 4;
          bgL.push(g[k]); bgR.push(rgba[j]); bgG.push(rgba[j + 1]); bgB.push(rgba[j + 2]);
        }
      }
    const bg = [median(bgR), median(bgG), median(bgB)].map(Math.round);
    const bgLum = median(bgL);
    const ink = new Uint8Array(Wd * Hd);
    for (let k = 0; k < Wd * Hd; k++) ink[k] = interior[k] && g[k] < bgLum - 60 ? 1 : 0;
    const b = { x: X, y: Y, w: Wd, h: Hd, interior, ink, g, bg, letterH: 0 };
    measureText(b);
    if (b.letterH) out.push(b);
  }
  return out;
}

function overlap(a, b) {
  const ix = Math.max(0, Math.min(a[0] + a[2], b[0] + b[2]) - Math.max(a[0], b[0]));
  const iy = Math.max(0, Math.min(a[1] + a[3], b[1] + b[3]) - Math.max(a[1], b[1]));
  return (ix * iy) / Math.min(a[2] * a[3], b[2] * b[3]);
}

function measureText(b) {
  const { n, stats } = label(b.ink, b.w, b.h, true);
  if (!n) return;
  const hts = stats.slice(1).map((s) => s[3]);
  const p90 = percentile(hts, 90);
  const big = hts.filter((v) => v >= 0.6 * p90);
  const med = median(big);
  const kept = stats.slice(1).filter((s) => s[3] > 0.3 * med && s[3] < 3 * med && s[4] > 3);
  if (kept.length < 3) return;
  const sh = kept.map((s) => s[3]);
  const p90k = percentile(sh, 90);
  b.letterH = median(sh.filter((v) => v >= 0.6 * p90k));
}

/* Group lettering marks into text blocks (two joined bubbles -> two blocks). */
function textBlocks(b) {
  const { w, h } = b;
  const { lab, n, stats } = label(b.ink, w, h, true);
  if (!n || !b.letterH) return [];
  const keep = new Uint8Array(n + 1), tinyL = new Uint8Array(n + 1);
  for (let i = 1; i <= n; i++) {
    const hh = stats[i][3];
    keep[i] = hh > 0.25 * b.letterH && hh < 2.5 * b.letterH && stats[i][4] > 2 ? 1 : 0;
    tinyL[i] = !keep[i] && hh <= 0.25 * b.letterH ? 1 : 0;
  }
  const marks = new Uint8Array(w * h);
  for (let k = 0; k < w * h; k++) marks[k] = keep[lab[k]] ? 1 : 0;
  // dots and punctuation close to letters belong to the text
  const kt = Math.max(3, Math.floor(b.letterH * 1.3)) / 2;
  const near = dilateEllipse(marks, w, h, kt, kt);
  for (let k = 0; k < w * h; k++) if (lab[k] && tinyL[lab[k]] && near[k]) marks[k] = 1;
  const k9 = Math.max(3, Math.floor(b.letterH * 0.9)) / 2;
  const grown = dilateEllipse(marks, w, h, k9, k9 * 1.4);
  const gl = label(grown, w, h, true);
  const blocks = [];
  for (let i = 1; i <= gl.n; i++) {
    const m = new Uint8Array(w * h);
    let cnt = 0, x0 = w, y0 = h, x1 = 0, y1 = 0;
    for (let k = 0; k < w * h; k++) {
      if (marks[k] && gl.lab[k] === i) {
        m[k] = 1; cnt++;
        const x = k % w, y = (k / w) | 0;
        if (x < x0) x0 = x; if (x > x1) x1 = x; if (y < y0) y0 = y; if (y > y1) y1 = y;
      }
    }
    if (cnt < 20) continue;
    blocks.push({ mask: m, box: [x0, y0, x1 + 1, y1 + 1] });
  }
  return blocks;
}

function looksLikeText(blk, b) {
  const { n } = label(blk.mask, b.w, b.h, true);
  return n >= 4 && blk.box[2] - blk.box[0] >= 2 * b.letterH;
}

/* Real bubbles have flat paper behind the letters; bright patches of art don't. */
function isLetteringArea(b, bgL) {
  const zone = morphRect(b.ink, b.w, b.h, 5, 2, false);
  let paper = 0, flat = 0;
  for (let k = 0; k < b.w * b.h; k++) {
    if (b.interior[k] && !zone[k]) { paper++; if (Math.abs(b.g[k] - bgL) < 22) flat++; }
  }
  return paper >= 50 && flat / paper >= 0.88;
}

/* ------------------------------------------------------------- clean-up */

function cleanLettering(rgba, W, H) {
  const gray = toGray(rgba, W * H);
  let cleaned = 0;
  const lhMin = 0.0035 * H, lhMax = 0.013 * H;
  for (const b of findBubbles(rgba, gray, W, H)) {
    if (!(b.letterH >= lhMin && b.letterH <= lhMax)) continue;
    const bgL = b.bg[0] * 0.299 + b.bg[1] * 0.587 + b.bg[2] * 0.114;
    // only neutral, near-white paper: tan skin or colored art with fine lines can look
    // like lettering, and the art must never be touched
    if (bgL < WHITE_BG || Math.max(...b.bg) - Math.min(...b.bg) >= 40) continue;
    if (!isLetteringArea(b, bgL)) continue;
    const blocks = textBlocks(b).filter((blk) => looksLikeText(blk, b));
    if (!blocks.length) continue;
    const done = new Uint8Array(b.w * b.h);
    for (const blk of blocks) {
      const [x0, y0, x1, y1] = blk.box;
      const pad = Math.floor(b.letterH * 0.6);
      // ink color: the darkest quarter of the lettering, made solid (same hue)
      const vals = [];
      for (let k = 0; k < b.w * b.h; k++) if (blk.mask[k]) vals.push(b.g[k]);
      const p25 = percentile(vals, 25);
      const ir = [], ig = [], ib = [];
      for (let k = 0; k < b.w * b.h; k++) {
        if (blk.mask[k] && b.g[k] <= p25) {
          const j = ((b.y + ((k / b.w) | 0)) * W + b.x + (k % b.w)) * 4;
          ir.push(rgba[j]); ig.push(rgba[j + 1]); ib.push(rgba[j + 2]);
        }
      }
      const ink = ir.length ? [median(ir) * 0.6, median(ig) * 0.6, median(ib) * 0.6] : [0, 0, 0];
      const region = new Uint8Array(b.w * b.h);
      for (let y = Math.max(0, y0 - pad); y < Math.min(b.h, y1 + pad); y++)
        for (let x = Math.max(0, x0 - pad); x < Math.min(b.w, x1 + pad); x++) {
          const k = y * b.w + x;
          if (b.interior[k] && !done[k]) { region[k] = 1; done[k] = 1; }
        }
      smoothstepInk(rgba, W, b, region, ink, Math.max(0, Math.round(b.letterH * CLEAN_GROW)));
      cleaned++;
    }
  }
  return { cleaned };
}

/* Map each pixel to a mix of paper and solid ink by how dark it is (steep curve): fuzzy gray
 * strokes become solid, the paper becomes flat; then thicken strokes by `grow` px. */
function smoothstepInk(rgba, W, b, region, ink, grow) {
  const { w, h } = b;
  const bgL = b.bg[0] * 0.299 + b.bg[1] * 0.587 + b.bg[2] * 0.114;
  const a = new Float32Array(w * h);
  for (let k = 0; k < w * h; k++) {
    if (!region[k]) continue;
    const j = ((b.y + ((k / w) | 0)) * W + b.x + (k % w)) * 4;
    const g = rgba[j] * 0.299 + rgba[j + 1] * 0.587 + rgba[j + 2] * 0.114;
    const t = Math.min(1, Math.max(0, (bgL - g) / Math.max(bgL - 40, 1)));
    let v = Math.min(1, Math.max(0, (t - 0.18) / 0.45));
    a[k] = v * v * (3 - 2 * v);
  }
  let alpha = a;
  if (grow > 0) {
    alpha = new Float32Array(a);
    const r = grow, r2 = (r + 0.5) * (r + 0.5);
    for (let y = 0; y < h; y++)
      for (let x = 0; x < w; x++) {
        const k = y * w + x;
        if (!region[k]) continue;
        let m = 0;
        for (let dy = -r; dy <= r; dy++) {
          const yy = y + dy; if (yy < 0 || yy >= h) continue;
          for (let dx = -r; dx <= r; dx++) {
            if (dx * dx + dy * dy > r2) continue;
            const xx = x + dx; if (xx < 0 || xx >= w) continue;
            const v = a[yy * w + xx]; if (v > m) m = v;
          }
        }
        alpha[k] = Math.max(a[k], m * 0.92);
      }
  }
  for (let k = 0; k < w * h; k++) {
    if (!region[k]) continue;
    const j = ((b.y + ((k / w) | 0)) * W + b.x + (k % w)) * 4;
    const v = alpha[k];
    rgba[j] = b.bg[0] * (1 - v) + ink[0] * v;
    rgba[j + 1] = b.bg[1] * (1 - v) + ink[1] * v;
    rgba[j + 2] = b.bg[2] * (1 - v) + ink[2] * v;
  }
}
