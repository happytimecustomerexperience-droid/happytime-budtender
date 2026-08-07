(function () {
  var supportsDetector = typeof BarcodeDetector !== "undefined";
  var loopByPanel = {};
  var streams = {};

  function ensureStatus(panel, text) {
    if (!panel) return;
    var status = panel.querySelector(".idscan__status") || panel.querySelector("[id$='-status']");
    if (status) status.textContent = text || "";
  }

  function stopPanel(panelId) {
    var loop = loopByPanel[panelId];
    if (loop && loop.timer) {
      cancelAnimationFrame(loop.timer);
    }
    loopByPanel[panelId] = null;

    var stream = streams[panelId];
    if (stream) {
      stream.getTracks().forEach(function (track) {
        track.stop();
      });
      streams[panelId] = null;
    }

    var panel = document.getElementById(panelId);
    if (!panel) return;
    var video = panel.querySelector("video");
    if (video) {
      video.srcObject = null;
      video.load();
    }
    panel.setAttribute("hidden", "");
    ensureStatus(panel, "");
  }

  function decodeWithBarcodeDetector(detector, video, callback) {
    var canvas = document.createElement("canvas");
    var ctx = canvas.getContext("2d");

    return function scanFrame() {
      if (!video.videoWidth || !video.videoHeight) return Promise.resolve(false);
      canvas.width = video.videoWidth;
      canvas.height = video.videoHeight;
      ctx.drawImage(video, 0, 0);
      return detector.detect(canvas).then(function (codes) {
        for (var i = 0; i < codes.length; i++) {
          var code = codes[i];
          if (!code || !code.rawValue) continue;
          if ((code.format || "pdf417") === "pdf417" || code.format === "qr_code" || code.format === "code_128") {
            callback(code.rawValue);
            return true;
          }
        }
        return false;
      }).catch(function () {
        return false;
      });
    };
  }

  function startPanel(panelId) {
    var panel = document.getElementById(panelId);
    if (!panel) return;
    var form = panel.closest("form");
    var hidden = form && form.querySelector('input[name="id_payload"]');
    var submitBtn = form && form.querySelector('button[type="submit"]');

    panel.removeAttribute("hidden");
    if (!supportsDetector) {
      ensureStatus(panel, "Barcode scanner not available on this browser. Use file fallback above.");
      return;
    }
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      ensureStatus(panel, "Camera is not available on this device.");
      return;
    }

    stopPanel(panelId);
    var detector;
    try {
      detector = new BarcodeDetector({ formats: ["pdf417", "code_128", "ean_13", "qr_code"] });
    } catch (err) {
      detector = new BarcodeDetector();
    }

    ensureStatus(panel, "Opening camera — point to the barcode side of the ID.");
    navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" } })
      .then(function (stream) {
        streams[panelId] = stream;
        var video = panel.querySelector("video");
        if (!video) {
          throw new Error("No video preview area");
        }
        video.srcObject = stream;
        return video.play().then(function () {
          return video;
        }).catch(function () {
          return video;
        });
      })
      .then(function (video) {
        var scanFrame = decodeWithBarcodeDetector(detector, video, function (payload) {
          hidden.value = String(payload || "").trim();
          if (!hidden.value) return;
          stopPanel(panelId);
          ensureStatus(panel, "ID captured. Submitting...");
          if (form) {
            if (form.requestSubmit) {
              form.requestSubmit(submitBtn || undefined);
            } else {
              form.submit();
            }
          }
        });

        loopByPanel[panelId] = { timer: null };
        (function loop() {
          var state = loopByPanel[panelId];
          if (!state) return;
          scanFrame().then(function (done) {
            if (done || !loopByPanel[panelId]) return;
            state.timer = requestAnimationFrame(loop);
          });
        })();
      })
      .catch(function (err) {
        ensureStatus(panel, "Camera scan blocked. Use file fallback or allow camera permission." +
          (err && err.message ? " " + err.message : ""));
      });
  }

  document.addEventListener("click", function (e) {
    var openBtn = e.target.closest(".idscan__toggle");
    if (openBtn) {
      e.preventDefault();
      var target = openBtn.getAttribute("data-target");
      if (target) startPanel(target);
      return;
    }

    var closeBtn = e.target.closest(".idscan__close") || e.target.closest("[data-idscan-close]");
    if (closeBtn) {
      e.preventDefault();
      var panel = closeBtn.closest(".idscan");
      if (panel && panel.id) stopPanel(panel.id);
      return;
    }
  });

  // Camera on by default. Someone is standing at the counter holding a card out;
  // making them wait for a budtender to find a "start camera" button was a step that
  // existed only because the camera used to be hidden behind a toggle.
  function autostart() {
    document.querySelectorAll(".idscan[data-autostart]").forEach(function (panel) {
      if (panel.id) startPanel(panel.id);
    });
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", autostart);
  } else {
    autostart();
  }
  // htmx swaps the station body without a page load, so re-arm after a swap.
  document.body.addEventListener("htmx:afterSwap", autostart);

  window.addEventListener("pagehide", function () {
    Object.keys(loopByPanel).forEach(function (id) {
      stopPanel(id);
    });
  });
})();
