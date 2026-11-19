/* 纯前端渲染：只消费 window.INTEL_DATA，不含任何后端模板或 Python 逻辑。 */
(function () {
  "use strict";
  const D = window.INTEL_DATA;
  if (!D) {
    document.body.innerHTML = '<div class="empty">缺少 api/data.js：请先运行 make build-api 生成数据契约。</div>';
    return;
  }

  const $ = (id) => document.getElementById(id);
  const esc = (v) => String(v == null ? "" : v).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
  const num = (v) => (typeof v === "number" ? v.toLocaleString("zh-CN") : esc(v));
  const pairs = (obj) => (Array.isArray(obj) ? obj : Object.entries(obj || {}));
  const empty = (text) => `<div class="empty">${esc(text)}</div>`;

  function table(heads, rows) {
    if (!rows || !rows.length) return empty("暂无数据");
    return `<div class="scroll"><table><thead><tr>${heads.map((h) => `<th>${esc(h)}</th>`).join("")}</tr></thead><tbody>${rows.join("")}</tbody></table></div>`;
  }
  function kpiCard(label, value) {
    return `<div class="card"><div class="kpi-value">${num(value)}</div><div class="kpi-label">${esc(label)}</div></div>`;
  }
  function chips(items, formatter) {
    if (!items || !items.length) return empty("暂无数据");
    return `<div class="chips">${items.map((item) => `<span class="chip">${formatter(item)}</span>`).join("")}</div>`;
  }

  function renderTabs() {
    $("tabs").innerHTML = (D.sections || []).map((section, i) =>
      `<button class="tab${i === 0 ? " active" : ""}" data-view="${esc(section.id)}" role="tab">${esc(section.title)}</button>`
    ).join("");
    $("tabs").addEventListener("click", (event) => {
      const button = event.target.closest(".tab");
      if (!button) return;
      document.querySelectorAll(".tab").forEach((tab) => tab.classList.remove("active"));
      document.querySelectorAll(".view").forEach((view) => view.classList.remove("active"));
      button.classList.add("active");
      const view = $("view-" + button.dataset.view);
      if (view) view.classList.add("active");
    });
  }

  function renderThemePicker() {
    const select = $("theme-select");
    select.innerHTML = (D.themes || []).map((theme) =>
      `<option value="${esc(theme.css)}" data-id="${esc(theme.id)}">${esc(theme.name)}</option>`
    ).join("");
    const saved = localStorage.getItem("intel-theme");
    const target = (D.themes || []).find((t) => t.css === saved);
    if (target) { select.value = target.css; $("theme-css").href = target.css; }
    select.addEventListener("change", () => {
      $("theme-css").href = select.value;
      localStorage.setItem("intel-theme", select.value);
    });
  }

  function renderOverview() {
    $("overview-body").innerHTML = Object.entries(D.kpi || {}).map(([k, v]) => kpiCard(k, v)).join("");
    const sens = (D.industry || {}).sensitivity || {};
    $("overview-notes").innerHTML = `<h3>敏感度与说明</h3><p>${pairs(sens).map(([k, v]) => `${esc(k)} <b>${esc(v)}</b>`).join(" · ") || "暂无预判数据"}</p>` +
      `<p class="muted">数据源为免费公开源；付费/商业源尚未接入。公司估值等动态为人工整理示例，需二次核验。</p>`;
  }

  function renderOpportunity() {
    const items = D.opportunities || [];
    $("opportunity-body").innerHTML = items.length ? items.map((item) =>
      `<div class="card"><h3><span class="tag">${esc(item.category)}</span>${esc(item.title)}</h3>` +
      `<p>${esc(item.detail)}</p><p class="muted">来源：${esc(item.source)}${item.confidence ? ` · 置信度 ${esc(item.confidence)}` : ""}</p></div>`
    ).join("") : empty("暂无机会信号，请先运行 analyze-forecast / build-api");
  }

  function renderAlerts() {
    const items = D.alerts || [];
    $("alerts-body").innerHTML = items.length ? items.map((item) =>
      `<div class="alert-item${item.level === "高" ? " high" : ""}"><b><span class="tag ${item.level === "高" ? "bad" : "warn"}">${esc(item.level)}</span>${esc(item.title)}</b>` +
      `<div class="muted">${esc(item.detail)} · ${esc(item.source)}</div></div>`
    ).join("") : empty("当前无预警");
  }

  function renderIndustry() {
    const trend = (D.industry || {}).trend_7d || {};
    $("industry-trend").innerHTML = table(["日期", "条数"], pairs(trend).map(([k, v]) => `<tr><td>${esc(k)}</td><td class="num">${esc(v)}</td></tr>`));
    $("industry-keywords").innerHTML = chips((D.industry || {}).top_keywords, ([word, count]) => `${esc(word)} <b>${esc(count)}</b>`);
    $("industry-signals").innerHTML = table(
      ["类型", "关键词", "标题", "热度", "来源"],
      ((D.industry || {}).signals || []).map((s) => `<tr><td>${esc(s.type)}</td><td>${esc(s.keyword)}</td><td>${esc(s.title)}</td><td class="num">${esc(s.heat)}</td><td>${esc((s.sources || []).slice(0, 2).join(", "))}</td></tr>`)
    );
    $("research-directions").innerHTML = table(["方向", "论文数", "说明"],
      ((D.industry || {}).research_directions || []).map((r) => `<tr><td>${esc(r.name)}</td><td class="num">${esc(r.count)}</td><td>${esc(r.desc)}</td></tr>`));
  }

  function renderTalent() {
    const talent = D.talent || {};
    $("talent-kpi").innerHTML = [
      kpiCard("JD 岗位", talent.total_jobs), kpiCard("热门岗位", talent.hot_jobs),
      kpiCard("简历技能覆盖", talent.resume_coverage == null ? "—" : `${Math.round(talent.resume_coverage * 100)}%`),
      kpiCard("投递台账", talent.applications),
    ].join("");
    const brackets = pairs(talent.salary_brackets);
    const max = Math.max(1, ...brackets.map(([, v]) => Number(v) || 0));
    $("talent-salary").innerHTML = brackets.length ? brackets.map(([k, v]) =>
      `<div style="margin:6px 0"><div style="display:flex;justify-content:space-between"><span>${esc(k)}</span><b>${esc(v)}</b></div><div class="bar"><i style="width:${Math.round((Number(v) || 0) / max * 100)}%"></i></div></div>`
    ).join("") : empty("暂无薪资数据");
    $("talent-skills").innerHTML = chips(talent.top_skills, ([name, count]) => `${esc(name)} <b>${esc(count)}</b>`);
    $("talent-salaries").innerHTML = table(["岗位", "公司", "城市", "薪资", "均价(K)"],
      (talent.top_salaries || []).map((j) => `<tr><td>${esc(j.title)}</td><td>${esc(j.company)}</td><td>${esc(j.city)}</td><td>${esc(j.salary)}</td><td class="num">${esc(j.avg_k)}</td></tr>`));
    $("talent-resume").innerHTML = talent.resume_missing && talent.resume_missing.length
      ? `<p>缺失技能：${talent.resume_missing.map((s) => `<span class="tag warn">${esc(s)}</span>`).join("")}</p>` +
        `<p class="muted">${esc(talent.note || "")}</p>`
      : empty("暂无简历数据；放入 resume.md 后运行 make resume 生成建议");
  }

  function renderRankings() {
    const r = D.rankings || {};
    $("rank-model").innerHTML = table(["厂商", "区域", "模型数", "开源数", "均分", "代表模型"],
      (r.model_vendors || []).map((v) => `<tr><td>${esc(v.vendor_name)}</td><td>${esc(v.region)}</td><td class="num">${esc(v.model_count)}</td><td class="num">${esc(v.open_models)}</td><td class="num">${esc(v.avg_composite)}</td><td>${esc(v.top_model)}</td></tr>`));
    $("rank-agent").innerHTML = table(["平台", "分类", "特性数", "计费", "免费层"],
      (r.agent_vendors || []).map((p) => `<tr><td>${esc(p.name)}</td><td>${esc(p.category)}</td><td class="num">${esc(p.feature_count)}</td><td>${esc(p.pricing)}</td><td>${esc(p.free)}</td></tr>`));
    const oss = r.oss_community || {};
    $("rank-oss").innerHTML = Object.keys(oss).map((bucket) => {
      const b = oss[bucket];
      return `<h3>${esc(bucket)}（${esc(b.count)}）<span class="muted"> ${esc(b.desc)}</span></h3>` +
        table(["项目", "语言", "Stars", "Forks", "简介"],
          (b.items || []).map((i) => `<tr><td><a href="${esc(i.url)}" target="_blank" rel="noopener">${esc(i.full_name)}</a></td><td>${esc(i.lang || "-")}</td><td class="num">${num(i.stars)}</td><td class="num">${num(i.forks)}</td><td>${esc(i.desc)}</td></tr>`));
    }).join("") || empty("暂无 GitHub 榜单数据");
  }

  function renderMonitor() {
    const monitor = D.monitor || {};
    $("monitor-kpi").innerHTML = Object.entries(monitor.kpi || {}).map(([k, v]) => kpiCard(k, v)).join("");
    $("monitor-channels").innerHTML = table(["渠道", "区域", "策略", "协议", "采集量", "成功率", "连败", "状态", "最近错误"],
      (monitor.channels || []).map((ch) => `<tr><td>${esc(ch.name)}</td><td>${esc(ch.region)}</td><td>${esc(ch.strategy)}</td><td>${esc(ch.protocol)}</td><td class="num">${esc(ch.total_items)}</td><td>${esc(ch.success_rate)}</td><td class="num">${esc(ch.consecutive_fail)}</td><td><span class="tag ${ch.dot === "green" ? "good" : ch.dot === "red" ? "bad" : "warn"}">${esc(ch.status)}</span></td><td>${esc(ch.last_err)}</td></tr>`));
    $("monitor-improvements").innerHTML = (monitor.improvements || []).length
      ? table(["原因", "改进建议"], monitor.improvements.map((i) => `<tr><td>${esc(i.reason)}</td><td>${esc(i.advice)}</td></tr>`))
      : empty("暂无失败项");
  }

  renderTabs();
  renderThemePicker();
  renderOverview();
  renderOpportunity();
  renderAlerts();
  renderIndustry();
  renderTalent();
  renderRankings();
  renderMonitor();
  $("data-updated").textContent = D.source_updated || D.generated_at || "—";
  $("contract-version").textContent = D.contract_version || "—";
  $("footer-time").textContent = "契约生成：" + (D.generated_at || "—");
})();
