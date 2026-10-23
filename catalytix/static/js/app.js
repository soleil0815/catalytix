/* Catalytix – Frontend-Logik (ohne Build-Schritt, ohne Abhängigkeiten). */
(function () {
  "use strict";
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const CSRF = ($('meta[name="csrf-token"]') || {}).content || "";
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* ignorieren */ } },
  };
  /* Übersetzung: T(<deutscher Text>, {name: wert}) – die Texte kommen vom Server (#i18n-strings). */
  const I18N = (() => {
    const el = document.getElementById("i18n-strings");
    try { return el ? JSON.parse(el.textContent) : {}; } catch (e) { return {}; }
  })();
  const T = (msg, vars) => String(I18N[msg] || msg).replace(/\{(\w+)\}/g,
    (m, k) => (vars && Object.prototype.hasOwnProperty.call(vars, k) ? String(vars[k]) : m));
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  /* ------------------------------------------------------------ Grundfunktionen */
  $$("[data-theme-toggle]").forEach((btn) => btn.addEventListener("click", () => {
    const root = document.documentElement;
    const dark = root.dataset.theme ? root.dataset.theme === "dark"
      : window.matchMedia("(prefers-color-scheme: dark)").matches;
    root.dataset.theme = dark ? "light" : "dark";
    store.set("theme", root.dataset.theme);
  }));

  $$("[data-nav-toggle]").forEach((btn) => btn.addEventListener("click", (e) => {
    e.stopPropagation();
    document.body.classList.toggle("nav-open");
  }));
  document.addEventListener("click", (e) => {
    if (document.body.classList.contains("nav-open") && !e.target.closest(".sidebar")) {
      document.body.classList.remove("nav-open");
    }
    // Benutzermenü schließen, wenn daneben getippt wird
    $$(".user-menu[open]").forEach((m) => { if (!m.contains(e.target)) m.removeAttribute("open"); });
  });

  document.addEventListener("submit", (e) => {
    const msg = e.target.dataset && e.target.dataset.confirm;
    if (msg && !window.confirm(msg)) e.preventDefault();
  }, true);

  // Kleine Helfer statt eingebetteter on…-Attribute (erlaubt eine strenge Content-Security-Policy)
  document.addEventListener("submit", (e) => {
    if (e.target.hasAttribute && e.target.hasAttribute("data-no-submit")) e.preventDefault();
  }, true);
  document.addEventListener("click", (e) => {
    const t = e.target.closest("[data-confirm-click],[data-dismiss],[data-print],[data-fill-previous]");
    if (!t) return;
    if (t.hasAttribute("data-confirm-click") && !window.confirm(t.dataset.confirmClick)) { e.preventDefault(); e.stopPropagation(); }
    if (t.hasAttribute("data-dismiss")) t.parentElement.remove();
    if (t.hasAttribute("data-print")) window.print();
    if (t.hasAttribute("data-fill-previous") && t.previousElementSibling) t.previousElementSibling.value = t.dataset.fillPrevious;
  }, true);
  $$("[data-enter-clicks-next]").forEach((el) => el.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); if (el.nextElementSibling) el.nextElementSibling.click(); }
  }));
  $$("[data-autosubmit]").forEach((el) => el.addEventListener("change", () => el.form && el.form.submit()));
  // Bilder, die nicht laden (z. B. Cover), ausblenden
  document.addEventListener("error", (e) => {
    if (e.target.tagName === "IMG" && e.target.hasAttribute("data-hide-broken")) e.target.remove();
  }, true);
  $$("img[data-hide-broken]").forEach((img) => { if (img.complete && img.naturalWidth === 0) img.remove(); });
  const scrollTarget = $("[data-scroll-to]");
  if (scrollTarget) {
    const el = document.getElementById(scrollTarget.dataset.scrollTo);
    if (el) el.scrollIntoView({ block: scrollTarget.dataset.scrollBlock || "start" });
  }

  $$(".flash").forEach((el) => {
    const ms = el.classList.contains("error") ? 12000 : 6000;
    setTimeout(() => { el.style.transition = "opacity .4s"; el.style.opacity = "0"; setTimeout(() => el.remove(), 400); }, ms);
  });

  // Kachel-/Listenansicht im Katalog
  const viewTarget = $("[data-view-target]");
  if (viewTarget) {
    const apply = (v) => {
      $(".view-grid", viewTarget).classList.toggle("hide", v === "list");
      $(".view-list", viewTarget).classList.toggle("hide", v !== "list");
      $$("[data-view]").forEach((b) => b.classList.toggle("on", b.dataset.view === v));
    };
    apply(store.get("catalogView") || "grid");
    $$("[data-view]").forEach((b) => b.addEventListener("click", () => { store.set("catalogView", b.dataset.view); apply(b.dataset.view); }));
  } else {
    $$("[data-view]").forEach((b) => b.closest(".seg") && b.closest(".seg").classList.add("hide"));
  }

  // Listen filtern (z. B. Gruppenmitglieder)
  $$("[data-filter-list]").forEach((input) => {
    const list = $(input.dataset.filterList);
    input.addEventListener("input", () => {
      const q = input.value.toLowerCase();
      $$("[data-filter-item]", list).forEach((el) => { el.style.display = el.textContent.toLowerCase().includes(q) ? "" : "none"; });
    });
  });

  // Vorlagen für den Verzeichnisdienst
  const PRESETS = {
    ad: { ldap_user_attr: "sAMAccountName", ldap_user_filter: "(&(objectClass=user)(!(userAccountControl:1.2.840.113556.1.4.803:=2)))", nested: true },
    samba: { ldap_user_attr: "sAMAccountName", ldap_user_filter: "(&(objectClass=user)(!(userAccountControl:1.2.840.113556.1.4.803:=2)))", nested: true },
    openldap: { ldap_user_attr: "uid", ldap_user_filter: "(objectClass=posixAccount)", nested: false },
  };
  $$("[data-ldap-preset]").forEach((btn) => btn.addEventListener("click", () => {
    const p = PRESETS[btn.dataset.ldapPreset];
    const form = btn.closest("form");
    form.elements.ldap_user_attr.value = p.ldap_user_attr;
    form.elements.ldap_user_filter.value = p.ldap_user_filter;
    form.elements.ldap_nested_groups.checked = p.nested;
    $$("[data-ldap-preset]").forEach((b) => b.classList.toggle("on", b === btn));
  }));

  // AD-Gruppe per Klick in die passende Rollenliste übernehmen
  $$("[data-add-rule]").forEach((btn) => btn.addEventListener("click", () => {
    const box = $(`[data-rule-box="${btn.dataset.addRule}"]`);
    if (!box) return;
    const name = btn.dataset.name;
    const lines = box.value.split("\n").map((l) => l.trim()).filter(Boolean);
    if (!lines.some((l) => l.toLowerCase() === name.toLowerCase())) lines.push(name);
    box.value = lines.join("\n");
    btn.classList.add("on");
    box.style.transition = "box-shadow .3s";
    box.style.boxShadow = "0 0 0 3px color-mix(in srgb, var(--accent) 35%, transparent)";
    setTimeout(() => { box.style.boxShadow = ""; }, 700);
  }));

  // SMTP: Standard-Port passend zur Verschlüsselung setzen
  const secSel = $("[data-smtp-security]"), portIn = $("[data-smtp-port]");
  if (secSel && portIn) {
    const defaults = { starttls: "587", ssl: "465", none: "25" };
    secSel.addEventListener("change", () => {
      if (!portIn.value || Object.values(defaults).includes(portIn.value)) portIn.value = defaults[secSel.value];
    });
  }

  /* ------------------------------------------------------------ Erscheinungsbild: Live-Vorschau */
  const brandForm = $("[data-branding-form]");
  if (brandForm) {
    const root = document.documentElement.style;
    const rgb = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));
    const hex = (c) => "#" + c.map((v) => Math.max(0, Math.min(255, Math.round(v))).toString(16).padStart(2, "0")).join("");
    const mix = (a, b, t) => { const x = rgb(a), y = rgb(b); return hex(x.map((v, i) => v + (y[i] - v) * t)); };
    const lum = (h) => { const c = rgb(h).map((v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; });
      return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]; };
    const contrast = (a, b) => { const [x, y] = [lum(a), lum(b)].sort((m, n) => n - m); return (x + 0.05) / (y + 0.05); };
    const ink = (bg) => (contrast(bg, "#ffffff") >= contrast(bg, "#14191a") ? "#ffffff" : "#14191a");
    const apply = {
      accent(c) { root.setProperty("--accent", c); root.setProperty("--accent-ink", ink(c)); root.setProperty("--accent-soft", mix(c, "#fffdf8", 0.84)); },
      brass(c) { root.setProperty("--brass", c); root.setProperty("--brass-ink", ink(c)); root.setProperty("--brass-soft", mix(c, "#fffdf8", 0.82));
        root.setProperty("--auth-eyebrow", mix(c, "#ffffff", 0.3)); },
      side(c) { const dark = lum(c) < 0.3; const sink = dark ? "#ece7dc" : "#1d2421";
        root.setProperty("--side", c); root.setProperty("--side-2", dark ? mix(c, "#ffffff", 0.08) : mix(c, "#000000", 0.07));
        root.setProperty("--side-ink", sink); root.setProperty("--side-muted", mix(sink, c, 0.38));
        root.setProperty("--auth-a", mix(c, "#ffffff", 0.06)); root.setProperty("--auth-b", mix(c, "#000000", 0.35)); },
    };
    $$("[data-color-picker]", brandForm).forEach((picker) => {
      const key = picker.dataset.colorPicker;
      picker.addEventListener("input", () => {
        $(`[data-color-value="${key}"]`).value = picker.value;
        $(`[data-color-label="${key}"]`).textContent = picker.value;
        apply[key](picker.value);
      });
    });
    $$("[data-color-reset]", brandForm).forEach((btn) => btn.addEventListener("click", () => {
      const key = btn.dataset.colorReset;
      $(`[data-color-value="${key}"]`).value = "";
      $(`[data-color-label="${key}"]`).textContent = T("Standard");
      $(`[data-color-picker="${key}"]`).value = btn.dataset.default;
      apply[key](btn.dataset.default);
    }));
    const preview = $("[data-login-preview]");
    $$("[data-file-preview]", brandForm).forEach((input) => input.addEventListener("change", () => {
      const file = input.files && input.files[0];
      if (!file) return;
      const url = URL.createObjectURL(file);
      if (input.dataset.filePreview === "logo") {
        $$("[data-preview-logo]").forEach((img) => { img.src = url; img.hidden = false; });
        $$("[data-preview-nologo]").forEach((el) => { el.hidden = true; });
        const ll = $("[data-preview-loginlogo]"); if (ll) ll.hidden = false;
      } else {
        preview.style.setProperty("--auth-bg", `url("${url}")`);
        preview.classList.add("has-bg");
      }
    }));
    const dim = $("[data-dim]", brandForm);
    if (dim) dim.addEventListener("input", () => {
      preview.style.setProperty("--auth-dim", dim.value / 100);
      $("[data-dim-label]").textContent = dim.value;
    });
    const shelf = $("[data-shelf-toggle]", brandForm);
    if (shelf) shelf.addEventListener("change", () => { $("[data-preview-shelf]").hidden = !shelf.checked; });
    const plate = $("[data-plate-toggle]", brandForm);
    if (plate) plate.addEventListener("change", () => { const ll = $("[data-preview-loginlogo]"); if (ll) ll.classList.toggle("plate", plate.checked); });
    $$("[data-bind]", brandForm).forEach((el) => el.addEventListener("input", () => {
      $(`[data-preview-${el.dataset.bind}]`).textContent = el.value;
    }));
  }

  /* ------------------------------------------------------------ Sicherheitsschlüssel (WebAuthn) */
  const b64u = {
    dec(str) {
      const s = str.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((str.length + 3) % 4);
      return Uint8Array.from(atob(s), (c) => c.charCodeAt(0));
    },
    enc(buf) {
      const bytes = new Uint8Array(buf);
      let bin = "";
      bytes.forEach((b) => { bin += String.fromCharCode(b); });
      return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
    },
  };
  const webauthnOk = () => window.isSecureContext && !!window.PublicKeyCredential;

  function creationOptions(o) {
    const pk = { ...o.publicKey };
    pk.challenge = b64u.dec(pk.challenge);
    pk.user = { ...pk.user, id: b64u.dec(pk.user.id) };
    pk.excludeCredentials = (pk.excludeCredentials || []).map((c) => ({ ...c, id: b64u.dec(c.id) }));
    return { publicKey: pk };
  }
  function requestOptions(o) {
    const pk = { ...o.publicKey };
    pk.challenge = b64u.dec(pk.challenge);
    pk.allowCredentials = (pk.allowCredentials || []).map((c) => ({ ...c, id: b64u.dec(c.id) }));
    return { publicKey: pk };
  }
  function credentialJSON(cred) {
    const r = cred.response;
    const out = { id: cred.id, rawId: b64u.enc(cred.rawId), type: cred.type,
      authenticatorAttachment: cred.authenticatorAttachment || null,
      clientExtensionResults: cred.getClientExtensionResults ? cred.getClientExtensionResults() : {}, response: {} };
    out.response.clientDataJSON = b64u.enc(r.clientDataJSON);
    if (r.attestationObject) out.response.attestationObject = b64u.enc(r.attestationObject);
    if (r.authenticatorData) out.response.authenticatorData = b64u.enc(r.authenticatorData);
    if (r.signature) out.response.signature = b64u.enc(r.signature);
    if (r.userHandle) out.response.userHandle = b64u.enc(r.userHandle);
    return out;
  }
  const webauthnError = (err) => (err && err.name === "NotAllowedError")
    ? T("Abgebrochen oder Zeit abgelaufen. Bitte erneut versuchen.")
    : (err && err.name === "InvalidStateError") ? T("Dieser Schlüssel ist bereits registriert.")
    : T("Fehler: {error}", { error: (err && (err.message || err.name)) || err });

  $$("[data-webauthn-register]").forEach((btn) => {
    const box = btn.closest("[data-webauthn-register-box]");
    const status = $("[data-webauthn-status]", box);
    if (!webauthnOk()) {
      btn.disabled = true;
      status.textContent = T("Sicherheitsschlüssel gehen nur über HTTPS (z. B. über deinen Reverse-Proxy), nicht per http://IP.");
      return;
    }
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      status.textContent = T("Schlüssel einstecken und berühren …");
      try {
        const begin = await api("/profil/sicherheit/schluessel/start", { method: "POST", body: "{}" });
        if (!begin.ok) throw new Error(begin.error);
        const cred = await navigator.credentials.create(creationOptions(begin.options));
        const done = await api("/profil/sicherheit/schluessel/speichern", { method: "POST",
          body: JSON.stringify({ credential: credentialJSON(cred), name: $("[data-webauthn-name]", box).value }) });
        if (!done.ok) throw new Error(done.error);
        window.location.href = done.redirect;
      } catch (err) {
        status.textContent = webauthnError(err);
        btn.disabled = false;
      }
    });
  });

  $$("[data-webauthn-login]").forEach((btn) => {
    const status = $("[data-webauthn-status]");
    if (!webauthnOk()) {
      btn.disabled = true;
      if (status) status.textContent = T("Sicherheitsschlüssel gehen nur über HTTPS.");
      return;
    }
    const run = async () => {
      btn.disabled = true;
      if (status) status.textContent = T("Schlüssel einstecken und berühren …");
      try {
        const begin = await api("/login/2fa/schluessel/start", { method: "POST", body: "{}" });
        if (!begin.ok) throw new Error(begin.error);
        const cred = await navigator.credentials.get(requestOptions(begin.options));
        const done = await api("/login/2fa/schluessel/pruefen", { method: "POST", body: JSON.stringify(credentialJSON(cred)) });
        if (!done.ok) throw new Error(done.error);
        window.location.href = done.redirect;
      } catch (err) {
        if (status) status.textContent = webauthnError(err);
        btn.disabled = false;
      }
    };
    btn.addEventListener("click", run);
  });

  $$("[data-copy-codes]").forEach((btn) => btn.addEventListener("click", async () => {
    const text = $$("[data-codes] > div").map((d) => d.textContent.trim()).join("\n");
    try { await navigator.clipboard.writeText(text); btn.textContent = T("✓ Kopiert"); } catch (e) { window.prompt(T("Codes:"), text); }
  }));

  /* ------------------------------------------------------------ ISBN */
  const cleanIsbn = (s) => String(s || "").replace(/[^0-9Xx]/g, "").toUpperCase();
  function validIsbn(s) {
    const v = cleanIsbn(s);
    if (v.length === 10 && /^\d{9}[\dX]$/.test(v)) {
      let t = 0;
      for (let i = 0; i < 10; i++) t += (10 - i) * (v[i] === "X" ? 10 : +v[i]);
      return t % 11 === 0;
    }
    if (v.length === 13 && /^\d{13}$/.test(v)) {
      let t = 0;
      for (let i = 0; i < 12; i++) t += +v[i] * (i % 2 ? 3 : 1);
      return (10 - (t % 10)) % 10 === +v[12];
    }
    return false;
  }

  // Fremde Coverbilder nie direkt laden – immer über den eigenen Server (Datenschutz)
  function coverProxy(url) {
    if (!url || url.startsWith("/")) return url;
    return "/covers/vorschau?u=" + encodeURIComponent(url);
  }

  async function api(url, opts = {}) {
    const res = await fetch(url, {
      credentials: "same-origin",
      headers: { "X-CSRFToken": CSRF, "Accept": "application/json", ...(opts.body ? { "Content-Type": "application/json" } : {}) },
      ...opts,
    });
    let data = {};
    try { data = await res.json(); } catch (e) { /* kein JSON */ }
    if (!res.ok && !data.error) data.error = T("Fehler {status}", { status: res.status });
    return data;
  }

  /* ------------------------------------------------------------ Kamera-Scanner */
  const dialog = $("#scanner-dialog");
  let stream = null, stopLoop = null;

  function closeScanner() {
    if (stopLoop) { stopLoop(); stopLoop = null; }
    if (stream) { stream.getTracks().forEach((t) => t.stop()); stream = null; }
    if (dialog && dialog.open) dialog.close();
  }

  // Bildquelle -> Code; eingebauter BarcodeDetector (Chrome, Edge, Android) oder eigene Erkennung
  async function makeDetector() {
    if ("BarcodeDetector" in window) {
      try {
        const formats = await window.BarcodeDetector.getSupportedFormats();
        if (formats.includes("ean_13")) {
          const det = new window.BarcodeDetector({ formats: ["ean_13"] });
          return async (video) => {
            const codes = await det.detect(video);
            return codes.length ? codes[0].rawValue : null;
          };
        }
      } catch (e) { /* weiter mit eigener Erkennung */ }
    }
    if (!window.CatalytixEAN13) return null;
    const canvas = document.createElement("canvas");
    const ctx = canvas.getContext("2d", { willReadFrequently: true });
    return async (video) => {
      const vw = video.videoWidth, vh = video.videoHeight;
      if (!vw || !vh) return null;
      const scale = Math.min(1, 1280 / vw);
      canvas.width = Math.round(vw * scale); canvas.height = Math.round(vh * scale);
      ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
      const img = ctx.getImageData(0, 0, canvas.width, canvas.height);
      return window.CatalytixEAN13.decodeImage(img.data, img.width, img.height);
    };
  }

  async function openScanner(onCode) {
    const status = $("[data-scanner-status]", dialog);
    const video = $("video", dialog);
    status.textContent = T("Halte den EAN-/ISBN-Barcode in den Rahmen.");
    dialog.showModal();
    if (!window.isSecureContext || !navigator.mediaDevices) {
      status.innerHTML = T("Die Kamera funktioniert nur über <b>HTTPS</b> (oder localhost). Bitte die Seite per HTTPS aufrufen oder einen USB-Barcodescanner verwenden.");
      return;
    }
    let done = false;
    const found = (raw) => {
      const code = cleanIsbn(raw);
      if (done || !validIsbn(code)) return;
      done = true;
      if (navigator.vibrate) navigator.vibrate(80);
      closeScanner();
      onCode(code);
    };
    try {
      const detect = await makeDetector();
      if (!detect) { status.textContent = T("Barcode-Erkennung ist in diesem Browser nicht verfügbar."); return; }
      stream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: "environment", width: { ideal: 1920 }, height: { ideal: 1080 } }, audio: false });
      video.srcObject = stream;
      await video.play();
      let active = true, last = null;
      stopLoop = () => { active = false; };
      const tick = async () => {
        if (!active || done) return;
        try {
          const code = await detect(video);
          // erst übernehmen, wenn zwei Bilder nacheinander denselben Code liefern
          if (code && code === last) found(code);
          last = code || last;
        } catch (e) { /* Bild noch nicht bereit */ }
        if (active && !done) setTimeout(tick, 120);
      };
      tick();
    } catch (err) {
      status.textContent = err && err.name === "NotAllowedError"
        ? T("Kamerazugriff wurde verweigert. Bitte im Browser erlauben.")
        : T("Kamera konnte nicht gestartet werden ({error}).", { error: err && (err.name || err.message) || err });
    }
  }

  if (dialog) {
    $("[data-scanner-close]", dialog).addEventListener("click", closeScanner);
    dialog.addEventListener("cancel", closeScanner);
  }

  $$("[data-scanner-open]").forEach((btn) => btn.addEventListener("click", () => {
    const input = $(btn.dataset.scannerTarget);
    openScanner((code) => {
      input.value = code;
      input.dispatchEvent(new CustomEvent("isbn:scanned", { bubbles: true }));
      if (btn.hasAttribute("data-scanner-submit")) {
        const form = input.form;
        if (form) form.requestSubmit ? form.requestSubmit() : form.submit();
      }
    });
  }));

  /* ------------------------------------------------------------ Buchformular: ISBN-Abruf */
  const bookForm = $("[data-book-form]");
  if (bookForm) {
    const isbnInput = $("[data-isbn-input]", bookForm);
    const statusBox = $("[data-isbn-status]", bookForm);
    const setField = (name, value, overwrite) => {
      const el = bookForm.elements[name];
      if (!el || value == null || value === "") return;
      if (overwrite || !el.value) el.value = value;
    };
    const showCover = (url) => {
      const box = $("[data-cover-preview] .cover", bookForm);
      if (!box || !url) return;
      let img = $("img", box);
      if (!img) { img = document.createElement("img"); img.alt = ""; img.onerror = () => img.remove(); box.appendChild(img); }
      img.src = coverProxy(url);
    };

    async function lookup() {
      const isbn = cleanIsbn(isbnInput.value);
      if (!isbn) return;
      if (!validIsbn(isbn)) {
        statusBox.innerHTML = `<div class="notice warn">⚠ ${T("„{isbn}“ ist keine gültige ISBN – Prüfziffer stimmt nicht.", { isbn: esc(isbnInput.value) })}</div>`;
        return;
      }
      statusBox.innerHTML = '<div class="notice"><span class="spinner"></span> ' + T("Suche in den Katalogen …") + "</div>";
      const res = await api(`/api/isbn/${isbn}`);
      if (!res.ok) { statusBox.innerHTML = `<div class="notice danger">${esc(res.error)}</div>`; return; }
      isbnInput.value = res.isbn;
      let html = "";
      if (res.existing && res.existing.length) {
        html += `<div class="notice warn" style="margin-bottom:10px">${T("Bereits im Bestand: {books} – ggf. dort die Anzahl der Exemplare erhöhen.", { books: res.existing.map((b) =>
          `<a href="${esc(b.url)}"><b>${esc(b.title)}</b></a> (${esc(b.group)}, ${T("{n} Expl.", { n: esc(b.copies) })})`).join(", ") })}</div>`;
      }
      const d = res.data || {};
      if (d.found) {
        const overwrite = !bookForm.elements.title.value;
        ["title", "subtitle", "authors", "publisher", "published", "pages", "language", "category", "edition", "cover_url"]
          .forEach((f) => setField(f, d[f], overwrite));
        setField("description", d.description, overwrite);
        showCover(d.cover_url);
        html += `<div class="notice ok">✓ ${T("Daten übernommen aus: {sources}. Bitte kurz prüfen.", { sources: esc(d.sources.join(", ")) })}</div>`;
      } else {
        html += `<div class="notice">${d.errors && d.errors.length
          ? T("Keine Daten gefunden ({errors}) – bitte manuell ausfüllen.", { errors: esc(d.errors.join("; ")) })
          : T("Keine Daten gefunden – bitte manuell ausfüllen.")}</div>`;
      }
      statusBox.innerHTML = html;
      if (bookForm.elements.title && !bookForm.elements.title.value) bookForm.elements.title.focus();
    }

    $("[data-isbn-lookup]", bookForm).addEventListener("click", lookup);
    isbnInput.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); lookup(); } });
    isbnInput.addEventListener("isbn:scanned", lookup);
    const coverUrl = $("[data-cover-url]", bookForm);
    if (coverUrl) coverUrl.addEventListener("change", () => showCover(coverUrl.value));
    // Mit ?isbn=… geöffnet (z. B. aus der Schnellerfassung): direkt abrufen
    if (isbnInput.value && !bookForm.elements.title.value) lookup();
  }

  /* ------------------------------------------------------------ Schnellerfassung */
  const quickForm = $("[data-quick-form]");
  if (quickForm) {
    const input = $("[data-quick-input]");
    const resultBox = $("[data-quick-result]");
    const logBox = $("[data-quick-log]");
    const countEl = $("[data-quick-count]");
    const autoBox = $("[data-quick-auto]");
    const groupSel = $("[data-quick-group]");
    const locInput = $("[data-quick-location]");
    let count = 0;

    autoBox.checked = store.get("quickAuto") === "1";
    autoBox.addEventListener("change", () => store.set("quickAuto", autoBox.checked ? "1" : "0"));
    groupSel.value = store.get("quickGroup") || "";
    groupSel.addEventListener("change", () => store.set("quickGroup", groupSel.value));
    locInput.value = store.get("quickLocation") || "";
    if (locInput.tagName === "SELECT" && locInput.selectedIndex < 0) locInput.value = "";  // Standort nicht mehr in der Liste
    ["input", "change"].forEach((ev) => locInput.addEventListener(ev, () => store.set("quickLocation", locInput.value)));

    const log = (cls, text, url) => {
      if (!count) logBox.innerHTML = "";
      count++;
      countEl.textContent = count === 1 ? T("{n} Vorgang", { n: count }) : T("{n} Vorgänge", { n: count });
      const row = document.createElement("div");
      row.className = "list-row";
      row.innerHTML = `<span class="badge ${cls}">${new Date().toLocaleTimeString(document.documentElement.lang || "de", { hour: "2-digit", minute: "2-digit" })}</span>
        <div class="grow">${url ? `<a href="${esc(url)}">${text}</a>` : text}</div>`;
      logBox.prepend(row);
    };
    const coverHtml = (d) => `<div class="cover ph" style="--h:${(d.title || "x").split("").reduce((a, c) => a + c.charCodeAt(0), 0) % 360}">
      <div><div class="pt">${esc(d.title)}</div><div class="rule"></div></div><div class="pa">${esc((d.authors || "").split(",")[0])}</div>
      ${d.cover_url ? `<img src="${esc(coverProxy(d.cover_url))}" alt="" data-hide-broken>` : ""}</div>`;
    const reset = () => { input.value = ""; input.focus(); };

    async function addBook(d) {
      const payload = { ...d, group_id: groupSel.value, location: locInput.value };
      const res = await api("/api/buecher", { method: "POST", body: JSON.stringify(payload) });
      if (res.ok) {
        log("ok", T("Neu: <b>{title}</b>", { title: esc(d.title) }), res.url);
        resultBox.innerHTML = `<div class="card-body"><div class="notice ok">✓ ${T("„{title}“ wurde angelegt. Bereit für die nächste ISBN.", { title: esc(d.title) })}</div></div>`;
      } else {
        log("danger", T("Fehler: {error}", { error: esc(res.error) }));
      }
      reset();
    }

    quickForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const isbn = cleanIsbn(input.value);
      if (!isbn) return;
      resultBox.hidden = false;
      if (!validIsbn(isbn)) {
        resultBox.innerHTML = `<div class="card-body"><div class="notice warn">${T("„{isbn}“ ist keine gültige ISBN.", { isbn: esc(input.value) })}</div></div>`;
        log("warn", T("Ungültig: {isbn}", { isbn: esc(input.value) }));
        reset();
        return;
      }
      resultBox.innerHTML = '<div class="card-body"><span class="spinner"></span> ' + T("Suche {isbn} …", { isbn: esc(isbn) }) + "</div>";
      const res = await api(`/api/isbn/${isbn}`);
      if (!res.ok) { resultBox.innerHTML = `<div class="card-body"><div class="notice danger">${esc(res.error)}</div></div>`; reset(); return; }
      const d = res.data || {};
      const existing = (res.existing || []).filter((b) => b.manageable);

      if (existing.length) {
        const b = existing[0];
        resultBox.innerHTML = `<div class="card-head"><h3>${T("Bereits im Bestand")}</h3><span class="badge warn">${esc(b.group)}</span></div>
          <div class="card-body"><p><b>${esc(b.title)}</b> – ${T("{copies} Exemplar(e), {available} verfügbar.", { copies: esc(b.copies), available: esc(b.available) })}</p>
          <div class="actions"><button class="btn primary" data-add-copy>${T("+1 Exemplar")}</button>
          <a class="btn" href="${esc(b.url)}">${T("Öffnen")}</a><button class="btn ghost" data-skip>${T("Überspringen")}</button></div></div>`;
        $("[data-add-copy]", resultBox).addEventListener("click", async () => {
          const r = await api(`/api/buecher/${b.id}/exemplar`, { method: "POST" });
          if (r.ok) {
            log("accent", T("+1 Exemplar: <b>{title}</b> (jetzt {copies})", { title: esc(b.title), copies: esc(r.copies) }), b.url);
            resultBox.innerHTML = `<div class="card-body"><div class="notice ok">✓ ${T("Jetzt {copies} Exemplare von „{title}“.", { copies: esc(r.copies), title: esc(b.title) })}</div></div>`;
          }
          reset();
        });
        $("[data-skip]", resultBox).addEventListener("click", reset);
        $("[data-add-copy]", resultBox).focus();
        return;
      }

      if (!d.found) {
        resultBox.innerHTML = `<div class="card-body"><div class="notice">${T("Zu {isbn} wurden keine Daten gefunden.", { isbn: esc(isbn) })}</div>
          <div class="actions" style="margin-top:12px"><a class="btn primary" href="/buecher/neu?isbn=${esc(isbn)}">${T("Manuell erfassen")}</a></div></div>`;
        log("warn", T("Nicht gefunden: {isbn}", { isbn: esc(isbn) }), `/buecher/neu?isbn=${isbn}`);
        reset();
        return;
      }

      if (autoBox.checked) { await addBook(d); return; }

      resultBox.innerHTML = `<div class="result-card">${coverHtml(d)}
        <div><h2>${esc(d.title)}</h2>${d.subtitle ? `<p class="muted">${esc(d.subtitle)}</p>` : ""}
        <p>${esc(d.authors || "")}</p><p class="muted">${esc([d.publisher, d.published, d.pages ? T("{n} S.", { n: d.pages }) : ""].filter(Boolean).join(" · "))}</p>
        <p><small class="muted">${T("Quelle: {sources}", { sources: esc(d.sources.join(", ")) })}</small></p>
        <div class="actions"><button class="btn primary" data-accept>${T("Übernehmen")} <span class="kbd">Enter</span></button>
        <a class="btn" href="/buecher/neu?isbn=${esc(isbn)}">${T("Bearbeiten …")}</a><button class="btn ghost" data-skip>${T("Verwerfen")}</button></div></div></div>`;
      const accept = $("[data-accept]", resultBox);
      accept.addEventListener("click", () => addBook(d));
      $("[data-skip]", resultBox).addEventListener("click", () => { resultBox.hidden = true; reset(); });
      accept.focus();
    });
  }

  /* ------------------------------------------------------------ Hilfe */
  const helpBox = $("[data-help-topics]");
  if (helpBox) {
    const topics = $$("[data-help-topic]", helpBox);
    const links = $$("[data-help-link]");
    const focus = helpBox.dataset.helpFocus;
    if (focus && document.getElementById(focus)) {
      const el = document.getElementById(focus);
      el.classList.add("focus");
      el.scrollIntoView({ block: "start" });
    }
    const search = $("[data-help-search]");
    if (search) search.addEventListener("input", () => {
      const words = search.value.toLowerCase().split(/\s+/).filter(Boolean);
      let shown = 0;
      topics.forEach((t) => {
        const text = t.textContent.toLowerCase();
        const hit = words.every((w) => text.includes(w));
        t.hidden = !hit;
        if (hit) shown += 1;
        const link = links.find((a) => a.dataset.helpLink === t.id);
        if (link) link.hidden = !hit;
      });
      const empty = $("[data-help-empty]");
      if (empty) empty.style.display = shown ? "none" : "block";
    });
  }

  /* ------------------------------------------------------------ Vorschlagslisten (Suche oben, Personen, Bestand) */
  // Gemeinsame Logik: tippen → Server fragen → Liste; Pfeiltasten, Enter, Klick; veraltete Antworten werden verworfen.
  // followUp: zweite, langsamere Abfrage (Active Directory) – die schnellen Treffer stehen schon vorher da.
  // onChoose: statt den Wert ins Feld zu schreiben etwas anderes tun (z. B. das Buch öffnen).
  const suggest = (input, { url, minLength = 2, render, emptyText, valueOf, submit, preselect = () => true,
                            followUp = null, onChoose = null, footer = null }) => {
    const box = input.closest(".user-search") || input.parentElement;
    const list = document.createElement("ul");
    const listId = "suggest-" + Math.random().toString(36).slice(2, 8);
    list.className = "user-suggest"; list.hidden = true; list.id = listId; list.setAttribute("role", "listbox");
    input.setAttribute("aria-controls", listId);
    input.setAttribute("role", "combobox"); input.setAttribute("aria-autocomplete", "list");
    input.setAttribute("aria-expanded", "false");
    box.appendChild(list);
    let items = [], active = -1, timer = null, seq = 0, ctrl = null, moved = false;
    const close = () => { list.hidden = true; input.setAttribute("aria-expanded", "false"); active = -1;
      input.removeAttribute("aria-activedescendant"); };
    const choose = (i) => {
      const it = items[i]; if (!it) return;
      if (onChoose) { close(); onChoose(it); return; }
      input.value = valueOf(it); close();
      if (submit && input.form) input.form.submit();
    };
    const mark = () => $$("li[data-i]", list).forEach((li) => {
      const on = Number(li.dataset.i) === active;
      li.classList.toggle("on", on); li.setAttribute("aria-selected", on ? "true" : "false");
      if (on) { input.setAttribute("aria-activedescendant", li.id); li.scrollIntoView({ block: "nearest" }); }
    });
    const show = (data, pending = "") => {
      const keep = moved && items[active] ? valueOf(items[active]) : null;
      items = data.results || [];
      const rows = items.map((it, i) => `<li role="option" id="${listId}-${i}" data-i="${i}">${render(it)}</li>`);
      if (!items.length && !pending) rows.push(`<li class="empty">${esc(emptyText)}</li>`);
      if (pending) rows.push(`<li class="empty">${esc(pending)}</li>`);
      if (data.directory_error) rows.push(`<li class="empty">${esc(data.directory_error)}</li>`);
      if (data.more) rows.push(`<li class="empty">${esc(footer ? footer(data) : T("Weitere Treffer – bitte genauer suchen."))}</li>`);
      else if (footer && items.length) rows.push(`<li class="empty">${esc(footer(data))}</li>`);
      list.innerHTML = rows.join("");
      list.hidden = false; input.setAttribute("aria-expanded", "true");
      // Nach dem Nachladen bleibt eine mit den Pfeiltasten gewählte Zeile gewählt.
      // Sonst Vorauswahl nur, wenn sie eindeutig ist – andernfalls übernimmt Enter den getippten Text.
      const kept = keep === null ? -1 : items.findIndex((it) => valueOf(it) === keep);
      if (kept >= 0) active = kept;
      else { moved = false; active = items.length && preselect(items) ? 0 : -1; }
      mark();
    };
    const ask = async (u, my) => {
      const res = await fetch(u, { headers: { Accept: "application/json" }, signal: ctrl.signal });
      const data = await res.json();
      return my === seq ? data : null;
    };
    input.addEventListener("input", () => {
      clearTimeout(timer);
      // Alte Treffer sofort verwerfen: Enter während einer laufenden Suche nimmt den getippten Text, nie einen alten Treffer
      items = []; active = -1; moved = false; const my = ++seq;
      if (ctrl) ctrl.abort();
      const q = input.value.trim();
      if (q.length < minLength) { close(); return; }
      timer = setTimeout(async () => {
        if (list.hidden) { list.hidden = false; list.innerHTML = `<li class="empty">${esc(T("Suche …"))}</li>`; }
        ctrl = new AbortController();
        try {
          const data = await ask(url(q), my);
          if (!data) return;
          const next = followUp && followUp(q, data);
          show(data, next ? T("Suche im Active Directory …") : "");
          if (!next) return;
          const more = await ask(next, my);
          if (more) show(more);
        } catch (e) { if (my === seq && !items.length) close(); }
      }, 150);
    });
    input.addEventListener("keydown", (e) => {
      if (list.hidden || !items.length) return;
      if (e.key === "ArrowDown") { e.preventDefault(); moved = true; active = (active + 1) % items.length; mark(); }
      else if (e.key === "ArrowUp") { e.preventDefault(); moved = true; active = (active - 1 + items.length) % items.length; mark(); }
      else if (e.key === "Enter" && active >= 0) { e.preventDefault(); choose(active); }
      else if (e.key === "Escape") { close(); }
    });
    list.addEventListener("mousedown", (e) => {
      const li = e.target.closest("li[data-i]"); if (!li) return;
      e.preventDefault(); choose(Number(li.dataset.i));
    });
    input.addEventListener("blur", () => setTimeout(close, 150));
  };

  const initials = (name) => (name || "").split(/[\s.]+/).filter(Boolean).slice(0, 2).map((w) => w[0]).join("").toUpperCase();
  $$("[data-user-search]").forEach((input) => suggest(input, {
    // Erst die bekannten Personen (sofort), dann zusätzlich das Active Directory
    url: (q) => `/api/personen?q=${encodeURIComponent(q)}&verzeichnis=0`,
    followUp: (q, data) => data.directory_pending ? `/api/personen?q=${encodeURIComponent(q)}` : null,
    emptyText: T("Niemand gefunden."),
    valueOf: (it) => it.username,
    submit: input.hasAttribute("data-user-search-submit"),
    render: (it) => `<span class="avatar sm">${esc(initials(it.name || it.username))}</span>
      <span class="who"><b>${esc(it.name)}</b> <small>${esc(it.username)}${it.email ? " · " + esc(it.email) : ""}</small>
      <small>${it.new ? `<span class="badge">${esc(T("neu aus dem AD"))}</span> ` : ""}${[it.groups.join(", "), !it.new && it.loans ? T("{n} ausgeliehen", {n: it.loans}) : ""].filter(Boolean).map(esc).join(" · ")}</small></span>`,
  }));

  $$("[data-book-search]").forEach((input) => suggest(input, {
    url: (q) => `/api/bestand?q=${encodeURIComponent(q)}&modus=${encodeURIComponent(input.dataset.bookSearch)}` +
      `&benutzer=${encodeURIComponent((input.form && input.form.elements.benutzer && input.form.elements.benutzer.value) || "")}`,
    minLength: 2,
    emptyText: T("Kein Buch gefunden."),
    valueOf: (it) => "#" + it.id,
    submit: true,
    // Buchfeld: nur ein genauer Signatur-/ISBN-Treffer ist vorausgewählt; sonst sendet Enter den getippten Text
    preselect: (items) => items[0].exact === true && !(items[1] && items[1].exact),
    render: (it) => {
      const meta = [it.authors, it.signature && T("Signatur {s}", {s: it.signature}), it.location].filter(Boolean).map(esc).join(" · ");
      const state = it.available > 0
        ? `<span class="badge ok">${esc(T("{n} von {m} frei", {n: it.available, m: it.copies}))}</span>`
        : `<span class="badge">${esc(T("alle verliehen"))}</span>`;
      const out = it.on_loan ? ` <span class="badge">${esc(T("{n} ausgeliehen", {n: it.on_loan}))}</span>` : "";
      const deny = it.allowed === false ? ` <span class="badge danger">${esc(T("andere Gruppe – nicht für diese Person"))}</span>`
        : (it.has ? ` <span class="badge">${esc(T("hat diese Person schon"))}</span>` : "");
      return `<span class="who"><b>${esc(it.title)}</b> <small>${meta}</small><small>${state}${out}${deny}${it.group ? " · " + esc(it.group) : ""}</small></span>`;
    },
  }));

  // Suchfeld oben: Vorschläge aus dem Katalog; Auswahl öffnet das Buch, Enter ohne Auswahl zeigt alle Treffer
  $$("[data-catalog-search]").forEach((input) => suggest(input, {
    url: (q) => `/api/katalog?q=${encodeURIComponent(q)}`,
    emptyText: T("Kein Buch gefunden – Enter sucht im ganzen Katalog."),
    valueOf: (it) => String(it.id),
    preselect: () => false,
    onChoose: (it) => { window.location.href = it.url; },
    footer: () => T("Enter: alle Treffer im Katalog anzeigen"),
    render: (it) => {
      const meta = [it.authors, it.signature && T("Signatur {s}", {s: it.signature})].filter(Boolean).map(esc).join(" · ");
      const state = it.available > 0
        ? `<span class="badge ok">${esc(T("{n} von {m} frei", {n: it.available, m: it.copies}))}</span>`
        : `<span class="badge">${esc(T("alle verliehen"))}</span>`;
      return `<span class="who"><b>${esc(it.title)}</b> <small>${meta}</small><small>${state}</small></span>`;
    },
  }));
})();
