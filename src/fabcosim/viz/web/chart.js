/* 手写 SVG 图表引擎：柱状+极值、折线+范围带、堆叠柱、配对斜线、散点。
   不引入任何外部库，离线答辩可用。所有图随容器 ResizeObserver 重绘。 */
(function (global) {
  "use strict";

  const NS = "http://www.w3.org/2000/svg";
  const FONT = '"Segoe UI","Microsoft YaHei","PingFang SC","Noto Sans SC",system-ui,sans-serif';
  const SEED_MARKERS = ["circle", "square", "triangle"];

  function s(tag, attrs, parent) {
    const node = document.createElementNS(NS, tag);
    if (attrs) {
      for (const k in attrs) if (attrs[k] !== undefined && attrs[k] !== null) node.setAttribute(k, attrs[k]);
    }
    if (parent) parent.appendChild(node);
    return node;
  }

  function cssVar(host, name, fallback) {
    const v = getComputedStyle(host).getPropertyValue(name).trim();
    return v || fallback;
  }

  function fmt(value, precision) {
    const p = precision || 0;
    const abs = Math.abs(value);
    const digits = abs >= 1000 ? 0 : p;
    return value.toLocaleString("zh-CN", {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    });
  }

  function fmtAxis(value) {
    const abs = Math.abs(value);
    if (abs >= 10000) return (value / 1000).toFixed(value % 1000 === 0 ? 0 : 1) + "k";
    if (abs >= 100) return Math.round(value).toLocaleString("zh-CN");
    if (abs >= 10) return String(Math.round(value * 10) / 10);
    return String(Math.round(value * 100) / 100);
  }

  function niceScale(min, max, count) {
    if (!isFinite(min) || !isFinite(max)) return { lo: 0, hi: 1, ticks: [0, 1] };
    if (max === min) {
      const pad = Math.abs(max) * 0.1 || 1;
      min -= pad;
      max += pad;
    }
    const raw = (max - min) / (count || 5);
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const norm = raw / mag;
    const step = (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 5 ? 5 : 10) * mag;
    const lo = Math.floor(min / step) * step;
    const hi = Math.ceil(max / step) * step;
    const ticks = [];
    for (let v = lo; v <= hi + step * 0.5; v += step) ticks.push(Math.round(v / step) * step);
    return { lo, hi, ticks };
  }

  function mount(host, draw) {
    host.__draw = draw;
    if (!host.__ro) {
      host.__ro = new ResizeObserver(function () {
        if (host.__raf) return;
        host.__raf = requestAnimationFrame(function () {
          host.__raf = 0;
          if (host.__draw) host.__draw();
        });
      });
      host.__ro.observe(host);
    }
    draw();
  }

  function clear(host) {
    while (host.firstChild) host.removeChild(host.firstChild);
  }

  function surface(host, margins) {
    const W = Math.max(host.clientWidth, 220);
    const H = Math.max(host.clientHeight, 160);
    clear(host);
    const svg = s("svg", {
      viewBox: "0 0 " + W + " " + H,
      width: "100%",
      height: "100%",
      role: "img",
      style: "display:block;overflow:visible",
    }, host);
    svg.style.fontFamily = FONT;
    const m = Object.assign({ top: 26, right: 20, bottom: 42, left: 62 }, margins);
    const iw = Math.max(W - m.left - m.right, 40);
    const ih = Math.max(H - m.top - m.bottom, 40);
    const g = s("g", { transform: "translate(" + m.left + "," + m.top + ")" }, svg);
    return {
      svg: svg, g: g, W: W, H: H, m: m, iw: iw, ih: ih,
      ink: cssVar(host, "--ink", "#17202b"),
      muted: cssVar(host, "--muted", "#5f6b7a"),
      grid: cssVar(host, "--grid", "#e4e7ec"),
      panel: cssVar(host, "--panel", "#ffffff"),
    };
  }

  function yAxis(ctx, scale, label) {
    const ink = ctx.ink, muted = ctx.muted;
    scale.ticks.forEach(function (t) {
      const y = ctx.ih - ((t - scale.lo) / (scale.hi - scale.lo)) * ctx.ih;
      s("line", { x1: 0, y1: y, x2: ctx.iw, y2: y, stroke: ctx.grid, "stroke-width": 1 }, ctx.g);
      s("text", {
        x: -10, y: y + 4, "text-anchor": "end", fill: muted, "font-size": 11.5,
      }, ctx.g).textContent = fmtAxis(t);
    });
    if (label) {
      s("text", {
        x: -ctx.m.left + 2, y: -12, fill: muted, "font-size": 11.5, "text-anchor": "start",
      }, ctx.g).textContent = label;
    }
  }

  function xAxis(ctx, categories) {
    const n = categories.length;
    const step = ctx.iw / n;
    categories.forEach(function (cat, i) {
      const cx = step * (i + 0.5);
      const node = s("text", {
        x: cx, y: ctx.ih + 18, "text-anchor": "middle",
        fill: ctx.ink, "font-size": 12, "font-weight": 500,
      }, ctx.g);
      node.textContent = cat.label;
      if (cat.sub) {
        s("text", {
          x: cx, y: ctx.ih + 34, "text-anchor": "middle",
          fill: ctx.muted, "font-size": 10.5,
        }, ctx.g).textContent = cat.sub;
      }
      if (cat.title) {
        const t = s("title", null, node);
        t.textContent = cat.title;
      }
    });
    s("line", { x1: 0, y1: ctx.ih, x2: ctx.iw, y2: ctx.ih, stroke: ctx.grid, "stroke-width": 1.4 }, ctx.g);
  }

  function marker(parent, kind, cx, cy, fill, r) {
    if (kind === "square") {
      return s("rect", { x: cx - r, y: cy - r, width: r * 2, height: r * 2, fill: fill }, parent);
    }
    if (kind === "triangle") {
      return s("polygon", {
        points: [cx, cy - r * 1.15, cx + r * 1.1, cy + r * 0.85, cx - r * 1.1, cy + r * 0.85].join(" "),
        fill: fill,
      }, parent);
    }
    return s("circle", { cx: cx, cy: cy, r: r, fill: fill }, parent);
  }

  function legendRow(ctx, items, y, columns, shape) {
    const cols = columns || Math.min(items.length, 4);
    const colW = ctx.iw / cols;
    const rows = Math.ceil(items.length / cols);
    items.forEach(function (item, i) {
      const col = i % cols;
      const row = Math.floor(i / cols);
      const x = col * colW;
      const yy = y + row * 17;
      if (shape === "square") {
        s("rect", { x: x + 2, y: yy - 8, width: 9, height: 9, rx: 1.5, fill: item.color }, ctx.g);
      } else if (shape === "dot") {
        s("circle", { cx: x + 5, cy: yy - 4, r: 3.6, fill: item.color }, ctx.g);
      } else {
        s("line", {
          x1: x + 2, y1: yy - 4, x2: x + 16, y2: yy - 4, stroke: item.color, "stroke-width": 2.4,
        }, ctx.g);
      }
      s("text", { x: x + 21, y: yy, fill: ctx.muted, "font-size": 11 }, ctx.g).textContent = item.name;
    });
    return rows;
  }

  /* ---------- 柱状 + 极值 + 逐种子点 ---------- */
  function barError(host, cfg) {
    mount(host, function () {
      const cats = cfg.categories;
      const precision = cfg.precision || 0;
      const values = [];
      cats.forEach(function (c) {
        values.push(c.value);
        if (c.min !== undefined) values.push(c.min);
        if (c.max !== undefined) values.push(c.max);
      });
      const hi = Math.max.apply(null, values);
      const scale = niceScale(0, hi * 1.06, 5);
      const ctx = surface(host, { bottom: 46 });
      yAxis(ctx, scale, cfg.yLabel);

      const n = cats.length;
      const step = ctx.iw / n;
      const barW = Math.min(step * 0.52, 86);
      const yOf = function (v) { return ctx.ih - ((v - scale.lo) / (scale.hi - scale.lo)) * ctx.ih; };

      cats.forEach(function (cat, i) {
        const cx = step * (i + 0.5);
        const x0 = cx - barW / 2;
        const yv = yOf(cat.value);
        const rect = s("rect", {
          x: x0, y: yv, width: barW, height: Math.max(ctx.ih - yv, 1),
          fill: cat.color, rx: 3, opacity: 0.92,
        }, ctx.g);
        const title = s("title", null, rect);
        title.textContent = cat.label + " · 均值 " + fmt(cat.value, precision);

        if (cat.min !== undefined && cat.max !== undefined) {
          const yMin = yOf(cat.min), yMax = yOf(cat.max);
          const xm = cx;
          s("line", { x1: xm, y1: yMin, x2: xm, y2: yMax, stroke: ctx.ink, "stroke-width": 1.1, opacity: 0.5 }, ctx.g);
          s("line", { x1: xm - 3, y1: yMin, x2: xm + 3, y2: yMin, stroke: ctx.ink, "stroke-width": 1.1, opacity: 0.5 }, ctx.g);
          s("line", { x1: xm - 3, y1: yMax, x2: xm + 3, y2: yMax, stroke: ctx.ink, "stroke-width": 1.1, opacity: 0.5 }, ctx.g);
        }

        (cat.seeds || []).forEach(function (pt, k) {
          const off = (k - (cat.seeds.length - 1) / 2) * 8;
          const kind = marker(ctx.g, SEED_MARKERS[k % 3], cx + off, yOf(pt.value), ctx.ink, 3.2);
          const t = s("title", null, kind);
          t.textContent = cat.label + " · 种子 " + pt.seed + " · " + fmt(pt.value, precision);
        });

        // 数值标签压在柱内顶部，加背景色描边，避免与柱色和种子点冲突。
        const label = s("text", {
          x: cx, y: Math.min(yv + 15, ctx.ih - 5), "text-anchor": "middle", fill: ctx.ink,
          "font-size": 12, "font-weight": 600,
          style: "paint-order:stroke;stroke:" + ctx.panel + ";stroke-width:3px;stroke-linejoin:round",
        }, ctx.g);
        label.textContent = fmt(cat.value, precision);
      });

      xAxis(ctx, cats);
      if (cfg.note) {
        s("text", {
          x: ctx.iw, y: ctx.ih + 34, "text-anchor": "end",
          fill: ctx.muted, "font-size": 10.5,
        }, ctx.g).textContent = cfg.note;
      }
    });
  }

  /* ---------- 折线 / 多系列 / 范围带 ---------- */
  function lines(host, cfg) {
    mount(host, function () {
      const series = cfg.series;
      let xMin = cfg.xDomain ? cfg.xDomain[0] : Infinity;
      let xMax = cfg.xDomain ? cfg.xDomain[1] : -Infinity;
      let yMin = Infinity, yMax = -Infinity;
      series.forEach(function (sr) {
        sr.points.forEach(function (p) {
          xMin = Math.min(xMin, p[0]); xMax = Math.max(xMax, p[0]);
          yMin = Math.min(yMin, p[1]); yMax = Math.max(yMax, p[1]);
        });
        (sr.band || []).forEach(function (p) {
          yMin = Math.min(yMin, p[1]); yMax = Math.max(yMax, p[2]);
        });
      });
      if (!isFinite(xMin)) { xMin = 0; xMax = 1; }
      if (!isFinite(yMin)) { yMin = 0; yMax = 1; }

      const legendRows = cfg.legend === false ? 0 : Math.ceil(series.length / 4);
      const bottom = 46 + legendRows * 18 + (legendRows ? 6 : 0);
      const ctx = surface(host, { bottom: bottom, left: 64, right: 22 });
      const yScale = niceScale(yMin, yMax, 5);
      yAxis(ctx, yScale, cfg.yLabel);

      const xOf = function (v) { return ((v - xMin) / (xMax - xMin || 1)) * ctx.iw; };
      const yOf = function (v) { return ctx.ih - ((v - yScale.lo) / (yScale.hi - yScale.lo)) * ctx.ih; };

      const xTickScale = niceScale(xMin, xMax, 8);
      const xTicks = xTickScale.ticks.filter(function (t) { return t >= xMin - 1e-9 && t <= xMax + 1e-9; });
      xTicks.forEach(function (t) {
        const x = xOf(t);
        s("line", { x1: x, y1: 0, x2: x, y2: ctx.ih, stroke: ctx.grid, "stroke-width": 1, opacity: 0.6 }, ctx.g);
        s("text", {
          x: x, y: ctx.ih + 18, "text-anchor": "middle", fill: ctx.muted, "font-size": 11,
        }, ctx.g).textContent = cfg.xFormat ? cfg.xFormat(t) : Math.round(t).toLocaleString("zh-CN");
      });

      (cfg.bands || []).forEach(function (band) {
        const up = band.upper.map(function (p) { return xOf(p[0]) + "," + yOf(p[1]); });
        const down = band.lower.slice().reverse().map(function (p) { return xOf(p[0]) + "," + yOf(p[1]); });
        s("path", {
          d: "M" + up.concat(down).join("L") + "Z",
          fill: band.color, opacity: band.opacity !== undefined ? band.opacity : 0.1,
        }, ctx.g);
      });

      (cfg.refLines || []).forEach(function (r) {
        const x = xOf(r.x);
        s("line", {
          x1: x, y1: 0, x2: x, y2: ctx.ih,
          stroke: r.color || ctx.muted, "stroke-width": 1.1,
          "stroke-dasharray": r.dash || "4 4", opacity: 0.85,
        }, ctx.g);
        if (r.label) {
          s("text", {
            x: x + 4, y: 12, fill: r.color || ctx.muted, "font-size": 10.5,
          }, ctx.g).textContent = r.label;
        }
      });

      series.forEach(function (sr) {
        const d = sr.points.map(function (p, i) {
          return (i ? "L" : "M") + xOf(p[0]).toFixed(1) + "," + yOf(p[1]).toFixed(1);
        }).join("");
        const path = s("path", {
          d: d, fill: "none", stroke: sr.color, "stroke-width": sr.width || 2,
          "stroke-linejoin": "round", "stroke-linecap": "round",
          opacity: sr.dim ? 0.55 : 1,
        }, ctx.g);
        const t = s("title", null, path);
        t.textContent = sr.name;
      });

      if (cfg.refValue !== undefined) {
        const y = yOf(cfg.refValue);
        s("line", {
          x1: 0, y1: y, x2: ctx.iw, y2: y, stroke: ctx.ink, "stroke-width": 1.2,
          "stroke-dasharray": "5 4", opacity: 0.7,
        }, ctx.g);
        s("text", {
          x: ctx.iw - 2, y: y - 5, "text-anchor": "end", fill: ctx.ink, "font-size": 10.5,
        }, ctx.g).textContent = cfg.refValueLabel;
      }

      s("text", {
        x: ctx.iw / 2, y: ctx.ih + 36, "text-anchor": "middle", fill: ctx.muted, "font-size": 11,
      }, ctx.g).textContent = cfg.xLabel || "";

      if (legendRows) {
        legendRow(ctx, series, ctx.ih + 56, 4);
      }
    });
  }

  /* ---------- 堆叠柱 ---------- */
  function stacked(host, cfg) {
    mount(host, function () {
      const cats = cfg.categories;
      const totals = cats.map(function (_, i) {
        return cfg.stacks.reduce(function (sum, st) { return sum + (st.values[i] || 0); }, 0);
      });
      const scale = niceScale(0, Math.max.apply(null, totals) * 1.04, 5);
      const ctx = surface(host, { bottom: 78 });
      yAxis(ctx, scale, cfg.yLabel);

      const n = cats.length;
      const step = ctx.iw / n;
      const barW = Math.min(step * 0.55, 90);
      const yOf = function (v) { return ctx.ih - (v / scale.hi) * ctx.ih; };

      cats.forEach(function (label, i) {
        const cx = step * (i + 0.5);
        let acc = 0;
        cfg.stacks.forEach(function (st) {
          const v = st.values[i] || 0;
          const y0 = yOf(acc + v);
          const y1 = yOf(acc);
          const rect = s("rect", {
            x: cx - barW / 2, y: y0, width: barW, height: Math.max(y1 - y0, 0.6),
            fill: st.color, opacity: 0.94,
          }, ctx.g);
          const t = s("title", null, rect);
          t.textContent = label + " · " + st.name + " · " + fmt(v, 0) + " lot";
          acc += v;
        });
        s("text", {
          x: cx, y: yOf(acc) - 8, "text-anchor": "middle", fill: ctx.ink,
          "font-size": 11.5, "font-weight": 600,
        }, ctx.g).textContent = fmt(totals[i], 0);
      });

      xAxis(ctx, cats.map(function (l) { return { label: l }; }));
      legendRow(ctx, cfg.stacks, ctx.ih + 46, 3, "square");
      s("text", {
        x: ctx.iw, y: ctx.ih + 66, "text-anchor": "end", fill: ctx.muted, "font-size": 10.5,
      }, ctx.g).textContent = "单位：lot";
    });
  }

  /* ---------- 配对斜线 ---------- */
  function placeLabels(ctx, items, gap) {
    items.sort(function (a, b) { return a.y - b.y; });
    let last = -Infinity;
    items.forEach(function (it) { it.ly = Math.max(it.y, last + gap); last = it.ly; });
    const end = items[items.length - 1];
    if (end && end.ly > ctx.ih - 2) {
      const shift = end.ly - (ctx.ih - 2);
      items.forEach(function (it) { it.ly -= shift; });
    }
  }

  function drawEndLabel(ctx, it, x, anchor) {
    if (Math.abs(it.ly - it.y) > 0.8) {
      s("line", {
        x1: x + (anchor === "end" ? 6 : -6), y1: it.y, x2: x, y2: it.ly,
        stroke: it.color, "stroke-width": 0.9, opacity: 0.65,
      }, ctx.g);
    }
    s("text", {
      x: x, y: it.ly + 4, "text-anchor": anchor, fill: it.color, "font-size": 11.5,
    }, ctx.g).textContent = it.text;
  }

  function slope(host, cfg) {
    mount(host, function () {
      const pairs = cfg.pairs;
      const hasLegend = !!(cfg.legend && cfg.legend.length);
      let lo = Infinity, hi = -Infinity;
      pairs.forEach(function (p) {
        lo = Math.min(lo, p.left, p.right);
        hi = Math.max(hi, p.left, p.right);
      });
      const pad = (hi - lo) * 0.18 || Math.abs(hi) * 0.1 || 1;
      const scale = niceScale(Math.max(0, lo - pad), hi + pad, 4);
      const ctx = surface(host, { bottom: hasLegend ? 60 : 44, left: 60, right: 74 });
      yAxis(ctx, scale, cfg.yLabel);

      const xL = ctx.iw * 0.28;
      const xR = ctx.iw * 0.72;
      const yOf = function (v) { return ctx.ih - ((v - scale.lo) / (scale.hi - scale.lo)) * ctx.ih; };

      [xL, xR].forEach(function (x, i) {
        s("line", { x1: x, y1: 0, x2: x, y2: ctx.ih, stroke: ctx.grid, "stroke-width": 1.2 }, ctx.g);
        s("text", {
          x: x, y: ctx.ih + 22, "text-anchor": "middle", fill: ctx.ink, "font-size": 12, "font-weight": 600,
        }, ctx.g).textContent = i === 0 ? cfg.leftLabel : cfg.rightLabel;
      });

      const leftLabels = [];
      const rightLabels = [];
      pairs.forEach(function (p) {
        const path = s("path", {
          d: "M" + xL + "," + yOf(p.left) + "L" + xR + "," + yOf(p.right),
          stroke: p.color, "stroke-width": 1.8, fill: "none", opacity: 0.9,
        }, ctx.g);
        s("title", null, path).textContent = "种子 " + p.seed;
        s("circle", { cx: xL, cy: yOf(p.left), r: 4, fill: p.color }, ctx.g);
        s("circle", { cx: xR, cy: yOf(p.right), r: 4, fill: p.color }, ctx.g);
        leftLabels.push({ y: yOf(p.left), text: fmt(p.left, cfg.precision || 0), color: p.color });
        rightLabels.push({ y: yOf(p.right), text: fmt(p.right, cfg.precision || 0), color: p.color });
      });

      // 端点数值避让：间距不足时纵向错开并画细引导线，避免数字叠在一起。
      placeLabels(ctx, leftLabels, 14);
      placeLabels(ctx, rightLabels, 14);
      leftLabels.forEach(function (it) { drawEndLabel(ctx, it, xL - 10, "end"); });
      rightLabels.forEach(function (it) { drawEndLabel(ctx, it, xR + 10, "start"); });

      if (hasLegend) legendRow(ctx, cfg.legend, ctx.ih + 42, Math.min(cfg.legend.length, 3), "dot");
    });
  }

  /* ---------- 散点（样本覆盖） ---------- */
  function scatter(host, cfg) {
    mount(host, function () {
      const cats = cfg.categories;
      let lo = Infinity, hi = -Infinity;
      cfg.groups.forEach(function (g) {
        g.points.forEach(function (p) { lo = Math.min(lo, p[1]); hi = Math.max(hi, p[1]); });
      });
      const scale = niceScale(lo * 0.94, hi * 1.06, 5);
      const ctx = surface(host, { bottom: 56 });
      yAxis(ctx, scale, cfg.yLabel);

      const n = cats.length;
      const step = ctx.iw / n;
      const yOf = function (v) { return ctx.ih - ((v - scale.lo) / (scale.hi - scale.lo)) * ctx.ih; };

      cfg.groups.forEach(function (g, gi) {
        g.points.forEach(function (p) {
          const cx = step * (p[0] + 0.5) + (gi - (cfg.groups.length - 1) / 2) * 12;
          const node = marker(ctx.g, SEED_MARKERS[gi % 3], cx, yOf(p[1]), g.color, 5);
          s("title", null, node).textContent = cats[p[0]].label + " · 种子 " + p[2] + " · " + fmt(p[1], cfg.precision || 2);
        });
      });

      xAxis(ctx, cats);
      legendRow(ctx, cfg.groups.map(function (g) { return { name: g.name, color: g.color }; }), ctx.ih + 40, 3);
    });
  }

  global.Charts = { barError: barError, lines: lines, stacked: stacked, slope: slope, scatter: scatter };
})(window);
