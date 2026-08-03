// Budtender POS client glue (external so CSP can be script-src 'self').
(function () {
  // Keep the chosen store on every htmx request (the select lives in the header).
  document.body.addEventListener("htmx:configRequest", function (e) {
    var sel = document.getElementById("store");
    if (sel) e.detail.parameters["store"] = sel.value;
  });

  // Cart drawer open/close.
  function openCart() { document.body.classList.add("cart-open"); }
  function closeCart() { document.body.classList.remove("cart-open"); }
  document.addEventListener("click", function (e) {
    var t = e.target.closest("[data-cart-open]");
    if (t) { openCart(); return; }
    if (e.target.closest("[data-cart-close]") || e.target.id === "cart-backdrop") closeCart();
  });
  // Pop the cart open right after an item is added.
  document.body.addEventListener("htmx:afterRequest", function (e) {
    var p = (e.detail && e.detail.requestConfig && e.detail.requestConfig.path) || "";
    if (p.indexOf("/cart/add/") !== -1 && e.detail.successful) openCart();
  });

  // Begin-gate autocomplete: clicking a suggestion fills the phone field (then "Begin").
  document.addEventListener("click", function (e) {
    var t = e.target.closest("[data-fill-phone]");
    if (!t) return;
    var inp = document.getElementById("startphone");
    if (inp) { inp.value = t.getAttribute("data-fill-phone"); inp.focus(); }
    var box = document.getElementById("begin-guests");
    if (box) box.innerHTML = "";
  });

  // Carousel arrows: page the sibling rail ~5 cards at a time.
  document.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-rail-prev],[data-rail-next]");
    if (!btn) return;
    var sec = btn.closest(".carousel");
    var rail = sec && sec.querySelector(".crl-rail");
    if (!rail) return;
    var card = rail.querySelector(".pcard");
    var step = card ? (card.offsetWidth + 14) * 5 : rail.clientWidth;
    rail.scrollBy({ left: btn.hasAttribute("data-rail-prev") ? -step : step, behavior: "smooth" });
  });

  /* New-order alert.
   *
   * The queue panel re-polls every 5s. An online order that nobody notices is a
   * customer standing at the counter while their order sits in a list — so any
   * token that wasn't in the previous poll rings a bell, flashes the panel, and
   * marks the tab title. Tones are synthesised (no asset, and CSP-safe).
   */
  var knownOrders = null;   // null until the first poll, so page load is never "new"
  var baseTitle = document.title;
  var unseen = 0;

  function chime() {
    try {
      var Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) return;
      var ctx = new Ctx();
      [880, 1174].forEach(function (freq, i) {
        var osc = ctx.createOscillator();
        var gain = ctx.createGain();
        osc.type = "sine";
        osc.frequency.value = freq;
        osc.connect(gain);
        gain.connect(ctx.destination);
        var t = ctx.currentTime + i * 0.16;
        gain.gain.setValueAtTime(0.0001, t);
        gain.gain.exponentialRampToValueAtTime(0.22, t + 0.02);
        gain.gain.exponentialRampToValueAtTime(0.0001, t + 0.15);
        osc.start(t);
        osc.stop(t + 0.16);
      });
      setTimeout(function () { ctx.close(); }, 600);
    } catch (err) { /* audio blocked until first interaction — the flash still shows */ }
  }

  function tokensNow() {
    var head = document.getElementById("orders-head");
    if (!head) return null;
    return (head.getAttribute("data-order-tokens") || "").trim().split(/\s+/).filter(Boolean);
  }

  function checkOrders() {
    var now = tokensNow();
    if (now === null) return;
    if (knownOrders === null) { knownOrders = now; return; }   // first poll = baseline
    var fresh = now.filter(function (t) { return knownOrders.indexOf(t) === -1; });
    knownOrders = now;
    if (!fresh.length) return;

    chime();
    var wrap = document.getElementById("queue");
    if (wrap) {
      wrap.classList.remove("neworder");
      void wrap.offsetWidth;                  // restart the animation
      wrap.classList.add("neworder");
    }
    unseen += fresh.length;
    document.title = "(" + unseen + ") New order — " + baseTitle;
  }

  document.body.addEventListener("htmx:afterSwap", function (e) {
    if (e.target && e.target.id === "queue") checkOrders();
  });

  // Clear the tab marker once a budtender is actually looking at the screen.
  ["click", "keydown"].forEach(function (evt) {
    document.addEventListener(evt, function () {
      if (unseen) { unseen = 0; document.title = baseTitle; }
    });
  });
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden && unseen) { unseen = 0; document.title = baseTitle; }
  });
})();
