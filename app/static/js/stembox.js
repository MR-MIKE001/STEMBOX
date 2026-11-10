/* STEMBox front-end helpers. Plain JavaScript, no libraries.
   Every page works without this file; it only adds conveniences. */
(function () {
  "use strict";
  document.documentElement.classList.add("js");

  function csrfToken() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute("content") : "";
  }

  document.addEventListener("DOMContentLoaded", function () {
    // Mobile menu toggle.
    var toggle = document.querySelector(".menu-toggle");
    var nav = document.getElementById("site-nav");
    if (toggle && nav) {
      toggle.addEventListener("click", function () {
        var open = nav.classList.toggle("open");
        toggle.setAttribute("aria-expanded", open ? "true" : "false");
      });
    }

    // Ask before destructive actions: <form data-confirm="Are you sure?">
    document.querySelectorAll("form[data-confirm]").forEach(function (form) {
      form.addEventListener("submit", function (event) {
        if (!window.confirm(form.getAttribute("data-confirm"))) {
          event.preventDefault();
        }
      });
    });

    // Prevent double submits on slow devices and show that something is happening.
    document.querySelectorAll("form").forEach(function (form) {
      form.addEventListener("submit", function (event) {
        if (event.defaultPrevented) { return; }
        var button = form.querySelector('button[type="submit"]');
        if (button && form.dataset.busyText !== undefined) {
          button.disabled = true;
          button.textContent = form.dataset.busyText || "Please wait…";
        }
      });
    });

    // Auto-submit filter forms when a select changes.
    document.querySelectorAll("select[data-autosubmit]").forEach(function (select) {
      select.addEventListener("change", function () { select.form.submit(); });
    });

    // Remember where a student stopped in a video or audio file.
    document.querySelectorAll("[data-save-position]").forEach(function (media) {
      var url = media.getAttribute("data-save-position");
      var start = parseFloat(media.getAttribute("data-start") || "0");
      if (start > 0) {
        media.addEventListener("loadedmetadata", function () {
          if (start < media.duration - 2) { media.currentTime = start; }
        }, { once: true });
      }
      var last = 0;
      function save() {
        var t = Math.floor(media.currentTime || 0);
        if (Math.abs(t - last) < 5 && !media.paused) { return; }
        last = t;
        var body = new FormData();
        body.append("position", String(t));
        fetch(url, { method: "POST", body: body, headers: { "X-CSRFToken": csrfToken() }, credentials: "same-origin" })
          .catch(function () { /* offline or server busy: try again later */ });
      }
      media.addEventListener("timeupdate", save);
      media.addEventListener("pause", save);
    });

    // Show the chosen file name and size next to file inputs.
    document.querySelectorAll('input[type="file"]').forEach(function (input) {
      input.addEventListener("change", function () {
        var out = document.getElementById(input.id + "-info");
        if (!out || !input.files.length) { return; }
        var f = input.files[0];
        out.textContent = f.name + " (" + (f.size / 1048576).toFixed(1) + " MB)";
      });
    });
  });
})();
