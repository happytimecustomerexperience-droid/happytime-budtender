// The 21+ gate. Lived inline in base.html until the edge CSP (script-src 'self') would have
// blocked it — an inline script under that policy means every shopper sits behind a gate that
// never opens. Reads the same storage keys the marketing site sets (happytime-age-session /
// happytime-age-verified) so a shopper who already confirmed on happytimeweed.com is not asked twice.
(function () {
  var SESSION = 'happytime-age-session', PERSIST = 'happytime-age-verified';
  function verified() {
    try {
      if (localStorage.getItem(PERSIST) === 'true') return true;
      var raw = sessionStorage.getItem(SESSION);
      if (!raw) return false;
      var s = JSON.parse(raw);
      return !!(s && (s.verified || s.isVerified));
    } catch (e) { return false; }
  }
  if (verified()) return;
  var gate = document.getElementById('htco-age');
  gate.hidden = false;
  document.documentElement.style.overflow = 'hidden';
  document.getElementById('htco-age-yes').addEventListener('click', function () {
    try {
      sessionStorage.setItem(SESSION, JSON.stringify({ verified: true, ts: Date.now() }));
      localStorage.setItem(PERSIST, 'true');
    } catch (e) { /* still let them through this pageview */ }
    gate.hidden = true;
    document.documentElement.style.overflow = '';
  });
})();
