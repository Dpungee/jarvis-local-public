"use strict";

const state = {
  conversationId: null,
  secondaryConversationId: null,
  splitEnabled: false,
  activeJobs: new Map(),
  conversations: new Map(),
  projectId: 1,
  lastEventId: 0,
  runtimeEpoch: null,
  polling: false,
  recovering: false,
  recoveryPromise: null,
  recognition: null,
  progressNodes: new Map(),
  streamNodes: new Map(),
  specialists: [],
  selectedAgent: "jarvis",
  projects: [],
  pinnedProjects: new Set(),
  activeView: "home",
  utilityGeneration: 0,
  models: {},
  jobs: [],
  pendingImages: [],
  screenCompanion: null,
  publicPresence: null,
  featureOnboarding: null,
  featureDecisionPending: new Set(),
  onboardingDismissedForSession: false,
  networkInventory: null,
  networkScanPending: false,
  networkDeviceDetail: null,
  networkDeviceFilter: "all",
  networkDeviceSearch: "",
  notifiedNetworkDevices: new Set(),
  bluetoothInventory: null,
  bluetoothCheckPending: false,
  bluetoothDeviceDetail: null,
  notifiedBluetoothDevices: new Set(),
  visibleBluetoothAlerts: new Map(),
  networkDefenseIncidents: new Map(),
  pendingDeleteConversationId: null,
  pageStartedAt: Date.now() / 1000,
};

const networkAlertStorageKey = "jarvis.network.first-observed-alerts.v1";

function loadNetworkAlertReceipts() {
  if (state.notifiedNetworkDevices.size) return;
  try {
    const values = JSON.parse(window.localStorage.getItem(networkAlertStorageKey) || "[]");
    if (!Array.isArray(values)) return;
    for (const value of values.slice(-500)) {
      if (typeof value === "string" && value.length <= 300) {
        state.notifiedNetworkDevices.add(value);
      }
    }
  } catch (_error) {
    // Storage may be unavailable in privacy modes; the event-time gate still
    // prevents a newly opened page from replaying historical alerts.
  }
}

function rememberNetworkAlertReceipt(value) {
  state.notifiedNetworkDevices.add(value);
  try {
    window.localStorage.setItem(
      networkAlertStorageKey,
      JSON.stringify([...state.notifiedNetworkDevices].slice(-500)),
    );
  } catch (_error) {
    // The visible alert remains correct even when persistence is unavailable.
  }
}

const bluetoothAlertStorageKey = "jarvis.bluetooth.first-observed-alerts.v1";

function loadBluetoothAlertReceipts() {
  if (state.notifiedBluetoothDevices.size) return;
  try {
    const values = JSON.parse(
      window.localStorage.getItem(bluetoothAlertStorageKey) || "[]",
    );
    if (!Array.isArray(values)) return;
    for (const value of values.slice(-500)) {
      if (typeof value === "string" && value.length <= 300) {
        state.notifiedBluetoothDevices.add(value);
      }
    }
  } catch (_error) {
    // The event-time gate still prevents historical replay without storage.
  }
}

function rememberBluetoothAlertReceipt(value) {
  state.notifiedBluetoothDevices.add(value);
  try {
    window.localStorage.setItem(
      bluetoothAlertStorageKey,
      JSON.stringify([...state.notifiedBluetoothDevices].slice(-500)),
    );
  } catch (_error) {
    // The current visible alert remains safe and bounded.
  }
}

const $ = (id) => document.getElementById(id);
const messages = $("messages");
const prompt = $("prompt");
const send = $("send");
const stop = $("stop");
const activity = $("activity");
const attachImage = $("attach-image");
const imageInput = $("image-input");
const imagePreview = $("image-preview");
const secondaryMessages = $("secondary-messages");
const secondaryPrompt = $("secondary-prompt");
const secondarySend = $("secondary-send");
const secondaryStop = $("secondary-stop");
const sessionKey = "jarvis.presence.session";
const maxImages = 4;
const maxImageBytes = 5 * 1024 * 1024;
const allowedImageTypes = new Set(["image/png", "image/jpeg", "image/webp", "image/gif"]);

async function api(path, options = {}) {
  const token = sessionStorage.getItem(sessionKey);
  const headers = {...(options.headers || {})};
  if (token && path !== "/api/pair") headers.Authorization = `Bearer ${token}`;
  if (options.body) headers["Content-Type"] = "application/json";
  const response = await fetch(path, {
    cache: "no-store",
    credentials: "omit",
    ...options,
    headers,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(payload.error || `Request failed (${response.status})`);
    error.status = response.status;
    error.retryAfterSeconds = Number(payload.retry_after_seconds) || null;
    if (response.status === 401 && path !== "/api/pair") showPairing();
    throw error;
  }
  return payload;
}

function post(path, payload = {}) {
  return api(path, {method: "POST", body: JSON.stringify(payload)});
}

function deleteRequest(path) {
  return api(path, {method: "DELETE"});
}

function toast(message) {
  const node = $("toast");
  node.textContent = String(message);
  node.classList.add("show");
  clearTimeout(node._timer);
  node._timer = setTimeout(() => node.classList.remove("show"), 3200);
}

function currentJobId() {
  return state.activeJobs.get(state.conversationId) || null;
}

function secondaryJobId() {
  return state.activeJobs.get(state.secondaryConversationId) || null;
}

function visibleJobActive() {
  return Boolean(currentJobId() || (state.splitEnabled && secondaryJobId()));
}

function activeProject() {
  return state.projects.find((project) => project.id === state.projectId) || null;
}

function updateProjectChrome() {
  const project = activeProject();
  if (!project) return;
  $("project-context").textContent = project.name;
  $("project-context").title = `Open ${project.name} workspace`;
  const badge = document.querySelector(".isolation-badge");
  if (badge) badge.textContent = project.isolated ? "Isolated" : "Main";
  $("project-scope-copy").textContent = project.isolated
    ? `${String(project.kind || "general").replace(/^./, (letter) => letter.toUpperCase())} project · chats and files stay in this workspace.`
    : "Your default workspace for general Jarvis work.";
}

function messageTarget(conversationId) {
  if (conversationId === state.conversationId) return messages;
  if (state.splitEnabled && conversationId === state.secondaryConversationId) return secondaryMessages;
  return null;
}

function setConversationActivity(conversationId, text) {
  if (conversationId === state.conversationId) activity.textContent = text;
  if (state.splitEnabled && conversationId === state.secondaryConversationId) {
    $("secondary-activity").textContent = text;
  }
}

function syncBusy() {
  const jobId = currentJobId();
  send.disabled = Boolean(jobId) || state.recovering;
  stop.disabled = !jobId;
  attachImage.disabled = Boolean(jobId) || state.recovering;
  const splitJobId = secondaryJobId();
  secondarySend.disabled = !state.splitEnabled || !state.secondaryConversationId || Boolean(splitJobId) || state.recovering;
  secondaryStop.disabled = !splitJobId;
}

function adoptRuntimeEpoch(nextEpoch) {
  if (typeof nextEpoch !== "string" || !/^[0-9a-f]{32}$/.test(nextEpoch)) return false;
  if (state.runtimeEpoch === null) {
    state.runtimeEpoch = nextEpoch;
    return false;
  }
  if (state.runtimeEpoch === nextEpoch) return false;
  state.runtimeEpoch = nextEpoch;
  state.lastEventId = 0;
  return true;
}

function replaceTrackedJobs(data) {
  const jobs = Array.isArray(data.jobs) ? data.jobs : (data.active_jobs || []);
  state.activeJobs = new Map(
    jobs
      .filter((job) => job?.conversation_id && job?.job_id)
      .map((job) => [job.conversation_id, job.job_id]),
  );
  syncBusy();
}

function providerLabel(provider = {}) {
  const labels = [];
  if (provider.openai_configured) {
    labels.push(provider.openai_healthy === true
      ? "OpenAI healthy"
      : provider.openai_healthy === false
        ? "OpenAI circuit open"
        : "OpenAI configured · unverified");
  }
  if (provider.anthropic_configured) {
    labels.push(provider.anthropic_healthy === true
      ? "Anthropic healthy"
      : provider.anthropic_healthy === false
        ? "Anthropic circuit open"
        : "Anthropic configured · unverified");
  }
  if (provider.codex_cli_configured) {
    labels.push(provider.codex_cli_auth_method !== "chatgpt"
      ? "Codex subscription sign-in required"
      : provider.codex_cli_healthy === true
        ? "Codex subscription healthy"
        : provider.codex_cli_healthy === false
          ? "Codex subscription circuit open"
          : "Codex subscription configured · unverified");
  }
  if (provider.claude_cli_configured) {
    labels.push(provider.claude_cli_healthy === true
      ? "Claude CLI healthy"
      : provider.claude_cli_healthy === false
        ? "Claude CLI circuit open"
        : "Claude CLI configured · unverified");
  }
  if (provider.ollama_enabled === false) {
    labels.push("Ollama disabled");
  } else if (provider.ollama_enabled === true || provider.ollama_online != null) {
    labels.push(provider.ollama_online === true
      ? "Ollama online"
      : provider.ollama_online === false
        ? "Ollama offline"
        : "Ollama configured · unchecked");
  }
  return labels.length ? labels.join(" · ") : "No provider configured";
}

const imageArtifactPattern = /\[\[jarvis-image:([A-Za-z0-9][A-Za-z0-9._/-]{0,999})\]\]/g;

function safeHttpUrl(value) {
  try {
    const parsed = new URL(String(value || ""));
    if (!["http:", "https:"].includes(parsed.protocol) || parsed.username || parsed.password) return null;
    return parsed.href;
  } catch (_error) {
    return null;
  }
}

function trimBareUrl(value) {
  let url = String(value || "");
  while (/[.,!?;:\]}]$/.test(url)) url = url.slice(0, -1);
  while (url.endsWith(")") && (url.match(/\(/g) || []).length < (url.match(/\)/g) || []).length) {
    url = url.slice(0, -1);
  }
  return url;
}

function renderLinkedText(container, value) {
  const text = String(value || "");
  const pattern = /\[([^\]\r\n]{1,300})\]\((https?:\/\/[^\s<>"']+)\)|(https?:\/\/[^\s<>"']+)/gi;
  let cursor = 0;
  let match;
  container.replaceChildren();
  while ((match = pattern.exec(text)) !== null) {
    if (match.index > cursor) container.append(document.createTextNode(text.slice(cursor, match.index)));
    const markdown = Boolean(match[1]);
    const rawUrl = markdown ? match[2] : trimBareUrl(match[3]);
    const href = safeHttpUrl(rawUrl);
    if (!href) {
      container.append(document.createTextNode(match[0]));
    } else {
      const link = document.createElement("a");
      link.href = href;
      link.textContent = markdown ? match[1] : rawUrl;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      container.append(link);
      if (!markdown && rawUrl.length < match[3].length) {
        container.append(document.createTextNode(match[3].slice(rawUrl.length)));
      }
    }
    cursor = pattern.lastIndex;
  }
  if (cursor < text.length) container.append(document.createTextNode(text.slice(cursor)));
}

function renderMessageContent(body, content) {
  const raw = String(content || "");
  const paths = [];
  const visible = raw.replace(imageArtifactPattern, (_marker, path) => {
    const parts = String(path).split("/");
    if (!parts.includes("..") && !parts.includes("") && !String(path).startsWith("/")) {
      paths.push(String(path));
    }
    return "";
  }).trim();
  renderLinkedText(body, visible);
  if (!paths.length || !state.projectId) return;
  const gallery = document.createElement("div");
  gallery.className = "generated-images";
  for (const path of [...new Set(paths)]) {
    const link = document.createElement("a");
    link.href = `/api/artifacts/image?project_id=${encodeURIComponent(state.projectId)}&path=${encodeURIComponent(path)}`;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.title = `Open ${path}`;
    const thumbnail = document.createElement("img");
    thumbnail.src = link.href;
    thumbnail.alt = `Generated image: ${path}`;
    link.append(thumbnail);
    gallery.append(link);
  }
  body.after(gallery);
}

function productFact(label, value) {
  const row = document.createElement("div");
  row.className = "product-fact";
  const key = document.createElement("span");
  key.className = "product-fact-label";
  key.textContent = `${label}:`;
  const detail = document.createElement("span");
  detail.textContent = value || "Unavailable";
  row.append(key, detail);
  return row;
}

function staleObservation(value) {
  const observed = Date.parse(String(value || ""));
  return !Number.isFinite(observed) || Date.now() - observed > 24 * 60 * 60 * 1000;
}

function renderProductComparison(article, comparison) {
  if (!article || !comparison || !Array.isArray(comparison.products) || !comparison.products.length) return;
  const bubble = article.querySelector(".bubble");
  if (!bubble) return;
  bubble.querySelector(".product-comparison")?.remove();
  const section = document.createElement("section");
  section.className = "product-comparison";
  section.setAttribute("aria-label", "Verified product comparison");
  if (comparison.ranking) {
    const ranking = document.createElement("p");
    ranking.className = "product-ranking";
    ranking.textContent = comparison.ranking;
    section.append(ranking);
  }
  const grid = document.createElement("div");
  grid.className = "product-grid";
  for (const [index, product] of comparison.products.slice(0, 4).entries()) {
    const card = document.createElement("article");
    card.className = "product-card";
    const badge = document.createElement("div");
    badge.className = "product-rank";
    badge.textContent = `#${index + 1}`;
    const placeholder = document.createElement("div");
    placeholder.className = "product-image-placeholder";
    placeholder.textContent = "No verified image";
    const heading = document.createElement("h3");
    heading.textContent = product.name || "Unnamed product";
    const observed = product.observed_at
      ? `${product.observed_at}${staleObservation(product.observed_at) ? " · stale" : ""}`
      : "Unavailable";
    const price = [product.price_text, product.currency].filter(Boolean).join(" · ") || "Unavailable";
    card.append(badge, placeholder, heading);
    card.append(
      productFact("Observed price", price),
      productFact("Observed", observed),
      productFact("Availability", product.availability),
      productFact("Seller", product.seller),
      productFact("Manufacturer", product.manufacturer),
      productFact("Source type", product.source_kind),
    );
    if (Array.isArray(product.key_specs) && product.key_specs.length) {
      const specs = document.createElement("ul");
      specs.className = "product-specs";
      for (const spec of product.key_specs) {
        const item = document.createElement("li");
        item.textContent = spec;
        specs.append(item);
      }
      card.append(specs);
    } else {
      card.append(productFact("Matching specs", null));
    }
    card.append(productFact("Why it fits", product.why_fit), productFact("Tradeoff", product.tradeoff));
    const href = safeHttpUrl(product.source_url);
    if (href) {
      const link = document.createElement("a");
      link.className = "product-link";
      link.href = href;
      link.textContent = "Open verified product source";
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      card.append(link);
    }
    grid.append(card);
  }
  section.append(grid);
  bubble.append(section);
}

function appendMessage(role, content, meta = "", images = [], target = messages) {
  const article = document.createElement("article");
  article.className = `message ${role}`;
  const inner = document.createElement("div");
  inner.className = "message-inner";
  const avatar = document.createElement("div");
  avatar.className = "avatar";
  avatar.textContent = role === "user" ? "Y" : "J";
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  const label = document.createElement("div");
  label.className = "role";
  label.textContent = role === "user" ? "YOU" : "JARVIS";
  const body = document.createElement("div");
  body.className = "content";
  renderMessageContent(body, content);
  bubble.append(label, body);
  if (meta) {
    const detail = document.createElement("div");
    detail.className = "meta";
    detail.textContent = meta;
    bubble.append(detail);
  }
  if (Array.isArray(images) && images.length) {
    const gallery = document.createElement("div");
    gallery.className = "message-images";
    for (const image of images) {
      const thumbnail = document.createElement("img");
      thumbnail.src = image.url || `data:${image.mime};base64,${image.data}`;
      thumbnail.alt = image.name || "Attached image";
      gallery.append(thumbnail);
    }
    bubble.append(gallery);
  }
  inner.append(avatar, bubble);
  article.append(inner);
  target.append(article);
  target.scrollTop = target.scrollHeight;
  return article;
}

function renderImagePreview() {
  imagePreview.replaceChildren();
  imagePreview.hidden = state.pendingImages.length === 0;
  state.pendingImages.forEach((image, index) => {
    const chip = document.createElement("div");
    chip.className = "image-chip";
    const thumbnail = document.createElement("img");
    thumbnail.src = image.url;
    thumbnail.alt = image.name;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.textContent = "×";
    remove.title = `Remove ${image.name}`;
    remove.addEventListener("click", () => {
      URL.revokeObjectURL(state.pendingImages[index].url);
      state.pendingImages.splice(index, 1);
      renderImagePreview();
    });
    chip.append(thumbnail, remove);
    imagePreview.append(chip);
  });
}

async function addImageFiles(files) {
  const selected = Array.from(files || []);
  if (state.pendingImages.length + selected.length > maxImages) {
    throw new Error(`Attach at most ${maxImages} images per message.`);
  }
  for (const file of selected) {
    if (!allowedImageTypes.has(file.type)) {
      throw new Error(`${file.name} is not a supported PNG, JPEG, WebP, or GIF image.`);
    }
    if (file.size > maxImageBytes) {
      throw new Error(`${file.name} is larger than 5 MiB.`);
    }
    const bytes = new Uint8Array(await file.arrayBuffer());
    let binary = "";
    for (let offset = 0; offset < bytes.length; offset += 0x8000) {
      binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
    }
    state.pendingImages.push({
      name: file.name,
      mime: file.type,
      data: btoa(binary),
      url: URL.createObjectURL(file),
    });
  }
  renderImagePreview();
}

function appendAssistantDelta(jobId, text, target = messages) {
  if (!jobId || !text) return;
  let stream = state.streamNodes.get(jobId);
  if (!stream) {
    const article = appendMessage("assistant", "", "responding…", [], target);
    article.classList.add("streaming");
    stream = {
      article,
      body: article.querySelector(".content"),
      meta: article.querySelector(".meta"),
      text: "",
    };
    state.streamNodes.set(jobId, stream);
  }
  stream.text += String(text);
  stream.body.textContent = stream.text;
  target.scrollTop = target.scrollHeight;
}

function finalizeAssistantStream(jobId, content, meta = "", target = messages) {
  const stream = state.streamNodes.get(jobId);
  if (!stream) {
    return appendMessage("assistant", content, meta, [], target);
  }
  stream.article.classList.remove("streaming");
  stream.article.querySelector(".generated-images")?.remove();
  renderMessageContent(stream.body, content);
  if (stream.meta) stream.meta.textContent = meta;
  state.streamNodes.delete(jobId);
  target.scrollTop = target.scrollHeight;
  return stream.article;
}

function discardAssistantStream(jobId) {
  const stream = state.streamNodes.get(jobId);
  if (!stream) return;
  stream.article.remove();
  state.streamNodes.delete(jobId);
}

function showProgress(jobId, message = "Starting…", target = messages) {
  if (!jobId) return;
  let progress = state.progressNodes.get(jobId);
  if (!progress) {
    const article = appendMessage("assistant", "", "Live activity", [], target);
    article.classList.add("progress");
    progress = {
      article,
      body: article.querySelector(".content"),
      meta: article.querySelector(".meta"),
      steps: [],
    };
    state.progressNodes.set(jobId, progress);
  }
  const step = String(message || "Working").trim();
  if (step && progress.steps.at(-1) !== step) progress.steps.push(step);
  progress.steps = progress.steps.slice(-8);
  progress.body.textContent = progress.steps.map((item) => `• ${item}`).join("\n");
  target.scrollTop = target.scrollHeight;
}

function finishProgress(jobId, stateLabel = "Completed") {
  const progress = state.progressNodes.get(jobId);
  if (!progress) return;
  progress.article.classList.remove("progress");
  progress.article.classList.add("progress-finished");
  progress.meta.textContent = stateLabel;
  state.progressNodes.delete(jobId);
}

function renderAgentDetail() {
  const panel = $("agent-detail");
  if (state.selectedAgent === "jarvis") {
    panel.hidden = true;
    panel.replaceChildren();
    return;
  }
  if (state.selectedAgent !== "agents") {
    state.selectedAgent = "jarvis";
    panel.hidden = true;
    panel.replaceChildren();
    return;
  }
  const title = document.createElement("div");
  title.className = "agents-title";
  title.textContent = "Specialist agents";
  const grid = document.createElement("div");
  grid.className = "agent-grid";
  for (const specialist of state.specialists) {
    const card = document.createElement("article");
    card.className = "agent-card";
    const name = document.createElement("strong");
    name.textContent = specialist.name;
    const status = document.createElement("span");
    const participating = Boolean(specialist.participating);
    const visiblyActive = specialist.status === "working" || participating;
    status.className = `agent-detail-status ${visiblyActive ? "working" : "ready"}`;
    status.textContent = specialist.status === "working" ? "Working" : participating ? "Reported" : "Ready";
    const heading = document.createElement("div");
    heading.className = "agent-detail-heading";
    heading.append(name, status);
    const purpose = document.createElement("p");
    purpose.textContent = specialist.purpose || "Purpose unavailable";
    const assignment = document.createElement("p");
    assignment.className = "agent-assignment";
    assignment.textContent = specialist.active_task_prompt
      ? `Task #${specialist.active_task_id}: ${specialist.active_task_prompt}`
      : specialist.last_task_prompt
        ? `${participating ? "Reported for this request" : `Last task (${specialist.last_task_status || "complete"})`} #${specialist.last_task_id}: ${specialist.last_task_prompt}`
        : "Standing by for a Jarvis assignment.";
    const metrics = document.createElement("small");
    metrics.textContent = `${specialist.model || specialist.model_profile} · ${specialist.completed_tasks || 0} completed · ${specialist.failed_tasks || 0} failed`;
    card.append(heading, purpose, assignment, metrics);
    grid.append(card);
  }
  panel.replaceChildren(title, grid);
  panel.hidden = false;
}

function renderAgentTabs(specialists = [], models = {}) {
  state.specialists = specialists.map((item) => ({
    ...item,
    model: models[item.model_profile] || item.model || item.model_profile,
  }));
  const order = new Map(["coding", "research", "cybersecurity", "network", "operations"].map((key, index) => [key, index]));
  state.specialists.sort((left, right) => (order.get(left.agent_key) ?? 99) - (order.get(right.agent_key) ?? 99));
  const tabs = $("agent-tabs");
  const anyWorking = state.specialists.some((item) => item.status === "working" || item.participating);
  const roster = [
    {agent_key: "jarvis", name: "JARVIS", status: "ready"},
    {agent_key: "agents", name: "AGENTS", status: anyWorking ? "working" : "ready"},
  ];
  const nodes = roster.map((item) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `agent-tab${state.selectedAgent === item.agent_key ? " active" : ""}`;
    button.dataset.agentKey = item.agent_key;
    button.setAttribute("aria-selected", state.selectedAgent === item.agent_key ? "true" : "false");
    const dot = document.createElement("span");
    dot.className = `agent-state ${item.status === "working" ? "working" : "ready"}`;
    dot.setAttribute("aria-hidden", "true");
    const label = document.createElement("span");
    label.textContent = item.name;
    button.append(dot, label);
    button.addEventListener("click", () => {
      state.selectedAgent = item.agent_key;
      renderAgentTabs(state.specialists, {});
    });
    return button;
  });
  tabs.replaceChildren(...nodes);
  renderAgentDetail();
}

function speak(text) {
  if (!$("speak-toggle").checked || !window.speechSynthesis || !text) return;
  window.speechSynthesis.cancel();
  const utterance = new SpeechSynthesisUtterance(String(text).slice(0, 10000));
  utterance.rate = 1.03;
  utterance.pitch = .92;
  window.speechSynthesis.speak(utterance);
}

async function newConversation() {
  showChat();
  const result = await post("/api/conversations", {
    title: "Presence chat",
    project_id: state.projectId,
  });
  state.conversationId = result.conversation_id;
  $("chat-title").textContent = "New task";
  updateProjectChrome();
  localStorage.setItem("jarvis.presence.conversation", String(state.conversationId));
  clearTrackedNodesForTarget(messages);
  messages.replaceChildren();
  appendMessage("assistant", "New conversation started. What are we working on?");
  await refreshConversations();
  syncBusy();
}

async function loadConversation(id) {
  showChat();
  const swappedConversation = (
    state.splitEnabled
    && id === state.secondaryConversationId
    && state.conversationId !== id
  ) ? state.conversationId : null;
  const result = await api(`/api/conversations/${id}/messages`);
  state.conversationId = result.conversation_id;
  const conversation = state.conversations.get(state.conversationId);
  $("chat-title").textContent = conversation?.title || "Jarvis task";
  if (conversation?.project_id) {
    state.projectId = conversation.project_id;
    $("project").value = String(state.projectId);
    updateProjectChrome();
  }
  localStorage.setItem("jarvis.presence.conversation", String(state.conversationId));
  clearTrackedNodesForTarget(messages);
  messages.replaceChildren();
  if (!result.messages.length) {
    appendMessage("assistant", "Conversation ready. What are we working on?");
  } else {
    for (const item of result.messages) appendMessage(item.role, item.content);
  }
  if (swappedConversation) await loadSecondaryConversation(swappedConversation);
  await refreshConversations();
  syncBusy();
  if (window.matchMedia("(max-width: 760px)").matches) setRailCollapsed(true);
}

async function ensureConversation() {
  const saved = localStorage.getItem("jarvis.presence.conversation");
  if (/^[1-9][0-9]*$/.test(saved || "")) {
    try { await loadConversation(Number(saved)); return; } catch (_) {}
  }
  await newConversation();
}

function clearTrackedNodesForTarget(target) {
  for (const [jobId, progress] of state.progressNodes) {
    if (progress?.article?.parentElement === target) state.progressNodes.delete(jobId);
  }
  for (const [jobId, stream] of state.streamNodes) {
    if (stream?.article?.parentElement === target) state.streamNodes.delete(jobId);
  }
}

function renderSecondaryConversationOptions() {
  const select = $("secondary-conversation");
  select.replaceChildren();
  const available = [...state.conversations.values()].filter(
    (row) => row.project_id === state.projectId,
  );
  for (const row of available) {
    const option = document.createElement("option");
    option.value = String(row.id);
    option.disabled = row.id === state.conversationId;
    const running = state.activeJobs.has(row.id) ? " · Working" : "";
    option.textContent = `${row.title || "Untitled task"}${running}`;
    select.append(option);
  }
  if (state.secondaryConversationId && state.conversations.has(state.secondaryConversationId)) {
    select.value = String(state.secondaryConversationId);
  }
}

async function loadSecondaryConversation(id) {
  const numericId = Number(id);
  if (!Number.isInteger(numericId) || numericId <= 0 || numericId === state.conversationId) {
    throw new Error("Choose a different conversation for the second pane.");
  }
  const result = await api(`/api/conversations/${numericId}/messages`);
  state.secondaryConversationId = result.conversation_id;
  localStorage.setItem(
    "jarvis.presence.secondary-conversation",
    String(state.secondaryConversationId),
  );
  clearTrackedNodesForTarget(secondaryMessages);
  secondaryMessages.replaceChildren();
  if (!result.messages.length) {
    appendMessage(
      "assistant",
      "Independent conversation ready. What should I work on here?",
      "",
      [],
      secondaryMessages,
    );
  } else {
    for (const item of result.messages) {
      appendMessage(item.role, item.content, "", [], secondaryMessages);
    }
  }
  const conversation = state.conversations.get(state.secondaryConversationId);
  $("secondary-activity").textContent = state.activeJobs.has(state.secondaryConversationId)
    ? "Working…"
    : (conversation?.title || "Independent chat ready");
  renderSecondaryConversationOptions();
  syncBusy();
}

async function newSecondaryConversation() {
  const result = await post("/api/conversations", {
    title: "Parallel Jarvis chat",
    project_id: state.projectId,
  });
  await refreshConversations();
  await loadSecondaryConversation(result.conversation_id);
  secondaryPrompt.focus();
}

async function setSplitView(enabled) {
  state.splitEnabled = Boolean(enabled);
  document.body.classList.toggle("split-view", state.splitEnabled);
  $("secondary-pane").hidden = !state.splitEnabled;
  $("split-view").setAttribute("aria-pressed", state.splitEnabled ? "true" : "false");
  $("split-view").textContent = state.splitEnabled ? "Single" : "Split";
  localStorage.setItem("jarvis.presence.split-view", state.splitEnabled ? "1" : "0");
  if (!state.splitEnabled) {
    syncBusy();
    prompt.focus();
    return;
  }
  showChat();
  const saved = Number(localStorage.getItem("jarvis.presence.secondary-conversation"));
  const candidate = (
    Number.isInteger(saved)
    && saved > 0
    && saved !== state.conversationId
    && state.conversations.get(saved)?.project_id === state.projectId
  ) ? saved : [...state.conversations.values()].find(
    (row) => row.id !== state.conversationId && row.project_id === state.projectId,
  )?.id;
  if (candidate) await loadSecondaryConversation(candidate);
  else await newSecondaryConversation();
}

async function refreshConversations() {
  const result = await api("/api/conversations");
  state.conversations = new Map(result.conversations.map((row) => [row.id, row]));
  const list = $("conversation-list");
  list.replaceChildren();
  const projectRows = result.conversations.filter(
    (row) => row.project_id === state.projectId,
  );
  if (!projectRows.length) {
    const empty = document.createElement("div");
    empty.className = "conversation-empty";
    empty.textContent = "No chats here yet.";
    list.append(empty);
  }
  for (const row of projectRows) {
    const entry = document.createElement("div");
    entry.className = `conversation-entry${row.id === state.conversationId ? " active" : ""}`;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "conversation-item";
    const running = state.activeJobs.has(row.id) ? "  ·  Working" : "";
    const split = state.splitEnabled && row.id === state.secondaryConversationId ? "  ·  Split" : "";
    button.textContent = `${row.title || "Untitled task"}${running}${split}`;
    button.title = row.title || "Conversation";
    button.addEventListener("click", () => loadConversation(row.id).catch(showError));
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "conversation-delete";
    remove.textContent = "×";
    remove.title = `Delete ${row.title || "this chat"}`;
    remove.setAttribute("aria-label", remove.title);
    remove.disabled = state.activeJobs.has(row.id);
    remove.addEventListener("click", () => requestConversationDelete(row));
    entry.append(button, remove);
    list.append(entry);
  }
  renderSecondaryConversationOptions();
}

function requestConversationDelete(conversation) {
  if (!conversation || state.activeJobs.has(conversation.id)) {
    toast("Stop the active request before deleting this chat.");
    return;
  }
  state.pendingDeleteConversationId = conversation.id;
  $("delete-chat-copy").textContent = `Delete “${conversation.title || "Untitled chat"}”? The chat history is removed, while files in ${conversation.project_name || "the project"} stay untouched.`;
  $("delete-chat-dialog").showModal();
}

async function confirmConversationDelete() {
  const conversationId = state.pendingDeleteConversationId;
  if (!Number.isInteger(conversationId) || conversationId <= 0) return;
  const confirm = $("confirm-delete-chat");
  confirm.disabled = true;
  try {
    await deleteRequest(`/api/conversations/${conversationId}`);
    const wasPrimary = state.conversationId === conversationId;
    const wasSecondary = state.secondaryConversationId === conversationId;
    if (wasPrimary) {
      state.conversationId = null;
      localStorage.removeItem("jarvis.presence.conversation");
    }
    if (wasSecondary) {
      state.secondaryConversationId = null;
      localStorage.removeItem("jarvis.presence.secondary-conversation");
    }
    state.activeJobs.delete(conversationId);
    state.pendingDeleteConversationId = null;
    $("delete-chat-dialog").close();
    await refreshConversations();
    if (wasPrimary) {
      const next = [...state.conversations.values()].find(
        (row) => row.project_id === state.projectId && row.id !== state.secondaryConversationId,
      );
      if (next) await loadConversation(next.id);
      else await newConversation();
    } else if (wasSecondary && state.splitEnabled) {
      const next = [...state.conversations.values()].find(
        (row) => row.project_id === state.projectId && row.id !== state.conversationId,
      );
      if (next) await loadSecondaryConversation(next.id);
      else await newSecondaryConversation();
    }
    await refreshProjects();
    if (state.activeView === "projects") renderProjects();
    toast("Chat deleted. Project files were not changed.");
  } finally {
    confirm.disabled = false;
  }
}

async function refreshProjects() {
  const result = await api("/api/projects");
  state.projects = (result.projects || []).filter((row) => row.enabled);
  const select = $("project");
  const previous = state.projectId;
  select.replaceChildren();
  for (const row of state.projects) {
    const option = document.createElement("option");
    option.value = String(row.id);
    option.textContent = `${row.name} · ${row.conversation_count} chats`;
    select.append(option);
  }
  if ([...select.options].some((option) => Number(option.value) === previous)) {
    select.value = String(previous);
  } else if (select.options.length) {
    state.projectId = Number(select.value);
  }
  updateProjectChrome();
  const validIds = new Set(state.projects.map((row) => row.id));
  state.pinnedProjects = new Set(
    [...state.pinnedProjects].filter((projectId) => validIds.has(projectId)).slice(0, 20),
  );
  savePinnedProjects();
  renderPinnedProjects();
}

async function createProject() {
  const dialog = $("project-dialog");
  const input = $("project-name");
  input.value = "";
  $("project-kind").value = "general";
  $("project-description").value = "";
  dialog.showModal();
  requestAnimationFrame(() => input.focus());
}

async function submitProject(event) {
  event.preventDefault();
  const input = $("project-name");
  const submit = $("create-project");
  const name = input.value.trim();
  const kind = $("project-kind").value;
  const description = $("project-description").value.trim();
  if (!name) return input.focus();
  submit.disabled = true;
  try {
    const result = await post("/api/projects", {name, kind, description});
    state.projectId = result.project.id;
    await refreshProjects();
    $("project").value = String(state.projectId);
    updateProjectChrome();
    await newConversation();
    $("project-dialog").close();
    toast(`Project ${result.project.name} is ready.`);
  } finally {
    submit.disabled = false;
  }
}

function loadPinnedProjects() {
  try {
    const saved = JSON.parse(localStorage.getItem("jarvis.presence.pinned-projects") || "[]");
    if (Array.isArray(saved)) {
      state.pinnedProjects = new Set(
        saved
          .filter((value) => Number.isSafeInteger(value) && value > 0)
          .slice(0, 20),
      );
    }
  } catch (_) {
    state.pinnedProjects = new Set();
  }
}

function savePinnedProjects() {
  localStorage.setItem(
    "jarvis.presence.pinned-projects",
    JSON.stringify([...state.pinnedProjects].slice(0, 20)),
  );
}

function renderPinnedProjects() {
  const list = $("pinned-projects");
  list.replaceChildren();
  const pinned = state.projects.filter((project) => state.pinnedProjects.has(project.id));
  if (!pinned.length) {
    const empty = document.createElement("div");
    empty.className = "pinned-empty";
    empty.textContent = "Pin active projects for quick access.";
    list.append(empty);
  } else {
    for (const project of pinned) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = `pinned-project${project.id === state.projectId ? " active" : ""}`;
      button.title = `Open ${project.name}`;
      const label = document.createElement("span");
      label.textContent = project.name;
      button.append(label);
      button.addEventListener("click", () => openProjectInChat(project.id).catch(showError));
      list.append(button);
    }
  }
  const currentPinned = state.pinnedProjects.has(state.projectId);
  $("pin-project").textContent = currentPinned ? "−" : "+";
  $("pin-project").title = currentPinned ? "Unpin current project" : "Pin current project";
  $("pin-project").setAttribute("aria-label", $("pin-project").title);
}

function toggleCurrentProjectPin() {
  if (!state.projects.some((project) => project.id === state.projectId)) return;
  if (state.pinnedProjects.has(state.projectId)) state.pinnedProjects.delete(state.projectId);
  else if (state.pinnedProjects.size < 20) state.pinnedProjects.add(state.projectId);
  else return toast("You can pin up to 20 projects.");
  savePinnedProjects();
  renderPinnedProjects();
}

function showChat() {
  state.utilityGeneration += 1;
  state.activeView = "home";
  document.body.classList.remove("utility-mode");
  $("utility-view").hidden = true;
  document.querySelectorAll("[data-view]").forEach((button) => button.classList.remove("active"));
  prompt.focus();
}

function setWorkspaceMode(mode) {
  const codeMode = mode === "code";
  $("home-mode").classList.toggle("active", !codeMode);
  $("home-mode").setAttribute("aria-selected", codeMode ? "false" : "true");
  $("code-mode").classList.toggle("active", codeMode);
  $("code-mode").setAttribute("aria-selected", codeMode ? "true" : "false");
  if (codeMode) {
    $("model").value = "coding";
    prompt.placeholder = "Ask Jarvis to build, fix, test, or inspect code";
  } else {
    $("model").value = "auto";
    prompt.placeholder = "Message Jarvis";
  }
  showChat();
}

const utilityCopy = {
  projects: ["Workspace", "Projects", "Keep chats, files, and agent work separated by project."],
  artifacts: ["Project workspace", "Project files", "Code, research, documents, images, datasets, and exports from the active project only."],
  scheduled: ["Automation", "Scheduled", "Queued tasks, recurring learning, and approved proactive work."],
  dispatch: ["Agent system", "Dispatch", "Live specialist assignments and model routing managed by Jarvis."],
  devices: ["Private devices", "Devices", "Review devices Jarvis has observed on paired private networks and endpoints Windows already reports as paired over Bluetooth."],
  companion: ["Opt-in assistance", "Screen Companion", "Let Jarvis observe the active window, suggest help, or run operator-authored routines with existing approvals."],
  "public-presence": ["Separate security domain", "Public Presence", "Inspect the disconnected public identity foundation and independently pause or emergency-stop all social activity."],
  customize: ["Preferences", "Settings", "Adjust Jarvis features, model mode, voice, and workspace behavior."],
};

function beginUtilityRender(view, generation = null) {
  if (state.activeView !== view) return null;
  const renderGeneration = generation === null
    ? state.utilityGeneration + 1
    : Number(generation);
  if (!Number.isSafeInteger(renderGeneration) || renderGeneration < 1) return null;
  if (generation === null) state.utilityGeneration = renderGeneration;
  if (state.utilityGeneration !== renderGeneration) return null;
  return {view, generation: renderGeneration, content: $("utility-content")};
}

function isUtilityRenderCurrent(render) {
  return Boolean(
    render
    && state.activeView === render.view
    && state.utilityGeneration === render.generation
    && !$("utility-view").hidden
  );
}

async function openUtility(view) {
  if (!Object.prototype.hasOwnProperty.call(utilityCopy, view)) return;
  const generation = state.utilityGeneration + 1;
  state.utilityGeneration = generation;
  state.activeView = view;
  document.body.classList.add("utility-mode");
  $("utility-view").hidden = false;
  const [kicker, title, description] = utilityCopy[view];
  $("utility-kicker").textContent = kicker;
  $("utility-title").textContent = title;
  $("utility-description").textContent = description;
  $("utility-content").replaceChildren();
  document.querySelectorAll("[data-view]").forEach((button) => {
    button.classList.toggle("active", button.dataset.view === view);
  });
  if (view === "projects") renderProjects();
  if (view === "artifacts") await renderArtifacts(generation);
  if (view === "scheduled") await renderSchedule(generation);
  if (view === "dispatch") renderDispatch();
  if (view === "devices") await renderNetworkInventory(generation);
  if (view === "companion") await renderCompanion(generation);
  if (view === "public-presence") await renderPublicPresence(generation);
  if (view === "customize") await renderCustomize(generation);
  if (!isUtilityRenderCurrent({view, generation})) return;
  $("utility-view").scrollTop = 0;
}

function emptyUtility(message) {
  const empty = document.createElement("div");
  empty.className = "utility-empty";
  empty.textContent = message;
  return empty;
}

function makePill(text) {
  const pill = document.createElement("span");
  const stateName = String(text || "unknown").toLowerCase();
  pill.className = `utility-pill ${stateName.replace(/[^a-z0-9_-]/g, "-")}`;
  pill.textContent = text || "unknown";
  return pill;
}

async function openProjectInChat(projectId) {
  const project = state.projects.find((row) => row.id === Number(projectId));
  if (!project) throw new Error("That project is no longer available.");
  state.projectId = project.id;
  $("project").value = String(project.id);
  updateProjectChrome();
  renderPinnedProjects();
  await refreshConversations();
  const current = state.conversations.get(state.conversationId);
  if (current?.project_id === project.id) {
    showChat();
    return;
  }
  const existing = [...state.conversations.values()].find(
    (row) => row.project_id === project.id && row.id !== state.secondaryConversationId,
  );
  if (existing) await loadConversation(existing.id);
  else await newConversation();
}

function renderProjects() {
  const content = $("utility-content");
  content.replaceChildren();
  const overview = document.createElement("section");
  overview.className = "utility-card project-overview";
  const overviewCopy = document.createElement("div");
  const overviewTitle = document.createElement("h3");
  overviewTitle.textContent = "One project, one clean environment";
  const overviewText = document.createElement("p");
  overviewText.textContent = "Every isolated project gets its own chats, agent context, code, research, documents, images, datasets, and exports. Work from one project is never silently mixed into another.";
  overviewCopy.append(overviewTitle, overviewText);
  const create = document.createElement("button");
  create.type = "button";
  create.className = "primary project-create";
  create.textContent = "＋ Create project";
  create.addEventListener("click", () => createProject().catch(showError));
  overview.append(overviewCopy, create);
  content.append(overview);
  if (!state.projects.length) {
    content.append(emptyUtility("No projects are available."));
    return;
  }
  const filters = document.createElement("div");
  filters.className = "project-filter-bar";
  const search = document.createElement("input");
  search.type = "search";
  search.placeholder = "Search projects";
  search.setAttribute("aria-label", "Search projects");
  const kindFilter = document.createElement("select");
  kindFilter.setAttribute("aria-label", "Filter projects by type");
  for (const [value, label] of [
    ["all", "All types"],
    ["coding", "Coding / app"],
    ["research", "Research"],
    ["creative", "Creative / media"],
    ["general", "General"],
  ]) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    kindFilter.append(option);
  }
  const count = document.createElement("span");
  count.textContent = `${state.projects.length} projects`;
  filters.append(search, kindFilter, count);
  content.append(filters);
  const grid = document.createElement("div");
  grid.className = "utility-grid project-grid";
  const kindLabels = {
    general: "General",
    coding: "Coding / app",
    research: "Research",
    creative: "Creative / media",
  };
  const kindIcons = {general: "◇", coding: "</>", research: "⌕", creative: "✦"};
  for (const project of state.projects) {
    const card = document.createElement("article");
    card.className = `utility-card project-card kind-${project.kind || "general"}${project.id === state.projectId ? " active" : ""}`;
    card.dataset.projectKind = project.kind || "general";
    card.dataset.projectSearch = `${project.name} ${project.description || ""}`.toLowerCase();
    const head = document.createElement("div");
    head.className = "utility-card-head";
    const identity = document.createElement("div");
    identity.className = "project-card-identity";
    const icon = document.createElement("span");
    icon.className = "project-type-icon";
    icon.textContent = kindIcons[project.kind] || kindIcons.general;
    const nameBlock = document.createElement("div");
    const title = document.createElement("h3");
    title.textContent = project.name;
    const type = document.createElement("small");
    type.textContent = `${kindLabels[project.kind] || kindLabels.general}${project.isolated ? " · isolated" : " · main workspace"}`;
    nameBlock.append(title, type);
    identity.append(icon, nameBlock);
    const active = makePill(project.id === state.projectId ? "active" : "ready");
    head.append(identity, active);
    const description = document.createElement("p");
    description.className = "project-description";
    description.textContent = project.description || "Dedicated Jarvis project workspace.";
    const stats = document.createElement("div");
    stats.className = "project-stats";
    for (const value of (
      [
        `${project.conversation_count || 0} chats`,
        `${project.task_count || 0} tasks`,
        project.isolated ? "Separate folder" : "Default folder",
      ]
    )) {
      const stat = document.createElement("span");
      stat.textContent = value;
      stats.append(stat);
    }
    const folders = document.createElement("div");
    folders.className = "project-folder-list";
    for (const folder of (project.folders || []).slice(0, 6)) {
      const chip = document.createElement("span");
      chip.textContent = folder;
      folders.append(chip);
    }
    const path = document.createElement("small");
    path.className = "project-path";
    path.textContent = project.isolated
      ? `Workspace: ${project.relative_path}`
      : "Workspace: Jarvis default";
    const cardActions = document.createElement("div");
    cardActions.className = "utility-actions";
    const open = document.createElement("button");
    open.type = "button";
    open.textContent = "Open project";
    open.addEventListener("click", () => openProjectInChat(project.id).catch(showError));
    const files = document.createElement("button");
    files.type = "button";
    files.textContent = "View files";
    files.addEventListener("click", async () => {
      try {
        await openProjectInChat(project.id);
        await openUtility("artifacts");
      } catch (error) {
        showError(error);
      }
    });
    const pin = document.createElement("button");
    pin.type = "button";
    pin.textContent = state.pinnedProjects.has(project.id) ? "Unpin" : "Pin";
    pin.addEventListener("click", () => {
      if (state.pinnedProjects.has(project.id)) state.pinnedProjects.delete(project.id);
      else if (state.pinnedProjects.size < 20) state.pinnedProjects.add(project.id);
      else return toast("You can pin up to 20 projects.");
      savePinnedProjects();
      renderPinnedProjects();
      renderProjects();
    });
    cardActions.append(open, files, pin);
    card.append(head, description, stats, folders, path, cardActions);
    grid.append(card);
  }
  content.append(grid);
  const applyProjectFilters = () => {
    const query = search.value.trim().toLowerCase();
    const kind = kindFilter.value;
    let visible = 0;
    for (const card of grid.children) {
      const matchesQuery = !query || String(card.dataset.projectSearch || "").includes(query);
      const matchesKind = kind === "all" || card.dataset.projectKind === kind;
      card.hidden = !(matchesQuery && matchesKind);
      if (!card.hidden) visible += 1;
    }
    count.textContent = `${visible} of ${state.projects.length} projects`;
  };
  search.addEventListener("input", applyProjectFilters);
  kindFilter.addEventListener("change", applyProjectFilters);
}

function formatBytes(value) {
  const bytes = Math.max(0, Number(value) || 0);
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${(bytes / 1024 ** 3).toFixed(1)} GB`;
}

function formatTimestamp(value, seconds = false) {
  if (value === null || value === undefined || value === "") return "Time unavailable";
  const date = seconds ? new Date((Number(value) || 0) * 1000) : new Date(value);
  return Number.isNaN(date.getTime()) ? "Time unavailable" : date.toLocaleString();
}

async function renderArtifacts(generation = null) {
  const render = beginUtilityRender("artifacts", generation);
  if (!render) return;
  const {content} = render;
  content.replaceChildren(emptyUtility("Loading project artifacts…"));
  const result = await api(`/api/artifacts?project_id=${encodeURIComponent(state.projectId)}`);
  if (!isUtilityRenderCurrent(render)) return;
  content.replaceChildren();
  const project = activeProject();
  const summary = document.createElement("section");
  summary.className = "utility-card project-files-summary";
  const summaryHead = document.createElement("div");
  const summaryTitle = document.createElement("h3");
  summaryTitle.textContent = project?.name || "Project files";
  const summaryText = document.createElement("p");
  summaryText.textContent = project?.isolated
    ? "Everything shown here is inside this project's dedicated folder. Other projects cannot use these files unless you explicitly move or copy them."
    : "Files from the default Jarvis workspace.";
  summaryHead.append(summaryTitle, summaryText);
  const projectButton = document.createElement("button");
  projectButton.type = "button";
  projectButton.className = "ghost";
  projectButton.textContent = "Project details";
  projectButton.addEventListener("click", () => openUtility("projects").catch(showError));
  summary.append(summaryHead, projectButton);
  content.append(summary);
  if (!(result.artifacts || []).length) {
    content.append(emptyUtility("This project does not have any visible artifacts yet."));
    return;
  }
  const list = document.createElement("div");
  list.className = "utility-list";
  const icons = {code: "</>", document: "DOC", image: "IMG", file: "FILE"};
  for (const artifact of result.artifacts) {
    const row = document.createElement("article");
    row.className = "utility-row";
    const icon = document.createElement("span");
    icon.className = "artifact-icon";
    icon.textContent = icons[artifact.kind] || "FILE";
    const main = document.createElement("div");
    main.className = "utility-row-main";
    const title = document.createElement("strong");
    title.textContent = artifact.relative_path;
    const meta = document.createElement("small");
    meta.textContent = `${formatBytes(artifact.size)} · ${formatTimestamp(artifact.modified_at, true)}`;
    main.append(title, meta);
    const actions = document.createElement("div");
    actions.className = "utility-actions";
    const use = document.createElement("button");
    use.type = "button";
    use.textContent = "Use in chat";
    use.addEventListener("click", () => {
      showChat();
      prompt.value = `Work with the project artifact \`${artifact.relative_path}\`: `;
      resizePrompt();
      prompt.focus();
    });
    actions.append(use);
    row.append(icon, main, actions);
    list.append(row);
  }
  content.append(list);
}

function scheduleSection(titleText, rows, renderRow) {
  const section = document.createElement("section");
  const title = document.createElement("h3");
  title.className = "utility-section-title";
  title.textContent = titleText;
  section.append(title);
  if (!rows.length) section.append(emptyUtility(`No ${titleText.toLowerCase()} are configured.`));
  else {
    const list = document.createElement("div");
    list.className = "utility-list";
    rows.forEach((row) => list.append(renderRow(row)));
    section.append(list);
  }
  return section;
}

function scheduledRow(titleText, metaText, statusText) {
  const row = document.createElement("article");
  row.className = "utility-row";
  const main = document.createElement("div");
  main.className = "utility-row-main";
  const title = document.createElement("strong");
  title.textContent = titleText;
  const meta = document.createElement("small");
  meta.textContent = metaText;
  main.append(title, meta);
  row.append(main, makePill(statusText));
  return row;
}

async function renderSchedule(generation = null) {
  const render = beginUtilityRender("scheduled", generation);
  if (!render) return;
  const {content} = render;
  content.replaceChildren(emptyUtility("Loading scheduled work…"));
  const result = await api("/api/schedule");
  if (!isUtilityRenderCurrent(render)) return;
  content.replaceChildren(
    scheduleSection("Task queue", result.tasks || [], (row) => scheduledRow(
      `#${row.id} · ${row.prompt || "Untitled task"}`,
      `${row.specialist_key || "Jarvis"} · updated ${formatTimestamp(row.updated_at)}`,
      row.status,
    )),
    scheduleSection("Continuous learning", result.learning_topics || [], (row) => scheduledRow(
      row.topic,
      `Every ${row.interval_hours} hours · next ${formatTimestamp(row.next_run)}`,
      row.enabled ? "enabled" : "paused",
    )),
    scheduleSection("Proactive backlog", result.backlog || [], (row) => scheduledRow(
      row.subject,
      `${row.kind} · next ${formatTimestamp(row.next_run)}`,
      row.enabled ? "enabled" : "paused",
    )),
  );
}

function renderDispatch() {
  const content = $("utility-content");
  if (!content || state.activeView !== "dispatch") return;
  content.replaceChildren();
  if (!state.specialists.length) {
    content.append(emptyUtility("No specialist agents are registered."));
    return;
  }
  const grid = document.createElement("div");
  grid.className = "utility-grid";
  for (const specialist of state.specialists) {
    const card = document.createElement("article");
    card.className = "utility-card";
    const head = document.createElement("div");
    head.className = "utility-card-head";
    const title = document.createElement("h3");
    title.textContent = specialist.name;
    head.append(title, makePill(
      specialist.status === "working" ? "working" : specialist.participating ? "reported" : "ready",
    ));
    const purpose = document.createElement("p");
    purpose.textContent = specialist.purpose || "Specialist purpose unavailable.";
    const assignment = document.createElement("p");
    assignment.textContent = specialist.active_task_prompt
      ? `Task #${specialist.active_task_id}: ${specialist.active_task_prompt}`
      : specialist.last_task_prompt
        ? `${specialist.participating ? "Reported for this request" : `Last task (${specialist.last_task_status || "complete"})`} #${specialist.last_task_id}: ${specialist.last_task_prompt}`
        : "Standing by for a Jarvis assignment.";
    const model = document.createElement("small");
    model.textContent = `${specialist.model || specialist.model_profile} · ${specialist.completed_tasks || 0} completed · ${specialist.failed_tasks || 0} failed`;
    card.append(head, purpose, assignment, model);
    grid.append(card);
  }
  content.append(grid);
}

function formatObservedDuration(value) {
  const seconds = Number(value);
  if (!Number.isFinite(seconds) || seconds < 0) return "Not currently observed";
  if (seconds < 60) return `${Math.floor(seconds)} sec`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min`;
  if (seconds < 86400) {
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    return minutes ? `${hours}h ${minutes}m` : `${hours}h`;
  }
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  return hours ? `${days}d ${hours}h` : `${days}d`;
}

function networkDeviceName(device) {
  return device.display_name || device.label || device.hostname || "Unknown device";
}

function networkPresenceLabel(device) {
  if (device.presence_state === "reachable" || device.visible_now) {
    return "reachable in last check";
  }
  if (device.presence_state === "cached" || device.cached_now) {
    return "cached in last check";
  }
  return "not observed in last check";
}

function networkFact(labelText, valueText) {
  const row = document.createElement("div");
  row.className = "network-fact";
  const label = document.createElement("span");
  label.textContent = labelText;
  const value = document.createElement("strong");
  value.textContent = valueText || "Unavailable";
  row.append(label, value);
  return row;
}

function networkSummaryCard(labelText, valueText, detailText = "") {
  const card = document.createElement("article");
  card.className = "utility-card network-summary-card";
  const label = document.createElement("small");
  label.textContent = labelText;
  const value = document.createElement("strong");
  value.textContent = String(valueText);
  card.append(label, value);
  if (detailText) {
    const detail = document.createElement("p");
    detail.textContent = detailText;
    card.append(detail);
  }
  return card;
}

function networkInventoryRows(data = state.networkInventory) {
  const inventory = data?.inventory || {};
  return Array.isArray(inventory.devices) ? inventory.devices : [];
}

function networkDeviceReviewSignal(data, deviceId) {
  const signals = Array.isArray(data?.security_assessment?.signals)
    ? data.security_assessment.signals
    : [];
  return signals.find((item) => (
    item?.rule_id === "new_unreviewed_device" && item?.device_id === deviceId
  )) || null;
}

function boundedIncidentText(value, fallback = "Unavailable", limit = 2400) {
  const text = typeof value === "string" || typeof value === "number"
    ? String(value).trim()
    : "";
  return text ? text.slice(0, limit) : fallback;
}

function incidentTextList(value, limit = 12) {
  if (!Array.isArray(value)) return [];
  return value
    .filter((item) => typeof item === "string" || typeof item === "number")
    .map((item) => boundedIncidentText(item, "", 1200))
    .filter(Boolean)
    .slice(0, limit);
}

function normalizeNetworkDefenseIncident(raw) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const incidentId = boundedIncidentText(raw.incident_id, "", 128);
  const receiptId = boundedIncidentText(raw.receipt_id, "", 64);
  if (
    !/^[0-9a-f]{32}$/.test(incidentId)
    || !/^[0-9a-f]{32}$/.test(receiptId)
  ) return null;
  const severityValue = boundedIncidentText(raw.severity, "informational", 24).toLowerCase();
  const severity = ["informational", "low", "medium", "high", "critical"].includes(severityValue)
    ? severityValue
    : "informational";
  const deviceRaw = raw.device && typeof raw.device === "object" ? raw.device : {};
  const actions = Array.isArray(raw.automatic_actions)
    ? raw.automatic_actions.filter((item) => item && typeof item === "object").slice(0, 12).map((item) => ({
      toolId: boundedIncidentText(item.tool_id, "Tool unavailable", 120),
      title: boundedIncidentText(item.title, "Recorded automatic action", 240),
      outcome: boundedIncidentText(item.outcome, "Outcome unavailable", 600),
      receiptId: /^[0-9a-f]{32}$/.test(String(item.receipt_id || ""))
        ? String(item.receipt_id)
        : null,
    }))
    : [];
  const actionId = Number(raw.approval?.action_id);
  const approval = raw.approval?.required === true
    && Number.isSafeInteger(actionId)
    && actionId > 0
    && typeof raw.approval?.label === "string"
    ? {
      required: true,
      actionId,
      label: boundedIncidentText(raw.approval.label, "Review requested action", 240),
    }
    : null;
  return {
    key: `${incidentId}:${receiptId}`,
    incidentId,
    receiptId,
    acknowledgeable: true,
    createdAt: raw.created_at,
    severity,
    category: boundedIncidentText(raw.category, "Network observation", 120),
    device: {
      id: boundedIncidentText(deviceRaw.device_id, "", 160),
      name: boundedIncidentText(deviceRaw.display_name, "Unknown device", 240),
      type: boundedIncidentText(deviceRaw.device_type || deviceRaw.type, "Unknown", 160),
      manufacturer: boundedIncidentText(deviceRaw.manufacturer, "Unknown", 240),
    },
    observedFact: boundedIncidentText(raw.observed_fact, "No observed fact was supplied.", 1600),
    assessment: boundedIncidentText(raw.assessment, "No assessment was supplied.", 2000),
    confidence: boundedIncidentText(raw.confidence, "Unavailable", 80),
    compromiseEstablished: raw.compromise_established === true,
    evidence: incidentTextList(raw.evidence_summary),
    automaticActions: actions,
    actionsNotTaken: incidentTextList(raw.actions_not_taken),
    recommendedAction: boundedIncidentText(raw.recommended_action, "Review the evidence and identify the device.", 1600),
    limitations: incidentTextList(raw.limitations),
    approval,
  };
}

function legacyNetworkDefenseIncidents(payload) {
  const devices = Array.isArray(payload?.new_devices) ? payload.new_devices : [];
  const assessment = payload?.security_assessment || {};
  const signals = Array.isArray(assessment.signals) ? assessment.signals : [];
  const scanId = boundedIncidentText(payload?.scan_id, "observation", 80);
  return devices.filter((device) => typeof device?.device_id === "string").slice(0, 12).map((device) => {
    const signal = signals.find((item) => item?.device_id === device.device_id) || {};
    const legacyReceipt = boundedIncidentText(assessment.receipt_sha256, "Unavailable", 128);
    return {
      key: `legacy:${scanId}:${device.device_id}`,
      incidentId: `observation:${scanId}:${device.device_id}`.slice(0, 128),
      receiptId: legacyReceipt,
      acknowledgeable: false,
      createdAt: payload?.observed_at || device.first_seen,
      severity: ["low", "medium", "high", "critical"].includes(String(signal.severity || "").toLowerCase())
        ? String(signal.severity).toLowerCase()
        : "informational",
      category: "First-observed network device",
      device: {
        id: boundedIncidentText(device.device_id, "", 160),
        name: networkDeviceName(device),
        type: boundedIncidentText(device.device_type, "Unknown", 160),
        manufacturer: boundedIncidentText(device.manufacturer || device.vendor, "Unknown", 240),
      },
      observedFact: `Jarvis first observed this device during network check ${scanId}.`,
      assessment: boundedIncidentText(signal.summary, "The device is new to Jarvis and needs operator identification.", 2000),
      confidence: boundedIncidentText(signal.confidence, device.identity_confidence || "limited", 80),
      compromiseEstablished: false,
      evidence: [
        `Presence: ${networkPresenceLabel(device)}`,
        `First observed: ${formatTimestamp(device.first_seen)}`,
        `Observation check: ${formatTimestamp(payload?.observed_at)}`,
      ],
      automaticActions: [],
      actionsNotTaken: ["Jarvis did not block, isolate, probe services, inspect traffic, or control this device."],
      recommendedAction: boundedIncidentText(signal.recommended_action, "Identify the device and label it only if you recognize it.", 1600),
      limitations: ["A first observation is not proof of compromise, physical proximity, ownership, or a new network connection."],
      approval: null,
    };
  });
}

function incidentSection(titleText, values, emptyText = "") {
  const section = document.createElement("section");
  section.className = "network-defense-incident-section";
  const title = document.createElement("h3");
  title.textContent = titleText;
  section.append(title);
  const rows = Array.isArray(values) ? values.filter(Boolean) : [];
  if (rows.length) {
    const list = document.createElement("ul");
    for (const value of rows) {
      const row = document.createElement("li");
      row.textContent = boundedIncidentText(value, "Unavailable", 1800);
      list.append(row);
    }
    section.append(list);
  } else {
    const text = document.createElement("p");
    text.textContent = emptyText || "None recorded.";
    section.append(text);
  }
  return section;
}

function automaticActionText(action) {
  return [
    action.title,
    action.outcome,
    action.toolId ? `Tool ${action.toolId}` : null,
    action.receiptId ? `Receipt ${action.receiptId}` : null,
  ].filter(Boolean).join(" · ");
}

async function acknowledgeNetworkDefenseIncident(incident) {
  if (incident.acknowledgeable) {
    await post("/api/network-defense/incidents/acknowledge", {
      incident_id: incident.incidentId,
      receipt_id: incident.receiptId,
    });
  }
  state.networkDefenseIncidents.delete(incident.key);
  renderNetworkDefenseIncidents();
}

function renderNetworkDefenseIncidents() {
  const list = $("network-defense-incident-list");
  list.replaceChildren();
  const incidents = [...state.networkDefenseIncidents.values()].slice(0, 12);
  for (const incident of incidents) {
    const card = document.createElement("article");
    card.className = `network-defense-incident-card severity-${incident.severity}`;
    const head = document.createElement("div");
    head.className = "utility-card-head";
    const title = document.createElement("strong");
    title.textContent = incident.device.name;
    head.append(title, makePill(`${incident.severity} · ${incident.category}`));
    const facts = document.createElement("div");
    facts.className = "network-defense-incident-facts";
    facts.append(
      networkFact("Device", incident.device.name),
      networkFact("Type", incident.device.type),
      networkFact("Manufacturer", incident.device.manufacturer),
      networkFact("Confidence", incident.confidence),
      networkFact("Detected signal", incident.observedFact),
      networkFact("Time", formatTimestamp(incident.createdAt)),
    );
    const boundary = document.createElement("p");
    boundary.className = "network-defense-boundary";
    boundary.textContent = incident.compromiseEstablished
      ? "The backend marked compromise as established; treat this as requiring immediate human validation."
      : "Compromise is not established by this evidence.";
    const detected = incidentSection("Detected signal", [incident.observedFact]);
    const assessment = incidentSection("Assessment", [incident.assessment]);
    const evidence = incidentSection("Evidence", incident.evidence, "No evidence summary was supplied.");
    const automatic = incidentSection(
      "What Jarvis automatically did",
      incident.automaticActions.map(automaticActionText),
      "No automatic action was recorded.",
    );
    const notDone = incidentSection(
      "What Jarvis did not do",
      incident.actionsNotTaken,
      "No omitted-action statement was supplied.",
    );
    const recommendation = incidentSection("Recommended action", [incident.recommendedAction]);
    const limitations = incidentSection("Limitations", incident.limitations, "No additional limitations were supplied.");
    const receipts = incidentSection("Tool and runbook receipts", [
      `Incident ${incident.incidentId} · Receipt ${incident.receiptId}`,
      ...incident.automaticActions
        .filter((item) => item.receiptId)
        .map((item) => `${item.toolId} · Receipt ${item.receiptId}`),
    ]);
    receipts.classList.add("network-defense-incident-receipts");
    const actions = document.createElement("div");
    actions.className = "network-defense-incident-actions";
    if (incident.device.id) {
      const reviewDevice = document.createElement("button");
      reviewDevice.type = "button";
      reviewDevice.className = "ghost";
      reviewDevice.textContent = "Review device";
      reviewDevice.addEventListener("click", () => {
        $("network-defense-incident-dialog").close();
        openUtility("devices")
          .then(() => openNetworkDevice(incident.device.id))
          .catch(showError);
      });
      actions.append(reviewDevice);
    }
    if (incident.approval?.required === true) {
      const deny = document.createElement("button");
      deny.type = "button";
      deny.className = "danger";
      deny.textContent = `Deny: ${incident.approval.label}`;
      const approve = document.createElement("button");
      approve.type = "button";
      approve.className = "primary";
      approve.textContent = `Approve once: ${incident.approval.label}`;
      const decide = async (approved) => {
        deny.disabled = true;
        approve.disabled = true;
        try {
          await decideApproval(incident.approval.actionId, approved);
          await acknowledgeNetworkDefenseIncident(incident);
        } catch (error) {
          deny.disabled = false;
          approve.disabled = false;
          showError(error);
        }
      };
      deny.addEventListener("click", () => decide(false));
      approve.addEventListener("click", () => decide(true));
      actions.append(deny, approve);
    }
    const acknowledge = document.createElement("button");
    acknowledge.type = "button";
    acknowledge.className = "ghost";
    acknowledge.textContent = incident.acknowledgeable ? "Mark reviewed" : "Dismiss explanation";
    acknowledge.addEventListener("click", () => {
      acknowledge.disabled = true;
      acknowledgeNetworkDefenseIncident(incident).catch((error) => {
        acknowledge.disabled = false;
        showError(error);
      });
    });
    actions.append(acknowledge);
    card.append(
      head, facts, boundary, detected, assessment, evidence, automatic,
      notDone, recommendation, limitations, receipts, actions,
    );
    list.append(card);
  }
  const dialog = $("network-defense-incident-dialog");
  if (!incidents.length && dialog.open) dialog.close();
}

function maybeShowNetworkDefenseIncidents() {
  if (!state.networkDefenseIncidents.size) return;
  if (
    $("approval-dialog").open
    || $("feature-onboarding-dialog").open
    || $("new-network-device-dialog").open
    || $("new-bluetooth-device-dialog").open
  ) return;
  renderNetworkDefenseIncidents();
  const dialog = $("network-defense-incident-dialog");
  if (!dialog.open) dialog.showModal();
}

function schedulePriorityDialogs() {
  if ($("approval-dialog").open) return;
  maybeShowFeatureOnboarding();
  if ($("feature-onboarding-dialog").open) return;

  const networkDialog = $("new-network-device-dialog");
  if ($("new-network-device-list").childElementCount) {
    if (!networkDialog.open) networkDialog.showModal();
    return;
  }
  const bluetoothDialog = $("new-bluetooth-device-dialog");
  if ($("new-bluetooth-device-list").childElementCount) {
    if (!bluetoothDialog.open) bluetoothDialog.showModal();
    return;
  }
  maybeShowNetworkDefenseIncidents();
}

function showNetworkDefenseIncidents(value, {legacyPayload = null} = {}) {
  if (state.networkInventory?.incident_popups_enabled === false) {
    state.networkDefenseIncidents.clear();
    const dialog = $("network-defense-incident-dialog");
    if (dialog.open) dialog.close();
    return;
  }
  const rawIncidents = Array.isArray(value?.incidents)
    ? value.incidents
    : (value?.incident ? [value.incident] : (value?.incident_id ? [value] : []));
  for (const raw of rawIncidents.slice(0, 12)) {
    const incident = normalizeNetworkDefenseIncident(raw);
    if (incident) state.networkDefenseIncidents.set(incident.key, incident);
  }
  if (legacyPayload && !rawIncidents.length) {
    for (const incident of legacyNetworkDefenseIncidents(legacyPayload)) {
      state.networkDefenseIncidents.set(incident.key, incident);
    }
  }
  maybeShowNetworkDefenseIncidents();
}

function showNewNetworkDeviceAlerts(data) {
  if (data?.inventory?.security_summary?.baseline_created === true) return;
  loadNetworkAlertReceipts();
  const newRows = networkInventoryRows(data)
    .filter((device) => device?.is_new === true && typeof device.device_id === "string")
    .map((device) => {
      const signal = networkDeviceReviewSignal(data, device.device_id);
      const scanId = data?.scan_id
        || data?.inventory?.scan_id
        || data?.security_assessment?.scan_id
        || "unknown-scan";
      return {
        device,
        signal,
        alertReceipt: `${scanId}:${device.device_id}:${device.first_seen || "unknown-time"}`,
      };
    })
    .filter((item) => !state.notifiedNetworkDevices.has(item.alertReceipt))
    .slice(0, 12);
  if (!newRows.length) return;
  const list = $("new-network-device-list");
  list.replaceChildren();
  for (const {device, signal, alertReceipt} of newRows) {
    rememberNetworkAlertReceipt(alertReceipt);
    const card = document.createElement("article");
    card.className = "new-network-device-card";
    const head = document.createElement("div");
    head.className = "utility-card-head";
    const title = document.createElement("strong");
    title.textContent = networkDeviceName(device);
    const priority = String(signal?.severity || "informational");
    head.append(title, makePill(`Review priority: ${priority}`));
    const facts = document.createElement("div");
    facts.className = "new-network-device-facts";
    const exactType = device.device_type
      || "Unknown — LAN evidence cannot establish an exact phone or hardware model";
    facts.append(
      networkFact("Presence", networkPresenceLabel(device)),
      networkFact("Device type / model", exactType),
      networkFact("Manufacturer", device.manufacturer || device.vendor || "Unknown"),
      networkFact("Reported hostname", device.hostname || "Unavailable"),
      networkFact("IP address", device.ipv4 || "Unavailable"),
      networkFact("MAC address", device.mac || "Unavailable"),
      networkFact("Identity confidence", device.identity_confidence || "limited"),
      networkFact("First observed by Jarvis", formatTimestamp(device.first_seen)),
      networkFact(
        "Observation check",
        formatTimestamp(
          data?.observed_at
          || data?.inventory?.observed_at
          || data?.security_assessment?.coverage?.last_scan_at,
        ),
      ),
    );
    const assessment = document.createElement("p");
    assessment.className = "network-defense-boundary";
    assessment.textContent = signal
      ? `${signal.summary} Compromise is not established. ${signal.recommended_action}`
      : "No verified device-specific assessment is available. Compromise is not established; review the device manually.";
    const actions = document.createElement("div");
    actions.className = "new-network-device-actions";
    const review = document.createElement("button");
    review.className = "primary";
    review.type = "button";
    review.textContent = "Review device";
    review.addEventListener("click", () => {
      $("new-network-device-dialog").close();
      openUtility("devices")
        .then(() => openNetworkDevice(device.device_id))
        .catch(showError);
    });
    actions.append(review);
    card.append(head, facts, assessment, actions);
    list.append(card);
  }
  schedulePriorityDialogs();
}

async function refreshNetworkInventory() {
  const data = await api("/api/network-inventory");
  state.networkInventory = data;
  paintNetworkInventory();
}

async function scanNetworkInventory(scopeId) {
  if (state.networkScanPending) return;
  state.networkScanPending = true;
  paintNetworkInventory();
  try {
    const result = await post("/api/network-inventory/scan", {
      scope_id: scopeId || null,
      max_hosts: 512,
    });
    state.networkInventory = result.status;
    showNewNetworkDeviceAlerts(result.status);
    showNetworkDefenseIncidents(result.status?.pending_incidents);
    toast("Home Network check complete.");
  } catch (error) {
    if (error.retryAfterSeconds) {
      toast(`Please wait ${error.retryAfterSeconds} seconds before checking again.`);
    }
    throw error;
  } finally {
    state.networkScanPending = false;
    paintNetworkInventory();
  }
}

async function pairNetworkScope(interfaceIndex, displayName, attested) {
  const result = await post("/api/network-inventory/scopes/pair", {
    interface_index: Number(interfaceIndex),
    owns_or_administers: attested === true,
    display_name: displayName.trim() || null,
  });
  state.networkInventory = result.status;
  toast("Network paired. No scan has started yet.");
  paintNetworkInventory();
}

async function unpairNetworkScope(scopeId) {
  const result = await post("/api/network-inventory/scopes/unpair", {scope_id: scopeId});
  state.networkInventory = result.status;
  state.networkDeviceDetail = null;
  toast("Network unpaired. Stored history remains available.");
  paintNetworkInventory();
}

async function openNetworkDevice(deviceId) {
  state.networkDeviceDetail = await api(
    `/api/network-inventory/device?device_id=${encodeURIComponent(deviceId)}&event_limit=100`,
  );
  paintNetworkInventory();
  document.querySelector(".network-detail")?.scrollIntoView({behavior: "smooth", block: "start"});
}

async function saveNetworkDeviceProfile(deviceId, label, trustState, deviceType) {
  const result = await post("/api/network-inventory/devices/profile", {
    device_id: deviceId,
    label: label.trim() || null,
    trust_state: trustState || null,
    device_type: deviceType.trim() || null,
  });
  state.networkDeviceDetail = {
    ...(state.networkDeviceDetail || {}),
    device: result.device,
  };
  toast("Device details saved.");
  await refreshNetworkInventory();
  await openNetworkDevice(deviceId);
}

function renderNetworkScopeSetup(container, data) {
  const scopes = Array.isArray(data.scopes) ? data.scopes : [];
  const candidates = Array.isArray(data.scope_candidates) ? data.scope_candidates : [];
  const section = document.createElement("section");
  section.className = "network-scope-section";
  const heading = document.createElement("h3");
  heading.className = "utility-section-title";
  heading.textContent = "Paired networks";
  section.append(heading);

  if (scopes.length) {
    const list = document.createElement("div");
    list.className = "utility-list";
    for (const scope of scopes) {
      const row = document.createElement("article");
      row.className = "utility-row network-scope-row";
      const main = document.createElement("div");
      main.className = "utility-row-main";
      const name = document.createElement("strong");
      name.textContent = scope.display_name || scope.interface_alias || "Paired network";
      const meta = document.createElement("small");
      meta.textContent = [
        scope.cidr,
        scope.gateway_ipv4 ? `gateway ${scope.gateway_ipv4}` : null,
        scope.active ? "adapter available" : "adapter not currently available",
      ].filter(Boolean).join(" · ");
      main.append(name, meta);
      const status = makePill(scope.active ? "active" : "unavailable");
      const unpair = document.createElement("button");
      unpair.type = "button";
      unpair.className = "ghost";
      unpair.textContent = "Unpair";
      unpair.addEventListener("click", () => unpairNetworkScope(scope.scope_id).catch(showError));
      row.append(main, status, unpair);
      list.append(row);
    }
    section.append(list);
  } else {
    section.append(emptyUtility("Pair a network you own or administer before Jarvis checks it."));
  }

  const pairedIndexes = new Set(
    scopes.filter((row) => row.active).map((row) => Number(row.interface_index)),
  );
  const eligible = candidates.filter(
    (row) => row.eligible !== false && !pairedIndexes.has(Number(row.interface_index)),
  );
  if (eligible.length) {
    const form = document.createElement("form");
    form.className = "utility-card network-pair-form";
    const formTitle = document.createElement("h3");
    formTitle.textContent = "Pair this computer's network";
    const help = document.createElement("p");
    help.textContent = "Pairing selects one current network adapter. It does not scan until you press Check network now.";
    const select = document.createElement("select");
    select.setAttribute("aria-label", "Network adapter");
    const choose = document.createElement("option");
    choose.value = "";
    choose.textContent = "Choose a network adapter";
    select.append(choose);
    for (const candidate of eligible) {
      const option = document.createElement("option");
      option.value = String(candidate.interface_index);
      option.textContent = [
        candidate.interface_alias || "Network adapter",
        candidate.address,
        candidate.scan_cidr,
      ].filter(Boolean).join(" · ");
      select.append(option);
    }
    const name = document.createElement("input");
    name.type = "text";
    name.maxLength = 120;
    name.placeholder = "Optional name, such as Home Wi-Fi";
    name.setAttribute("aria-label", "Network name");
    const attestation = document.createElement("label");
    attestation.className = "network-attestation";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    const attestText = document.createElement("span");
    attestText.textContent = "I own or administer this network and authorize Jarvis to inventory devices on it.";
    attestation.append(checkbox, attestText);
    const pair = document.createElement("button");
    pair.type = "submit";
    pair.textContent = "Pair network";
    pair.disabled = true;
    const syncPair = () => { pair.disabled = !checkbox.checked || !select.value; };
    checkbox.addEventListener("change", syncPair);
    select.addEventListener("change", syncPair);
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      if (!select.value || !checkbox.checked) return;
      pair.disabled = true;
      pairNetworkScope(select.value, name.value, checkbox.checked).catch((error) => {
        pair.disabled = !checkbox.checked;
        showError(error);
      });
    });
    form.append(formTitle, help, select, name, attestation, pair);
    section.append(form);
  } else if (candidates.length && !scopes.length) {
    const unavailable = document.createElement("article");
    unavailable.className = "utility-card network-notice";
    const title = document.createElement("h3");
    title.textContent = "No eligible private network adapter";
    const reason = document.createElement("p");
    reason.textContent = candidates.map((row) => row.reason).filter(Boolean).join(" ") || "Connect this PC to a private home network, then reopen this page.";
    unavailable.append(title, reason);
    section.append(unavailable);
  }
  container.append(section);
}

function renderNetworkDeviceDetail(container) {
  const detail = state.networkDeviceDetail;
  if (!detail?.device) return;
  const device = detail.device;
  const section = document.createElement("section");
  section.className = "utility-card network-detail";
  const head = document.createElement("div");
  head.className = "utility-card-head";
  const heading = document.createElement("h3");
  heading.textContent = networkDeviceName(device);
  const close = document.createElement("button");
  close.type = "button";
  close.className = "ghost";
  close.textContent = "Close details";
  close.addEventListener("click", () => {
    state.networkDeviceDetail = null;
    paintNetworkInventory();
  });
  head.append(heading, close);

  const facts = document.createElement("div");
  facts.className = "network-detail-facts";
  facts.append(
    networkFact("Presence", networkPresenceLabel(device)),
    networkFact("Trust label", device.trust_state || "unreviewed"),
    networkFact("Type", device.device_type || "Not labeled"),
    networkFact("Identity", device.identity_confidence || "Limited"),
    networkFact("IP address", device.ipv4 || "Unavailable"),
    networkFact("MAC address", device.mac || "Unavailable"),
    networkFact("First observed", formatTimestamp(device.first_seen)),
    networkFact("Last observed", formatTimestamp(device.last_seen)),
  );

  const form = document.createElement("form");
  form.className = "network-profile-form";
  const formTitle = document.createElement("h3");
  formTitle.textContent = "Your device labels";
  const label = document.createElement("input");
  label.type = "text";
  label.maxLength = 120;
  label.value = device.label || "";
  label.placeholder = "Friendly name, such as Living room TV";
  label.setAttribute("aria-label", "Friendly device name");
  const trust = document.createElement("select");
  trust.setAttribute("aria-label", "Device trust label");
  const trustLabels = {
    unreviewed: "Not reviewed",
    recognized: "Recognized",
    watch: "Watch closely",
    retired: "No longer used",
  };
  for (const [value, text] of Object.entries(trustLabels)) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = text;
    option.selected = value === (device.trust_state || "unreviewed");
    trust.append(option);
  }
  const type = document.createElement("input");
  type.type = "text";
  type.maxLength = 80;
  type.value = device.device_type || "";
  type.placeholder = "Optional type, such as phone or camera";
  type.setAttribute("aria-label", "Device type");
  const save = document.createElement("button");
  save.type = "submit";
  save.textContent = "Save labels";
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    save.disabled = true;
    saveNetworkDeviceProfile(device.device_id, label.value, trust.value, type.value)
      .catch((error) => { save.disabled = false; showError(error); });
  });
  form.append(formTitle, label, trust, type, save);

  const history = document.createElement("div");
  history.className = "network-history";
  const historyTitle = document.createElement("h3");
  historyTitle.textContent = "Observation history";
  history.append(historyTitle);
  const events = Array.isArray(detail.events) ? detail.events : [];
  const sessions = Array.isArray(detail.sessions) ? detail.sessions : [];
  const addresses = Array.isArray(detail.addresses) ? detail.addresses : [];
  const historyRows = [
    ...events.slice(0, 20).map((row) => ({
      title: row.event_type || row.presence_state || "Observed",
      detail: [row.observed_at || row.created_at, row.ipv4, row.summary || row.detail].filter(Boolean).join(" · "),
    })),
    ...sessions.slice(0, 10).map((row) => ({
      title: "Observed session",
      detail: [
        [row.started_at || row.first_seen, row.ended_at || row.last_reachable_at || row.last_seen].filter(Boolean).join(" to "),
        row.observation_count != null ? `${row.observation_count} observations` : null,
      ].filter(Boolean).join(" · "),
    })),
    ...addresses.slice(0, 10).map((row) => ({
      title: row.ipv4 || row.address || "Observed address",
      detail: [
        row.hostname,
        row.mac,
        row.last_seen,
        row.seen_count != null ? `${row.seen_count} observations` : null,
      ].filter(Boolean).join(" · "),
    })),
  ];
  if (!historyRows.length) {
    history.append(emptyUtility("No detailed history has been recorded for this device yet."));
  } else {
    const list = document.createElement("div");
    list.className = "utility-list";
    for (const item of historyRows) {
      const row = document.createElement("article");
      row.className = "utility-row";
      const main = document.createElement("div");
      main.className = "utility-row-main";
      const title = document.createElement("strong");
      title.textContent = item.title;
      const meta = document.createElement("small");
      meta.textContent = item.detail || "No additional observation detail";
      main.append(title, meta);
      row.append(main);
      list.append(row);
    }
    history.append(list);
  }
  section.append(head, facts, form, history);
  container.append(section);
}

function renderNetworkDevices(container, data) {
  const rows = networkInventoryRows(data);
  const controls = document.createElement("div");
  controls.className = "network-filter-bar";
  const filters = [
    ["all", "All"],
    ["online", "Reachable in last check"],
    ["new", "New"],
    ["review", "Needs review"],
  ];
  for (const [value, label] of filters) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = label;
    button.classList.toggle("active", state.networkDeviceFilter === value);
    button.addEventListener("click", () => {
      state.networkDeviceFilter = value;
      paintNetworkInventory();
    });
    controls.append(button);
  }
  const search = document.createElement("input");
  search.type = "search";
  search.placeholder = "Find a device";
  search.value = state.networkDeviceSearch;
  search.setAttribute("aria-label", "Find a network device");
  search.addEventListener("input", () => {
    state.networkDeviceSearch = search.value;
    paintNetworkInventory();
    const next = document.querySelector(".network-filter-bar input");
    if (next) {
      next.focus();
      next.setSelectionRange(state.networkDeviceSearch.length, state.networkDeviceSearch.length);
    }
  });
  controls.append(search);
  container.append(controls);

  const query = state.networkDeviceSearch.trim().toLowerCase();
  const visible = rows.filter((device) => {
    const filterMatch = state.networkDeviceFilter === "all"
      || (state.networkDeviceFilter === "online" && (device.visible_now || device.presence_state === "reachable"))
      || (state.networkDeviceFilter === "new" && device.is_new)
      || (state.networkDeviceFilter === "review" && ["unreviewed", "watch"].includes(device.trust_state || "unreviewed"));
    const text = [
      networkDeviceName(device), device.hostname, device.ipv4, device.mac,
      device.device_type, device.trust_state,
    ].filter(Boolean).join(" ").toLowerCase();
    return filterMatch && (!query || text.includes(query));
  });
  if (!rows.length) {
    container.append(emptyUtility("No devices have been observed yet. Pair a network, then press Check network now."));
    return;
  }
  if (!visible.length) {
    container.append(emptyUtility("No devices match this filter."));
    return;
  }

  const grid = document.createElement("div");
  grid.className = "network-device-grid";
  for (const device of visible) {
    const card = document.createElement("article");
    card.className = "utility-card network-device-card";
    const head = document.createElement("div");
    head.className = "utility-card-head";
    const heading = document.createElement("h3");
    heading.textContent = networkDeviceName(device);
    const badges = document.createElement("div");
    badges.className = "network-badges";
    badges.append(makePill(networkPresenceLabel(device)));
    if (device.is_new) badges.append(makePill("new"));
    head.append(heading, badges);
    const subtitle = document.createElement("p");
    subtitle.textContent = [
      device.device_type || "Type not labeled",
      device.trust_state === "unreviewed" || !device.trust_state
        ? "Needs your review"
        : device.trust_state,
    ].join(" · ");
    const facts = document.createElement("div");
    facts.className = "network-card-facts";
    const reachable = Boolean(
      device.visible_now || device.presence_state === "reachable"
    );
    const cached = !reachable && Boolean(
      device.cached_now || device.presence_state === "cached"
    );
    const presenceDetail = reachable
      ? formatObservedDuration(device.continuous_visible_seconds)
      : (cached ? "Cached evidence only" : "Not observed in the last check");
    facts.append(
      networkFact("IP", device.ipv4 || "Unavailable"),
      networkFact("MAC", device.mac || "Identity limited"),
      networkFact("Last seen", formatTimestamp(device.last_seen)),
      networkFact(
        reachable ? "Reachability in last check" : "Last-check presence",
        presenceDetail,
      ),
    );
    const detail = document.createElement("button");
    detail.type = "button";
    detail.className = "ghost";
    detail.textContent = "View history and labels";
    detail.addEventListener("click", () => openNetworkDevice(device.device_id).catch(showError));
    card.append(head, subtitle, facts, detail);
    grid.append(card);
  }
  container.append(grid);
}

function renderNetworkDefense(container, data) {
  const perScope = Array.isArray(data?.security_assessments)
    ? data.security_assessments.filter((item) => item && typeof item === "object")
    : [];
  const fallback = data?.security_assessment || data?.inventory?.security_assessment;
  const defenses = perScope.length
    ? perScope
    : (fallback && typeof fallback === "object" ? [fallback] : []);
  if (!defenses.length) return;
  for (const defense of defenses) {
  const section = document.createElement("article");
  section.className = "utility-card network-defense";
  const head = document.createElement("div");
  head.className = "utility-card-head";
  const heading = document.createElement("h3");
  heading.textContent = defense.scope_name
    ? `Network security observations — ${String(defense.scope_name)}`
    : "Network security observations";
  const postureLabels = {
    urgent_review: "Attention needed",
    review_required: "Review",
    monitor: "Limited observation",
    no_current_signals: "No current inventory signals",
    assessment_unavailable: "Assessment unavailable",
    scope_selection_required: "Review each network separately",
  };
  head.append(heading, makePill(
    postureLabels[defense.posture] || "Limited evidence",
  ));
  const conclusion = document.createElement("p");
  conclusion.textContent = defense.conclusion
    || "No evidence-scored network conclusion is available.";
  const boundary = document.createElement("p");
  boundary.className = "network-defense-boundary";
  boundary.textContent = "Inventory observations cannot detect malware, traffic attacks, vulnerable services, stolen credentials, or account compromise. No automatic blocking is performed.";
  const coverage = document.createElement("small");
  const freshness = defense?.coverage?.freshness_state || "unknown freshness";
  const lastCheck = formatTimestamp(defense?.coverage?.last_scan_at);
  const rangeCoverage = defense?.coverage?.complete_for_selected_range;
  coverage.textContent = [
    `Evidence freshness: ${String(freshness)}`,
    `Last check: ${lastCheck}`,
    rangeCoverage === true
      ? "Selected range complete"
      : rangeCoverage === false
        ? "Selected range incomplete"
        : "Range coverage unknown",
  ].join(" · ");
  section.append(head, conclusion, coverage, boundary);

  const signals = Array.isArray(defense.signals) ? defense.signals.slice(0, 12) : [];
  if (signals.length) {
    const list = document.createElement("div");
    list.className = "network-defense-signals";
    for (const signal of signals) {
      const row = document.createElement("article");
      row.className = `network-defense-signal severity-${String(signal.severity || "informational")}`;
      const signalHead = document.createElement("div");
      signalHead.className = "utility-card-head";
      const title = document.createElement("strong");
      title.textContent = String(signal.summary || "Evidence needs review");
      signalHead.append(
        title,
        makePill(`Review priority: ${String(signal.severity || "informational")}`),
      );
      const evidence = document.createElement("small");
      evidence.textContent = [
        signal.rule_id ? `Rule ${signal.rule_id}` : null,
        signal.confidence ? `${signal.confidence} evidence confidence` : null,
        signal.device_id ? `device ${String(signal.device_id).slice(0, 8)}` : null,
      ].filter(Boolean).join(" · ");
      const recommendation = document.createElement("p");
      recommendation.textContent = signal.recommended_action
        || "Review the underlying observation before taking action.";
      row.append(signalHead, evidence, recommendation);
      const alternatives = Array.isArray(signal.benign_explanations)
        ? signal.benign_explanations.slice(0, 3)
        : [];
      if (alternatives.length) {
        const benign = document.createElement("small");
        benign.textContent = `Possible benign explanation: ${alternatives.join(" ")}`;
        row.append(benign);
      }
      list.append(row);
    }
    section.append(list);
  }
  const receipt = document.createElement("small");
  receipt.className = "network-defense-receipt";
  receipt.textContent = [
    defense.ruleset_version ? `Rules ${defense.ruleset_version}` : null,
    defense.receipt_sha256 ? `Receipt ${String(defense.receipt_sha256).slice(0, 12)}` : null,
    "Automatic action: None",
  ].filter(Boolean).join(" · ");
  section.append(receipt);
  container.append(section);
  }
  const tools = data?.defensive_tools;
  if (tools && typeof tools === "object") {
    const card = document.createElement("article");
    card.className = "utility-card network-defense-tools";
    const head = document.createElement("div");
    head.className = "utility-card-head";
    const heading = document.createElement("h3");
    heading.textContent = "Defensive tool readiness";
    const mode = String(tools.mode || "alert-only");
    head.append(heading, makePill(mode === "safe-readonly" ? "safe read-only" : mode));
    const boundary = document.createElement("p");
    boundary.className = "network-defense-boundary";
    boundary.textContent = "Jarvis may automatically run only allowlisted passive local diagnostics. Active probes are not automatic and must be wired to an exact approval before use; this registry cannot apply blocking, quarantine, or firewall changes.";
    card.append(head, boundary);
    const installed = Array.isArray(tools.installed) ? tools.installed.slice(0, 32) : [];
    if (installed.length) {
      const list = document.createElement("ul");
      for (const tool of installed) {
        const row = document.createElement("li");
        row.textContent = `${boundedIncidentText(tool.display_name, "Approved defensive tool", 240)} · ${boundedIncidentText(tool.category, "defensive", 120)}`;
        list.append(row);
      }
      card.append(list);
    } else {
      const empty = document.createElement("p");
      empty.textContent = mode === "safe-readonly"
        ? "No approved passive diagnostic has been discovered yet. Alerts still remain active."
        : "Passive diagnostics are not enabled; durable explanatory alerts remain available.";
      card.append(empty);
    }
    const unavailable = Array.isArray(tools.unavailable) ? tools.unavailable.length : 0;
    const status = document.createElement("small");
    status.textContent = [
      `${installed.length} approved tool adapter${installed.length === 1 ? "" : "s"} ready`,
      `${unavailable} optional adapter${unavailable === 1 ? "" : "s"} unavailable`,
      tools.last_error ? boundedIncidentText(tools.last_error, "", 500) : null,
    ].filter(Boolean).join(" · ");
    card.append(status);
    container.append(card);
  }
}

function bluetoothDeviceName(device) {
  return device.display_name
    || device.label
    || device.os_reported_name
    || "Unknown Bluetooth endpoint";
}

function bluetoothConnectionLabel(device) {
  if (device.connected_evidence_available === true) {
    return device.connected === true
      ? "Windows reports connected"
      : "Windows reports not connected";
  }
  return "Connection evidence unavailable";
}

function bluetoothReviewSignal(data, deviceId) {
  const signals = Array.isArray(data?.security_assessment?.signals)
    ? data.security_assessment.signals
    : [];
  return signals.find((item) => (
    item?.rule_id === "new_unreviewed_paired_endpoint"
    && item?.device_id === deviceId
  )) || null;
}

function showNewBluetoothDeviceAlerts(data) {
  if (data?.baseline_created === true) return;
  loadBluetoothAlertReceipts();
  const rows = Array.isArray(data?.devices) ? data.devices : [];
  const rowsById = new Map(rows.map((device) => [device?.device_id, device]));
  const pending = Array.isArray(data?.pending_alerts?.alerts)
    ? data.pending_alerts.alerts
    : [];
  const newRows = pending
    .filter((alert) => (
      Number.isInteger(alert?.event_id)
      && typeof alert?.receipt_id === "string"
      && /^[0-9a-f]{32}$/.test(alert.receipt_id)
      && typeof alert?.device_id === "string"
    ))
    .map((alert) => {
      const device = {
        ...(rowsById.get(alert.device_id) || {}),
        device_id: alert.device_id,
        display_name: alert.display_name
          || rowsById.get(alert.device_id)?.display_name
          || "Unknown Bluetooth endpoint",
        trust_state: alert.trust_state || "unreviewed",
        first_seen: rowsById.get(alert.device_id)?.first_seen || alert.observed_at,
      };
      return {
        device,
        signal: bluetoothReviewSignal(data, device.device_id) || {
          severity: "informational",
          summary: alert.summary || "A paired endpoint is new to Jarvis.",
          recommended_action: "Identify it before changing its review state.",
        },
        alertReceipt: `${alert.event_id}:${alert.receipt_id}`,
        serverAlert: alert,
      };
    })
    .filter((item) => !state.notifiedBluetoothDevices.has(item.alertReceipt))
    .slice(0, 12);
  if (!newRows.length) return;
  const list = $("new-bluetooth-device-list");
  list.replaceChildren();
  for (const {device, signal, alertReceipt, serverAlert} of newRows) {
    state.notifiedBluetoothDevices.add(alertReceipt);
    state.visibleBluetoothAlerts.set(alertReceipt, {
      event_id: serverAlert.event_id,
      receipt_id: serverAlert.receipt_id,
    });
    const card = document.createElement("article");
    card.className = "new-network-device-card";
    const head = document.createElement("div");
    head.className = "utility-card-head";
    const title = document.createElement("strong");
    title.textContent = bluetoothDeviceName(device);
    head.append(
      title,
      makePill(`Review priority: ${String(signal?.severity || "informational")}`),
    );
    const facts = document.createElement("div");
    facts.className = "new-network-device-facts";
    facts.append(
      networkFact("Pairing evidence", "Windows reports paired in this check"),
      networkFact("Connection", bluetoothConnectionLabel(device)),
      networkFact("Transport", (device.transports || []).join(" / ") || "Unknown"),
      networkFact("Device type", device.device_type || "Unknown"),
      networkFact("Manufacturer", device.manufacturer || "Unknown"),
      networkFact("Model", device.model_name || "Unknown"),
      networkFact("First observed by Jarvis", formatTimestamp(device.first_seen)),
      networkFact("Observation check", formatTimestamp(data.last_check_at)),
    );
    const assessment = document.createElement("p");
    assessment.className = "network-defense-boundary";
    assessment.textContent = signal
      ? `${signal.summary} Compromise is not established. ${serverAlert.evidence_boundary || signal.recommended_action}`
      : "This endpoint needs identification. Compromise is not established and Jarvis took no automatic action.";
    const review = document.createElement("button");
    review.type = "button";
    review.className = "primary";
    review.textContent = "Review Bluetooth device";
    review.addEventListener("click", () => {
      $("new-bluetooth-device-dialog").close();
      openUtility("devices")
        .then(() => openBluetoothDevice(device.device_id))
        .catch(showError);
    });
    card.append(head, facts, assessment, review);
    list.append(card);
  }
  schedulePriorityDialogs();
}

async function acknowledgeBluetoothAlert(alertReceipt, alert) {
  try {
    await post("/api/bluetooth-inventory/alerts/acknowledge", alert);
    rememberBluetoothAlertReceipt(alertReceipt);
    state.visibleBluetoothAlerts.delete(alertReceipt);
  } catch (error) {
    showError(error);
  }
}

function acknowledgeVisibleBluetoothAlerts() {
  for (const [alertReceipt, alert] of [...state.visibleBluetoothAlerts.entries()]) {
    acknowledgeBluetoothAlert(alertReceipt, alert).catch(showError);
  }
}

async function refreshBluetoothInventory() {
  state.bluetoothInventory = await api("/api/bluetooth-inventory");
  showNewBluetoothDeviceAlerts(state.bluetoothInventory);
  paintNetworkInventory();
}

async function checkBluetoothInventory() {
  if (state.bluetoothCheckPending) return;
  state.bluetoothCheckPending = true;
  paintNetworkInventory();
  try {
    const result = await post("/api/bluetooth-inventory/check", {});
    state.bluetoothInventory = result.status;
    showNewBluetoothDeviceAlerts(result.status);
  } finally {
    state.bluetoothCheckPending = false;
    paintNetworkInventory();
  }
}

async function openBluetoothDevice(deviceId) {
  state.bluetoothDeviceDetail = await api(
    `/api/bluetooth-inventory/device?device_id=${encodeURIComponent(deviceId)}&event_limit=100`,
  );
  paintNetworkInventory();
}

async function saveBluetoothDeviceProfile(deviceId, label, trustState, deviceType) {
  await post("/api/bluetooth-inventory/devices/profile", {
    device_id: deviceId,
    label: label || null,
    trust_state: trustState || null,
    device_type: deviceType || null,
  });
  state.bluetoothDeviceDetail = null;
  await refreshBluetoothInventory();
}

function renderBluetoothInventorySection(container, data) {
  const heading = document.createElement("h2");
  heading.className = "utility-section-title";
  heading.textContent = "Paired Bluetooth devices";
  container.append(heading);
  if (!data) {
    container.append(emptyUtility("Loading paired Bluetooth history…"));
    return;
  }
  const overview = document.createElement("article");
  overview.className = "utility-card network-overview";
  const head = document.createElement("div");
  head.className = "utility-card-head";
  const title = document.createElement("h3");
  title.textContent = "Bluetooth";
  head.append(title, makePill(
    !data.enabled ? "disabled" : data.available ? "paired read-only" : "unavailable",
  ));
  const explanation = document.createElement("p");
  explanation.textContent = data.enabled
    ? "Jarvis checks only endpoints Windows already reports as paired. It never scans nearby unpaired radios, exposes Bluetooth addresses, pairs, connects, controls, or blocks devices."
    : "Paired Bluetooth inventory is disabled in Jarvis settings.";
  const monitor = document.createElement("p");
  const monitorState = data.monitor || {};
  monitor.textContent = monitorState.enabled
    ? monitorState.suppressed_by_control
      ? `Automatic paired-device checks: paused by runtime control (${String(monitorState.control_state || "stopped")}).`
      : `Automatic paired-device checks: ${monitorState.running ? "on" : "starting"} · every ${Number(monitorState.interval_seconds || 60)} seconds · last check ${formatTimestamp(monitorState.last_check_at, true)}.`
    : "Automatic paired-device checks: off.";
  overview.append(head, explanation, monitor);
  if (data.error) {
    const error = document.createElement("p");
    error.className = "network-error";
    error.textContent = data.error;
    overview.append(error);
  }
  container.append(overview);
  if (!data.enabled || !data.available) return;

  const devices = Array.isArray(data.devices) ? data.devices : [];
  const paired = Number(
    data.paired_in_last_check ?? data.paired_now ?? 0,
  );
  const summary = document.createElement("div");
  summary.className = "network-summary-grid";
  summary.append(
    networkSummaryCard(
      "Paired in last check",
      paired,
      `Last check: ${formatTimestamp(data.last_check_at)}`,
    ),
    networkSummaryCard("Known", Number(data.known_endpoints || devices.length), "Private Jarvis history"),
    networkSummaryCard("New", Number(data.new_endpoints || 0), "New to Jarvis, not proof of a new pairing"),
    networkSummaryCard(
      "Needs review",
      devices.filter((item) => ["unreviewed", "watch"].includes(item.trust_state || "unreviewed")).length,
      "Review priority, not a threat verdict",
    ),
  );
  container.append(summary);

  const defense = data.security_assessment || {};
  const defenseCard = document.createElement("article");
  defenseCard.className = "utility-card network-defense";
  const defenseHead = document.createElement("div");
  defenseHead.className = "utility-card-head";
  const defenseTitle = document.createElement("h3");
  defenseTitle.textContent = "Bluetooth security observations";
  defenseHead.append(
    defenseTitle,
    makePill(`Review priority: ${String(defense.highest_severity || "none")}`),
  );
  const conclusion = document.createElement("p");
  conclusion.textContent = defense.conclusion || "No Bluetooth review conclusion is available.";
  const boundary = document.createElement("p");
  boundary.className = "network-defense-boundary";
  boundary.textContent = defense.evidence_boundary
    || "Paired-device evidence cannot establish compromise. No automatic containment is performed.";
  defenseCard.append(defenseHead, conclusion, boundary);
  const signals = Array.isArray(defense.signals) ? defense.signals.slice(0, 12) : [];
  for (const signal of signals) {
    const row = document.createElement("article");
    row.className = `network-defense-signal severity-${String(signal.severity || "informational")}`;
    const title = document.createElement("strong");
    title.textContent = String(signal.summary || "Bluetooth evidence needs review");
    const advice = document.createElement("p");
    advice.textContent = signal.recommended_action || "Review the paired-device evidence.";
    row.append(title, advice);
    defenseCard.append(row);
  }
  container.append(defenseCard);

  const actions = document.createElement("article");
  actions.className = "utility-card network-scan-card";
  const actionTitle = document.createElement("h3");
  actionTitle.textContent = "Check paired devices";
  const actionHelp = document.createElement("p");
  actionHelp.textContent = "Reads the Windows paired-device list only. This is not a nearby Bluetooth scan.";
  const check = document.createElement("button");
  check.type = "button";
  check.textContent = state.bluetoothCheckPending || data.check_in_progress
    ? "Checking paired devices…"
    : "Check Bluetooth now";
  check.disabled = state.bluetoothCheckPending || data.check_in_progress;
  check.addEventListener("click", () => checkBluetoothInventory().catch(showError));
  actions.append(actionTitle, actionHelp, check);
  container.append(actions);

  if (state.bluetoothDeviceDetail?.device) {
    const detail = state.bluetoothDeviceDetail;
    const device = detail.device;
    const detailCard = document.createElement("article");
    detailCard.className = "utility-card network-detail";
    const detailHead = document.createElement("div");
    detailHead.className = "utility-card-head";
    const detailTitle = document.createElement("h3");
    detailTitle.textContent = bluetoothDeviceName(device);
    const close = document.createElement("button");
    close.type = "button";
    close.className = "ghost";
    close.textContent = "Close details";
    close.addEventListener("click", () => {
      state.bluetoothDeviceDetail = null;
      paintNetworkInventory();
    });
    detailHead.append(detailTitle, close);
    const facts = document.createElement("div");
    facts.className = "network-detail-facts";
    facts.append(
      networkFact("Paired evidence", device.paired_in_last_check ? "Windows reported paired in last check" : "Not reported in last check"),
      networkFact("Connection", bluetoothConnectionLabel(device)),
      networkFact("Manufacturer", device.manufacturer || "Unknown"),
      networkFact("Model", device.model_name || "Unknown"),
      networkFact("First observed", formatTimestamp(device.first_seen)),
      networkFact("Last observed", formatTimestamp(device.last_observed_at || device.last_seen)),
    );
    const form = document.createElement("form");
    form.className = "network-profile-form";
    const label = document.createElement("input");
    label.type = "text";
    label.maxLength = 120;
    label.value = device.label || "";
    label.placeholder = "Friendly name";
    const trust = document.createElement("select");
    for (const [value, text] of Object.entries({
      unreviewed: "Not reviewed",
      recognized: "Recognized",
      watch: "Watch closely",
      retired: "No longer used",
    })) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = text;
      option.selected = value === (device.trust_state || "unreviewed");
      trust.append(option);
    }
    const type = document.createElement("input");
    type.type = "text";
    type.maxLength = 80;
    type.value = device.device_type || "";
    type.placeholder = "Device type";
    const save = document.createElement("button");
    save.type = "submit";
    save.textContent = "Save labels";
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      save.disabled = true;
      saveBluetoothDeviceProfile(device.device_id, label.value, trust.value, type.value)
        .catch((error) => { save.disabled = false; showError(error); });
    });
    form.append(label, trust, type, save);
    detailCard.append(detailHead, facts, form);
    container.append(detailCard);
  }

  const grid = document.createElement("div");
  grid.className = "network-device-grid";
  for (const device of devices) {
    const card = document.createElement("article");
    card.className = "utility-card network-device-card";
    const head = document.createElement("div");
    head.className = "utility-card-head";
    const title = document.createElement("h3");
    title.textContent = bluetoothDeviceName(device);
    head.append(title, makePill(
      device.paired_now ? "fresh paired evidence" : "saved paired evidence",
    ));
    const facts = document.createElement("div");
    facts.className = "network-card-facts";
    facts.append(
      networkFact("Connection", bluetoothConnectionLabel(device)),
      networkFact("Transport", (device.transports || []).join(" / ") || "Unknown"),
      networkFact("Model", device.model_name || "Unknown"),
      networkFact("Last observed", formatTimestamp(device.last_observed_at || device.last_seen)),
    );
    const review = document.createElement("button");
    review.type = "button";
    review.className = "ghost";
    review.textContent = "View history and labels";
    review.addEventListener("click", () => openBluetoothDevice(device.device_id).catch(showError));
    card.append(head, facts, review);
    grid.append(card);
  }
  if (devices.length) container.append(grid);
  else container.append(emptyUtility("No paired Bluetooth endpoints have been observed yet."));
}

function paintNetworkInventory() {
  if (state.activeView !== "devices") return;
  const content = $("utility-content");
  const data = state.networkInventory;
  content.replaceChildren();
  try {
  if (!data) {
    content.append(emptyUtility("Loading stored Home Network inventory…"));
    return;
  }

  const overview = document.createElement("article");
  overview.className = "utility-card network-overview";
  const head = document.createElement("div");
  head.className = "utility-card-head";
  const title = document.createElement("h3");
  title.textContent = "Home Network";
  head.append(title, makePill(
    !data.enabled ? "disabled" : data.available ? "ready" : "unavailable",
  ));
  const explanation = document.createElement("p");
  explanation.textContent = data.enabled
    ? "Jarvis keeps a private history of devices it has observed on networks you explicitly pair. Opening this page never scans."
    : "Home Network inventory is disabled in Jarvis settings. No adapter is paired and no scan can run.";
  const monitor = document.createElement("p");
  const monitorState = data?.monitor || {};
  monitor.textContent = monitorState.enabled
    ? monitorState.suppressed_by_control
      ? `Automatic bounded checks: paused by runtime control (${String(monitorState.control_state || "stopped")}). No checks or passive diagnostics will start.`
      : `Automatic bounded checks: ${monitorState.running ? "on" : "starting"} · every ${Number(monitorState.interval_seconds || 300)} seconds · last check ${formatTimestamp(monitorState.last_check_at, true)}. No automatic blocking.`
    : "Automatic checks: off. New-device alerts appear after an explicit check.";
  overview.append(head, explanation, monitor);
  if (data.error) {
    const error = document.createElement("p");
    error.className = "network-error";
    error.textContent = data.error;
    overview.append(error);
  }
  content.append(overview);
  if (!data.enabled || !data.available) return;

  const inventory = data.inventory || {};
  const rows = networkInventoryRows(data);
  const online = Number(inventory.visible_devices) || rows.filter((row) => row.visible_now || row.presence_state === "reachable").length;
  const cached = Number(inventory.cached_devices) || rows.filter((row) => row.cached_now || row.presence_state === "cached").length;
  const fresh = Number(inventory.new_devices) || rows.filter((row) => row.is_new).length;
  const known = Number(inventory.total_known_devices ?? inventory.known_devices) || rows.length;
  const review = rows.filter((row) => ["unreviewed", "watch"].includes(row.trust_state || "unreviewed")).length;
  const summary = document.createElement("div");
  summary.className = "network-summary-grid";
  summary.append(
    networkSummaryCard(
      "Reachable in last check",
      online,
      `Most recent check: ${formatTimestamp(inventory.last_scan_at)}`,
    ),
    networkSummaryCard("Cached", cached, "Recently known to this computer"),
    networkSummaryCard("New", fresh, fresh ? "Review devices you do not recognize" : "No newly observed devices"),
    networkSummaryCard("Known", known, "Kept in private observation history"),
    networkSummaryCard("Needs review", review, "Your labels, not a security verdict"),
  );
  content.append(summary);
  renderNetworkDefense(content, data);
  const security = inventory.security_summary || {};
  if (security.baseline_created || Number(security.review_new_devices) > 0) {
    const notice = document.createElement("article");
    notice.className = "utility-card network-baseline-notice";
    const noticeTitle = document.createElement("h3");
    noticeTitle.textContent = security.baseline_created
      ? "First observation baseline created"
      : "New devices need your review";
    const noticeText = document.createElement("p");
    noticeText.textContent = security.baseline_created
      ? "Match each device to something you recognize. The first check is a baseline, not an alert."
      : (security.advice || "Label devices you recognize and mark anything unexpected for closer review.");
    notice.append(noticeTitle, noticeText);
    content.append(notice);
  }

  renderNetworkScopeSetup(content, data);
  const activeScopes = (data.scopes || []).filter((scope) => scope.active);
  if (activeScopes.length) {
    const actions = document.createElement("section");
    actions.className = "utility-card network-scan-card";
    const heading = document.createElement("h3");
    heading.textContent = "Check for devices";
    const help = document.createElement("p");
    help.textContent = "This sends one bounded presence check across the selected paired network. It does not inspect ports, services, files, or traffic.";
    const select = document.createElement("select");
    select.setAttribute("aria-label", "Paired network to check");
    for (const scope of activeScopes) {
      const option = document.createElement("option");
      option.value = scope.scope_id;
      option.textContent = scope.display_name || scope.interface_alias || scope.cidr || "Paired network";
      select.append(option);
    }
    const scan = document.createElement("button");
    scan.type = "button";
    scan.textContent = state.networkScanPending || data.scan_in_progress
      ? "Checking network…"
      : "Check network now";
    scan.disabled = state.networkScanPending || data.scan_in_progress || !data.can_scan;
    scan.addEventListener("click", () => scanNetworkInventory(select.value).catch(showError));
    const last = document.createElement("small");
    last.textContent = inventory.last_scan_at
      ? `Last checked ${formatTimestamp(inventory.last_scan_at)}`
      : "This network has not been checked yet.";
    actions.append(heading, help, select, scan, last);
    content.append(actions);
  }

  renderNetworkDeviceDetail(content);
  const deviceHeading = document.createElement("h3");
  deviceHeading.className = "utility-section-title";
  deviceHeading.textContent = "Observed devices";
  content.append(deviceHeading);
  renderNetworkDevices(content, data);

  const limits = document.createElement("article");
  limits.className = "utility-card network-limitations";
  const limitsTitle = document.createElement("h3");
  limitsTitle.textContent = "What this view can and cannot know";
  const list = document.createElement("ul");
  for (const item of data.limitations || []) {
    const row = document.createElement("li");
    row.textContent = item;
    list.append(row);
  }
  limits.append(limitsTitle, list);
  content.append(limits);
  } finally {
    renderBluetoothInventorySection(content, state.bluetoothInventory);
  }
}

async function renderNetworkInventory(generation = null) {
  const render = beginUtilityRender("devices", generation);
  if (!render) return;
  const {content} = render;
  content.replaceChildren(emptyUtility("Loading stored Home Network inventory…"));
  const [networkResult, bluetoothResult] = await Promise.allSettled([
    api("/api/network-inventory"),
    api("/api/bluetooth-inventory"),
  ]);
  if (networkResult.status === "fulfilled") {
    state.networkInventory = networkResult.value;
  } else {
    const error = networkResult.reason;
    state.networkInventory = {
      enabled: true,
      available: false,
      error: error.message || "Home Network is unavailable.",
    };
  }
  if (bluetoothResult.status === "fulfilled") {
    state.bluetoothInventory = bluetoothResult.value;
    showNewBluetoothDeviceAlerts(state.bluetoothInventory);
  } else {
    const error = bluetoothResult.reason;
    state.bluetoothInventory = {
      enabled: true,
      available: false,
      error: error.message || "Paired Bluetooth inventory is unavailable.",
    };
  }
  showNetworkDefenseIncidents(state.networkInventory?.pending_incidents);
  if (!isUtilityRenderCurrent(render)) return;
  paintNetworkInventory();
}

function publicPresenceSummary(data) {
  const control = data?.control || {};
  if (!data || data.error || data.effective_state === "unavailable") {
    return {label: "Unavailable · safely offline", tone: "failed"};
  }
  if (control.emergency_stopped) return {label: "Emergency stopped", tone: "failed"};
  if (!data?.configured_enabled || !control.enabled) return {label: "Disabled", tone: "disabled"};
  if (control.paused) return {label: "Social activity paused", tone: "paused"};
  return {label: "Foundation ready", tone: "ready"};
}

async function controlPublicPresence(action) {
  const result = await post("/api/public-presence/control", {action});
  state.publicPresence = result.status;
  toast(`Public Presence: ${publicPresenceSummary(result.status).label}.`);
  await renderPublicPresence();
}

async function renderPublicPresence(generation = null) {
  const render = beginUtilityRender("public-presence", generation);
  if (!render) return;
  const {content} = render;
  content.replaceChildren(emptyUtility("Checking the Public Presence boundary…"));
  const data = await api("/api/public-presence");
  state.publicPresence = data;
  if (!isUtilityRenderCurrent(render)) return;
  const summary = publicPresenceSummary(data);

  const overview = document.createElement("article");
  overview.className = "utility-card public-presence-overview";
  const head = document.createElement("div");
  head.className = "utility-card-head";
  const title = document.createElement("h3");
  title.textContent = "Public Presence foundation";
  const summaryPill = makePill(summary.label);
  summaryPill.className = `utility-pill ${summary.tone}`;
  head.append(title, summaryPill);
  const explanation = document.createElement("p");
  explanation.textContent = data.error
    ? "The isolated public store is unavailable. Public Presence remains safely offline; repair its local foundation before using these controls."
    : data.configured_enabled
    ? "The separate public process is permitted by configuration, but no platform is connected and no publishing tool exists in this foundation."
    : "Disabled by configuration. No public listener, account connection, social API, or publishing method can run.";
  overview.append(head, explanation);

  const grid = document.createElement("div");
  grid.className = "utility-grid public-presence-grid";
  const facts = [
    ["Process", data.process_running ? "Running" : "Not running", data.process_running ? "running" : "disabled"],
    ["Platforms", String(data.connected_platforms || 0), (data.connected_platforms || 0) ? "running" : "disabled"],
    ["Publishing", data.publishing_available ? "Available" : "Unavailable", data.publishing_available ? "running" : "disabled"],
    ["Private bridge", data.private_bridge || "Closed + sanitized", "ready"],
  ];
  for (const [name, value, tone] of facts) {
    const card = document.createElement("article");
    card.className = "utility-card public-presence-fact";
    const cardHead = document.createElement("div");
    cardHead.className = "utility-card-head";
    const cardTitle = document.createElement("h3");
    cardTitle.textContent = name;
    cardHead.append(cardTitle, makePill(tone));
    const detail = document.createElement("p");
    detail.textContent = value;
    card.append(cardHead, detail);
    grid.append(card);
  }

  const controls = document.createElement("article");
  controls.className = "utility-card public-presence-controls";
  const controlsTitle = document.createElement("h3");
  controlsTitle.textContent = "Independent social controls";
  const controlsHelp = document.createElement("p");
  controlsHelp.textContent = "These controls affect only Public Presence. They never pause normal Jarvis chats, projects, or Screen Companion.";
  const actions = document.createElement("div");
  actions.className = "public-presence-actions";
  const pause = document.createElement("button");
  pause.type = "button";
  pause.textContent = !data.control?.enabled
    ? "Social controls locked (disabled)"
    : data.control?.paused
      ? "Resume social foundation"
      : "Pause all social activity";
  pause.disabled = Boolean(data.error || data.control?.emergency_stopped || !data.control?.enabled);
  pause.addEventListener("click", () => controlPublicPresence(data.control?.paused ? "resume" : "pause").catch(showError));
  const stopAll = document.createElement("button");
  stopAll.type = "button";
  stopAll.className = "danger";
  stopAll.textContent = "Emergency stop public activity";
  stopAll.disabled = Boolean(data.error || data.control?.emergency_stopped);
  stopAll.addEventListener("click", () => controlPublicPresence("emergency_stop").catch(showError));
  actions.append(pause, stopAll);
  if (data.control?.emergency_stopped && !data.error) {
    const clear = document.createElement("button");
    clear.type = "button";
    clear.textContent = "Clear emergency stop (stays disabled)";
    clear.addEventListener("click", () => controlPublicPresence("clear_emergency_stop").catch(showError));
    actions.append(clear);
  }
  controls.append(controlsTitle, controlsHelp, actions);

  const boundary = document.createElement("article");
  boundary.className = "utility-card public-presence-boundary";
  const boundaryTitle = document.createElement("h3");
  boundaryTitle.textContent = "Permanent foundation boundaries";
  const boundaryText = document.createElement("p");
  boundaryText.textContent = "Public content is untrusted. This process cannot read private memory, files, browser state, credentials, desktop controls, trading, purchases, deployments, or external communication tools.";
  boundary.append(boundaryTitle, boundaryText);
  content.replaceChildren(overview, grid, controls, boundary);
}

function companionField(labelText, control, detailText = "") {
  const wrapper = document.createElement("label");
  wrapper.className = "companion-field";
  const label = document.createElement("span");
  label.textContent = labelText;
  wrapper.append(label, control);
  if (detailText) {
    const detail = document.createElement("small");
    detail.textContent = detailText;
    wrapper.append(detail);
  }
  return wrapper;
}

function companionQuickView(data) {
  if (!data || data.available === false) {
    return {label: "Unavailable", detail: "Screen Companion is unavailable", tone: "unavailable", mode: "observe", paused: true, off: true};
  }
  const mode = ["disabled", "observe", "suggest", "collaborate"].includes(data.mode) ? data.mode : "disabled";
  const paused = Boolean(data.paused);
  if (mode === "disabled") {
    return {label: "Off", detail: "Screen Companion is off", tone: "off", mode: "observe", paused: true, off: true};
  }
  if (paused) {
    return {label: `Paused · ${mode}`, detail: "No screen observation is happening", tone: "paused", mode, paused: true, off: false};
  }
  const labels = {
    observe: ["Observing", "Active app metadata only"],
    suggest: ["Suggest mode", "Transient visual suggestions enabled"],
    collaborate: ["Collaborating", "Approved routines may run"],
  };
  return {label: labels[mode][0], detail: labels[mode][1], tone: mode, mode, paused: false, off: false};
}

function renderCompanionQuick(data = state.screenCompanion) {
  const view = companionQuickView(data);
  $("companion-chip-label").textContent = view.label;
  $("companion-quick-status").textContent = view.label;
  $("companion-quick-detail").textContent = view.detail;
  $("companion-chip-dot").className = `companion-chip-dot ${view.tone}`;
  $("companion-chip").setAttribute("aria-label", `Screen Companion: ${view.label}`);
  $("companion-quick-mode").value = view.mode;
  const available = Boolean(data && data.available !== false);
  $("companion-quick-mode").disabled = !available;
  $("companion-on").disabled = !available || (!view.off && !view.paused);
  $("companion-pause").disabled = !available || view.off;
  $("companion-pause").textContent = view.paused && !view.off ? "Resume" : "Pause";
  $("companion-off").disabled = !available || view.off;
}

function setCompanionPopover(open, {restoreFocus = false} = {}) {
  const popover = $("companion-popover");
  popover.hidden = !open;
  $("companion-chip").setAttribute("aria-expanded", open ? "true" : "false");
  if (open) $("companion-quick-mode").focus();
  if (!open && restoreFocus) $("companion-chip").focus();
}

async function controlCompanionQuick(action, mode = null) {
  const payload = {action};
  if (mode !== null) payload.mode = mode;
  const result = await post("/api/screen-companion/control", payload);
  const previous = state.screenCompanion || {};
  state.screenCompanion = {
    ...previous,
    ...(result.state || {}),
    available: previous.available !== false,
  };
  renderCompanionQuick();
  toast(`Screen Companion: ${companionQuickView(state.screenCompanion).label}.`);
  if (state.activeView === "companion") await renderCompanion();
}

async function renderCompanion(generation = null) {
  const render = beginUtilityRender("companion", generation);
  if (!render) return;
  const {content} = render;
  content.replaceChildren(emptyUtility("Loading Screen Companion…"));
  const data = await api("/api/screen-companion");
  state.screenCompanion = data;
  renderCompanionQuick();
  if (!isUtilityRenderCurrent(render)) return;
  content.replaceChildren();

  const overview = document.createElement("section");
  overview.className = "utility-card companion-overview";
  const head = document.createElement("div");
  head.className = "utility-card-head";
  const heading = document.createElement("h3");
  heading.textContent = "Active-window assistance";
  head.append(heading, makePill(data.paused ? "paused" : data.mode));
  const privacy = document.createElement("p");
  privacy.textContent = "Off by default. Observe never learns from or saves screen content. Suggest keeps the active-window capture out of Jarvis storage; your configured model provider may still receive it to generate the suggestion.";
  const current = document.createElement("p");
  current.className = "companion-current";
  current.textContent = data.current
    ? `Current: ${data.current.application} · ${data.current.title || "Untitled window"}`
    : "No active-window observation is currently retained.";

  const mode = document.createElement("select");
  for (const [value, label] of [
    ["disabled", "Disabled"],
    ["observe", "Observe only"],
    ["suggest", "Observe + suggest"],
    ["collaborate", "Collaborate on approved routines"],
  ]) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    mode.append(option);
  }
  mode.value = data.mode || "disabled";
  const paused = document.createElement("input");
  paused.type = "checkbox";
  paused.checked = Boolean(data.paused);
  const autoSuggest = document.createElement("input");
  autoSuggest.type = "checkbox";
  autoSuggest.checked = Boolean(data.auto_suggest);
  const exclusions = document.createElement("input");
  exclusions.type = "text";
  exclusions.value = (data.excluded_apps || []).join(", ");
  exclusions.placeholder = "extra-sensitive-app.exe, another-app.exe";
  exclusions.autocomplete = "off";

  const controls = document.createElement("div");
  controls.className = "companion-controls";
  controls.append(
    companionField("Mode", mode, "Collaborate still keeps every normal approval and safety boundary."),
    companionField("Paused", paused, "Pause immediately without losing your saved rules."),
    companionField("Proactive suggestions", autoSuggest, "Rate-limited; available only in Suggest or Collaborate mode."),
    companionField("Always excluded apps", exclusions, "Credential managers are excluded automatically."),
  );
  const actions = document.createElement("div");
  actions.className = "utility-actions";
  const save = document.createElement("button");
  save.type = "button";
  save.className = "primary";
  save.textContent = "Save companion settings";
  save.addEventListener("click", async () => {
    await post("/api/screen-companion/state", {
      mode: mode.value,
      paused: paused.checked,
      auto_suggest: autoSuggest.checked,
      excluded_apps: exclusions.value.split(",").map((item) => item.trim()).filter(Boolean),
    });
    toast("Screen Companion settings saved.");
    await renderCompanion();
  });
  const suggest = document.createElement("button");
  suggest.type = "button";
  suggest.textContent = "Suggest for this screen";
  suggest.disabled = data.paused || !["suggest", "collaborate"].includes(data.mode);
  suggest.addEventListener("click", async () => {
    const result = await post("/api/screen-companion/suggest");
    toast(result.job_id ? "Screen Companion suggestion is ready." : "No suggestion was needed for this screen.");
  });
  const forget = document.createElement("button");
  forget.type = "button";
  forget.className = "ghost";
  forget.textContent = "Forget observations & learning";
  forget.addEventListener("click", async () => {
    await post("/api/screen-companion/forget");
    toast("Screen Companion observations, feedback, and learned outcomes were forgotten.");
    await renderCompanion();
  });
  actions.append(save, suggest, forget);
  overview.append(head, privacy, current, controls, actions);
  content.append(overview);

  const learning = data.learning || {};
  const learningCard = document.createElement("section");
  learningCard.className = "utility-card companion-learning";
  const learningHead = document.createElement("div");
  learningHead.className = "utility-card-head";
  const learningTitle = document.createElement("h3");
  learningTitle.textContent = "What Jarvis has learned from Companion";
  learningHead.append(learningTitle, makePill(`${Number(learning.reusable_outcomes || 0)} verified`));
  const learningSummary = document.createElement("p");
  const feedback = Number(learning.feedback || 0);
  const accepted = Number(learning.accepted || 0);
  const dismissed = Number(learning.dismissed || 0);
  const verified = Number(learning.verified_outcomes || 0);
  const reusable = Number(learning.reusable_outcomes || 0);
  learningSummary.textContent = feedback
    ? `${feedback} explicit decision${feedback === 1 ? "" : "s"}: ${accepted} accepted, ${dismissed} dismissed. ${verified} accepted action${verified === 1 ? " has" : "s have"} a verified outcome; ${reusable} provide a reusable category preference signal.`
    : "Nothing yet. Observe mode is intentionally non-learning; accept or dismiss a suggestion to provide the first privacy-safe feedback signal.";
  const learningBoundary = document.createElement("p");
  learningBoundary.className = "muted";
  learningBoundary.textContent = "Jarvis stores only digests and closed outcome categories here—not screenshots, window titles, visible text, or suggestion text. Verified outcomes may rank categories and repeated dismissals may suppress them, but learning never grants new authority or bypasses approvals.";
  learningCard.append(learningHead, learningSummary, learningBoundary);
  content.append(learningCard);

  const form = document.createElement("form");
  form.className = "utility-card companion-rule-form";
  const formTitle = document.createElement("h3");
  formTitle.textContent = "Add an operator-authored routine";
  const app = document.createElement("input");
  app.required = true;
  app.maxLength = 120;
  app.placeholder = "chrome.exe";
  const titleMatch = document.createElement("input");
  titleMatch.maxLength = 200;
  titleMatch.placeholder = "Optional title text, such as Gmail";
  const actionMode = document.createElement("select");
  for (const [value, label] of [["suggest", "Suggest only"], ["collaborate", "Carry out the routine"]]) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    actionMode.append(option);
  }
  const cooldown = document.createElement("input");
  cooldown.type = "number";
  cooldown.min = "30";
  cooldown.max = "86400";
  cooldown.value = "300";
  const routine = document.createElement("textarea");
  routine.required = true;
  routine.maxLength = 4000;
  routine.rows = 4;
  routine.placeholder = "Example: When Gmail is open, summarize unread mail and draft replies. Never send without my normal approval.";
  const add = document.createElement("button");
  add.type = "submit";
  add.className = "primary";
  add.textContent = "Add routine";
  form.append(
    formTitle,
    companionField("Application", app),
    companionField("Window title contains", titleMatch),
    companionField("Action", actionMode),
    companionField("Cooldown in seconds", cooldown),
    companionField("What Jarvis should do", routine),
    add,
  );
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    await post("/api/screen-companion/rules", {
      trigger_app: app.value,
      title_contains: titleMatch.value || null,
      action_mode: actionMode.value,
      cooldown_seconds: Number(cooldown.value),
      action_prompt: routine.value,
    });
    toast("Screen Companion routine added.");
    await renderCompanion();
  });
  content.append(form);

  const rules = document.createElement("section");
  const rulesTitle = document.createElement("h3");
  rulesTitle.className = "utility-section-title";
  rulesTitle.textContent = "Saved routines";
  rules.append(rulesTitle);
  if (!(data.rules || []).length) {
    rules.append(emptyUtility("No Screen Companion routines are configured."));
  } else {
    const list = document.createElement("div");
    list.className = "utility-list";
    for (const rule of data.rules) {
      const row = document.createElement("article");
      row.className = "utility-row companion-rule";
      const main = document.createElement("div");
      main.className = "utility-row-main";
      const title = document.createElement("strong");
      title.textContent = `${rule.trigger_app}${rule.title_contains ? ` · ${rule.title_contains}` : ""}`;
      const copy = document.createElement("small");
      copy.textContent = `${rule.action_mode} · ${rule.cooldown_seconds}s cooldown · ${rule.action_prompt}`;
      main.append(title, copy);
      const rowActions = document.createElement("div");
      rowActions.className = "utility-actions";
      const toggle = document.createElement("button");
      toggle.type = "button";
      toggle.textContent = rule.enabled ? "Pause" : "Enable";
      toggle.addEventListener("click", async () => {
        await post(`/api/screen-companion/rules/${rule.id}/${rule.enabled ? "disable" : "enable"}`);
        await renderCompanion();
      });
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "danger";
      remove.textContent = "Delete";
      remove.addEventListener("click", async () => {
        await post(`/api/screen-companion/rules/${rule.id}/delete`);
        await renderCompanion();
      });
      rowActions.append(toggle, remove);
      row.append(main, makePill(rule.enabled ? "enabled" : "paused"), rowActions);
      list.append(row);
    }
    rules.append(list);
  }
  content.append(rules);
}

function settingRow(labelText, detailText, control) {
  const row = document.createElement("div");
  row.className = "setting-row";
  const copy = document.createElement("div");
  const label = document.createElement("label");
  label.textContent = labelText;
  const detail = document.createElement("p");
  detail.textContent = detailText;
  copy.append(label, detail);
  row.append(copy, control);
  return row;
}

function featureStateLabel(feature) {
  if (feature.restart_pending) return "restart required";
  if (feature.effective_now) return "on";
  if (feature.decision === "skip") return "not now";
  if (feature.decision === "disable") return "off";
  if (feature.decision === "pending") return "not reviewed";
  return feature.configured ? "set up" : "off";
}

async function decideOptionalFeature(capabilityId, decision) {
  if (!state.featureOnboarding?.configuration_sha256) {
    throw new Error("Optional-feature status is unavailable. Refresh Settings and try again.");
  }
  state.featureDecisionPending.add(capabilityId);
  try {
    const payload = await post("/api/feature-onboarding/decision", {
      capability_id: capabilityId,
      decision,
      expected_configuration_sha256: state.featureOnboarding.configuration_sha256,
    });
    state.featureOnboarding = payload.status;
    const savedFeature = (payload.status?.features || []).find(
      (feature) => feature.capability_id === capabilityId,
    );
    toast(savedFeature?.restart_pending
      ? "Choice saved. Restart Jarvis to apply the change."
      : "Choice saved. You can change it later in Settings.");
  } catch (error) {
    if (error?.status === 409) await refreshFeatureOnboarding();
    throw error;
  } finally {
    state.featureDecisionPending.delete(capabilityId);
  }
  renderFeatureOnboardingDialog();
  if (state.activeView === "customize") await renderCustomize();
}

function featureOnboardingCard(feature, {wizard = false} = {}) {
  const card = document.createElement("article");
  card.className = "feature-onboarding-card";
  const head = document.createElement("div");
  head.className = "utility-card-head";
  const title = document.createElement("h3");
  title.textContent = feature.title || feature.capability_id;
  head.append(title, makePill(featureStateLabel(feature)));
  const description = document.createElement("p");
  description.textContent = feature.description || "Optional Jarvis feature.";
  const safety = document.createElement("p");
  safety.className = "feature-safety";
  safety.textContent = `Safety boundary: ${feature.safety_boundary || "Normal Jarvis safety and approval boundaries remain active."}`;
  card.append(head, description, safety);

  if ((feature.depends_on || []).length) {
    const dependencies = document.createElement("p");
    dependencies.className = "feature-safety";
    const featureRows = state.featureOnboarding?.features || [];
    const dependencyNames = feature.depends_on.map((capabilityId) => (
      featureRows.find((row) => row.capability_id === capabilityId)?.title
      || capabilityId
    ));
    dependencies.textContent = `Also sets up: ${dependencyNames.join(", ")}.`;
    card.append(dependencies);
  }
  if ((feature.disables_dependents || []).length) {
    const dependentNames = feature.disables_dependents.map((capabilityId) => (
      (state.featureOnboarding?.features || []).find(
        (row) => row.capability_id === capabilityId,
      )?.title || capabilityId
    ));
    const cascade = document.createElement("p");
    cascade.className = "feature-safety";
    cascade.textContent = `Turning this off also turns off: ${dependentNames.join(", ")}.`;
    card.append(cascade);
  }
  if (feature.restart_pending) {
    const restart = document.createElement("p");
    restart.className = "feature-restart-note";
    restart.textContent = "Saved configuration differs from the running service. Restart Jarvis to apply it.";
    card.append(restart);
  }
  if (
    feature.capability_id.startsWith("private-lan")
    || feature.capability_id.startsWith("network-defense")
  ) {
    const pairing = document.createElement("p");
    pairing.className = "feature-safety";
    pairing.textContent = "Network access still requires you to pair a network you own or administer in Devices. Setup never pairs or scans automatically.";
    card.append(pairing);
  }

  const actions = document.createElement("div");
  actions.className = "feature-onboarding-actions";
  const busy = state.featureDecisionPending.has(feature.capability_id);
  const setup = document.createElement("button");
  setup.type = "button";
  setup.className = "primary";
  setup.textContent = busy ? "Saving…" : "Set up";
  setup.disabled = busy || feature.configured;
  setup.addEventListener("click", () => decideOptionalFeature(feature.capability_id, "setup").catch(showError));
  actions.append(setup);
  if (wizard) {
    const later = document.createElement("button");
    later.type = "button";
    later.className = "ghost";
    later.textContent = "Not now";
    later.disabled = busy;
    later.addEventListener("click", () => decideOptionalFeature(feature.capability_id, "skip").catch(showError));
    actions.append(later);
  }
  const disable = document.createElement("button");
  disable.type = "button";
  disable.className = feature.effective_now ? "danger" : "ghost";
  disable.textContent = wizard ? "Keep disabled" : "Turn off";
  disable.disabled = busy || (
    !wizard
    && !feature.configured
    && feature.decision === "disable"
  );
  disable.addEventListener("click", () => decideOptionalFeature(feature.capability_id, "disable").catch(showError));
  actions.append(disable);
  card.append(actions);
  return card;
}

async function refreshFeatureOnboarding() {
  state.featureOnboarding = await api("/api/feature-onboarding");
  return state.featureOnboarding;
}

function renderFeatureOnboardingDialog() {
  const dialog = $("feature-onboarding-dialog");
  const list = $("feature-onboarding-list");
  const pending = (state.featureOnboarding?.features || []).filter(
    (feature) => feature.decision === "pending",
  );
  list.replaceChildren();
  for (const feature of pending) {
    list.append(featureOnboardingCard(feature, {wizard: true}));
  }
  if (!pending.length && dialog.open) dialog.close();
}

function maybeShowFeatureOnboarding() {
  if (state.onboardingDismissedForSession) return;
  if (!state.featureOnboarding?.available || !state.featureOnboarding.pending_count) return;
  if (
    $("approval-dialog").open
    || $("new-network-device-dialog").open
    || $("new-bluetooth-device-dialog").open
    || $("network-defense-incident-dialog").open
  ) return;
  renderFeatureOnboardingDialog();
  const dialog = $("feature-onboarding-dialog");
  if (!dialog.open) dialog.showModal();
}

async function renderCustomize(generation = null) {
  const render = beginUtilityRender("customize", generation);
  if (!render) return;
  const {content} = render;
  content.replaceChildren();
  const card = document.createElement("section");
  card.className = "utility-card";
  const modelSelect = $("model").cloneNode(true);
  modelSelect.id = "custom-model";
  modelSelect.value = $("model").value;
  modelSelect.addEventListener("change", () => { $("model").value = modelSelect.value; });
  const voice = document.createElement("input");
  voice.type = "checkbox";
  voice.checked = $("speak-toggle").checked;
  voice.addEventListener("change", () => { $("speak-toggle").checked = voice.checked; });
  const compact = document.createElement("input");
  compact.type = "checkbox";
  compact.checked = document.body.classList.contains("rail-collapsed");
  compact.addEventListener("change", () => setRailCollapsed(compact.checked));
  const clearPins = document.createElement("button");
  clearPins.type = "button";
  clearPins.className = "ghost";
  clearPins.textContent = "Clear pins";
  clearPins.addEventListener("click", () => {
    state.pinnedProjects.clear();
    savePinnedProjects();
    renderPinnedProjects();
    toast("Pinned projects cleared.");
  });
  card.append(
    settingRow("Task model", "Choose the model profile used by the next message.", modelSelect),
    settingRow("Voice responses", "Read completed Jarvis responses aloud.", voice),
    settingRow("Compact sidebar", "Collapse the navigation rail until you reopen it.", compact),
    settingRow("Pinned projects", "Remove every project shortcut from this browser.", clearPins),
  );
  content.append(card);

  const featureStatus = await refreshFeatureOnboarding();
  if (!isUtilityRenderCurrent(render)) return;
  const features = document.createElement("section");
  features.className = "utility-card";
  const featureHead = document.createElement("div");
  featureHead.className = "utility-card-head";
  const featureTitle = document.createElement("h3");
  featureTitle.textContent = "Optional capabilities";
  featureHead.append(featureTitle, makePill(
    featureStatus.pending_count ? `${featureStatus.pending_count} not reviewed` : "reviewed",
  ));
  const featureCopy = document.createElement("p");
  featureCopy.className = "feature-onboarding-summary";
  featureCopy.textContent = "Set up or turn off any implemented capability at any time. These switches never download security tools, pair a network, run a scan, or grant containment authority.";
  features.append(featureHead, featureCopy);
  if (featureStatus.error) {
    features.append(emptyUtility(featureStatus.error));
  } else {
    for (const feature of featureStatus.features || []) {
      features.append(featureOnboardingCard(feature));
    }
  }
  content.append(features);
}

function applyStatus(data) {
  const dot = $("status-dot");
  dot.className = `dot ${data.ready ? "online" : "error"}`;
  $("status-label").textContent = data.ready ? "Presence online" : "Presence degraded";
  const provider = data.provider || {};
  $("provider-label").textContent = providerLabel(provider);
  $("control-label").textContent = `Runtime ${data.control?.state || "unknown"} · uptime ${Math.floor(data.uptime_seconds / 60)}m`;
  $("agent-label").textContent = `${data.active_agent_count || 0}/${data.max_agents || 1} agents active · ${data.queued_jobs || 0} queued`;
  renderAgentTabs(data.specialists || [], data.models || {});
  state.models = data.models || {};
  state.jobs = Array.isArray(data.jobs) ? data.jobs : (data.active_jobs || []);
  state.screenCompanion = data.screen_companion || null;
  state.publicPresence = data.public_presence || null;
  renderCompanionQuick();
  replaceTrackedJobs(data);
  $("approval-count").textContent = String(data.pending_approvals || 0);
  if (state.activeView === "dispatch") renderDispatch();
  if (state.activeView === "public-presence") renderPublicPresence().catch(showError);
}

async function reconcileRuntimeState() {
  if (state.recoveryPromise) return state.recoveryPromise;
  state.recovering = true;
  syncBusy();
  state.recoveryPromise = (async () => {
    const selectedConversation = state.conversationId;
    const selectedSecondaryConversation = state.secondaryConversationId;
    state.activeJobs.clear();
    state.progressNodes.clear();
    state.streamNodes.clear();
    syncBusy();
    activity.textContent = "Presence restarted · reconnecting…";

    await refreshProjects();
    await refreshConversations();
    let loaded = false;
    if (selectedConversation) {
      try {
        await loadConversation(selectedConversation);
        loaded = true;
      } catch (_) {
        // The conversation may have been removed while Presence was offline.
      }
    }
    if (!loaded) await ensureConversation();
    if (
      state.splitEnabled
      && selectedSecondaryConversation
      && selectedSecondaryConversation !== state.conversationId
    ) {
      try { await loadSecondaryConversation(selectedSecondaryConversation); } catch (_) {}
    }

    const status = await api("/api/status");
    adoptRuntimeEpoch(status.runtime_epoch);
    applyStatus(status);
    activity.textContent = currentJobId() ? "Working…" : "Ready when you are.";
    toast("Presence reconnected after restart.");
  })();
  try {
    await state.recoveryPromise;
  } finally {
    state.recovering = false;
    state.recoveryPromise = null;
    syncBusy();
  }
}

async function refreshStatus() {
  try {
    const data = await api("/api/status");
    const restarted = adoptRuntimeEpoch(data.runtime_epoch);
    applyStatus(data);
    if (restarted) await reconcileRuntimeState();
  } catch (error) {
    $("status-dot").className = "dot error";
    $("status-label").textContent = "Disconnected";
  }
}

function isHistoricalConversationEvent(event) {
  const replayedKinds = new Set([
    "started", "activity", "assistant_delta", "assistant", "error", "fatal", "cancelled",
  ]);
  if (!replayedKinds.has(String(event?.kind || ""))) return false;
  const createdAt = Number(event?.created_at || 0);
  return Number.isFinite(createdAt) && createdAt < state.pageStartedAt - 2;
}

function isCurrentConversationJob(payload) {
  if (!payload?.conversation_id || !payload?.job_id) return false;
  return state.activeJobs.get(payload.conversation_id) === payload.job_id
    // The server removes a finished job just after enqueueing its terminal
    // event. A status refresh can therefore clear activeJobs before the next
    // event poll. Progress/stream nodes are client-held, job-correlated proof
    // that the terminal event belongs to the request already being rendered.
    || state.progressNodes.has(payload.job_id)
    || state.streamNodes.has(payload.job_id);
}

async function pollEvents() {
  if (state.polling) return;
  state.polling = true;
  try {
    const epoch = state.runtimeEpoch ? `&epoch=${encodeURIComponent(state.runtimeEpoch)}` : "";
    const data = await api(`/api/events?after=${state.lastEventId}${epoch}`);
    const restarted = adoptRuntimeEpoch(data.runtime_epoch);
    if (restarted || data.cursor_reset) {
      const returnedIds = (data.events || []).map((event) => Number(event.id) || 0);
      state.lastEventId = Math.max(0, Number(data.latest_event_id) || 0, ...returnedIds);
      await reconcileRuntimeState();
      return;
    }
    for (const event of data.events || []) {
      state.lastEventId = Math.max(state.lastEventId, event.id || 0);
      // Conversation history was loaded from SQLite during boot. Replaying old
      // runtime events after that would append the same assistant answer and
      // progress card a second time. Events created after this page opened are
      // still processed, including a job that finishes during startup.
      if (isHistoricalConversationEvent(event)) continue;
      const payload = event.payload || {};
      const eventTarget = payload.conversation_id
        ? messageTarget(payload.conversation_id)
        : null;
      const belongsHere = Boolean(eventTarget);
      if (event.kind === "started" && payload.conversation_id) {
        const activeJob = state.activeJobs.get(payload.conversation_id);
        if (activeJob && activeJob !== payload.job_id) continue;
        state.activeJobs.set(payload.conversation_id, payload.job_id);
        refreshConversations().catch(() => {});
        syncBusy();
      }
      const requiresCurrentJob = [
        "activity", "assistant_delta", "assistant", "error", "fatal", "cancelled",
      ].includes(event.kind) && payload.conversation_id;
      if (requiresCurrentJob && !isCurrentConversationJob(payload)) continue;
      if (["assistant", "error", "fatal", "cancelled"].includes(event.kind)
          && payload.conversation_id) {
        state.activeJobs.delete(payload.conversation_id);
        syncBusy();
        refreshConversations().catch(() => {});
      }
      if (event.kind === "activity" && belongsHere) {
        setConversationActivity(payload.conversation_id, payload.message || "Working");
      }
      if (event.kind === "started" && belongsHere) {
        setConversationActivity(payload.conversation_id, "Working…");
        showProgress(payload.job_id, "Starting request", eventTarget);
      }
      if (event.kind === "activity" && belongsHere) {
        showProgress(payload.job_id, payload.message || "Working", eventTarget);
      }
      if (event.kind === "assistant_delta" && belongsHere) {
        appendAssistantDelta(payload.job_id, payload.text || "", eventTarget);
      }
      if (event.kind === "assistant" && belongsHere) {
        finishProgress(payload.job_id, payload.status === "complete" ? "Work completed" : "Work paused");
        const answerArticle = finalizeAssistantStream(
          payload.job_id,
          payload.content,
          [payload.model, payload.status].filter(Boolean).join(" · "),
          eventTarget,
        );
        renderProductComparison(answerArticle, payload.product_comparison);
        if (eventTarget === messages) speak(payload.content);
        if (payload.approval_id) {
          toast(`Approval #${payload.approval_id} is waiting for review.`);
          await refreshApprovals();
          if (!$("approval-dialog").open) $("approval-dialog").showModal();
        }
        state.activeJobs.delete(payload.conversation_id);
        syncBusy();
        setConversationActivity(
          payload.conversation_id,
          payload.status === "complete" ? "Ready when you are." : (payload.reason || "Request incomplete"),
        );
        refreshConversations().catch(() => {});
        refreshStatus().catch(() => {});
      }
      if ((event.kind === "error" || event.kind === "fatal") && belongsHere) {
        finishProgress(payload.job_id, "Work failed");
        discardAssistantStream(payload.job_id);
        appendMessage(
          "assistant",
          payload.message || "Jarvis encountered an error.",
          "error",
          [],
          eventTarget,
        );
        if (payload.conversation_id) state.activeJobs.delete(payload.conversation_id);
        syncBusy();
        setConversationActivity(payload.conversation_id, "Needs attention");
      }
      if (event.kind === "cancelled" && belongsHere) {
        finishProgress(payload.job_id, "Work stopped");
        discardAssistantStream(payload.job_id);
        appendMessage(
          "assistant",
          payload.message || "Request stopped.",
          "cancelled",
          [],
          eventTarget,
        );
        if (payload.conversation_id) state.activeJobs.delete(payload.conversation_id);
        syncBusy();
        setConversationActivity(payload.conversation_id, "Ready when you are.");
      }
      if (event.kind === "approval_decided") refreshApprovals().catch(() => {});
      if (event.kind === "network_inventory_updated") {
        const eventIsCurrent = Number(event.created_at || 0) >= state.pageStartedAt - 2;
        if (
          eventIsCurrent
          && Array.isArray(payload.new_devices)
          && payload.new_devices.length
        ) {
          showNewNetworkDeviceAlerts({
            scan_id: payload.scan_id,
            observed_at: payload.observed_at,
            inventory: {
              scan_id: payload.scan_id,
              observed_at: payload.observed_at,
              devices: payload.new_devices,
              security_summary: {baseline_created: payload.baseline_created === true},
            },
            security_assessment: payload.security_assessment || {},
          });
        }
        if (eventIsCurrent) {
          showNetworkDefenseIncidents(
            payload.pending_incidents || payload.incident || null,
            {legacyPayload: payload},
          );
        }
        if (state.activeView === "devices" && !state.networkScanPending) {
          refreshNetworkInventory().catch(showError);
        }
      }
      if (event.kind === "network_defense_incident") {
        const eventIsCurrent = Number(event.created_at || 0) >= state.pageStartedAt - 2;
        if (eventIsCurrent) {
          showNetworkDefenseIncidents(payload.incident || payload);
        }
      }
      if (event.kind === "bluetooth_inventory_updated") {
        if (state.activeView === "devices" && !state.bluetoothCheckPending) {
          refreshBluetoothInventory().catch(showError);
        } else {
          api("/api/bluetooth-inventory")
            .then((status) => {
              state.bluetoothInventory = status;
              showNewBluetoothDeviceAlerts(status);
            })
            .catch(showError);
        }
      }
      if (event.kind === "approval_resume_failed" && belongsHere) {
        appendMessage(
          "assistant",
          payload.message || "Approval was recorded, but automatic resume could not start.",
          "needs attention",
          [],
          eventTarget,
        );
      }
    }
  } catch (_) {
    // Status polling owns visible disconnect state; event polling retries quietly.
  } finally {
    state.polling = false;
    window.setTimeout(pollEvents, currentJobId() ? 150 : 700);
  }
}

async function submitPrompt(event) {
  event.preventDefault();
  const text = prompt.value.trim();
  if ((!text && !state.pendingImages.length) || currentJobId()) return;
  const sentImages = state.pendingImages.splice(0);
  appendMessage("user", text, "", sentImages);
  renderImagePreview();
  prompt.value = "";
  resizePrompt();
  activity.textContent = "Queuing request…";
  try {
    const result = await post("/api/chat", {
      conversation_id: state.conversationId,
      prompt: text,
      model: $("model").value,
      images: sentImages.map(({name, mime, data}) => ({name, mime, data})),
    });
    state.activeJobs.set(state.conversationId, result.job_id);
    syncBusy();
    refreshConversations().catch(() => {});
  } catch (error) {
    appendMessage("assistant", error.message, "request error");
    activity.textContent = "Ready when you are.";
  }
}

async function submitSecondaryPrompt(event) {
  event.preventDefault();
  const text = secondaryPrompt.value.trim();
  if (!text || !state.splitEnabled || !state.secondaryConversationId || secondaryJobId()) return;
  appendMessage("user", text, "", [], secondaryMessages);
  secondaryPrompt.value = "";
  resizeSecondaryPrompt();
  $("secondary-activity").textContent = "Queuing request…";
  try {
    const result = await post("/api/chat", {
      conversation_id: state.secondaryConversationId,
      prompt: text,
      model: $("model").value,
    });
    state.activeJobs.set(state.secondaryConversationId, result.job_id);
    syncBusy();
    refreshConversations().catch(() => {});
  } catch (error) {
    appendMessage("assistant", error.message, "request error", [], secondaryMessages);
    $("secondary-activity").textContent = "Ready when you are.";
  }
}

async function cancelActive() {
  const jobId = currentJobId();
  if (!jobId) return;
  await post("/api/cancel", {job_id: jobId});
  activity.textContent = "Stopping safely…";
}

async function cancelSecondaryActive() {
  const jobId = secondaryJobId();
  if (!jobId) return;
  await post("/api/cancel", {job_id: jobId});
  $("secondary-activity").textContent = "Stopping safely…";
}

async function refreshApprovals() {
  const result = await api("/api/approvals");
  const list = $("approval-list");
  list.replaceChildren();
  const pending = result.approvals.filter((row) => row.status === "pending");
  const persistent = result.persistent_approvals || [];
  $("approval-count").textContent = String(pending.length);
  if (!pending.length && !persistent.length) {
    const empty = document.createElement("div");
    empty.className = "muted";
    empty.textContent = "No sensitive actions are waiting for approval.";
    list.append(empty);
    return 0;
  }
  for (const row of pending) {
    const card = document.createElement("section");
    card.className = "approval-card";
    const title = document.createElement("strong");
    title.textContent = `#${row.id} · ${row.action}`;
    const reason = document.createElement("p");
    reason.className = "muted";
    reason.textContent = row.reason;
    const resource = document.createElement("pre");
    resource.textContent = row.resource;
    const actions = document.createElement("div");
    actions.className = "approval-actions";
    const deny = document.createElement("button");
    deny.className = "danger";
    deny.textContent = "Deny";
    const approve = document.createElement("button");
    approve.className = "primary";
    approve.textContent = "Approve once";
    deny.addEventListener("click", () => decideApproval(row.id, false));
    approve.addEventListener("click", () => decideApproval(row.id, true));
    actions.append(deny);
    if (row.persistent_eligible) {
      const approveSession = document.createElement("button");
      approveSession.textContent = "Approve for this session";
      approveSession.title = "Allow only this exact read-only target in this chat until it expires";
      approveSession.addEventListener("click", () => decideApprovalForSession(row.id));
      const approveAlways = document.createElement("button");
      approveAlways.textContent = "Approve always";
      approveAlways.title = "Always allow only this exact read-only target and arguments";
      approveAlways.addEventListener("click", () => decideApprovalAlways(row.id));
      actions.append(approveSession, approveAlways);
    }
    actions.append(approve);
    card.append(title, reason, resource, actions);
    list.append(card);
  }
  if (persistent.length) {
    const heading = document.createElement("strong");
    heading.textContent = "Active exact read-only grants";
    list.append(heading);
  }
  for (const row of persistent) {
    const card = document.createElement("section");
    card.className = "approval-card";
    const title = document.createElement("strong");
    const grantLabel = row.grant_kind === "session" ? "This session" : "Always";
    title.textContent = `${grantLabel} · grant #${row.id} · ${row.action}`;
    const detail = document.createElement("p");
    detail.className = "muted";
    detail.textContent = row.grant_kind === "session"
      ? `Current chat only · expires ${row.expires_at}`
      : "Remains active until revoked";
    const resource = document.createElement("pre");
    resource.textContent = row.resource;
    const actions = document.createElement("div");
    actions.className = "approval-actions";
    const revoke = document.createElement("button");
    revoke.className = "danger";
    revoke.textContent = "Revoke";
    revoke.addEventListener("click", () => revokeApprovalGrant(row.id));
    actions.append(revoke);
    card.append(title, detail, resource, actions);
    list.append(card);
  }
  return pending.length;
}

async function decideApproval(id, approve) {
  await post(`/api/approvals/${id}/${approve ? "approve" : "deny"}`, {});
  toast(`Approval #${id} ${approve ? "approved once" : "denied"}.`);
  const pending = await refreshApprovals();
  if (!pending && $("approval-dialog").open) $("approval-dialog").close();
  await refreshStatus();
}

async function decideApprovalAlways(id) {
  const result = await post(`/api/approvals/${id}/approve-always`, {});
  toast(`Approval #${id} now always allows only this exact read-only target (grant #${result.grant_id}).`);
  const pending = await refreshApprovals();
  if (!pending && $("approval-dialog").open) $("approval-dialog").close();
  await refreshStatus();
}

async function decideApprovalForSession(id) {
  const result = await post(`/api/approvals/${id}/approve-session`, {});
  toast(`Approval #${id} now allows only this exact read-only target in this chat (grant #${result.grant_id}).`);
  const pending = await refreshApprovals();
  if (!pending && $("approval-dialog").open) $("approval-dialog").close();
  await refreshStatus();
}

async function revokeApprovalGrant(id) {
  await post(`/api/approval-grants/${id}/revoke`, {});
  toast(`Always-allow grant #${id} revoked.`);
  await refreshApprovals();
  await refreshStatus();
}

function resizePrompt() {
  prompt.style.height = "auto";
  prompt.style.height = `${Math.min(prompt.scrollHeight, 180)}px`;
}

function resizeSecondaryPrompt() {
  secondaryPrompt.style.height = "auto";
  secondaryPrompt.style.height = `${Math.min(secondaryPrompt.scrollHeight, 180)}px`;
}

function setupVoice() {
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!Recognition) {
    $("microphone").disabled = true;
    $("microphone").title = "Speech recognition is unavailable in this browser";
    return;
  }
  const recognition = new Recognition();
  recognition.lang = navigator.language || "en-US";
  recognition.interimResults = true;
  recognition.continuous = false;
  recognition.onstart = () => { $("microphone").classList.add("listening"); activity.textContent = "Listening…"; };
  recognition.onend = () => { $("microphone").classList.remove("listening"); activity.textContent = "Ready when you are."; };
  recognition.onerror = (event) => toast(`Voice input: ${event.error || "unavailable"}`);
  recognition.onresult = (event) => {
    let transcript = "";
    for (let i = event.resultIndex; i < event.results.length; i += 1) transcript += event.results[i][0].transcript;
    prompt.value = transcript.trim();
    resizePrompt();
  };
  state.recognition = recognition;
  $("microphone").addEventListener("click", () => {
    try { recognition.start(); } catch (_) { recognition.stop(); }
  });
}

function showError(error) { toast(error?.message || String(error)); }

function showPairing() {
  sessionStorage.removeItem(sessionKey);
  const dialog = $("pairing-dialog");
  if (!dialog.open) dialog.showModal();
  $("pairing-code").focus();
}

async function pairDevice(event) {
  event.preventDefault();
  const code = $("pairing-code").value.trim();
  const result = await post("/api/pair", {code});
  if (!result.token) throw new Error("Pairing did not return a session token");
  sessionStorage.setItem(sessionKey, result.token);
  $("pairing-code").value = "";
  $("pairing-dialog").close();
  toast("Device paired. This browser session is now authenticated.");
  await boot();
}

function setRailCollapsed(collapsed) {
  document.body.classList.toggle("rail-collapsed", Boolean(collapsed));
  localStorage.setItem("jarvis.presence.rail-collapsed", collapsed ? "1" : "0");
}

function toggleRail() {
  setRailCollapsed(!document.body.classList.contains("rail-collapsed"));
}

$("composer").addEventListener("submit", submitPrompt);
$("secondary-composer").addEventListener("submit", submitSecondaryPrompt);
attachImage.addEventListener("click", () => imageInput.click());
imageInput.addEventListener("change", async () => {
  try {
    await addImageFiles(imageInput.files);
  } catch (error) {
    showError(error);
  } finally {
    imageInput.value = "";
  }
});
$("new-chat").addEventListener("click", () => newConversation().catch(showError));
$("split-view").addEventListener("click", () => {
  setSplitView(!state.splitEnabled).catch(showError);
});
$("close-split-view").addEventListener("click", () => setSplitView(false).catch(showError));
$("secondary-new-chat").addEventListener("click", () => newSecondaryConversation().catch(showError));
$("secondary-stop").addEventListener("click", () => cancelSecondaryActive().catch(showError));
$("secondary-conversation").addEventListener("change", () => {
  loadSecondaryConversation(Number($("secondary-conversation").value)).catch(showError);
});
$("new-project").addEventListener("click", () => createProject().catch(showError));
$("project-form").addEventListener("submit", (event) => submitProject(event).catch(showError));
$("cancel-project").addEventListener("click", () => $("project-dialog").close());
$("cancel-delete-chat").addEventListener("click", () => {
  state.pendingDeleteConversationId = null;
  $("delete-chat-dialog").close();
});
$("confirm-delete-chat").addEventListener("click", () => {
  confirmConversationDelete().catch(showError);
});
$("delete-chat-dialog").addEventListener("cancel", () => {
  state.pendingDeleteConversationId = null;
});
$("close-new-network-device").addEventListener(
  "click", () => $("new-network-device-dialog").close(),
);
$("new-network-device-dialog").addEventListener(
  "close", () => {
    $("new-network-device-list").replaceChildren();
    schedulePriorityDialogs();
  },
);
$("close-new-bluetooth-device").addEventListener(
  "click", () => $("new-bluetooth-device-dialog").close(),
);
$("new-bluetooth-device-dialog").addEventListener(
  "close", () => {
    acknowledgeVisibleBluetoothAlerts();
    $("new-bluetooth-device-list").replaceChildren();
    schedulePriorityDialogs();
  },
);
$("close-network-defense-incident").addEventListener(
  "click", () => $("network-defense-incident-dialog").close(),
);
$("close-feature-onboarding").addEventListener("click", () => {
  state.onboardingDismissedForSession = true;
  $("feature-onboarding-dialog").close();
});
$("feature-onboarding-dialog").addEventListener("cancel", () => {
  state.onboardingDismissedForSession = true;
});
$("feature-onboarding-dialog").addEventListener("close", schedulePriorityDialogs);
$("home-mode").addEventListener("click", () => setWorkspaceMode("home"));
$("code-mode").addEventListener("click", () => setWorkspaceMode("code"));
$("close-utility").addEventListener("click", showChat);
$("project-context").addEventListener("click", () => openUtility("projects").catch(showError));
$("pin-project").addEventListener("click", toggleCurrentProjectPin);
document.querySelectorAll("[data-view]").forEach((button) => {
  button.addEventListener("click", () => openUtility(button.dataset.view).catch(showError));
});
$("rail-toggle").addEventListener("click", toggleRail);
$("mobile-rail-toggle").addEventListener("click", toggleRail);
$("project").addEventListener("change", () => {
  const projectId = Number($("project").value) || 1;
  // A project selector must change the conversation's execution workspace, not
  // merely repaint the selector. Starting a project-bound conversation also
  // makes the choice survive a Presence restart through the saved conversation.
  openProjectInChat(projectId).catch(showError);
});
$("stop").addEventListener("click", () => cancelActive().catch(showError));
$("approvals").addEventListener("click", async () => {
  try { await refreshApprovals(); $("approval-dialog").showModal(); } catch (error) { showError(error); }
});
$("close-approvals").addEventListener("click", () => $("approval-dialog").close());
$("approval-dialog").addEventListener("close", schedulePriorityDialogs);
$("companion-chip").addEventListener("click", () => {
  setCompanionPopover($("companion-popover").hidden);
});
$("companion-settings").addEventListener("click", () => {
  setCompanionPopover(false);
  openUtility("companion").catch(showError);
});
$("companion-on").addEventListener("click", () => controlCompanionQuick("on").catch(showError));
$("companion-pause").addEventListener("click", () => {
  const action = state.screenCompanion?.paused ? "resume" : "pause";
  controlCompanionQuick(action).catch(showError);
});
$("companion-off").addEventListener("click", () => controlCompanionQuick("off").catch(showError));
$("companion-quick-mode").addEventListener("change", () => {
  controlCompanionQuick("mode", $("companion-quick-mode").value).catch(showError);
});
document.addEventListener("click", (event) => {
  if (!$("companion-popover").hidden && !event.target.closest(".companion-quick")) {
    setCompanionPopover(false);
  }
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !$("companion-popover").hidden) {
    event.preventDefault();
    setCompanionPopover(false, {restoreFocus: true});
  }
});
$("pairing-form").addEventListener("submit", (event) => pairDevice(event).catch(showError));
prompt.addEventListener("input", resizePrompt);
prompt.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    $("composer").requestSubmit();
  }
});
secondaryPrompt.addEventListener("input", resizeSecondaryPrompt);
secondaryPrompt.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    $("secondary-composer").requestSubmit();
  }
});

async function boot() {
  const savedRailState = localStorage.getItem("jarvis.presence.rail-collapsed");
  setRailCollapsed(savedRailState === "1" || (savedRailState === null && window.matchMedia("(max-width: 760px)").matches));
  loadPinnedProjects();
  if (!state.recognition) setupVoice();
  await refreshProjects();
  await ensureConversation();
  await refreshStatus();
  await refreshFeatureOnboarding().catch(() => {});
  await refreshBluetoothInventory().catch(() => {});
  await refreshConversations();
  if (localStorage.getItem("jarvis.presence.split-view") === "1") {
    await setSplitView(true);
  }
  const pendingApprovals = await refreshApprovals();
  if (pendingApprovals && !$("approval-dialog").open) $("approval-dialog").showModal();
  schedulePriorityDialogs();
  setInterval(refreshStatus, 5000);
  pollEvents();
  prompt.focus();
}

boot().catch((error) => {
  if (error?.status === 401) return;
  $("status-dot").className = "dot error";
  $("status-label").textContent = "Startup failed";
  appendMessage("assistant", error.message || String(error), "Presence startup error");
});
