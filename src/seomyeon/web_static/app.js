/*
 * Same-origin API contract used by this client:
 * GET  /api/session
 * POST /api/login {email,password}
 * POST /api/signup {display_name,email,password}
 * POST /api/logout
 * GET  /api/workspace
 * POST /api/briefs {title,text,pseudonymize?}
 * POST /api/briefs/upload  raw file body, X-Filename header
 * POST /api/profile/build
 * POST /api/profile/approve {trait_ids}
 * POST /api/matters {title}
 * POST /api/matters/:id/assertions {assertion_id,kind,text,source_label}
 * POST /api/matters/:id/authorities {authority_id,citation,proposition,source_url}
 * GET  /api/legal/precedents?query=...
 * GET  /api/legal/statutes?query=...
 * POST /api/matters/:id/authorities/import-precedent {authority_id,precedent_id}
 * PUT  /api/matters/:id/perspective {thesis,priorities,risks}
 * PUT  /api/matters/:id/case-structure
 * POST /api/matters/:id/argument-plan
 * POST /api/matters/:id/argument-plan/approve
 * POST /api/matters/:id/approve {assertion_ids,authority_ids,approve_perspective}
 * POST /api/matters/:id/draft {title}
 *
 * Every mutation except login receives X-CSRF-Token from GET /api/session.
 */

const state = {
  session: null,
  csrfToken: "",
  workspace: { briefs: [], profile: null, matters: [], drafts: [] },
  activeMatterId: null,
  displayedMatterId: null,
  activeStep: 1,
  authMode: "login",
  precedentResults: [],
  statuteResults: [],
};

const el = (id) => document.getElementById(id);
const escapeHtml = (value = "") => String(value).replace(/[&<>'"]/g, (char) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
}[char]));

async function api(path, options = {}) {
  const method = options.method || "GET";
  const headers = { Accept: "application/json", ...(options.headers || {}) };
  if (options.body && !(options.body instanceof FormData) && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
  if (method !== "GET" && !["/api/login", "/api/signup"].includes(path) && state.csrfToken) headers["X-CSRF-Token"] = state.csrfToken;

  const response = await fetch(path, { ...options, method, headers, credentials: "same-origin" });
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json") ? await response.json() : await response.text();

  if (response.status === 401) {
    showAuth();
    throw new Error("로그인이 만료되었습니다. 다시 로그인하세요.");
  }
  if (!response.ok) {
    const message = payload?.detail || payload?.message || payload?.error || (typeof payload === "string" && payload) || `요청을 완료하지 못했습니다 (${response.status}).`;
    throw new Error(message);
  }
  return payload;
}

function setBusy(isBusy, label = "처리 중") {
  el("busy-label").textContent = label;
  el("busy-overlay").hidden = !isBusy;
}

function toast(message, type = "success") {
  const node = document.createElement("div");
  node.className = `toast${type === "error" ? " is-error" : ""}`;
  node.textContent = message;
  el("toast-region").append(node);
  window.setTimeout(() => node.remove(), 3600);
}

function clearToasts() {
  el("toast-region").replaceChildren();
}

function runAction(action, busyLabel) {
  return async (...args) => {
    setBusy(true, busyLabel);
    try { return await action(...args); }
    catch (error) { toast(error.message || "요청을 완료하지 못했습니다.", "error"); return null; }
    finally { setBusy(false); }
  };
}

function normalizeWorkspace(payload = {}) {
  const source = payload.workspace || payload;
  const matters = Array.isArray(source.matters) ? source.matters : [];
  return {
    briefs: Array.isArray(source.briefs) ? source.briefs : [],
    profile: source.profile || source.latest_profile || null,
    matters,
    drafts: Array.isArray(source.drafts) ? source.drafts : [],
  };
}

function showAuth() {
  clearToasts();
  el("workspace-view").hidden = true;
  el("auth-view").hidden = false;
  state.session = null;
  setAuthMode("login");
  window.setTimeout(() => el("login-email").focus(), 0);
}

function showWorkspace() {
  clearToasts();
  el("auth-view").hidden = true;
  el("workspace-view").hidden = false;
  const user = state.session || {};
  el("user-email").textContent = user.display_name
    ? `${user.display_name} · ${user.email}`
    : (user.email || "");
  renderWorkspace();
}

function setAuthMode(mode) {
  state.authMode = mode;
  const signingUp = mode === "signup";
  el("signup-name-field").hidden = !signingUp;
  el("signup-name").required = signingUp;
  el("login-password").minLength = signingUp ? 8 : 0;
  el("login-title").textContent = signingUp ? "개인 작업 공간을 만드세요." : "바로 작업을 시작하세요.";
  el("auth-supporting-copy").textContent = signingUp
    ? "가입 즉시 나만의 서면 프로필과 사건 공간이 만들어집니다."
    : "내 서면과 사건 자료는 내 계정에만 저장됩니다.";
  el("auth-submit-button").textContent = signingUp ? "계정 만들기" : "로그인";
  ["login", "signup"].forEach((value) => {
    const button = el(`${value}-mode-button`);
    const active = value === mode;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-selected", String(active));
  });
  el("login-error").hidden = true;
}

async function refreshWorkspace() {
  state.workspace = normalizeWorkspace(await api("/api/workspace"));
  if (!state.activeMatterId || !state.workspace.matters.some((item) => matterId(item) === state.activeMatterId)) {
    state.activeMatterId = state.workspace.matters[0] ? matterId(state.workspace.matters[0]) : null;
  }
  renderWorkspace();
}

function matterId(matter) { return matter?.matter_id || matter?.id; }
function traitId(trait) { return trait?.trait_id || trait?.id; }
function activeMatter() { return state.workspace.matters.find((item) => matterId(item) === state.activeMatterId) || null; }

function renderWorkspace() {
  renderBriefs();
  renderProfile();
  renderMatters();
  renderMatterDetails();
  renderPrecedentResults();
  renderStatuteResults();
  renderApprovedSources();
  renderLatestDraft();
}

function renderBriefs() {
  const briefs = state.workspace.briefs;
  el("brief-count").textContent = `${briefs.length}건`;
  el("build-profile-button").disabled = briefs.length === 0;
  el("brief-list").innerHTML = briefs.length ? briefs.map((brief, index) => `
    <div class="document-row">
      <div class="document-meta">
        <strong>${escapeHtml(brief.title || `서면 ${index + 1}`)}</strong>
        <span>${escapeHtml(brief.created_at ? formatDate(brief.created_at) : "분석 대기")}</span>
      </div>
      <span class="source-id">${escapeHtml(brief.brief_id || brief.id || "")}</span>
    </div>`).join("") : '<div class="empty-state">아직 등록된 서면이 없습니다.</div>';
}

function renderProfile() {
  const profile = state.workspace.profile;
  const traits = profile?.traits || [];
  el("profile-section").hidden = !profile;
  if (!profile) return;
  el("trait-list").innerHTML = traits.length ? traits.map((trait) => {
    const evidence = trait.evidence?.[0];
    return `<label class="trait-card">
      <input type="checkbox" name="trait" value="${escapeHtml(traitId(trait))}" ${trait.approved ? "checked disabled" : ""} />
      <span class="trait-copy">
        <strong>${escapeHtml(trait.statement)}</strong>
        <p>${escapeHtml(categoryLabel(trait.category))} · 신뢰도 ${Math.round((trait.confidence || 0) * 100)}%</p>
        ${evidence ? `<p class="trait-evidence">“${escapeHtml(evidence.excerpt)}” — ${escapeHtml(evidence.label || evidence.locator || "기존 서면")}</p>` : ""}
      </span>
    </label>`;
  }).join("") : '<div class="empty-state">분석된 작성 원칙이 없습니다.</div>';
  el("approve-profile-button").disabled = traits.length === 0 || traits.every((trait) => trait.approved);
}

function renderMatters() {
  const matters = state.workspace.matters;
  const wrap = el("matter-picker-wrap");
  wrap.hidden = matters.length === 0;
  el("matter-workspace").hidden = matters.length === 0;
  if (!matters.length) return;
  el("matter-picker").innerHTML = matters.map((matter) => `<option value="${escapeHtml(matterId(matter))}" ${matterId(matter) === state.activeMatterId ? "selected" : ""}>${escapeHtml(matter.title)}</option>`).join("");
}

function renderMatterDetails() {
  const matter = activeMatter();
  if (!matter) return;
  const assertions = matter.assertions || [];
  const authorities = matter.authorities || [];
  el("assertion-list").innerHTML = assertions.length ? assertions.map((item) => `
    <div class="source-row">
      <input class="source-approval" type="checkbox" value="${escapeHtml(item.assertion_id)}" aria-label="${escapeHtml(item.assertion_id)} 승인" data-approve-assertion ${item.confirmed ? "checked disabled" : ""} />
      <span class="source-id">${escapeHtml(item.assertion_id)}</span>
      <div><strong>${escapeHtml(item.text)}</strong><span>${escapeHtml(item.source_label)}</span></div>
    </div>`).join("") : '<div class="empty-state">등록된 사건 기록이 없습니다.</div>';
  el("authority-list").innerHTML = authorities.length ? authorities.map((item) => `
    <div class="source-row">
      <input class="source-approval" type="checkbox" value="${escapeHtml(item.authority_id)}" aria-label="${escapeHtml(item.authority_id)} 승인" data-approve-authority ${item.verified ? "checked disabled" : ""} />
      <span class="source-id">${escapeHtml(item.authority_id)}</span>
      <div>
        <strong>${escapeHtml(item.citation)}</strong>
        <details class="authority-detail">
          <summary>${String(item.official_id || "").startsWith("law:") ? "조문 원문 보기" : "판결요지 보기"}</summary>
          <p>${escapeHtml(item.proposition)}</p>
        </details>
        <a class="source-link" href="${escapeHtml(item.source_url)}" target="_blank" rel="noreferrer">${item.official_source ? `${escapeHtml(item.official_source)} 원문 · ${escapeHtml(item.official_id)}` : "입력한 원문 열기"}</a>
      </div>
    </div>`).join("") : '<div class="empty-state">등록된 법률 근거가 없습니다.</div>';

  const perspective = matter.perspective || {};
  el("perspective-thesis").value = perspective.thesis || "";
  el("perspective-priorities").value = (perspective.priorities || []).join("\n");
  el("perspective-risks").value = (perspective.risks || []).join("\n");
  el("approve-matter-button").disabled = !assertions.length || !perspective.thesis;
  renderCaseStructure(matter);
}

function responseLabel(value) {
  return ({ admit: "인정", deny: "부인", lack_knowledge: "부지", partial: "일부 인정·일부 부인" })[value] || value;
}

function renderCaseStructure(matter) {
  const metadata = matter.case_metadata || {};
  el("case-court").value = metadata.court || "";
  el("case-number").value = metadata.case_number || "";
  el("document-type").value = metadata.document_type || "준비서면";
  el("filing-party").value = metadata.filing_party || "";
  el("opposing-party").value = metadata.opposing_party || "";
  el("relief-sought").value = metadata.relief_sought || "";
  const claims = matter.claims || [];
  el("claim-structure-list").innerHTML = claims.length ? claims.map((claim) => `
    <article class="structure-card">
      <div><strong>${escapeHtml(claim.claim_id)} · ${escapeHtml(claim.label)}</strong><span>${claim.position === "defense" ? "항변" : "청구"} · ${escapeHtml(claim.requested_relief)}</span><button class="text-button danger-link" type="button" data-remove-claim="${escapeHtml(claim.claim_id)}">전체 삭제</button></div>
      ${(claim.elements || []).map((item) => `<p><b>${escapeHtml(item.element_id)} · ${escapeHtml(item.name)}</b><span>입증: ${escapeHtml(item.burden_party)} · 사실 ${escapeHtml((item.assertion_ids || []).join(", "))} · 법률 ${escapeHtml((item.authority_ids || []).join(", "))}</span><button class="text-button danger-link" type="button" data-remove-element="${escapeHtml(claim.claim_id)}:${escapeHtml(item.element_id)}">삭제</button></p>`).join("")}
    </article>`).join("") : '<div class="empty-state compact-empty">등록된 청구·항변이 없습니다.</div>';
  const positions = matter.opposing_positions || [];
  el("position-structure-list").innerHTML = positions.length ? positions.map((item) => `
    <article class="structure-card"><div><strong>${escapeHtml(item.position_id)} · ${escapeHtml(responseLabel(item.response))}</strong><span>${escapeHtml(item.allegation)}</span><button class="text-button danger-link" type="button" data-remove-position="${escapeHtml(item.position_id)}">삭제</button></div><p>${escapeHtml(item.response_text || "구체적 답변 없음")}</p></article>`).join("") : '<div class="empty-state compact-empty">등록된 상대방 주장이 없습니다.</div>';
  const plan = matter.argument_plan;
  el("approve-argument-plan-button").disabled = !plan || Boolean(plan.approved) || (plan.unresolved || []).length > 0;
  el("argument-plan-result").innerHTML = !plan
    ? '<div class="empty-state compact-empty">자료 승인 후 논증계획을 생성하세요.</div>'
    : (plan.unresolved || []).length
      ? `<div class="plan-status is-blocked"><strong>보완 필요 ${(plan.unresolved || []).length}건</strong>${plan.unresolved.map((item) => `<p>${escapeHtml(item)}</p>`).join("")}</div>`
      : `<div class="plan-status is-ready"><strong>${plan.approved ? "승인된 논증계획" : "승인 가능"}</strong><p>요건·입증책임·포섭·반박·결론 ${(plan.units || []).length}개 단위가 연결되었습니다.</p></div>`;
}

function nextAuthorityId() {
  const used = new Set((activeMatter()?.authorities || []).map((item) => item.authority_id));
  let index = used.size + 1;
  while (used.has(`A${index}`)) index += 1;
  return `A${index}`;
}

function renderPrecedentResults() {
  const results = state.precedentResults;
  el("precedent-results").innerHTML = results.length ? results.map((item) => `
    <article class="precedent-result">
      <div>
        <p>${escapeHtml([item.court_name, item.decision_date, item.case_number].filter(Boolean).join(" · "))}</p>
        <strong>${escapeHtml(item.case_name)}</strong>
        <span>${escapeHtml(item.source_name)} · 원문 ID ${escapeHtml(item.precedent_id)}</span>
      </div>
      <button class="text-button" type="button" data-import-precedent="${escapeHtml(item.precedent_id)}">근거로 추가</button>
    </article>`).join("") : '<div class="empty-state compact-empty">검색 결과가 없습니다.</div>';
}

function renderSuggestions(result) {
  const issues = (result.issues || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("");
  const statutes = (result.statutes || []).map((item) => `
    <article class="precedent-result">
      <div>
        <p>조문 · ${escapeHtml(item.via)}</p>
        <strong>${escapeHtml(item.law_name)} 제${escapeHtml(item.article_number)}조${item.title ? ` (${escapeHtml(item.title)})` : ""}</strong>
        <span>${escapeHtml(item.reason)}</span>
        <details><summary>조문 원문 보기</summary><p>${escapeHtml(item.text)}</p></details>
      </div>
      <button class="text-button" type="button" data-suggest-statute="${escapeHtml(item.mst)}" data-article="${escapeHtml(item.article_number)}">근거로 추가</button>
    </article>`).join("");
  const precedents = (result.precedents || []).map((item) => `
    <article class="precedent-result">
      <div>
        <p>판례 · 관련도 ${escapeHtml(String(item.score))}/3 · ${escapeHtml(item.via)}</p>
        <strong>${escapeHtml(item.citation)}</strong>
        <span>${escapeHtml(item.reason)}</span>
        <details><summary>판시사항·판결요지 보기</summary><p>${escapeHtml(item.issues)}</p><p>${escapeHtml(item.holding)}</p></details>
      </div>
      <button class="text-button" type="button" data-suggest-precedent="${escapeHtml(item.precedent_id)}">근거로 추가</button>
    </article>`).join("");
  const notes = (result.notes || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("");
  el("suggest-results").innerHTML = `
    ${issues ? `<p class="precedent-help">찾은 쟁점</p><ul>${issues}</ul>` : ""}
    ${statutes || precedents || '<div class="empty-state compact-empty">후보를 찾지 못했습니다.</div>'}
    ${notes ? `<details><summary>검색 과정 메모 ${(result.notes || []).length}건</summary><ul>${notes}</ul></details>` : ""}`;
}

function renderStatuteResults() {
  const results = state.statuteResults;
  el("statute-results").innerHTML = results.length ? results.map((item) => `
    <article class="precedent-result statute-result">
      <div><p>${escapeHtml([item.law_type, item.ministry, item.enforcement_date].filter(Boolean).join(" · "))}</p><strong>${escapeHtml(item.name)}</strong><span>공식 법령 ID ${escapeHtml(item.mst)}</span></div>
      <div class="statute-import"><label class="sr-only" for="article-${escapeHtml(item.mst)}">조문 번호</label><input id="article-${escapeHtml(item.mst)}" data-statute-article="${escapeHtml(item.mst)}" placeholder="예: 598" /><button class="text-button" type="button" data-import-statute="${escapeHtml(item.mst)}">조문 추가</button></div>
    </article>`).join("") : '<div class="empty-state compact-empty">검색 결과가 없습니다.</div>';
}

function sourceIds(value) {
  return String(value || "").split(",").map((item) => item.trim()).filter(Boolean);
}

function caseStructurePayload(overrides = {}) {
  const matter = activeMatter();
  if (!matter) throw new Error("먼저 사건을 만드세요.");
  const payload = {
    case_metadata: matter.case_metadata,
    claims: matter.claims || [],
    opposing_positions: matter.opposing_positions || [],
    ...overrides,
  };
  if (!payload.case_metadata) throw new Error("먼저 제출 정보를 저장하세요.");
  return payload;
}

async function saveCaseStructure(payload) {
  await api(`/api/matters/${encodeURIComponent(state.activeMatterId)}/case-structure`, { method: "PUT", body: JSON.stringify(payload) });
  await refreshWorkspace();
}

function renderApprovedSources() {
  const matter = activeMatter();
  const assertions = (matter?.assertions || []).filter((item) => item.confirmed);
  const authorities = (matter?.authorities || []).filter((item) => item.verified);
  const ids = [...assertions.map((item) => item.assertion_id), ...authorities.map((item) => item.authority_id)];
  el("approved-source-list").innerHTML = ids.length ? ids.map((id) => `<span class="source-chip">${escapeHtml(id)}</span>`).join("") : '<span class="empty-state">아직 승인된 근거가 없습니다.</span>';
}

function renderLatestDraft() {
  if (state.displayedMatterId === state.activeMatterId) return;
  state.displayedMatterId = state.activeMatterId;
  const drafts = state.workspace.drafts
    .filter((item) => item.matter_id === state.activeMatterId)
    .sort((left, right) => String(right.created_at).localeCompare(String(left.created_at)));
  if (drafts[0]) {
    renderDraft(drafts[0]);
    return;
  }
  el("draft-content").value = "";
  el("draft-content").readOnly = true;
  el("copy-draft-button").disabled = true;
  el("save-status").textContent = "아직 생성되지 않음";
}

function categoryLabel(category) {
  return ({ reasoning: "논증", structure: "구조", evidence: "근거 사용", style: "표현", guardrail: "금지 원칙" })[category] || category;
}

function formatDate(value) {
  try { return new Intl.DateTimeFormat("ko-KR", { dateStyle: "medium" }).format(new Date(value)); }
  catch { return value; }
}

function goStep(step) {
  state.activeStep = Number(step);
  document.querySelectorAll("[data-step-panel]").forEach((panel) => { panel.hidden = Number(panel.dataset.stepPanel) !== state.activeStep; });
  document.querySelectorAll(".step-button").forEach((button) => {
    const active = Number(button.dataset.step) === state.activeStep;
    button.classList.toggle("is-active", active);
    if (active) button.setAttribute("aria-current", "step"); else button.removeAttribute("aria-current");
  });
  window.history.replaceState(null, "", `#step-${state.activeStep}`);
  el("main-content").focus({ preventScroll: true });
  window.scrollTo({ top: 0, behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
}

async function uploadTextBrief(title, text) {
  await api("/api/briefs", { method: "POST", body: JSON.stringify({ title, text }) });
}

async function uploadFiles(files) {
  const supported = [...files].filter((file) => /\.(hwp|hwpx|txt|md)$/i.test(file.name));
  if (!supported.length) throw new Error("HWP, HWPX, TXT, MD 파일만 읽을 수 있습니다.");
  let names = 0;
  for (const file of supported) {
    const result = await api("/api/briefs/upload", {
      method: "POST",
      body: await file.arrayBuffer(),
      headers: { "Content-Type": "application/octet-stream", "X-Filename": encodeURIComponent(file.name) },
    }).catch((error) => { throw new Error(`${file.name}: ${error.message}`); });
    names += result.redaction?.names || 0;
  }
  await refreshWorkspace();
  toast(`${supported.length}건의 서면을 추가했습니다. 실명 ${names}개를 가명으로 바꿨습니다.`);
}

function lines(value) { return value.split("\n").map((item) => item.trim()).filter(Boolean); }

function renderDraft(result) {
  const content = result.content || result.draft?.content || "";
  const findings = result.findings || result.draft?.findings || [];
  const passed = Boolean(result.deterministic_passed ?? !findings.some((item) => item.severity === "high"));
  el("draft-content").value = content;
  state.displayedMatterId = result.matter_id || state.activeMatterId;
  el("draft-content").readOnly = false;
  el("copy-draft-button").disabled = !content;
  const generation = result.generation || result.draft?.generation || {};
  const model = generation.model && generation.model !== "deterministic" ? ` · ${generation.model} 문장 다듬기 ${generation.rewritten_paragraphs ?? 0}문단` : "";
  el("save-status").textContent = (result.draft_id ? `초안 ${result.draft_id}` : "초안 생성됨") + model;

  const summary = el("verification-summary");
  summary.className = `verification-summary ${passed ? "status-pass" : "status-fail"}`;
  summary.innerHTML = `<span class="status-dot"></span><div><strong>${passed ? "형식 검사 통과" : "수정 필요"}</strong><span>${passed ? "출처 ID와 일부 금지 표현만 검사했습니다. 사실·법리의 의미상 근거는 직접 확인하세요." : `중요 문제 ${findings.filter((item) => item.severity === "high").length}건을 확인하세요.`}</span></div>`;
  el("finding-list").innerHTML = findings.length ? findings.map((item) => `<div class="finding-card" data-severity="${escapeHtml(item.severity)}"><strong>${escapeHtml(item.message)}</strong><p>${escapeHtml(item.locator || item.code || "초안")}</p></div>`).join("") : '<div class="empty-state">자동 검사에서 발견된 항목이 없습니다.</div>';
}

function bindEvents() {
  el("login-mode-button").addEventListener("click", () => setAuthMode("login"));
  el("signup-mode-button").addEventListener("click", () => setAuthMode("signup"));
  el("login-form").addEventListener("submit", runAction(async (event) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    el("login-error").hidden = true;
    try {
      const path = state.authMode === "signup" ? "/api/signup" : "/api/login";
      const body = { email: form.get("email"), password: form.get("password") };
      if (state.authMode === "signup") body.display_name = form.get("display_name");
      const result = await api(path, { method: "POST", body: JSON.stringify(body) });
      state.session = result.user || result.session || { email: form.get("email") };
      state.csrfToken = result.csrf_token || result.csrfToken || state.csrfToken;
      const session = await api("/api/session");
      state.csrfToken = session.csrf_token || session.csrfToken || state.csrfToken;
      state.session = session.user || state.session;
      await refreshWorkspace();
      showWorkspace();
    } catch (error) {
      el("login-error").textContent = error.message;
      el("login-error").hidden = false;
    }
  }, "계정 확인 중"));

  el("logout-button").addEventListener("click", runAction(async () => {
    await api("/api/logout", { method: "POST" });
    state.csrfToken = "";
    el("login-form").reset();
    showAuth();
  }, "로그아웃 중"));

  document.querySelectorAll(".step-button").forEach((button) => button.addEventListener("click", () => goStep(button.dataset.step)));
  document.querySelectorAll("[data-go-step]").forEach((button) => button.addEventListener("click", () => goStep(button.dataset.goStep)));

  const uploadZone = el("brief-upload-form");
  el("choose-files").addEventListener("click", () => el("brief-files").click());
  el("brief-files").addEventListener("change", runAction(async (event) => uploadFiles(event.target.files), "서면 추가 중"));
  ["dragenter", "dragover"].forEach((name) => uploadZone.addEventListener(name, (event) => { event.preventDefault(); uploadZone.classList.add("is-dragging"); }));
  ["dragleave", "drop"].forEach((name) => uploadZone.addEventListener(name, (event) => { event.preventDefault(); uploadZone.classList.remove("is-dragging"); }));
  uploadZone.addEventListener("drop", runAction(async (event) => uploadFiles(event.dataTransfer.files), "서면 추가 중"));
  uploadZone.addEventListener("submit", (event) => event.preventDefault());

  el("brief-text-form").addEventListener("submit", runAction(async (event) => {
    event.preventDefault();
    const formElement = event.currentTarget;
    const form = new FormData(formElement);
    await uploadTextBrief(form.get("title").trim(), form.get("text").trim());
    formElement.reset();
    await refreshWorkspace();
    toast("서면을 추가했습니다.");
  }, "서면 추가 중"));

  el("build-profile-button").addEventListener("click", runAction(async () => {
    await api("/api/profile/build", { method: "POST", body: "{}" });
    await refreshWorkspace();
    el("profile-section").scrollIntoView({ behavior: "smooth", block: "start" });
    toast("작성 원칙 분석을 마쳤습니다.");
  }, "작성 원칙 분석 중"));

  el("approve-profile-button").addEventListener("click", runAction(async () => {
    const traitIds = [...document.querySelectorAll('input[name="trait"]:checked:not(:disabled)')].map((input) => input.value);
    if (!traitIds.length) throw new Error("승인할 작성 원칙을 선택하세요.");
    await api("/api/profile/approve", { method: "POST", body: JSON.stringify({ trait_ids: traitIds }) });
    await refreshWorkspace();
    toast("선택한 작성 원칙을 승인했습니다.");
  }, "작성 원칙 승인 중"));

  el("matter-form").addEventListener("submit", runAction(async (event) => {
    event.preventDefault();
    const formElement = event.currentTarget;
    const title = new FormData(formElement).get("title").trim();
    const result = await api("/api/matters", { method: "POST", body: JSON.stringify({ title }) });
    state.activeMatterId = result.matter_id || result.id;
    state.precedentResults = [];
    state.statuteResults = [];
    formElement.reset();
    await refreshWorkspace();
    toast("새 사건을 만들었습니다.");
  }, "사건 생성 중"));

  el("matter-picker").addEventListener("change", (event) => {
    state.activeMatterId = event.target.value;
    state.precedentResults = [];
    state.statuteResults = [];
    renderMatterDetails();
    renderPrecedentResults();
    renderStatuteResults();
    renderApprovedSources();
    renderLatestDraft();
  });

  el("assertion-form").addEventListener("submit", runAction(async (event) => {
    event.preventDefault();
    if (!state.activeMatterId) throw new Error("먼저 사건을 만드세요.");
    const formElement = event.currentTarget;
    const data = Object.fromEntries(new FormData(formElement));
    await api(`/api/matters/${encodeURIComponent(state.activeMatterId)}/assertions`, { method: "POST", body: JSON.stringify(data) });
    formElement.reset();
    await refreshWorkspace();
    toast("사건 기록을 추가했습니다.");
  }, "사건 기록 추가 중"));

  el("authority-form").addEventListener("submit", runAction(async (event) => {
    event.preventDefault();
    if (!state.activeMatterId) throw new Error("먼저 사건을 만드세요.");
    const formElement = event.currentTarget;
    const data = Object.fromEntries(new FormData(formElement));
    await api(`/api/matters/${encodeURIComponent(state.activeMatterId)}/authorities`, { method: "POST", body: JSON.stringify(data) });
    formElement.reset();
    await refreshWorkspace();
    toast("법률 근거를 추가했습니다.");
  }, "법률 근거 추가 중"));

  el("precedent-search-form").addEventListener("submit", runAction(async (event) => {
    event.preventDefault();
    if (!state.activeMatterId) throw new Error("먼저 사건을 만드세요.");
    const query = new FormData(event.currentTarget).get("query").trim();
    const result = await api(`/api/legal/precedents?query=${encodeURIComponent(query)}`);
    state.precedentResults = result.results || [];
    renderPrecedentResults();
  }, "공식 판례 검색 중"));

  el("precedent-results").addEventListener("click", runAction(async (event) => {
    const button = event.target.closest("[data-import-precedent]");
    if (!button) return;
    if (!state.activeMatterId) throw new Error("먼저 사건을 만드세요.");
    const authorityId = nextAuthorityId();
    await api(`/api/matters/${encodeURIComponent(state.activeMatterId)}/authorities/import-precedent`, {
      method: "POST",
      body: JSON.stringify({ authority_id: authorityId, precedent_id: button.dataset.importPrecedent }),
    });
    await refreshWorkspace();
    toast(`${authorityId}로 공식 판례를 추가했습니다. 판결요지를 읽고 승인하세요.`);
  }, "판례 원문 확인 중"));

  el("statute-search-form").addEventListener("submit", runAction(async (event) => {
    event.preventDefault();
    if (!state.activeMatterId) throw new Error("먼저 사건을 만드세요.");
    const query = new FormData(event.currentTarget).get("query").trim();
    const result = await api(`/api/legal/statutes?query=${encodeURIComponent(query)}`);
    state.statuteResults = result.results || [];
    renderStatuteResults();
  }, "현행 법령 검색 중"));

  el("suggest-button").addEventListener("click", runAction(async () => {
    if (!state.activeMatterId) throw new Error("먼저 사건을 만드세요.");
    const result = await api(`/api/matters/${encodeURIComponent(state.activeMatterId)}/authorities/suggest`, { method: "POST", body: "{}" });
    renderSuggestions(result);
    toast(`조문 ${(result.statutes || []).length}건, 판례 ${(result.precedents || []).length}건을 찾았습니다. 읽어 보고 필요한 것만 추가하세요.`);
  }, "근거 후보를 찾는 중 (몇 분 걸릴 수 있습니다)"));

  el("suggest-results").addEventListener("click", runAction(async (event) => {
    const statute = event.target.closest("[data-suggest-statute]");
    const precedent = event.target.closest("[data-suggest-precedent]");
    if (!statute && !precedent) return;
    const authorityId = nextAuthorityId();
    const base = `/api/matters/${encodeURIComponent(state.activeMatterId)}/authorities`;
    if (statute) {
      await api(`${base}/import-statute`, { method: "POST", body: JSON.stringify({ authority_id: authorityId, mst: statute.dataset.suggestStatute, article_number: statute.dataset.article }) });
    } else {
      await api(`${base}/import-precedent`, { method: "POST", body: JSON.stringify({ authority_id: authorityId, precedent_id: precedent.dataset.suggestPrecedent }) });
    }
    (statute || precedent).disabled = true;
    (statute || precedent).textContent = `${authorityId}로 추가됨`;
    await refreshWorkspace();
    toast(`${authorityId}로 추가했습니다. 원문을 읽고 승인하세요.`);
  }, "공식 원문 확인 중"));

  el("statute-results").addEventListener("click", runAction(async (event) => {
    const button = event.target.closest("[data-import-statute]");
    if (!button) return;
    const mst = button.dataset.importStatute;
    const articleNumber = document.querySelector(`[data-statute-article="${CSS.escape(mst)}"]`).value.trim();
    if (!articleNumber) throw new Error("추가할 조문 번호를 입력하세요.");
    const authorityId = nextAuthorityId();
    await api(`/api/matters/${encodeURIComponent(state.activeMatterId)}/authorities/import-statute`, {
      method: "POST",
      body: JSON.stringify({ authority_id: authorityId, mst, article_number: articleNumber }),
    });
    await refreshWorkspace();
    toast(`${authorityId}로 현행 법령 조문을 추가했습니다. 원문을 읽고 승인하세요.`);
  }, "법령 조문 확인 중"));

  el("perspective-form").addEventListener("submit", runAction(async (event) => {
    event.preventDefault();
    if (!state.activeMatterId) throw new Error("먼저 사건을 만드세요.");
    const form = new FormData(event.currentTarget);
    const body = { thesis: form.get("thesis").trim(), priorities: lines(form.get("priorities")), risks: lines(form.get("risks")) };
    await api(`/api/matters/${encodeURIComponent(state.activeMatterId)}/perspective`, { method: "PUT", body: JSON.stringify(body) });
    await refreshWorkspace();
    toast("사건 관점을 저장했습니다.");
  }, "관점 저장 중"));

  el("case-metadata-form").addEventListener("submit", runAction(async (event) => {
    event.preventDefault();
    const data = Object.fromEntries(new FormData(event.currentTarget));
    const matter = activeMatter();
    await saveCaseStructure({ case_metadata: data, claims: matter.claims || [], opposing_positions: matter.opposing_positions || [] });
    toast("제출 정보를 저장했습니다.");
  }, "제출 정보 저장 중"));

  el("claim-element-form").addEventListener("submit", runAction(async (event) => {
    event.preventDefault();
    const formElement = event.currentTarget;
    const data = Object.fromEntries(new FormData(formElement));
    const matter = activeMatter();
    const claims = structuredClone(matter.claims || []);
    let claim = claims.find((item) => item.claim_id === data.claim_id.trim());
    if (!claim) {
      claim = { claim_id: data.claim_id.trim(), label: data.label.trim(), position: data.position, requested_relief: data.requested_relief.trim(), elements: [] };
      claims.push(claim);
    } else {
      claim.label = data.label.trim(); claim.position = data.position; claim.requested_relief = data.requested_relief.trim();
    }
    if (claim.elements.some((item) => item.element_id === data.element_id.trim())) throw new Error("같은 요건 ID가 이미 있습니다.");
    claim.elements.push({ element_id: data.element_id.trim(), name: data.name.trim(), legal_test: data.legal_test.trim(), burden_party: data.burden_party.trim(), conclusion: data.conclusion, assertion_ids: sourceIds(data.assertion_ids), authority_ids: sourceIds(data.authority_ids) });
    await saveCaseStructure(caseStructurePayload({ claims }));
    formElement.reset();
    toast("요건사실과 입증책임을 추가했습니다.");
  }, "논증 구조 저장 중"));

  el("opposing-position-form").addEventListener("submit", runAction(async (event) => {
    event.preventDefault();
    const formElement = event.currentTarget;
    const data = Object.fromEntries(new FormData(formElement));
    const matter = activeMatter();
    const positions = structuredClone(matter.opposing_positions || []);
    if (positions.some((item) => item.position_id === data.position_id.trim())) throw new Error("같은 상대방 주장 ID가 이미 있습니다.");
    positions.push({ position_id: data.position_id.trim(), allegation: data.allegation.trim(), response: data.response, response_text: data.response_text.trim(), assertion_ids: sourceIds(data.assertion_ids), authority_ids: sourceIds(data.authority_ids) });
    await saveCaseStructure(caseStructurePayload({ opposing_positions: positions }));
    formElement.reset();
    toast("상대방 주장에 대한 답변을 추가했습니다.");
  }, "답변 구조 저장 중"));

  el("claim-structure-list").addEventListener("click", runAction(async (event) => {
    const claimButton = event.target.closest("[data-remove-claim]");
    const elementButton = event.target.closest("[data-remove-element]");
    if (!claimButton && !elementButton) return;
    const matter = activeMatter();
    let claims = structuredClone(matter.claims || []);
    if (claimButton) {
      claims = claims.filter((item) => item.claim_id !== claimButton.dataset.removeClaim);
    } else {
      const [claimId, elementId] = elementButton.dataset.removeElement.split(":", 2);
      const claim = claims.find((item) => item.claim_id === claimId);
      if (claim) claim.elements = (claim.elements || []).filter((item) => item.element_id !== elementId);
    }
    await saveCaseStructure(caseStructurePayload({ claims }));
    toast("논증 구조에서 삭제했습니다.");
  }, "논증 구조 수정 중"));

  el("position-structure-list").addEventListener("click", runAction(async (event) => {
    const button = event.target.closest("[data-remove-position]");
    if (!button) return;
    const matter = activeMatter();
    const opposingPositions = (matter.opposing_positions || []).filter((item) => item.position_id !== button.dataset.removePosition);
    await saveCaseStructure(caseStructurePayload({ opposing_positions: opposingPositions }));
    toast("상대방 주장 답변을 삭제했습니다.");
  }, "답변 구조 수정 중"));

  el("build-argument-plan-button").addEventListener("click", runAction(async () => {
    await api(`/api/matters/${encodeURIComponent(state.activeMatterId)}/argument-plan`, { method: "POST", body: "{}" });
    await refreshWorkspace();
    toast("논증계획과 누락된 근거를 검사했습니다.");
  }, "논증계획 생성 중"));

  el("approve-argument-plan-button").addEventListener("click", runAction(async () => {
    await api(`/api/matters/${encodeURIComponent(state.activeMatterId)}/argument-plan/approve`, { method: "POST", body: JSON.stringify({ approve: true }) });
    await refreshWorkspace();
    toast("논증계획을 승인했습니다.");
  }, "논증계획 승인 중"));

  el("approve-matter-button").addEventListener("click", runAction(async () => {
    const matter = activeMatter();
    if (!matter) throw new Error("먼저 사건을 만드세요.");
    const body = {
      assertion_ids: [...document.querySelectorAll("[data-approve-assertion]:checked")].map((input) => input.value),
      authority_ids: [...document.querySelectorAll("[data-approve-authority]:checked")].map((input) => input.value),
      approve_perspective: true,
    };
    await api(`/api/matters/${encodeURIComponent(state.activeMatterId)}/approve`, { method: "POST", body: JSON.stringify(body) });
    await refreshWorkspace();
    toast("사건 자료와 관점을 승인했습니다.");
  }, "자료 승인 중"));

  el("generate-draft-button").addEventListener("click", runAction(async () => {
    if (!state.activeMatterId) throw new Error("먼저 사건을 선택하세요.");
    const result = await api(`/api/matters/${encodeURIComponent(state.activeMatterId)}/draft`, { method: "POST", body: JSON.stringify({ title: el("draft-title").value.trim() || "준비서면" }) });
    renderDraft(result);
    toast("초안을 생성하고 자동 검사를 마쳤습니다.");
  }, "근거를 확인하며 초안 생성 중"));

  el("copy-draft-button").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(el("draft-content").value); toast("초안 전체를 복사했습니다."); }
    catch { el("draft-content").select(); document.execCommand("copy"); toast("초안 전체를 복사했습니다."); }
  });

  el("draft-content").addEventListener("input", () => { el("save-status").textContent = "수정됨 · 아직 저장되지 않음"; });
}

async function boot() {
  bindEvents();
  setBusy(true, "작업 공간 확인 중");
  try {
    const session = await api("/api/session");
    state.csrfToken = session.csrf_token || session.csrfToken || "";
    state.session = session.user || (session.authenticated ? session : null);
    // 초대 전용 운영에서는 계정 만들기 버튼을 숨긴다
    if (session.signup_enabled === false) el("signup-mode-button").hidden = true;
    if (!state.session) return showAuth();
    await refreshWorkspace();
    showWorkspace();
    const hashStep = Number(location.hash.replace("#step-", ""));
    if ([1, 2, 3].includes(hashStep)) goStep(hashStep);
  } catch (error) {
    if (error.message.includes("로그인이 만료")) showAuth();
    else { showAuth(); toast(error.message, "error"); }
  } finally { setBusy(false); }
}

boot();
