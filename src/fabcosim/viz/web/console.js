/* 工厂控制台：单屏监控 730 日长期运行结果，七种调度方案联动切换。 */
(function () {
  "use strict";

  var SVG = "http://www.w3.org/2000/svg";
  var state = { data: null, rule: "fifo" };

  var PHASE_GROUPS = [
    { key: "admission", label: "准入前等待", color: "#5b7b90", phases: ["release", "input_admission"] },
    { key: "queue", label: "机台队列", color: "#c9a24b", phases: ["queue"] },
    { key: "dispatched", label: "已派工等待", color: "#7f9bb0", phases: ["dispatched_wait"] },
    { key: "processing", label: "加工", color: "#3fbfa6", phases: ["processing"] },
    { key: "handling", label: "换型/装卸", color: "#a8764e", phases: ["setup", "loading", "unloading"] },
    { key: "moves", label: "搬运/后处理", color: "#8a6f9e", phases: ["transfer", "postprocess"] },
  ];

  var KPIS = [
    { label: "累计出厂", unit: "lot", prec: 0, better: "high",
      get: function (d, r) { return metric(d, r, "completed"); } },
    { label: "第二年出厂率", unit: "lot/日", prec: 2, better: "high",
      get: function (d, r) { return metric(d, r, "main_throughput"); } },
    { label: "第二年平均在制", unit: "lot", prec: 0, better: "low",
      get: function (d, r) { return metric(d, r, "main_mean_wip"); } },
    { label: "末 30 日在制净增", unit: "lot", prec: 0, better: "low", signed: true,
      get: function (d, r) { return metric(d, r, "tail_wip_growth"); } },
    { label: "CQT 违例率", unit: "%", prec: 2, better: "low",
      get: function (d, r) { return metric(d, r, "main_cqt_rate", 100); } },
    { label: "完成 lot 逾期率", unit: "%", prec: 1, better: "low",
      get: function (d, r) { return lateRate(d, r); } },
  ];

  var TABLE_COLS = [
    { label: "累计出厂", prec: 0, get: function (d, r) { return metric(d, r, "completed"); } },
    { label: "出厂率", unit: "lot/日", prec: 2, get: function (d, r) { return metric(d, r, "main_throughput"); } },
    { label: "平均在制", unit: "lot", prec: 0, get: function (d, r) { return metric(d, r, "main_mean_wip"); } },
    { label: "末30日净增", unit: "lot", prec: 0, get: function (d, r) { return metric(d, r, "tail_wip_growth"); } },
    { label: "CQT", unit: "%", prec: 2, get: function (d, r) { return metric(d, r, "main_cqt_rate", 100); } },
    { label: "逾期率", unit: "%", prec: 1, get: function (d, r) { return lateRate(d, r); } },
  ];

  function $(sel) { return document.querySelector(sel); }

  function el(tag, cls, html) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (html !== undefined) node.innerHTML = html;
    return node;
  }

  function mk(tag, attrs, parent) {
    var node = document.createElementNS(SVG, tag);
    if (attrs) {
      for (var k in attrs) if (attrs[k] !== undefined && attrs[k] !== null) node.setAttribute(k, attrs[k]);
    }
    if (parent) parent.appendChild(node);
    return node;
  }

  function fmt(value, prec) {
    var p = prec || 0;
    var abs = Math.abs(value);
    var digits = abs >= 1000 ? 0 : p;
    return value.toLocaleString("zh-CN", { minimumFractionDigits: digits, maximumFractionDigits: digits });
  }

  function fmtSigned(value, prec) {
    return (value > 0 ? "+" : "") + fmt(value, prec);
  }

  function metric(data, rule, key, scale) {
    var stat = data.series[rule][key];
    return stat.mean * (scale || 1);
  }

  function lateRate(data, rule) {
    var rows = data.priority.filter(function (p) { return p.rule === rule; });
    var completed = rows.reduce(function (a, p) { return a + p.completed; }, 0);
    var late = rows.reduce(function (a, p) { return a + p.late; }, 0);
    return completed ? (late / completed) * 100 : 0;
  }

  function sampleN(data, rule) {
    var item = data.coverage.filter(function (c) { return c.rule === rule; })[0];
    return item ? item.n : 0;
  }

  function ruleLabel(data, key) {
    var rule = data.meta.rules.filter(function (r) { return r.key === key; })[0];
    return rule ? rule.label : key;
  }

  function ruleColor(data, key) {
    var rule = data.meta.rules.filter(function (r) { return r.key === key; })[0];
    return rule ? rule.color : "#3fd6c0";
  }

  /* ---------- 方案条 ---------- */
  function renderRuleBar(data, active) {
    var bar = $("#rule-bar");
    bar.innerHTML = "";
    data.meta.rules.forEach(function (rule) {
      var btn = el("button", "rule-btn");
      btn.style.setProperty("--c", rule.color);
      btn.innerHTML = "<i></i><span>" + rule.label + "</span>";
      btn.classList.toggle("is-active", rule.key === active);
      btn.addEventListener("click", function () { setRule(rule.key); });
      bar.appendChild(btn);
    });
  }

  /* ---------- KPI ---------- */
  function renderKpis(data, active) {
    var host = $("#kpi-row");
    host.innerHTML = "";
    KPIS.forEach(function (kpi) {
      var value = kpi.get(data, active);
      var base = kpi.get(data, "fifo");
      var n = sampleN(data, active);

      var deltaText = "相对 FIFO 基线";
      var deltaCls = "flat";
      if (active !== "fifo" && Math.abs(base) > 1e-9) {
        var change = ((value - base) / Math.abs(base)) * 100;
        var good = kpi.better === "high" ? change > 0 : change < 0;
        deltaCls = Math.abs(change) < 0.05 ? "flat" : good ? "good" : "bad";
        deltaText = (deltaCls === "good" ? "▲ " : deltaCls === "bad" ? "▼ " : "") +
          fmtSigned(change, 1) + "% vs FIFO";
      }

      var accent = deltaCls === "good" ? "var(--green)" : deltaCls === "bad" ? "var(--red)" : "var(--cyan)";
      var card = el("div", "kpi");
      card.style.setProperty("--accent", accent);
      card.innerHTML =
        '<span class="kpi-label">' + kpi.label + "</span>" +
        '<span class="kpi-value">' + (kpi.signed ? fmtSigned(value, kpi.prec) : fmt(value, kpi.prec)) + "</span>" +
        '<span class="kpi-meta"><span class="kpi-unit">' + kpi.unit + " · n=" + n + "</span>" +
        '<span class="kpi-delta ' + deltaCls + '">' + deltaText + "</span></span>";
      host.appendChild(card);
    });
  }

  /* ---------- 趋势 ---------- */
  function drawTrend(data, active) {
    var host = $("#trend-chart");
    var W = host.clientWidth;
    var H = host.clientHeight;
    host.innerHTML = "";
    if (W < 20 || H < 20) return;

    var m = { top: 16, right: 14, bottom: 24, left: 58 };
    var iw = W - m.left - m.right;
    var ih = H - m.top - m.bottom;
    var svg = mk("svg", { viewBox: "0 0 " + W + " " + H, width: "100%", height: "100%" }, host);
    svg.style.display = "block";
    var g = mk("g", { transform: "translate(" + m.left + "," + m.top + ")" }, svg);

    var maxWip = 0;
    data.trends.forEach(function (t) { if (t.wip > maxWip) maxWip = t.wip; });

    var raw = maxWip / 5;
    var mag = Math.pow(10, Math.floor(Math.log10(raw)));
    var norm = raw / mag;
    var ladder = [1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10];
    var pick = ladder.filter(function (v) { return v >= norm; })[0] || 10;
    var step = pick * mag;
    var yMax = Math.ceil((maxWip * 1.02) / step) * step;
    var ticks = Math.round(yMax / step);

    function xOf(day) { return (day / 730) * iw; }
    function yOf(v) { return ih - (v / yMax) * ih; }

    for (var i = 0; i <= ticks; i++) {
      var val = step * i;
      var y = yOf(val);
      mk("line", { x1: 0, y1: y, x2: iw, y2: y, stroke: "#17303d", "stroke-width": 1 }, g);
      mk("text", {
        x: -8, y: y + 3.5, "text-anchor": "end", fill: "#47616f",
        "font-size": 10, "font-family": "var(--mono)",
      }, g).textContent = val >= 1000 ? Math.round(val / 1000) + "k" : Math.round(val);
    }

    for (var d = 0; d <= 700; d += 100) {
      var x = xOf(d);
      mk("line", { x1: x, y1: 0, x2: x, y2: ih, stroke: "#122630", "stroke-width": 1 }, g);
      mk("text", {
        x: x, y: ih + 15, "text-anchor": "middle", fill: "#47616f", "font-size": 10,
        "font-family": "var(--mono)",
      }, g).textContent = d;
    }

    var warmX = xOf(365);
    mk("line", {
      x1: warmX, y1: 0, x2: warmX, y2: ih, stroke: "#6f8a9b",
      "stroke-width": 1, "stroke-dasharray": "3 4", opacity: 0.6,
    }, g);
    mk("text", { x: warmX + 5, y: 11, fill: "#6f8a9b", "font-size": 10 }, g).textContent = "热身结束";

    var defs = mk("defs", null, svg);
    var grad = mk("linearGradient", { id: "wipFill", x1: 0, y1: 0, x2: 0, y2: 1 }, defs);
    mk("stop", { offset: "0%", "stop-color": ruleColor(data, active), "stop-opacity": 0.16 }, grad);
    mk("stop", { offset: "100%", "stop-color": ruleColor(data, active), "stop-opacity": 0 }, grad);

    var activePoints = null;

    data.meta.rules.forEach(function (rule) {
      var rows = data.trends
        .filter(function (t) { return t.rule === rule.key; })
        .sort(function (a, b) { return a.day - b.day; });
      if (!rows.length) return;
      var dPath = rows.map(function (r, i) {
        return (i ? "L" : "M") + xOf(r.day).toFixed(1) + "," + yOf(r.wip).toFixed(1);
      }).join("");
      var isActive = rule.key === active;

      if (isActive) {
        activePoints = rows;
        mk("path", {
          d: dPath + "L" + xOf(rows[rows.length - 1].day) + "," + ih + "L" + xOf(rows[0].day) + "," + ih + "Z",
          fill: "url(#wipFill)",
        }, g);
      }

      var path = mk("path", {
        d: dPath, fill: "none", stroke: rule.color,
        "stroke-width": isActive ? 2.2 : 1.2,
        opacity: isActive ? 1 : 0.45,
        "stroke-linejoin": "round", "stroke-linecap": "round",
      }, g);
      if (isActive) path.style.filter = "drop-shadow(0 0 5px " + rule.color + ")";
      var title = mk("title", null, path);
      title.textContent = rule.label + " · 第 730 日在制 " + fmt(Math.round(rows[rows.length - 1].wip)) + " lot";
    });

    if (activePoints && activePoints.length) {
      var last = activePoints[activePoints.length - 1];
      mk("circle", {
        cx: xOf(last.day), cy: yOf(last.wip), r: 3.2,
        fill: ruleColor(data, active),
      }, g).style.filter = "drop-shadow(0 0 6px " + ruleColor(data, active) + ")";
    }
  }

  /* ---------- 设备组 ---------- */
  function renderDevices(data, active) {
    var host = $("#device-list");
    host.innerHTML = "";
    var rows = data.devices.filter(function (d) { return d.rule === active; }).slice(0, 7);
    if (!rows.length) {
      host.appendChild(el("div", "empty", "该方案无设备组数据"));
      return;
    }

    var maxWip = Math.max.apply(null, rows.map(function (d) { return d.waiting_mean_wip; })) || 1;

    rows.forEach(function (d) {
      var segs = [
        { label: "加工", value: d.processing, color: "#3fbfa6" },
        { label: "换型", value: d.setup, color: "#c9a24b" },
        { label: "装卸", value: d.loading + d.unloading, color: "#6f8fb0" },
        { label: "停机", value: d.down, color: "#b65c5c" },
        { label: "空闲", value: d.idle, color: "#2c3d4b" },
      ];
      var total = segs.reduce(function (a, s) { return a + s.value; }, 0) || 1;

      var row = el("div", "device-row" + (d.waiting_mean_wip >= 5000 ? " is-hot" : ""));
      var name = el("span", "dev-name", d.group);
      name.title = d.group + " · " + d.tools + " 台 · 等待 WIP " + fmt(Math.round(d.waiting_mean_wip));

      // 条长按积压量级缩放（开方压缩），色段仍是机台时间构成。
      var bar = el("span", "dev-bar");
      bar.style.width = (16 + 84 * Math.sqrt(d.waiting_mean_wip / maxWip)).toFixed(1) + "%";
      segs.forEach(function (s) {
        var piece = document.createElement("i");
        piece.style.width = ((s.value / total) * 100).toFixed(2) + "%";
        piece.style.background = s.color;
        piece.title = d.group + " · " + s.label + " " + (s.value * 100).toFixed(1) + "%";
        bar.appendChild(piece);
      });

      var wipCls = d.waiting_mean_wip >= 10000 ? " hot" : d.waiting_mean_wip >= 3000 ? " warm" : "";
      var wip = el("span", "dev-wip" + wipCls, fmt(Math.round(d.waiting_mean_wip)));
      row.appendChild(name);
      row.appendChild(bar);
      row.appendChild(wip);
      host.appendChild(row);
    });

    var note = $("#device-note");
    if (note) {
      note.textContent = "条长=积压 · 色段=工况 · n=" + sampleN(data, active);
    }
  }

  /* ---------- 阶段构成 ---------- */
  function renderPhases(data, active) {
    var rows = data.phase_mix.filter(function (p) { return p.rule === active; });
    var groups = PHASE_GROUPS.map(function (group) {
      var value = rows
        .filter(function (r) { return group.phases.indexOf(r.phase) >= 0; })
        .reduce(function (a, r) { return a + r.mean_wip; }, 0);
      return { label: group.label, color: group.color, value: value };
    });
    var total = groups.reduce(function (a, g) { return a + g.value; }, 0) || 1;

    var bar = $("#phase-bar");
    bar.innerHTML = "";
    groups.forEach(function (g) {
      var share = (g.value / total) * 100;
      var seg = el("div", "phase-seg", share >= 7 ? Math.round(share) + "%" : "");
      seg.style.width = share.toFixed(2) + "%";
      seg.style.background = g.color;
      seg.title = g.label + " · " + fmt(Math.round(g.value)) + " lot · " + share.toFixed(1) + "%";
      bar.appendChild(seg);
    });

    var legend = $("#phase-legend");
    legend.innerHTML = "";
    groups.forEach(function (g) {
      var li = el("li");
      li.innerHTML = '<i style="--c:' + g.color + '"></i>' + g.label +
        "<b>" + fmt(Math.round(g.value)) + "</b>";
      legend.appendChild(li);
    });
  }

  /* ---------- 对照表 ---------- */
  function renderTable(data, active) {
    var table = $("#rule-table");
    var head = "<thead><tr><th>方案</th>" +
      TABLE_COLS.map(function (c) {
        return "<th>" + c.label + (c.unit ? " / " + c.unit : "") + "</th>";
      }).join("") + "<th>n</th></tr></thead>";

    var body = "<tbody>";
    data.meta.rules.forEach(function (rule) {
      var cells = TABLE_COLS.map(function (c) {
        return "<td>" + fmt(c.get(data, rule.key), c.prec) + "</td>";
      }).join("");
      body += '<tr data-rule="' + rule.key + '" class="' + (rule.key === active ? "is-active" : "") + '">' +
        "<td>" + rule.label + "</td>" + cells +
        "<td>" + sampleN(data, rule.key) + "</td></tr>";
    });
    body += "</tbody>";

    table.innerHTML = head + body;
    table.querySelectorAll("tbody tr").forEach(function (tr) {
      tr.addEventListener("click", function () { setRule(tr.getAttribute("data-rule")); });
    });
  }

  /* ---------- 底部 ---------- */
  function renderFoot(data, active) {
    var tail = metric(data, active, "tail_wip_growth");
    var late = lateRate(data, active);
    var cqt = metric(data, active, "main_cqt_rate", 100);
    $("#foot-source").textContent = data.meta.source;
    $("#foot-alert").textContent =
      "末 30 日在制净增 " + fmtSigned(Math.round(tail), 0) + " lot · " +
      "完成 lot 逾期率 " + late.toFixed(1) + "% · CQT 违例率 " + cqt.toFixed(2) + "%";
  }

  function renderTrendFoot(data, active) {
    var rows = data.trends
      .filter(function (t) { return t.rule === active; })
      .sort(function (a, b) { return a.day - b.day; });
    var foot = $("#trend-foot");
    if (!rows.length) { foot.textContent = ""; return; }
    var last = rows[rows.length - 1];
    var start = rows[0];
    foot.textContent =
      ruleLabel(data, active) + " · 第 730 日在制 " + fmt(Math.round(last.wip)) + " lot · " +
      "较第 0 日 " + fmtSigned(Math.round(last.wip - start.wip), 0) + " lot · " +
      "投片均值 " + (data.release_rate ? data.release_rate.toFixed(2) : "—") + " lot/日";
  }

  /* ---------- 交互 ---------- */
  function setRule(key) {
    if (!state.data || key === state.rule) return;
    state.rule = key;
    document.querySelectorAll(".rule-btn").forEach(function (btn, i) {
      btn.classList.toggle("is-active", state.data.meta.rules[i].key === key);
    });
    document.querySelectorAll("#rule-table tbody tr").forEach(function (tr) {
      tr.classList.toggle("is-active", tr.getAttribute("data-rule") === key);
    });
    renderKpis(state.data, key);
    drawTrend(state.data, key);
    renderDevices(state.data, key);
    renderPhases(state.data, key);
    renderTrendFoot(state.data, key);
    renderFoot(state.data, key);
  }

  function tickClock() {
    var now = new Date();
    $("#hud-clock").textContent = now.toLocaleTimeString("zh-CN", { hour12: false });
  }

  function showToast(message) {
    var node = $("#toast");
    node.textContent = message;
    node.hidden = false;
    clearTimeout(node.__timer);
    node.__timer = setTimeout(function () { node.hidden = true; }, 3200);
  }

  function boot() {
    tickClock();
    setInterval(tickClock, 1000);

    fetch("/api/dataset/extended-730")
      .then(function (res) { return res.json(); })
      .then(function (data) {
        if (!data.available) {
          showToast("分析产物不可用：" + (data.error || "缺少文件"));
          $("#foot-source").textContent = "runs/20261118-extended-review/analysis-01/analysis.json";
          return;
        }
        state.data = data;
        var wanted = /[?&]rule=([a-z0-9_]+)/.exec(location.search);
        var known = data.meta.rules.map(function (r) { return r.key; });
        var active = wanted && known.indexOf(wanted[1]) >= 0 ? wanted[1] : "fifo";
        state.rule = active;

        $("#hud-seed").textContent = String(data.trend_seed);
        renderRuleBar(data, active);
        renderKpis(data, active);
        drawTrend(data, active);
        renderDevices(data, active);
        renderPhases(data, active);
        renderTable(data, active);
        renderTrendFoot(data, active);
        renderFoot(data, active);

        var ro = new ResizeObserver(function () {
          if (state.__raf) return;
          state.__raf = requestAnimationFrame(function () {
            state.__raf = 0;
            if (state.data) drawTrend(state.data, state.rule);
          });
        });
        ro.observe($("#trend-chart"));
      })
      .catch(function (err) {
        showToast("加载失败：" + err.message);
      });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
