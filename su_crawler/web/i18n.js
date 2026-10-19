/* Local interface translations. Research data never passes through this catalog. */
(() => {
  "use strict";
  const storageKey = "sourceledger.ui.language";
  const locales = Object.freeze({ en: "en-US", ko: "ko-KR", ja: "ja-JP" });
  const messages = { en: Object.create(null), ko: Object.create(null), ja: Object.create(null) };
  let language = "en";
  let initialized = false;
  const supported = value => Object.hasOwn(locales, value);
  const normalize = value => supported(value) ? value : "en";

  function register(catalog) {
    for (const code of Object.keys(locales)) {
      for (const [key, value] of Object.entries(catalog[code] || {})) {
        if (typeof value === "string") messages[code][key] = value;
      }
    }
  }

  function t(key, params = {}) {
    const template = messages[language][key] ?? messages.en[key] ?? key;
    return template.replace(/\{(\w+)\}/g, (match, name) =>
      Object.hasOwn(params, name) ? String(params[name]) : match);
  }

  function translateStatic(root = document) {
    const attributes = {
      "data-i18n-placeholder": "placeholder",
      "data-i18n-aria-label": "aria-label",
      "data-i18n-title": "title",
    };
    const selector = ["[data-i18n]", ...Object.keys(attributes).map(key => `[${key}]`)].join(",");
    const elements = [...(root.matches?.(selector) ? [root] : []), ...root.querySelectorAll(selector)];
    for (const element of elements) {
      if (element.hasAttribute("data-i18n")) element.textContent = t(element.getAttribute("data-i18n"));
      for (const [marker, attribute] of Object.entries(attributes)) {
        if (element.hasAttribute(marker)) element.setAttribute(attribute, t(element.getAttribute(marker)));
      }
    }
  }

  function applyLanguage(value, { persist = true, notify = true } = {}) {
    const next = normalize(value);
    const changed = language !== next;
    language = next;
    document.documentElement.lang = language;
    const selector = document.getElementById("language-select");
    if (selector) selector.value = language;
    translateStatic();
    if (persist) {
      try { localStorage.setItem(storageKey, language); } catch { /* Storage may be disabled. */ }
    }
    if (notify && changed) {
      window.dispatchEvent(new CustomEvent("sourceledger-language-change", { detail: { language } }));
    }
  }

  function initialize() {
    if (initialized) return;
    initialized = true;
    let saved = "en";
    try { saved = localStorage.getItem(storageKey); } catch { /* English is the fallback. */ }
    applyLanguage(saved, { persist: false, notify: false });
    document.getElementById("language-select")?.addEventListener("change", event => applyLanguage(event.target.value));
    window.addEventListener("storage", event => {
      if (event.key === storageKey || event.key === null) applyLanguage(event.newValue, { persist: false });
    });
  }

  window.SourceLedgerI18n = Object.freeze({
    register, t, translateStatic, initialize,
    setLanguage: value => applyLanguage(value),
    getLanguage: () => language,
    getLocale: () => locales[language],
  });
})();
