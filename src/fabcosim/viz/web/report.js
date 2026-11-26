/* 演示平台应用层：加载两套已验收结果，按固定叙事渲染八页。 */
(function () {
  "use strict";

  var PAGES = [
    { label: "封面", sub: "总览" },
    { label: "短期对照", sub: "60 日 · 四指标" },
    { label: "趋势", sub: "60 日 · 逐日" },
    { label: "C 与 Q2", sub: "60 日 · 配对" },
    { label: "期末在制", sub: "60 日 · 阶段" },
    { label: "长期结果", sub: "730 日 · 四指标" },
    { label: "长期趋势", sub: "730 日 · 分块" },
    { label: "边界", sub: "口径与复现" },
  ];
  var SEED_COLORS = ["#3869A2", "#D08C30", "#359780"];
  var state = { page: 0, data: {}, index: null, rendered: false };

  /* ---------- 工具 ---------- */
  function $(sel) { return document.querySelector(sel); }
  function el(tag, cls, html) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (html !== undefined) node.innerHTML = html;
    return node;
  }
  function metricDef(ds, key) {
    return ds.metrics.filter(function (m) { return m.key === key; })[0];
  }
  function avg(list) {
    return list.reduce(function (a, b) { return a + b; }, 0) / (list.length || 1);
  }
  function interpAt(rows, x, key) {
    if (!rows.length) return 0;
    if (x <= rows[0].day) return rows[0][key];
    if (x >= rows[rows.length - 1].day) return rows[rows.length - 1][key];
    for (var i = 1; i < rows.length; i++) {
      if (rows[i].day >= x) {
        var a = rows[i - 1], b = rows[i];
        var t = (x - a.day) / (b.day - a.day || 1);
        return a[key] + (b[key] - a[key]) * t;
      }
    }
    return rows[rows.length - 1][key];
  }
  function addCard(grid, title, note) {
    var card = el("div", "chart-card");
    var head = el("div", "card-head");
    head.appendChild(el("h3", null, title));
    if (note) head.appendChild(el("span", "card-note", note));
    card.appendChild(head);
    var chart = el("div", "chart");
    card.appendChild(chart);
    grid.appendChild(card);
    return chart;
  }
  function missing(grid, message) {
    var box = el("div", "missing", "<strong>数据不可用</strong><span>" + message + "</span>");
    grid.appendChild(box);
  }
  function toast(message) {
    var node = $("#toast");
    node.textContent = message;
    node.hidden = false;
    clearTimeout(node.__timer);
    node.__timer = setTimeout(function () { node.hidden = true; }, 2400);
  }

  /* ---------- 渲染：封面 ---------- */
  function renderCover(weekly, extended) {
    var stats = document.querySelectorAll("#cover-stats .stat-value");
    if (stats[0]) stats[0].textContent = weekly && weekly.available ? String(weekly.cases.length) : "—";
    if (stats[1]) stats[1].textContent = extended && extended.available ? String(extended.cases.length) : "—";
    if (stats[2]) stats[2].textContent = weekly && weekly.available ? String(weekly.meta.rules.length) : "—";

    var wc = $("#dc-weekly"), ec = $("#dc-extended");
    if (weekly && weekly.available) {
      wc.textContent = weekly.count + " · 主窗 " + weekly.meta.main_window.join("–") + " 日";
    } else {
      wc.textContent = "分析产物缺失，请检查 runs/ 路径";
    }
    if (extended && extended.available) {
      ec.textContent = extended.count + " · 趋势种子 " + extended.trend_seed;
    } else {
      ec.textContent = "分析产物缺失，请检查 runs/ 路径";
    }
  }

  /* ---------- 渲染：四指标柱状 ---------- */
  function renderMetricBars(grid, ds, specs) {
    specs.forEach(function (spec) {
      var metric = metricDef(ds, spec.key);
      var scale = metric.scale;
      var note = spec.note || "";
      var chart = addCard(grid, spec.title, note);
      var categories = ds.meta.rules.map(function (rule) {
        var stat = ds.series[rule.key][spec.key];
        return {
          label: rule.label,
          color: rule.color,
          value: stat.mean * scale,
          min: stat.min * scale,
          max: stat.max * scale,
          seeds: stat.values.map(function (v) { return { seed: v.seed, value: v.value * scale }; }),
        };
      });
      Charts.barError(chart, {
        categories: categories,
        precision: spec.precision,
        yLabel: metric.label + "（" + metric.unit + "）",
        note: spec.footnote || "",
      });
    });
  }

  /* ---------- 页 1 ---------- */
  function renderP1(ds) {
    var grid = $("#p1-grid");
    if (!ds || !ds.available) { missing(grid, "缺少 runs/20261117-weekly-cloud/final-analysis-02/analysis.json"); return; }
    renderMetricBars(grid, ds, [
      { key: "completed", title: "累计出厂", precision: 0 },
      { key: "main_throughput", title: "主窗出厂率", precision: 2 },
      { key: "main_cqt_rate", title: "主窗 CQT 违例率（越低越好）", precision: 2 },
      { key: "tail_wip_growth", title: "末窗在制净增（越低越好）", precision: 0 },
    ]);
  }

  /* ---------- 页 2：趋势 ---------- */
  function trendSeries(ds, key) {
    var days = ds.meta.model_days;
    var grid = [];
    for (var d = 0; d <= days; d++) grid.push(d);
    return ds.meta.rules.map(function (rule) {
      var perSeed = ds.meta.seeds.map(function (seed) {
        var rows = ds.trends
          .filter(function (t) { return t.rule === rule.key && t.seed === seed; })
          .sort(function (a, b) { return a.day - b.day; });
        return grid.map(function (x) { return interpAt(rows, x, key); });
      });
      var mean = grid.map(function (_, i) { return avg(perSeed.map(function (arr) { return arr[i]; })); });
      var lower = grid.map(function (_, i) { return Math.min.apply(null, perSeed.map(function (a) { return a[i]; })); });
      var upper = grid.map(function (_, i) { return Math.max.apply(null, perSeed.map(function (a) { return a[i]; })); });
      return {
        name: rule.label,
        color: rule.color,
        points: grid.map(function (x, i) { return [x, mean[i]]; }),
        band: grid.map(function (x, i) { return [x, lower[i], upper[i]]; }),
      };
    });
  }

  function renderP2(ds) {
    var grid = $("#p2-grid");
    if (!ds || !ds.available) { missing(grid, "缺少 60 日分析产物"); return; }
    var wip = addCard(grid, "在制库存（lot）", "三种子均值与范围");
    var out = addCard(grid, "累计出厂（lot）", "三种子均值与范围");
    var refLines = [
      { x: 30, label: "主窗", color: "#8a94a3" },
      { x: 46, label: "末窗", color: "#8a94a3", dash: "2 3" },
    ];
    var series = trendSeries(ds, "wip");
    Charts.lines(wip, {
      series: series,
      bands: series.map(function (sr) { return { upper: sr.band.map(function (p) { return [p[0], p[2]]; }), lower: sr.band.map(function (p) { return [p[0], p[1]]; }), color: sr.color }; }),
      xDomain: [0, ds.meta.model_days],
      xLabel: "仿真日",
      yLabel: "在制（lot）",
      refLines: refLines,
    });
    var series2 = trendSeries(ds, "completed");
    Charts.lines(out, {
      series: series2,
      bands: series2.map(function (sr) { return { upper: sr.band.map(function (p) { return [p[0], p[2]]; }), lower: sr.band.map(function (p) { return [p[0], p[1]]; }), color: sr.color }; }),
      xDomain: [0, ds.meta.model_days],
      xLabel: "仿真日",
      yLabel: "累计出厂（lot）",
    });
  }

  /* ---------- 页 3：配对 ---------- */
  function renderP3(ds) {
    var grid = $("#p3-grid");
    if (!ds || !ds.available) { missing(grid, "缺少 60 日配对样本"); return; }
    var pairs = ds.pair || [];
    var seedColor = {};
    ds.meta.seeds.forEach(function (seed, i) { seedColor[seed] = SEED_COLORS[i % SEED_COLORS.length]; });
    var make = function (leftKey, rightKey) {
      return pairs.map(function (p) {
        return { seed: p.seed, color: seedColor[p.seed], left: p[leftKey], right: p[rightKey] };
      });
    };
    var legend = ds.meta.seeds.map(function (seed, i) {
      return { name: "种子 " + seed, color: SEED_COLORS[i % SEED_COLORS.length] };
    });
    var specs = [
      ["共同主窗 CQT 违例（次）", "同 lot 配对 · 纵轴非零起点", "c_violations", "q2_violations", 0],
      ["60 日累计出厂（lot）", "越高越好 · 纵轴非零起点", "c_completed", "q2_completed", 0],
      ["期末过期未闭 CQT 窗口（个）", "越低越好 · 纵轴非零起点", "c_overdue", "q2_overdue", 0],
      ["末 14 日在制净增（lot）", "越低越好 · 纵轴非零起点", "c_tail_wip_growth", "q2_tail_wip_growth", 0],
    ];
    specs.forEach(function (sp) {
      var chart = addCard(grid, sp[0], sp[1]);
      Charts.slope(chart, {
        pairs: make(sp[2], sp[3]),
        leftLabel: "C",
        rightLabel: "修正版 Q2",
        precision: sp[4],
        yLabel: sp[0],
        legend: legend,
      });
    });
  }

  /* ---------- 页 4：期末在制分解 ---------- */
  function renderP4(ds) {
    var grid = $("#p4-grid");
    if (!ds || !ds.available) { missing(grid, "缺少 60 日阶段快照"); return; }
    var chart = addCard(grid, "第 60 日在制阶段构成（三种子均值，lot）", "等待仍占主要部分");
    var phaseOrder = ["waiting_production_buffer", "waiting_tool", "processing", "handling", "production_transfer"];
    var colors = { waiting_production_buffer: "#B2C5D4", waiting_tool: "#E0B66C", processing: "#4C9687", handling: "#A58EB5", production_transfer: "#DA7462" };
    var labels = { waiting_production_buffer: "输入准入等待", waiting_tool: "已到站等待", processing: "加工中", handling: "换型/装卸", production_transfer: "交接中" };
    var stacks = phaseOrder.map(function (phase) {
      var values = ds.meta.rules.map(function (rule) {
        var rows = ds.phases.filter(function (p) { return p.rule === rule.key && p.phase === phase; });
        return rows.reduce(function (sum, r) { return sum + r.lots; }, 0) / ds.meta.seeds.length;
      });
      return { name: labels[phase], color: colors[phase], values: values };
    });
    Charts.stacked(chart, {
      categories: ds.meta.rules.map(function (r) { return r.label; }),
      stacks: stacks,
      yLabel: "在制（lot）",
    });
  }

  /* ---------- 页 5：730 日四指标 ---------- */
  function renderP5(ds) {
    var grid = $("#p5-grid");
    if (!ds || !ds.available) { missing(grid, "缺少 730 日分析产物"); return; }
    var nBy = {};
    (ds.coverage || []).forEach(function (c) { nBy[c.rule] = c.n; });
    var specs = [
      { key: "completed", title: "730 日累计出厂", precision: 0 },
      { key: "main_throughput", title: "第二年出厂率", precision: 2 },
      { key: "main_mean_wip", title: "第二年平均在制", precision: 0 },
      { key: "tail_wip_growth", title: "末 30 日在制净增（越低越好）", precision: 0 },
    ];
    specs.forEach(function (spec) {
      var metric = metricDef(ds, spec.key);
      var chart = addCard(grid, spec.title, "样本已通过 " + ds.cases.length + " 例");
      var categories = ds.meta.rules.map(function (rule) {
        var stat = ds.series[rule.key][spec.key];
        var n = nBy[rule.key] || stat.values.length;
        return {
          label: rule.label,
          sub: "n=" + n,
          color: rule.color,
          value: stat.mean * metric.scale,
          min: stat.min * metric.scale,
          max: stat.max * metric.scale,
          seeds: stat.values.map(function (v) { return { seed: v.seed, value: v.value * metric.scale }; }),
        };
      });
      Charts.barError(chart, {
        categories: categories,
        precision: spec.precision,
        yLabel: metric.label + "（" + metric.unit + "）",
      });
    });
  }

  /* ---------- 页 6：长期趋势 ---------- */
  function renderP6(ds) {
    var grid = $("#p6-grid");
    if (!ds || !ds.available) { missing(grid, "缺少 730 日趋势数据"); return; }
    var seed = ds.trend_seed;
    var wipChart = addCard(grid, "在制库存（lot，逐日快照）", "种子 " + seed + " · FIFO/SPT/EDD/CR 轨迹高度重合");
    var blockChart = addCard(grid, "分块出厂率（lot/日）", "30 日块，末块按实际长度折算");

    var series = ds.meta.rules.map(function (rule) {
      var rows = ds.trends
        .filter(function (t) { return t.rule === rule.key; })
        .sort(function (a, b) { return a.day - b.day; });
      return {
        name: rule.label,
        color: rule.color,
        points: rows.map(function (r) { return [r.day, r.wip]; }),
      };
    });
    var bands = [];
    series.forEach(function (sr) {
      bands.push({ upper: sr.points.map(function (p) { return [p[0], p[1]]; }), lower: sr.points.map(function (p) { return [p[0], 0]; }), color: sr.color, opacity: 0 });
    });
    Charts.lines(wipChart, {
      series: series,
      xDomain: [0, 730],
      xLabel: "仿真日",
      yLabel: "在制（lot）",
      refLines: [{ x: 365, label: "热身 365 日", color: "#8a94a3" }],
    });

    var blockSeries = ds.meta.rules.map(function (rule) {
      var rows = ds.blocks
        .filter(function (b) { return b.rule === rule.key; })
        .sort(function (a, b) { return a.start_day - b.start_day; });
      return {
        name: rule.label,
        color: rule.color,
        points: rows.map(function (b) { return [(b.start_day + b.end_day) / 2, b.throughput]; }),
        width: 1.7,
      };
    });
    Charts.lines(blockChart, {
      series: blockSeries,
      xDomain: [0, 730],
      xLabel: "仿真日（块中点）",
      yLabel: "出厂率（lot/日）",
      refValue: ds.release_rate,
      refValueLabel: ds.release_rate ? "次年投片均值 " + ds.release_rate.toFixed(2) : "",
    });
  }

  /* ---------- 页 7：数据源 ---------- */
  function renderP7(index) {
    var panel = $("#source-panel");
    if (!panel || !index) return;
    panel.innerHTML = "";
    index.datasets.forEach(function (item) {
      var row = el("div", "sp-row");
      var name = item.id === "weekly-60d" ? "短期 60 日" : "长期 730 日";
      var meta = item.available
        ? item.size_mib + " MiB · 修改于 " + new Date(item.modified * 1000).toLocaleString("zh-CN")
        : "文件缺失";
      row.innerHTML =
        '<span class="sp-key">' + name + "</span>" +
        "<code>" + item.path.replace(/^.*课程设计[\\/]/, "") + "</code>" +
        '<span class="sp-meta">' + meta + "</span>";
      panel.appendChild(row);
    });
    var cmd = el("div", "sp-cmd", "<code>python -m fabcosim.viz</code><span>启动本地只读服务；原始 JSON 不被改写。</span>");
    panel.appendChild(cmd);
  }

  /* ---------- 导航 ---------- */
  function buildPager() {
    var nav = $("#pager");
    PAGES.forEach(function (p, i) {
      var btn = el("button", "pager-btn", '<span class="pb-label">' + p.label + "</span>");
      btn.title = p.sub;
      btn.addEventListener("click", function () { goPage(i); });
      nav.appendChild(btn);
    });
  }

  function activatePage(index) {
    state.page = index;
    var pages = document.querySelectorAll(".page");
    pages.forEach(function (p, i) { p.classList.toggle("is-active", i === index); });
    document.querySelectorAll(".pager-btn").forEach(function (b, i) { b.classList.toggle("is-current", i === index); });
    $("#app").classList.toggle("on-cover", index === 0);
    $("#page-label").textContent = String(index + 1).padStart(2, "0") + " / " + String(PAGES.length).padStart(2, "0") + "  " + PAGES[index].label;
    var mPage = $("#m-page");
    if (mPage) mPage.textContent = index + 1 + " / " + PAGES.length;
    var ds = index >= 1 && index <= 4 ? state.data["weekly-60d"] : index >= 5 && index <= 6 ? state.data["extended-730"] : null;
    $("#footer-source").textContent = ds && ds.available ? ds.meta.source : "";
    var active = pages[index];
    if (active) {
      var charts = active.querySelectorAll(".chart");
      requestAnimationFrame(function () {
        charts.forEach(function (c) { if (c.__draw) c.__draw(); });
      });
    }
  }

  function goPage(index) {
    var n = Math.max(0, Math.min(PAGES.length - 1, index));
    if (history.replaceState) history.replaceState(null, "", "#/" + n);
    else location.hash = "#/" + n;
    activatePage(n);
  }

  function bindMobileNav() {
    var prev = $("#m-prev"), next = $("#m-next");
    if (prev) prev.addEventListener("click", function () { goPage(state.page - 1); });
    if (next) next.addEventListener("click", function () { goPage(state.page + 1); });
  }

  function bindKeys() {
    document.addEventListener("keydown", function (e) {
      if (e.target && /INPUT|TEXTAREA|SELECT/.test(e.target.tagName)) return;
      if (e.key === "ArrowRight" || e.key === " " || e.key === "PageDown") { e.preventDefault(); goPage(state.page + 1); }
      else if (e.key === "ArrowLeft" || e.key === "PageUp") { e.preventDefault(); goPage(state.page - 1); }
      else if (e.key === "Home") { e.preventDefault(); goPage(0); }
      else if (e.key === "End") { e.preventDefault(); goPage(PAGES.length - 1); }
    });
  }

  function bindTheme() {
    $("#theme-toggle").addEventListener("click", function () {
      var root = document.documentElement;
      var next = root.getAttribute("data-theme") === "dark" ? "light" : "dark";
      root.setAttribute("data-theme", next);
      try { localStorage.setItem("fabcosim-theme", next); } catch (err) { /* 忽略 */ }
      requestAnimationFrame(function () {
        document.querySelectorAll(".chart").forEach(function (c) { if (c.__draw) c.__draw(); });
      });
    });
    // 演示时可用 ?theme=dark|light 固定主题，优先于本地记忆。
    var forced = /[?&]theme=(dark|light)/.exec(location.search);
    if (forced) { document.documentElement.setAttribute("data-theme", forced[1]); return; }
    var saved = null;
    try { saved = localStorage.getItem("fabcosim-theme"); } catch (err) { /* 忽略 */ }
    if (saved === "dark") document.documentElement.setAttribute("data-theme", "dark");
  }

  /* ---------- 启动 ---------- */
  function fetchJSON(url) {
    return fetch(url).then(function (res) {
      return res.json().then(function (body) { return { status: res.status, body: body }; });
    });
  }

  function boot() {
    bindTheme();
    bindKeys();
    bindMobileNav();
    buildPager();

    fetchJSON("/api/datasets")
      .then(function (res) {
        state.index = res.body;
        return Promise.all(
          res.body.datasets.map(function (item) {
            return fetchJSON("/api/dataset/" + item.id).then(function (r) { return [item.id, r.body]; });
          })
        );
      })
      .then(function (entries) {
        entries.forEach(function (pair) { state.data[pair[0]] = pair[1]; });
        var weekly = state.data["weekly-60d"];
        var extended = state.data["extended-730"];
        state.rendered = true;

        renderCover(weekly, extended);
        renderP1(weekly);
        renderP2(weekly);
        renderP3(weekly);
        renderP4(weekly);
        renderP5(extended);
        renderP6(extended);
        renderP7(state.index);

        var start = 0;
        var m = /#\/(\d+)/.exec(location.hash || "");
        if (m) start = Number(m[1]);
        activatePage(Math.max(0, Math.min(PAGES.length - 1, start)));
      })
      .catch(function (err) {
        toast("数据加载失败：" + err.message);
        var stage = $("#stage");
        var banner = el("div", "banner", "无法加载分析产物。请确认已用 <code>python -m fabcosim.viz</code> 启动，且 runs/ 下有已验收的 analysis.json。");
        stage.insertBefore(banner, stage.firstChild);
      });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
