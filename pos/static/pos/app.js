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
})();
