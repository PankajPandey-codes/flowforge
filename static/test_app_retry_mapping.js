"use strict";

// No JS test framework exists in this repo. Node ships a built-in test
// runner (node:test) since v18, so this needs zero new dependencies.
// app.js is a plain DOM script (no module system), so it's loaded into a
// vm sandbox with a minimal document/fetch stub rather than required
// directly, and the DOMContentLoaded handler at the bottom of the file
// never fires because the sandboxed addEventListener is a no-op.
//
// Run with: node tools/capture/static/test_app_retry_mapping.js

const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

function loadApp(stateOverrides) {
  // app.js declares `const App`, a top-level lexical binding — it never
  // becomes a property of the sandbox object itself, so every interaction
  // (seeding state, calling functions, reading results) has to happen via
  // vm.runInContext against the same context rather than reaching in from
  // the outside.
  const source = fs.readFileSync(path.join(__dirname, "app.js"), "utf8");
  const sandbox = {
    document: { addEventListener: () => {} },
    fetch: () => Promise.resolve({ json: () => Promise.resolve({}) }),
    console,
  };
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox);
  vm.runInContext(`Object.assign(App, ${JSON.stringify(stateOverrides)});`, sandbox);
  return {
    existingMappingsForRetry: () => vm.runInContext("existingMappingsForRetry()", sandbox),
  };
}

test("preserves a test case the auto-mapper matched but the user never clicked", () => {
  const sandbox = loadApp({
    mappings: [{ tc: 1, api_id: 5, confidence: 92, reason: "auto-mapped" }],
    rowState: { 1: { flowId: 5, manuallySelected: false } },
  });

  const result = sandbox.existingMappingsForRetry();

  assert.strictEqual(result.length, 1);
  assert.strictEqual(result[0].tc, 1);
  assert.strictEqual(result[0].api_id, 5);
});

test("still preserves a manually-selected test case", () => {
  const sandbox = loadApp({
    mappings: [],
    rowState: { 3: { flowId: 9, manuallySelected: true } },
  });

  const result = sandbox.existingMappingsForRetry();

  assert.strictEqual(result.length, 1);
  assert.strictEqual(result[0].tc, 3);
  assert.strictEqual(result[0].api_id, 9);
});

test("a manual selection overrides a stale auto-mapped entry for the same TC", () => {
  const sandbox = loadApp({
    mappings: [{ tc: 2, api_id: 5, confidence: 80, reason: "auto-mapped" }],
    rowState: { 2: { flowId: 11, manuallySelected: true } },
  });

  const result = sandbox.existingMappingsForRetry();

  assert.strictEqual(result.length, 1);
  assert.strictEqual(result[0].api_id, 11);
});

test("a row manually cleared back to no-match is not preserved as still-mapped", () => {
  const sandbox = loadApp({
    mappings: [{ tc: 4, api_id: 5, confidence: 80, reason: "auto-mapped" }],
    rowState: { 4: { flowId: null, manuallySelected: false } },
  });

  const result = sandbox.existingMappingsForRetry();

  assert.strictEqual(result.length, 0);
});

test("an untouched auto-mapped row with no rowState entry yet is still preserved", () => {
  const sandbox = loadApp({
    mappings: [{ tc: 6, api_id: 20, confidence: 70, reason: "auto-mapped" }],
    rowState: {},
  });

  const result = sandbox.existingMappingsForRetry();

  assert.strictEqual(result.length, 1);
  assert.strictEqual(result[0].api_id, 20);
});
