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

  syncCount();
})();
