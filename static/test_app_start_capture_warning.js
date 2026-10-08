"use strict";

// Same rationale as the other test_app_*.js files in this directory: no JS
// test framework exists in this repo, so this uses Node's built-in
// node:test. startCaptureWarning() needs no DOM at all, but app.js still
// needs the minimal document stub to load (DOMContentLoaded at the bottom).
//
// Run with: node tools/capture/static/test_app_start_capture_warning.js

const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

function loadApp() {
  const source = fs.readFileSync(path.join(__dirname, "app.js"), "utf8");
  const sandbox = {
    document: { addEventListener: () => {} },
    fetch: () => Promise.resolve({ json: () => Promise.resolve({}) }),
    console,
  };
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox);
  return {
    startCaptureWarning: (data) => vm.runInContext(`startCaptureWarning(${JSON.stringify(data)})`, sandbox),
  };
}

test("warns when the proxy did not start correctly despite an ok response", () => {
  const app = loadApp();

  const warning = app.startCaptureWarning({ ok: true, proxy_ok: false });

  assert.ok(warning, "expected a non-null warning message");
});

test("no warning when the proxy started correctly", () => {
  const app = loadApp();

  assert.strictEqual(app.startCaptureWarning({ ok: true, proxy_ok: true }), null);
});

test("no warning when the request itself failed (a different alert already covers that)", () => {
  const app = loadApp();

  assert.strictEqual(app.startCaptureWarning({ ok: false, proxy_ok: false }), null);
});
