/* jbvip auto-join, injected into jackbox.tv.
 *
 * Sole contract with the client:
 *   window.jbvipJoin(code, name) -> Promise<{ok: boolean, reason?: string}>
 *
 * Selectors are ids verified against the shipped @connect bundle on 2026-08-01.
 * The inputs are Vue `modelValue`-bound: assigning `.value` alone does NOT
 * update Vue's model, so a bubbling `input` event must be dispatched. */
(function () {
  'use strict';

  var SEL_CODE = '#roomcode';
  var SEL_NAME = '#username';
  var SEL_JOIN = '#button-join';
  var ENABLE_TIMEOUT_MS = 3000;
  var JOIN_TIMEOUT_MS = 8000;
  var POLL_MS = 50;

  function setModelValue(el, value) {
    var desc = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), 'value');
    if (desc && desc.set) { desc.set.call(el, value); } else { el.value = value; }
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  }

  function waitFor(predicate, timeoutMs) {
    return new Promise(function (resolve) {
      var deadline = Date.now() + timeoutMs;
      (function poll() {
        if (predicate()) { return resolve(true); }
        if (Date.now() > deadline) { return resolve(false); }
        setTimeout(poll, POLL_MS);
      })();
    });
  }

  function visibleFailureText() {
    var el = document.querySelector('.warnings') || document.querySelector('.status');
    var text = el && el.textContent ? el.textContent.trim() : '';
    return text || 'join-did-not-complete';
  }

  window.jbvipJoin = function (code, name) {
    var codeEl = document.querySelector(SEL_CODE);
    var nameEl = document.querySelector(SEL_NAME);
    var joinEl = document.querySelector(SEL_JOIN);
    if (!codeEl || !nameEl || !joinEl) {
      return Promise.resolve({ ok: false, reason: 'form-not-found' });
    }
    setModelValue(codeEl, code);
    setModelValue(nameEl, name);
    return waitFor(function () { return !joinEl.disabled; }, ENABLE_TIMEOUT_MS)
      .then(function (enabled) {
        if (!enabled) { return { ok: false, reason: 'join-button-never-enabled' }; }
        joinEl.click();
        return waitFor(function () {
          return document.querySelector(SEL_CODE) === null;
        }, JOIN_TIMEOUT_MS).then(function (joined) {
          return joined ? { ok: true } : { ok: false, reason: visibleFailureText() };
        });
      });
  };
})();
