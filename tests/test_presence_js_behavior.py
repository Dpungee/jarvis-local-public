from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "jarvis" / "presence.js"


def function_block(source: str, name: str) -> str:
    for prefix in (f"async function {name}(", f"function {name}("):
        start = source.find(prefix)
        if start >= 0:
            break
    else:
        raise AssertionError(f"Presence function {name!r} was not found")
    brace = source.find("{", start)
    depth = 0
    quote: str | None = None
    escaped = False
    in_regex = False
    regex_class = False
    index = brace
    while index < len(source):
        character = source[index]
        following = source[index + 1] if index + 1 < len(source) else ""
        if quote is not None:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            index += 1
            continue
        if in_regex:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == "[":
                regex_class = True
            elif character == "]":
                regex_class = False
            elif character == "/" and not regex_class:
                in_regex = False
            index += 1
            continue
        if character in {'"', "'", "`"}:
            quote = character
            index += 1
            continue
        if character == "/" and following == "/":
            newline = source.find("\n", index + 2)
            index = len(source) if newline < 0 else newline + 1
            continue
        if character == "/" and following == "*":
            end = source.find("*/", index + 2)
            index = len(source) if end < 0 else end + 2
            continue
        if character == "/":
            prior = source[:index].rstrip()[-1:]
            if prior in {"(", "=", ":", ",", "!", "&", "|", "?", ";", "{"}:
                in_regex = True
                index += 1
                continue
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
        index += 1
    raise AssertionError(f"Presence function {name!r} is incomplete")


DOM_HARNESS = r"""
"use strict";
class TextNode {
  constructor(value) { this.nodeType = 3; this._text = String(value); this.parentElement = null; }
  get textContent() { return this._text; }
  set textContent(value) { this._text = String(value); }
  remove() {
    if (!this.parentElement) return;
    this.parentElement.children = this.parentElement.children.filter((item) => item !== this);
    this.parentElement = null;
  }
}
class Element {
  constructor(tagName) {
    this.nodeType = 1;
    this.tagName = String(tagName).toUpperCase();
    this.children = [];
    this.parentElement = null;
    this.className = "";
    this.attributes = {};
    this.listeners = {};
    this._text = "";
    this.open = false;
    this.hidden = false;
    this.scrollTop = 0;
    this.scrollHeight = 100;
    this.classList = {
      add: (...names) => {
        const values = new Set(this.className.split(/\s+/).filter(Boolean));
        names.forEach((name) => values.add(name));
        this.className = [...values].join(" ");
      },
      remove: (...names) => {
        const removed = new Set(names);
        this.className = this.className.split(/\s+/).filter((name) => name && !removed.has(name)).join(" ");
      },
      contains: (name) => this.className.split(/\s+/).includes(name),
      toggle: (name, force) => {
        const desired = force === undefined ? !this.classList.contains(name) : Boolean(force);
        if (desired) this.classList.add(name); else this.classList.remove(name);
        return desired;
      },
    };
  }
  get textContent() { return this._text + this.children.map((item) => item.textContent).join(""); }
  set textContent(value) { this.replaceChildren(); this._text = String(value); }
  get childElementCount() { return this.children.filter((item) => item.nodeType === 1).length; }
  append(...items) {
    for (let item of items) {
      if (!(item instanceof Element) && !(item instanceof TextNode)) item = new TextNode(item);
      if (item.parentElement) item.remove();
      item.parentElement = this;
      this.children.push(item);
    }
  }
  appendChild(item) { this.append(item); return item; }
  replaceChildren(...items) {
    this.children.forEach((item) => { item.parentElement = null; });
    this.children = [];
    this._text = "";
    this.append(...items);
  }
  after(item) {
    if (!this.parentElement) return;
    const index = this.parentElement.children.indexOf(this);
    item.parentElement = this.parentElement;
    this.parentElement.children.splice(index + 1, 0, item);
  }
  remove() {
    if (!this.parentElement) return;
    this.parentElement.children = this.parentElement.children.filter((item) => item !== this);
    this.parentElement = null;
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); }
  dispatchEvent(name) { for (const callback of this.listeners[name] || []) callback({target: this}); }
  click() { this.dispatchEvent("click"); }
  showModal() { this.open = true; }
  close() { this.open = false; this.dispatchEvent("close"); }
  querySelector(selector) { return findAll(this, selector)[0] || null; }
  querySelectorAll(selector) { return findAll(this, selector); }
  set href(value) { this.attributes.href = String(value); }
  get href() { return this.attributes.href || ""; }
  set rel(value) { this.attributes.rel = String(value); }
  get rel() { return this.attributes.rel || ""; }
  set target(value) { this.attributes.target = String(value); }
  get target() { return this.attributes.target || ""; }
}
function findAll(root, selector) {
  const output = [];
  const matches = (node) => {
    if (!(node instanceof Element)) return false;
    if (selector.startsWith(".")) return node.classList.contains(selector.slice(1));
    return node.tagName === selector.toUpperCase();
  };
  const visit = (node) => {
    for (const child of node.children || []) {
      if (matches(child)) output.push(child);
      visit(child);
    }
  };
  visit(root);
  return output;
}
const nodes = {};
const document = {
  body: new Element("body"),
  createElement: (name) => new Element(name),
  createTextNode: (value) => new TextNode(value),
};
const $ = (id) => nodes[id];
const localValues = new Map();
const localStorage = {
  setItem: (key, value) => localValues.set(String(key), String(value)),
  getItem: (key) => localValues.get(String(key)) ?? null,
};
const window = {matchMedia: () => ({matches: true})};
function assert(condition, message) { if (!condition) throw new Error(message); }
"""


@unittest.skipUnless(shutil.which("node"), "Node.js is required for Presence behavior tests")
class PresenceJavaScriptBehaviorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = SCRIPT_PATH.read_text(encoding="utf-8")
        cls.node = shutil.which("node") or "node"

    def run_harness(self, functions: tuple[str, ...], body: str) -> None:
        source = DOM_HARNESS + "\n" + "\n".join(
            function_block(self.source, name) for name in functions
        ) + "\n" + body
        completed = subprocess.run(
            [self.node, "-e", source],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)

    def test_safe_bare_and_markdown_urls_linkify_without_evaluating_text(self) -> None:
        self.run_harness(
            ("safeHttpUrl", "trimBareUrl", "renderLinkedText"),
            r"""
const container = new Element("div");
renderLinkedText(
  container,
  'Bare https://example.com/path?q=1, [Docs](https://docs.example.com/start). <img src=x onerror=boom> [bad](javascript:alert(1))',
);
const links = container.querySelectorAll("a");
assert(links.length === 2, `expected two safe links, got ${links.length}`);
assert(links[0].href === "https://example.com/path?q=1", "bare URL punctuation was not trimmed");
assert(links[0].textContent === "https://example.com/path?q=1", "bare label changed");
assert(links[1].textContent === "Docs", "Markdown label was not preserved");
for (const link of links) {
  assert(link.target === "_blank", "safe target missing");
  assert(link.rel === "noopener noreferrer", "safe rel missing");
}
assert(container.querySelectorAll("img").length === 0, "XSS text became an element");
assert(container.textContent.includes("<img src=x onerror=boom>"), "XSS text was not preserved as text");
assert(container.textContent.includes("javascript:alert(1)"), "unsafe scheme disappeared or executed");
""",
        )

    def test_product_cards_bound_missing_malicious_duplicate_and_stale_data(self) -> None:
        comparison = {
            "ranking": "Best verified matches",
            "products": [
                {
                    "name": "Board One",
                    "price_text": "$99",
                    "currency": "USD",
                    "observed_at": "2000-01-01T00:00:00Z",
                    "availability": "In stock",
                    "seller": "Seller A",
                    "manufacturer": "Maker A",
                    "source_kind": "seller",
                    "key_specs": ["ANSI", "RGB"],
                    "why_fit": "Matches requirements",
                    "tradeoff": "Wired only",
                    "source_url": "https://example.com/one",
                },
                {"name": "Board One", "source_url": "https://example.com/one"},
                {"name": "Missing Fields", "source_url": "javascript:alert(1)"},
                {"name": "Fourth", "source_url": "https://example.com/four"},
                {"name": "Fifth must be bounded", "source_url": "https://example.com/five"},
            ],
        }
        self.run_harness(
            ("safeHttpUrl", "productFact", "staleObservation", "renderProductComparison"),
            f"""
const article = new Element("article");
const bubble = new Element("div"); bubble.className = "bubble"; article.append(bubble);
renderProductComparison(article, {json.dumps(comparison)});
const cards = article.querySelectorAll(".product-card");
assert(cards.length === 4, `product cards were not bounded: ${{cards.length}}`);
assert(cards[0].textContent.includes("stale"), "stale observation was not labeled");
assert(cards[2].textContent.includes("Unavailable"), "missing fields did not get placeholders");
assert(cards[2].querySelectorAll("a").length === 0, "malicious product URL became clickable");
assert(cards[0].querySelector("a").rel === "noopener noreferrer", "product link rel is unsafe");
assert(cards[0].textContent.includes("Board One") && cards[1].textContent.includes("Board One"), "duplicate input was not rendered deterministically");
assert(!article.textContent.includes("Fifth must be bounded"), "fifth product escaped the four-card bound");
""",
        )

    def test_message_deltas_and_finals_remain_job_correlated(self) -> None:
        self.run_harness(
            (
                "safeHttpUrl",
                "trimBareUrl",
                "renderLinkedText",
                "splitTableRow",
                "markdownBlocks",
                "renderInline",
                "makeCodeBlock",
                "renderList",
                "renderTable",
                "renderMarkdown",
                "formatMessageTime",
                "copyToClipboard",
                "messageActions",
                "renderMessageContent",
                "appendMessage",
                "appendAssistantDelta",
                "finalizeAssistantStream",
            ),
            r"""
const messages = new Element("main");
const state = {streamNodes: new Map(), projectId: null};
const imageArtifactPattern = /\[\[jarvis-image:([A-Za-z0-9][A-Za-z0-9._/-]{0,999})\]\]/g;
appendAssistantDelta("job-a", "Partial ", messages);
appendAssistantDelta("job-a", "answer", messages);
appendAssistantDelta("job-b", "Other", messages);
assert(state.streamNodes.size === 2, "streams were not job-correlated");
const firstArticle = state.streamNodes.get("job-a").article;
assert(firstArticle.querySelector(".content").textContent === "Partial answer", "deltas did not assemble");
const finalized = finalizeAssistantStream("job-a", "Authoritative https://example.com/final", "model · complete", messages);
assert(finalized === firstArticle, "final created a duplicate article");
assert(!state.streamNodes.has("job-a") && state.streamNodes.has("job-b"), "final removed the wrong stream");
assert(finalized.querySelectorAll("a").length === 1, "authoritative final was not linkified");
assert(finalized.querySelector(".content").textContent.includes("Authoritative"), "final did not replace delta text");
""",
        )

    def test_incident_dialog_renders_normalized_receipt_and_closes_when_empty(self) -> None:
        incident = {
            "incident_id": "a" * 32,
            "receipt_id": "b" * 32,
            "created_at": "2026-08-28T12:00:00Z",
            "severity": "high",
            "category": "New device",
            "device": {
                "device_id": "device-1",
                "display_name": "Unknown phone",
                "device_type": "Phone",
                "manufacturer": "Unknown",
            },
            "observed_fact": "First observed on the private LAN.",
            "assessment": "Operator identification is required.",
            "confidence": "limited",
            "compromise_established": False,
            "evidence_summary": ["DHCP observation"],
            "automatic_actions": [],
            "actions_not_taken": ["No blocking was performed."],
            "recommended_action": "Identify the device.",
            "limitations": ["Observation is not proof of compromise."],
        }
        self.run_harness(
            (
                "makePill",
                "formatTimestamp",
                "networkFact",
                "boundedIncidentText",
                "incidentTextList",
                "normalizeNetworkDefenseIncident",
                "incidentSection",
                "automaticActionText",
                "renderNetworkDefenseIncidents",
                "maybeShowNetworkDefenseIncidents",
            ),
            f"""
nodes["network-defense-incident-list"] = new Element("div");
nodes["network-defense-incident-dialog"] = new Element("dialog");
nodes["approval-dialog"] = new Element("dialog");
nodes["feature-onboarding-dialog"] = new Element("dialog");
nodes["new-network-device-dialog"] = new Element("dialog");
nodes["new-bluetooth-device-dialog"] = new Element("dialog");
const state = {{networkDefenseIncidents: new Map()}};
const normalized = normalizeNetworkDefenseIncident({json.dumps(incident)});
assert(normalized && normalized.severity === "high", "incident normalization failed");
state.networkDefenseIncidents.set(normalized.key, normalized);
maybeShowNetworkDefenseIncidents();
assert(nodes["network-defense-incident-dialog"].open, "incident dialog did not open");
assert(nodes["network-defense-incident-list"].querySelectorAll(".network-defense-incident-card").length === 1, "incident card missing");
assert(nodes["network-defense-incident-list"].textContent.includes("Receipt {'b' * 32}"), "receipt was not rendered");
assert(nodes["network-defense-incident-list"].textContent.includes("Compromise is not established"), "safety boundary missing");
state.networkDefenseIncidents.clear();
renderNetworkDefenseIncidents();
assert(!nodes["network-defense-incident-dialog"].open, "empty incident state did not close dialog");
""",
        )

    def test_narrow_screen_state_collapses_navigation_deterministically(self) -> None:
        self.run_harness(
            ("setRailCollapsed",),
            r"""
assert(window.matchMedia("(max-width: 760px)").matches, "harness is not narrow");
if (window.matchMedia("(max-width: 760px)").matches) setRailCollapsed(true);
assert(document.body.classList.contains("rail-collapsed"), "narrow mode did not collapse rail");
assert(localStorage.getItem("jarvis.presence.rail-collapsed") === "1", "narrow state was not persisted");
""",
        )

    def test_incident_dialog_close_button_binding_closes_the_dialog(self) -> None:
        start = self.source.index('$("close-network-defense-incident").addEventListener(')
        end = self.source.index("\n);", start) + len("\n);")
        close_binding = self.source[start:end]
        self.run_harness(
            (),
            f"""
nodes["close-network-defense-incident"] = new Element("button");
nodes["network-defense-incident-dialog"] = new Element("dialog");
{close_binding}
nodes["network-defense-incident-dialog"].showModal();
nodes["close-network-defense-incident"].click();
assert(!nodes["network-defense-incident-dialog"].open, "incident close control did not close dialog");
""",
        )


if __name__ == "__main__":
    unittest.main()
