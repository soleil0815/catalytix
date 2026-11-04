/* Catalytix – EAN-13 detection (ISBN barcodes) without third-party libraries.
 *
 * Used in browsers that have no built-in BarcodeDetector (Firefox, Safari).
 * Reads several rows (and columns) of a camera frame, determines the bar widths with
 * subpixel accuracy and decodes them via edge distances (robust against
 * "bleeding" bars). Returns a result only with a valid check digit.
 *
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */
(function (root) {
  "use strict";

  // Edge distances (bar+space, space+bar) in modules -> digit
  // L/R codes and G codes; 1/7 and 2/8 have the same distances and are told apart by bar width.
  const L_EDGES = { "53": 0, "44": [1, 7], "33": [2, 8], "55": 3, "24": 4, "35": 5, "22": 6, "42": 9 };
  const G_EDGES = { "23": 0, "34": [1, 7], "43": [2, 8], "25": 3, "54": 4, "45": 5, "52": 6, "32": 9 };
  const PARITY = ["LLLLLL", "LLGLGG", "LLGGLG", "LLGGGL", "LGLLGG", "LGGLLG", "LGGGLL", "LGLGLG", "LGLGGL", "LGGLGL"];

  // Read one character from 4 runs r[i..i+3]. barsFirst: R codes start with a bar.
  function readDigit(r, i, barsFirst) {
    const a = r[i], b = r[i + 1], c = r[i + 2], d = r[i + 3];
    const sum = a + b + c + d;
    if (sum <= 0) return null;
    const k = 7 / sum;
    const e1 = Math.round((a + b) * k), e2 = Math.round((b + c) * k);
    if (e1 < 2 || e1 > 5 || e2 < 2 || e2 > 5) return null;
    const key = "" + e1 + e2;
    const bars = (barsFirst ? a + c : b + d) * k;
    const pick = (hit, wideBarsMeansFirst) => {
      if (hit === undefined) return undefined;
      if (typeof hit === "number") return hit;
      // L: 1/2 have narrow bars (3 modules), 7/8 wide (5). G and R: 1/2 wide (4), 7/8 narrow (2).
      return wideBarsMeansFirst ? (bars > 3 ? hit[0] : hit[1]) : (bars < 4 ? hit[0] : hit[1]);
    };
    if (barsFirst) {
      const v = pick(L_EDGES[key], true);
      return v === undefined ? null : { d: v, p: "R" };
    }
    const l = pick(L_EDGES[key], false);
    if (l !== undefined) return { d: l, p: "L" };
    const g = pick(G_EDGES[key], true);
    if (g !== undefined) return { d: g, p: "G" };
    return null;
  }

  function checksumOk(digits) {
    let s = 0;
    for (let i = 0; i < 12; i++) s += digits[i] * (i % 2 ? 3 : 1);
    return (10 - (s % 10)) % 10 === digits[12];
  }

  function near(x, m, lo, hi) { return x >= m * lo && x <= m * hi; }

  // Decode runs (widths, alternating light/dark, starting with light)
  function decodeRuns(r) {
    // try every start position: a noise spike in the white can shift the light/dark count
    for (let s = 1; s + 59 <= r.length; s++) {
      let total = 0;
      for (let j = s; j < s + 59; j++) total += r[j];
      const m = total / 95;
      // quiet zone left and right (a narrow noise spike right at the edge is allowed)
      const quietL = r[s - 1] >= 3 * m || (s >= 2 && r[s - 1] < 0.6 * m && r[s - 2] >= 3 * m);
      const quietR = s + 59 >= r.length || r[s + 59] >= 3 * m ||
        (r[s + 59] < 0.6 * m && (s + 60 >= r.length || r[s + 60] >= 3 * m));
      if (!quietL || !quietR) continue;
      // guard and center patterns
      let ok = true;
      for (const j of [s, s + 1, s + 2, s + 27, s + 28, s + 29, s + 30, s + 31, s + 56, s + 57, s + 58]) {
        if (!near(r[j], m, 0.35, 2.2)) { ok = false; break; }
      }
      if (!ok) continue;
      let left = 0, right = 0;
      for (let j = s + 3; j < s + 27; j++) left += r[j];
      for (let j = s + 32; j < s + 56; j++) right += r[j];
      if (!near(left, 42 * m, 0.8, 1.2) || !near(right, 42 * m, 0.8, 1.2)) continue;

      const digits = [0];
      let parity = "";
      for (let k = 0; k < 6 && ok; k++) {
        const x = readDigit(r, s + 3 + 4 * k, false);
        if (!x) ok = false; else { digits.push(x.d); parity += x.p; }
      }
      for (let k = 0; k < 6 && ok; k++) {
        const x = readDigit(r, s + 32 + 4 * k, true);
        if (!x) ok = false; else digits.push(x.d);
      }
      if (!ok) continue;
      const first = PARITY.indexOf(parity);
      if (first < 0) continue;
      digits[0] = first;
      if (checksumOk(digits)) return digits.join("");
    }
    return null;
  }

  // brightness profile -> runs with subpixel edges
  function runsFromProfile(p, cfg) {
    cfg = cfg || {};
    const n = p.length;
    if (n < 100) return null;
    // smooth slightly
    const s = new Float32Array(n);
    s[0] = p[0]; s[n - 1] = p[n - 1];
    for (let i = 1; i < n - 1; i++) s[i] = cfg.smooth === false ? p[i] : (p[i - 1] + 2 * p[i] + p[i + 1]) / 4;
    // local mean as threshold (sliding window)
    const w = Math.max(12, Math.round(n / (cfg.win || 10)));
    const pre = new Float64Array(n + 1);
    for (let i = 0; i < n; i++) pre[i + 1] = pre[i] + s[i];
    let lo = 255, hi = 0;
    for (let i = 0; i < n; i++) { if (s[i] < lo) lo = s[i]; if (s[i] > hi) hi = s[i]; }
    if (hi - lo < 30) return null; // no contrast
    const margin = (hi - lo) * (cfg.margin === undefined ? 0.04 : cfg.margin);
    const thr = new Float32Array(n);
    for (let i = 0; i < n; i++) {
      const a = Math.max(0, i - w), b = Math.min(n, i + w + 1);
      thr[i] = (pre[b] - pre[a]) / (b - a);
    }
    // state with hysteresis: dark below thr-margin, light above thr+margin
    const runs = [];
    let dark = false, last = 0;
    for (let i = 1; i < n; i++) {
      const v = s[i];
      const flip = dark ? v > thr[i] + margin : v < thr[i] - margin;
      if (!flip) continue;
      // interpolate the edge between i-1 and i to the threshold
      const a = s[i - 1], t = thr[i];
      let x = i;
      if (a !== v) x = i - 1 + Math.min(1, Math.max(0, (t - a) / (v - a)));
      runs.push(x - last);
      last = x;
      dark = !dark;
    }
    runs.push(n - last);
    return runs; // beginnt hell
  }

  // Find edges via the brightness gradient (robust against blur): extrema of the derivative,
  // alternating light->dark and dark->light, position refined to subpixel via parabola.
  function runsFromEdges(p, cfg) {
    const n = p.length;
    if (n < 100) return null;
    let lo = 255, hi = 0;
    for (let i = 0; i < n; i++) { if (p[i] < lo) lo = p[i]; if (p[i] > hi) hi = p[i]; }
    if (hi - lo < 30) return null;
    const g = new Float32Array(n);
    const d = cfg.wide ? 2 : 1;
    for (let i = d; i < n - d; i++) g[i] = (p[i + d] - p[i - d]) / (2 * d);
    const minStep = (hi - lo) * (cfg.edge || 0.06);
    const edges = []; // {x, sign}
    for (let i = 2; i < n - 2; i++) {
      const a = Math.abs(g[i]);
      if (a < minStep || a < Math.abs(g[i - 1]) || a <= Math.abs(g[i + 1])) continue;
      const sign = g[i] < 0 ? -1 : 1; // -1: light -> dark
      const y0 = Math.abs(g[i - 1]), y1 = a, y2 = Math.abs(g[i + 1]);
      const den = y0 - 2 * y1 + y2;
      const x = i + (den !== 0 ? 0.5 * (y0 - y2) / den : 0);
      const prev = edges[edges.length - 1];
      if (prev && prev.sign === sign) { if (a > prev.a) { prev.x = x; prev.a = a; } continue; }
      edges.push({ x, sign, a });
    }
    while (edges.length && edges[0].sign !== -1) edges.shift();
    if (edges.length < 60) return null;
    const runs = [edges[0].x];
    for (let k = 1; k < edges.length; k++) runs.push(edges[k].x - edges[k - 1].x);
    runs.push(n - edges[edges.length - 1].x);
    return runs; // beginnt hell
  }

  // methods in turn; the first valid result (with check digit) counts
  let CFGS = [{ mode: "edge" }, { margin: 0.015, smooth: false, win: 20 }, { mode: "edge", wide: true }];

  function decodeProfile(p) {
    const rev = Array.from(p).reverse(); // barcode is upside down
    for (const cfg of CFGS) {
      for (const prof of [p, rev]) {
        const r = cfg.mode === "edge" ? runsFromEdges(prof, cfg) : runsFromProfile(prof, cfg);
        const code = r && decodeRuns(r);
        if (code) return code;
      }
    }
    return null;
  }

  /**
   * Decode a grayscale or RGBA image.
   * @param {Uint8ClampedArray|Uint8Array} data  RGBA (4 bytes/pixel) or gray (1 byte/pixel)
   * @returns {string|null} 13-digit code with a valid check digit
   */
  function decodeImage(data, width, height, opts) {
    const channels = data.length >= width * height * 4 ? 4 : 1;
    const lum = (x, y) => {
      const o = (y * width + x) * channels;
      return channels === 1 ? data[o] : (data[o] * 299 + data[o + 1] * 587 + data[o + 2] * 114) / 1000;
    };
    const steps = (opts && opts.lines) || 9;
    // rows around the image center (barcode horizontal), then columns (barcode vertical)
    for (let k = 0; k < steps; k++) {
      const off = (k % 2 ? 1 : -1) * Math.ceil(k / 2) * 0.06;
      const y = Math.round(height * (0.5 + off));
      if (y < 0 || y >= height) continue;
      const row = new Float32Array(width);
      for (let x = 0; x < width; x++) row[x] = lum(x, y);
      const c = decodeProfile(row);
      if (c) return c;
    }
    if (opts && opts.rowsOnly) return null;
    for (let k = 0; k < steps; k++) {
      const off = (k % 2 ? 1 : -1) * Math.ceil(k / 2) * 0.06;
      const x = Math.round(width * (0.5 + off));
      if (x < 0 || x >= width) continue;
      const col = new Float32Array(height);
      for (let y = 0; y < height; y++) col[y] = lum(x, y);
      const c = decodeProfile(col);
      if (c) return c;
    }
    return null;
  }

  const api = { decodeImage, decodeProfile, _setCfgs: (c) => { CFGS = c; } };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.CatalytixEAN13 = api;
})(typeof window !== "undefined" ? window : this);
