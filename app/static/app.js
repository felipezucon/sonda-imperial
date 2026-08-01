/* Sonda Imperial — lógica da interface */
const $ = (id) => document.getElementById(id);

let polling = null;
let currentStatus = "active";
let allAds = [];
let totalCount = 0;

/* ---------- helpers ---------- */
async function api(path, opts) {
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Erro ${res.status}`);
  return data;
}

function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.classList.add("hidden"), 3500);
}

function setDot(state, title) {
  const d = $("statusDot");
  d.className = `dot ${state}`;
  d.title = title || "";
}

function esc(s) {
  return (s || "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* ---------- formulário ---------- */
document.querySelectorAll(".seg").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".seg").forEach((b) => {
      b.classList.remove("active");
      b.setAttribute("aria-checked", "false");
    });
    btn.classList.add("active");
    btn.setAttribute("aria-checked", "true");
    currentStatus = btn.dataset.status;
  });
});

$("keyword").addEventListener("keydown", (e) => { if (e.key === "Enter") startScrape(); });
$("btnRun").addEventListener("click", startScrape);
$("btnCancel").addEventListener("click", () => api("/api/cancel", { method: "POST" }).catch(() => {}));
$("btnFolder").addEventListener("click", () =>
  api("/api/open", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ target: "folder" }) })
    .catch((e) => toast(e.message)));

async function startScrape() {
  const payload = {
    keyword: $("keyword").value,
    url: $("rawUrl").value,
    country: $("country").value,
    max_results: $("maxResults").value,
    status: currentStatus,
  };
  $("formError").classList.add("hidden");
  try {
    await api("/api/scrape", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  } catch (e) {
    $("formError").textContent = e.message;
    $("formError").classList.remove("hidden");
    return;
  }
  $("btnRun").disabled = true;
  $("progressBox").classList.remove("hidden");
  setDot("busy", "Varredura em andamento");
  pollStatus();
}

/* ---------- progresso ---------- */
function pollStatus() {
  clearInterval(polling);
  polling = setInterval(async () => {
    let st;
    try { st = await api("/api/status"); } catch { return; }

    if (st.running) {
      const max = st.max || 0;
      $("progressText").textContent = max
        ? `Sondando… ${st.count}/${max} anúncios`
        : `Sondando… ${st.count} anúncios`;
      const fill = $("barFill");
      if (max) {
        fill.classList.remove("indeterminate");
        fill.style.width = `${Math.min(100, (st.count / max) * 100)}%`;
      } else {
        fill.classList.add("indeterminate");
        fill.style.width = "30%";
      }
      return;
    }

    /* terminou */
    clearInterval(polling);
    $("btnRun").disabled = false;
    $("progressBox").classList.add("hidden");

    if (st.error && !st.count) {
      setDot("err", st.error);
      $("formError").textContent = st.error;
      $("formError").classList.remove("hidden");
      return;
    }
    if (st.done) {
      setDot("ok", "Varredura concluída");
      toast(`Varredura concluída: ${st.count} anúncios exportados.`);
      loadResults();
      loadHistory();
    }
  }, 1000);
}

/* ---------- resultados ---------- */
async function loadResults(file) {
  let data;
  try {
    data = await api(`/api/results${file ? `?file=${encodeURIComponent(file)}` : ""}`);
  } catch (e) { toast(e.message); return; }

  $("emptyState").classList.add("hidden");
  $("results").classList.remove("hidden");

  const s = data.summary;
  $("chips").innerHTML = `
    <span class="chip"><b>${s.total}</b> anúncios</span>
    <span class="chip"><b>${s.advertisers}</b> anunciantes</span>
    ${s.avg_force != null ? `<span class="chip" title="Média do Índice de Força da amostra">⚡ força média <b>${s.avg_force}</b></span>` : ""}
    ${s.legendary_count ? `<span class="chip" title="Anúncios com Índice de Força 70+ (rodando há muito tempo e escalados)">🏆 <b>${s.legendary_count}</b> lendários</span>` : ""}
    ${s.variation_groups ? `<span class="chip" title="${s.ads_in_groups} anúncios desta amostra são variações de ${s.variation_groups} criativos — anunciante replicando = criativo que converte">🧬 <b>${s.variation_groups}</b> criativos c/ variações</span>` : ""}
    ${Object.entries(s.formats).map(([k, v]) => `<span class="chip"><b>${v}</b> ${esc(k)}</span>`).join("")}`;

  $("topForce").innerHTML = (s.top_force || []).map((a) =>
    `<li title="${esc(a.page_name)} — ${a.days} dias no ar${a.variations > 1 ? `, ${a.variations} variações` : ""}">
       <span>${esc(a.page_name)} · ${a.days}d</span><span class="n">${a.score}</span></li>`).join("");

  $("topPages").innerHTML = s.top_pages.map((p) =>
    `<li title="${esc(p.name)}"><span>${esc(p.name)}</span><span class="n">${p.count}</span></li>`).join("");

  $("topCtas").innerHTML = s.top_ctas.map((c) =>
    `<li><span>${esc(c.name)}</span><span class="n">${c.count}</span></li>`).join("");

  $("formats").innerHTML = Object.entries(s.destinations).map(([k, v]) =>
    `<li><span>${esc(k)}</span><span class="n">${v}</span></li>`).join("");

  const files = data.files || {};
  $("btnJson").onclick = () => files.json && openFile(files.json);
  $("btnCsv").onclick = () => files.csv && openFile(files.csv);
  $("btnJson").style.display = files.json ? "" : "none";
  $("btnCsv").style.display = files.csv ? "" : "none";

  allAds = data.ads;
  totalCount = s.total;
  renderGrid(null);
  $("content").scrollTop = 0;
}

function renderGrid(groupLabel) {
  const ads = groupLabel ? allAds.filter((a) => a.var_group === groupLabel) : allAds;
  if (groupLabel) {
    $("gridTitle").innerHTML =
      `🧬 Variações do criativo ${esc(groupLabel)} (${ads.length}) — <a href="#" id="clearFilter">mostrar todos</a>`;
  } else {
    $("gridTitle").textContent =
      `Anúncios · ordenados pelo Índice de Força${allAds.length < totalCount ? ` (exibindo ${allAds.length} de ${totalCount})` : ""}`;
  }
  $("adsGrid").innerHTML = ads.map(adCard).join("");
  const clear = document.getElementById("clearFilter");
  if (clear) clear.addEventListener("click", (e) => { e.preventDefault(); renderGrid(null); });
}

/* clique no selo de variação filtra o grupo */
$("adsGrid").addEventListener("click", (e) => {
  const badge = e.target.closest(".badge.vargroup");
  if (badge) renderGrid(badge.dataset.group);
});

function adCard(ad) {
  const libUrl = `https://www.facebook.com/ads/library/?id=${ad.id}`;
  const thumb = ad.thumb
    ? `<img src="${esc(ad.thumb)}" alt="" loading="lazy" onerror="this.remove()">`
    : "";
  const tierClass = (ad.tier || "").replace(" ", "-");
  const scoreTitle = `Índice de Força ${ad.score} (${esc(ad.tier || "")}) — ${ad.days ?? "?"} dias no ar`
    + (ad.variations > 1 ? `, ${ad.variations} variações do criativo` : "");
  const vgBadge = ad.var_group
    ? `<span class="badge vargroup vg-${vgColor(ad.var_group)}" data-group="${esc(ad.var_group)}"
         title="Criativo replicado em ${ad.var_size} anúncios desta amostra — clique para ver só o grupo ${esc(ad.var_group)}">🧬 ${esc(ad.var_group)} · ${ad.var_size}×</span>`
    : "";
  return `
  <article class="ad-card">
    <div class="ad-thumb">${thumb}
      ${ad.is_video ? '<span class="badge video">▶ vídeo</span>' : ""}
      ${ad.is_active === false ? '<span class="badge">inativo</span>' : ""}
      ${ad.score != null ? `<span class="badge score ${tierClass}" title="${scoreTitle}">⚡ ${ad.score}</span>` : ""}
      ${vgBadge}
    </div>
    <div class="ad-body">
      <span class="ad-page">${esc(ad.page_name)}</span>
      ${ad.title ? `<strong class="ad-text" style="color:var(--text)">${esc(ad.title)}</strong>` : ""}
      <p class="ad-text">${esc(ad.body)}</p>
      <div class="ad-meta">
        <span title="No ar desde ${esc(ad.start_date || "?")}">${ad.days != null ? `${ad.days} dias` : esc(ad.start_date || "")}</span>
        ${ad.cta ? `<span class="cta">${esc(ad.cta)}</span>` : ""}
      </div>
      <a class="ad-link" href="${libUrl}" target="_blank" rel="noopener">Ver na Biblioteca ↗</a>
    </div>
  </article>`;
}

function vgColor(label) {
  return ((label.charCodeAt(0) - 65) + (label.length - 1) * 26) % 6;
}

function openFile(name) {
  api("/api/open", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ target: name }),
  }).catch((e) => toast(e.message));
}

/* ---------- histórico ---------- */
async function loadHistory() {
  let items;
  try { items = await api("/api/history"); } catch { return; }
  $("historyEmpty").style.display = items.length ? "none" : "";
  $("historyList").innerHTML = items.map((it) => {
    const when = it.when.replace(/(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})\d{2}/, "$3/$2 $4:$5");
    return `<li data-file="${esc(it.file)}" title="Clique para reabrir">
      <span class="h-label">${esc(it.label)}</span>
      <span class="h-meta">${esc(when)}</span>
    </li>`;
  }).join("");
  document.querySelectorAll("#historyList li").forEach((li) =>
    li.addEventListener("click", () => loadResults(li.dataset.file)));
}

/* ---------- inicialização ---------- */
loadHistory();
api("/api/status").then((st) => { if (st.running) { $("btnRun").disabled = true; $("progressBox").classList.remove("hidden"); setDot("busy"); pollStatus(); } }).catch(() => {});
