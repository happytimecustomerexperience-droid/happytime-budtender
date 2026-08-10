/* Public storefront: menu filtering + cart mutations.
 *
 * Every cart mutation posts only a product id and a quantity; the server reprices
 * from live register inventory and returns the whole cart partial. The client
 * never computes or sends a price, so a tampered DOM cannot move a real total.
 */
(function () {
  "use strict";

  var results = document.getElementById("results");
  var filters = document.getElementById("menu-filters");
  var cartPanel = document.getElementById("cart-panel");
  var cartCount = document.getElementById("cart-count");
  var page = 1;

  function debounce(fn, ms) {
    var t;
    return function () { clearTimeout(t); t = setTimeout(fn, ms); };
  }

  function syncCount() {
    var box = document.querySelector(".cartbox");
    if (box && cartCount) cartCount.textContent = box.getAttribute("data-count") || "0";
  }

  // ── menu ───────────────────────────────────────────────────────────────────
  function loadResults(toPage) {
    if (!results || !filters) return;
    page = toPage || 1;
    var params = new URLSearchParams(new FormData(filters));
    params.set("page", String(page));
    results.setAttribute("aria-busy", "true");
    fetch(filters.getAttribute("data-results-url") + "?" + params.toString(),
          { credentials: "same-origin" })
      .then(function (r) { return r.text(); })
      .then(function (html) {
        results.innerHTML = html;
        results.removeAttribute("aria-busy");
      })
      .catch(function () {
        results.innerHTML = '<p class="muted">Couldn\'t load the menu. Please try again.</p>';
        results.removeAttribute("aria-busy");
      });
  }

  if (filters) {
    filters.addEventListener("submit", function (e) { e.preventDefault(); loadResults(1); });
    filters.addEventListener("input", debounce(function () { loadResults(1); }, 300));
    filters.addEventListener("change", function () { loadResults(1); });
    loadResults(1);
  }

  // ── cart ───────────────────────────────────────────────────────────────────
  function postForm(url, form, btn) {
    var body = new URLSearchParams(new FormData(form));
    if (btn) { btn.disabled = true; }
    return fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: body.toString()
    })
      .then(function (r) { return r.text(); })
      .then(function (html) {
        if (cartPanel) cartPanel.innerHTML = html;
        syncCount();
        if (btn) { btn.disabled = false; }
      })
      .catch(function () { if (btn) { btn.disabled = false; } });
  }

  document.addEventListener("click", function (e) {
    var add = e.target.closest(".add");
    if (add) {
      var addForm = add.closest("form");
      postForm(addForm.getAttribute("data-url"), addForm, add).then(function () {
        add.textContent = "Added ✓";
        setTimeout(function () { add.textContent = "Add to cart"; }, 1200);
      });
      return;
    }

    var qbtn = e.target.closest(".qbtn");
    if (qbtn) {
      var qForm = qbtn.closest("form");
      var input = qForm.querySelector(".qty");
      var next = (parseInt(input.value, 10) || 1) + parseInt(qbtn.getAttribute("data-delta"), 10);
      input.value = Math.max(next, 0);
      postForm(qForm.getAttribute("data-url"), qForm, qbtn);
      return;
    }

    var rm = e.target.closest(".remove");
    if (rm) {
      var rmForm = rm.closest("form");
      postForm(rmForm.getAttribute("data-url"), rmForm, rm);
      return;
    }

    var tab = e.target.closest(".tab");
    if (tab && filters) {
      document.querySelectorAll("#cat-tabs .tab").forEach(function (t) { t.classList.remove("active"); });
      tab.classList.add("active");
      document.getElementById("f-cat").value = tab.getAttribute("data-cat") || "";
      loadResults(1);
      return;
    }

    var pageBtn = e.target.closest(".page-btn");
    if (pageBtn && !pageBtn.disabled) {
      loadResults(parseInt(pageBtn.getAttribute("data-page"), 10) || 1);
      window.scrollTo({ top: 0, behavior: "smooth" });
    }

    // Lab data: fetched once per tile, then just toggled shut/open — the
    // endpoint is one Dutchie request per batch, so re-fetching on every click
    // would cost the same as never caching it client-side at all.
    var labBtn = e.target.closest(".labbtn");
    if (labBtn) {
      var slot = labBtn.parentElement.querySelector(".labslot");
      if (!slot) return;
      if (slot.getAttribute("data-loaded")) {
        slot.hidden = !slot.hidden;
        return;
      }
      slot.hidden = false;
      slot.innerHTML = '<p class="muted">Loading…</p>';
      fetch(labBtn.getAttribute("data-url"), { credentials: "same-origin" })
        .then(function (r) { return r.text(); })
        .then(function (html) {
          slot.innerHTML = html;
          slot.setAttribute("data-loaded", "1");
        })
        .catch(function () {
          slot.innerHTML = '<p class="muted">Couldn\'t load lab data.</p>';
        });
      return;
    }
  });

  // Typing a quantity directly.
  document.addEventListener("change", function (e) {
    if (e.target.classList && e.target.classList.contains("qty")) {
      var form = e.target.closest("form");
      e.target.value = Math.max(parseInt(e.target.value, 10) || 0, 0);
      postForm(form.getAttribute("data-url"), form, null);
    }
  });

  // ── customer lookup ────────────────────────────────────────────────────────
  // Someone who has bought here before shouldn't retype the name Dutchie already
  // has. This is a convenience and nothing more, so it obeys three rules:
  //   1. it NEVER blocks the submit — in flight, failed and "no account" all look
  //      the same, and the form works with this whole block deleted;
  //   2. the fields stay editable and required, so a wrong match is correctable;
  //   3. it never overwrites something the shopper typed themselves.
  var orderForm = document.querySelector(".orderform[data-lookup-url]");
  if (orderForm) {
    var phoneInput = orderForm.querySelector('input[name="phone"]');
    var statusEl = orderForm.querySelector(".lookup");
    var firstEl = orderForm.querySelector('input[name="first_name"]');
    var lastEl = orderForm.querySelector('input[name="last_name"]');
    // What WE put in each box. Anything else in there was typed by a person and
    // is never touched — but our own guess stays replaceable, so correcting a
    // mistyped phone number also corrects the name it pulled in.
    var autofilled = {};
    var lastLooked = "";
    var seq = 0;

    function setStatus(state, text) {
      if (!statusEl) return;
      statusEl.className = "lookup" + (state ? " " + state : "");
      statusEl.textContent = text;
      statusEl.hidden = !text;
    }

    function fill(el, value) {
      if (!el) return;
      if (el.value && el.value !== autofilled[el.name]) return;
      el.value = value || "";
      autofilled[el.name] = el.value;
    }

    function tenDigits(raw) {
      var d = String(raw || "").replace(/[^0-9]/g, "");
      if (d.length === 11 && d.charAt(0) === "1") d = d.slice(1);
      return d;
    }

    function lookup() {
      if (!phoneInput) return;
      var digits = tenDigits(phoneInput.value);
      if (digits.length !== 10) {
        lastLooked = "";
        setStatus("", "");
        return;
      }
      if (digits === lastLooked) return;   // blur after the debounce already ran
      lastLooked = digits;

      var mine = ++seq;
      setStatus("busy", "Checking…");
      var body = new URLSearchParams();
      body.set("phone", digits);
      var loc = orderForm.querySelector('input[name="loc"]');
      if (loc) body.set("loc", loc.value);
      // The endpoint is NOT csrf_exempt (the cart ones are): it reads customer
      // identity, so it stays same-origin only. The token is already in the form.
      var token = orderForm.querySelector('input[name="csrfmiddlewaretoken"]');
      if (token) body.set("csrfmiddlewaretoken", token.value);

      fetch(orderForm.getAttribute("data-lookup-url"), {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: body.toString()
      })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (data) {
          if (mine !== seq) return;   // a newer number is already in flight
          if (data && data.found) {
            fill(firstEl, data.first_name);
            fill(lastEl, data.last_name);
            setStatus("ok", "We found your profile — check it's right.");
          } else {
            fill(firstEl, "");
            fill(lastEl, "");
            // "new" is a CLEAN no from the register, so we can promise a profile.
            // Anything else means we could not ask, and staying quiet is the honest
            // answer — telling a returning customer they're new is how you end up
            // with two profiles and half their loyalty points on each.
            if (data && data.state === "new") {
              setStatus("new", "New here — we'll set up your profile with this order.");
            } else {
              setStatus("", "");
            }
          }
        })
        .catch(function () {
          // Throttled, offline, register down: we could not ask, so say nothing.
          if (mine === seq) setStatus("", "");
        });
    }

    if (phoneInput) {
      phoneInput.addEventListener("input", debounce(lookup, 400));
      phoneInput.addEventListener("blur", lookup);
    }
  }

  syncCount();
})();
