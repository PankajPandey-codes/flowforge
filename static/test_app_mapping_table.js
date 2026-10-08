"use strict";

// Same rationale as test_app_retry_mapping.js: no JS test framework exists
// in this repo, so this uses Node's built-in node:test (zero new deps).
// buildMappingTable() is DOM-heavy (creates elements, queries into them,
// wires event listeners), so this harness needs a slightly richer document
// stub than the retry-mapping tests: selectors are cached so the same
// element instance is returned across repeated queries (e.g. the tbody
// captured once inside buildMappingTable, then queried again here to read
// back the resulting HTML), and elements support the innerHTML/appendChild/
// classList/dataset/addEventListener surface buildMappingTable touches.
//
// Run with: node tools/capture/static/test_app_mapping_table.js

const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

function fakeElement() {
  return {
    innerHTML: "",
    textContent: "",
    dataset: {},
    style: {},
    classList: { add() {}, remove() {}, contains() { return false; } },
    appendChild(child) {
      this.innerHTML += child.innerHTML || "";
    },
    querySelector() { return fakeElement(); },
    querySelectorAll() { return []; },
    addEventListener() {},
  };
}

function loadAppForRendering(stateOverrides) {
  const source = fs.readFileSync(path.join(__dirname, "app.js"), "utf8");
  const elementsBySelector = new Map();
  const documentStub = {
    addEventListener: () => {},
    querySelector(sel) {
      if (!elementsBySelector.has(sel)) elementsBySelector.set(sel, fakeElement());
      return elementsBySelector.get(sel);
    },
    querySelectorAll: () => [],
    createElement: () => fakeElement(),
  };
  const sandbox = {
    document: documentStub,
    fetch: () => Promise.resolve({ json: () => Promise.resolve({}) }),
    console,
  };
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox);
  vm.runInContext(`Object.assign(App, ${JSON.stringify(stateOverrides)});`, sandbox);
  return {
    buildMappingTableHtml() {
      vm.runInContext("buildMappingTable()", sandbox);
      return documentStub.querySelector("#mapping-tbody").innerHTML;
    },
  };
}

test("a downgraded mapping's reason is surfaced as a tooltip in the rendered row", () => {
  const app = loadAppForRendering({
    testCases: [{ testScenarioNumber: "1", testObjective: "Disburse the loan", expectedResponseCode: 200 }],
    mappings: [{
      tc: 1,
      api_id: null,
      confidence: 40,
      reason: "No keyword overlap between the test objective and this flow's path/body despite LLM confidence 85 — please double-check this mapping.",
    }],
    rowState: {},
    presets: {},
  });

  const html = app.buildMappingTableHtml();

  assert.ok(html.includes("please double-check this mapping"), "expected the reason text in the rendered row");
});

test("a row with no reason renders without a broken/empty tooltip artifact", () => {
  const app = loadAppForRendering({
    testCases: [{ testScenarioNumber: "1", testObjective: "Disburse the loan", expectedResponseCode: 200 }],
    mappings: [{ tc: 1, api_id: null, confidence: 0 }],
    rowState: {},
    presets: {},
  });

  assert.doesNotThrow(() => app.buildMappingTableHtml());
});
