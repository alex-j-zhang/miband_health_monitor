function axis(ctx, width, height, pad, minValue, maxValue, ticks, format) {
  ctx.strokeStyle = "#2a323c"; ctx.fillStyle = "#8b98a5";
  ctx.font = "11px -apple-system, 'PingFang SC', sans-serif";
  ctx.lineWidth = 1;
  for (let i = 0; i <= ticks; i++) {
    const value = minValue + (maxValue - minValue) * (i / ticks);
    const y = height - pad.bottom - (height - pad.top - pad.bottom) * (i / ticks);
    ctx.beginPath(); ctx.moveTo(pad.left, y); ctx.lineTo(width - pad.right, y); ctx.stroke();
    ctx.fillText(format(value), 4, y + 4);
  }
}

function lineChart(canvasId, points, options) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return;
  const opts = options || {};
  const draw = () => {
    const dpr = window.devicePixelRatio || 1;
    const width = canvas.clientWidth, height = canvas.clientHeight;
    canvas.width = width * dpr; canvas.height = height * dpr;
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);

    if (!points.length) {
      ctx.fillStyle = "#8b98a5"; ctx.font = "12px sans-serif";
      ctx.fillText("暂无数据", 12, height / 2);
      return;
    }
    const pad = {left: 38, right: 10, top: 8, bottom: 18};
    const xs = points.map(p => p[0]);
    const ys = points.map(p => p[1]);
    const x0 = Math.min(...xs), x1 = Math.max(...xs);
    const y0 = opts.min !== undefined ? opts.min : Math.min(...ys);
    const y1 = opts.max !== undefined ? opts.max : Math.max(...ys);
    const spanY = (y1 - y0) || 1;
    const spanX = (x1 - x0) || 1;

    axis(ctx, width, height, pad, y0, y1, 4, v => Math.round(v));

    const sx = value => pad.left + ((value - x0) / spanX) * (width - pad.left - pad.right);
    const sy = value => height - pad.bottom - ((value - y0) / spanY) * (height - pad.top - pad.bottom);

    ctx.strokeStyle = opts.color || "#ff5c7a";
    ctx.lineWidth = 1.4;
    // Break the line across data gaps instead of drawing a long diagonal
    // through them (an unsynced night would otherwise invent a straight line).
    const maxGap = opts.maxGap || 300;
    const path = new Path2D();
    let pen = false;
    let lastX = null;
    for (const [t, v] of points) {
      const x = sx(t), y = sy(v);
      if (lastX !== null && t - lastX > maxGap) pen = false;
      if (!pen) { path.moveTo(x, y); pen = true; } else { path.lineTo(x, y); }
      lastX = t;
    }
    ctx.stroke(path);

    ctx.fillStyle = "#8b98a5"; ctx.font = "11px sans-serif";
    ctx.fillText(new Date(x0 * 1000).toLocaleString("zh-CN", {month: "2-digit", day: "2-digit", hour: "2-digit"}), pad.left, height - 4);
    const endLabel = new Date(x1 * 1000).toLocaleString("zh-CN", {month: "2-digit", day: "2-digit", hour: "2-digit"});
    ctx.fillText(endLabel, width - pad.right - ctx.measureText(endLabel).width, height - 4);
  };
  requestAnimationFrame(draw);
  window.addEventListener("resize", () => requestAnimationFrame(draw));
}

function renderHeartRate() {
  const hr = DATA.hr;
  lineChart("hrChart", hr.points, {color: "#ff5c7a"});
  const scale = document.getElementById("hrScale");
  if (hr.points.length) {
    scale.textContent = `${hr.min}–${hr.max} BPM · 均值 ${hr.avg} · 来源：${hr.source === "history" ? "手环分钟历史" : "实时流"}`;
  } else {
    scale.textContent = "未采集到心率";
  }
}

function renderActivity() {
  const canvas = document.getElementById("actChart");
  const points = DATA.activity.points;
  const draw = () => {
    const dpr = window.devicePixelRatio || 1;
    const width = canvas.clientWidth || 600, height = canvas.clientHeight || 120;
    canvas.width = width * dpr; canvas.height = height * dpr;
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    if (!points.length) {
      ctx.fillStyle = "#8b98a5"; ctx.font = "12px sans-serif"; ctx.fillText("暂无数据", 12, height / 2); return;
    }

    const dayStart = ts => {
      const d = new Date(ts * 1000);
      return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime() / 1000;
    };
    const firstDay = dayStart(points[0][0]);
    const lastDay = dayStart(points[points.length - 1][0]);
    const days = Math.round((lastDay - firstDay) / 86400) + 1;
    const DAY = 86400;

    const left = 54, rowGap = 5;
    const rowH = Math.max(8, (height - rowGap * (days - 1)) / days);
    const plotW = width - left - 6;
    const colors = ["#33404f", "#4ade80", "#8b5cf6", "#1e2630"]; // rest / active / sleep / not worn
    // "Not worn" and "no record" are two different facts and must not look the
    // same.  The band writes a state byte for every minute it was off the wrist
    // (0xF3), so those minutes are *measured*; a hole in the history is a minute
    // the band no longer has -- typically because the phone already fetched it.
    // A diagonal hatch marks the former, a plain panel background the latter.
    const nonwearHatch = (() => {
      const tile = document.createElement("canvas");
      tile.width = 6; tile.height = 6;
      const t = tile.getContext("2d");
      t.fillStyle = "#1e2630"; t.fillRect(0, 0, 6, 6);
      t.strokeStyle = "#4a5866"; t.lineWidth = 1;
      t.beginPath(); t.moveTo(-1, 7); t.lineTo(7, -1); t.stroke();
      return ctx.createPattern(tile, "repeat");
    })();
    const cellW = Math.max(1, plotW / 1440);

    ctx.font = "11px -apple-system, 'PingFang SC', sans-serif";
    for (let day = 0; day < days; day++) {
      const y = day * (rowH + rowGap);
      ctx.strokeStyle = "#232b35";
      for (let hour = 0; hour <= 24; hour += 6) {
        const x = left + (hour / 24) * plotW;
        ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(x, y + rowH); ctx.stroke();
      }
      const label = new Date((firstDay + day * DAY) * 1000)
        .toLocaleDateString("zh-CN", {month: "2-digit", day: "2-digit"});
      ctx.fillStyle = "#8b98a5";
      ctx.fillText(label, 4, y + rowH / 2 + 4);
      // baseline so an empty day still reads as a row
      ctx.fillStyle = "#1b232c";
      ctx.fillRect(left, y, plotW, rowH);
    }

    for (const [ts, code] of points) {
      const day = Math.floor((ts - firstDay) / DAY);
      if (day < 0 || day >= days) continue;
      const minute = (ts - (firstDay + day * DAY)) / 60;
      const y = day * (rowH + rowGap);
      ctx.fillStyle = code === 3 ? nonwearHatch : (colors[code] || "#333");
      ctx.fillRect(left + (minute / 1440) * plotW, y, cellW, rowH);
    }

    ctx.fillStyle = "#8b98a5";
    ["00", "06", "12", "18", "24"].forEach((hour, index) => {
      ctx.fillText(hour, left + (index / 4) * plotW - 6, height - 2);
    });
  };
  requestAnimationFrame(draw);
  window.addEventListener("resize", () => requestAnimationFrame(draw));
  document.getElementById("actScale").textContent =
    points.length ? "紫=睡眠 绿=活动 深灰=静止 斜纹=未佩戴 空白=无记录 · 每格 1 分钟，按天分行" : "";
}

function renderSteps() {
  const canvas = document.getElementById("stepChart");
  const daily = DATA.steps.daily;
  const draw = () => {
    const dpr = window.devicePixelRatio || 1;
    const width = canvas.clientWidth || 600, height = canvas.clientHeight || 150;
    canvas.width = width * dpr; canvas.height = height * dpr;
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    if (!daily.length) {
      ctx.fillStyle = "#8b98a5"; ctx.font = "12px sans-serif"; ctx.fillText("暂无数据", 12, height / 2); return;
    }

    const pad = {left: 46, right: 8, top: 10, bottom: 22};
    const maxValue = Math.max(...daily.map(d => d.steps), 1);
    const niceMax = Math.ceil(maxValue / 1000) * 1000 || 1000;
    const plotW = width - pad.left - pad.right;
    const plotH = height - pad.top - pad.bottom;

    ctx.font = "11px -apple-system, 'PingFang SC', sans-serif";
    ctx.strokeStyle = "#2a323c";
    for (let i = 0; i <= 3; i++) {
      const y = pad.top + plotH * (i / 3);
      ctx.beginPath(); ctx.moveTo(pad.left, y); ctx.lineTo(width - pad.right, y); ctx.stroke();
      ctx.fillStyle = "#8b98a5";
      ctx.fillText(Math.round(niceMax * (1 - i / 3)).toLocaleString(), 4, y + 4);
    }

    const slot = plotW / daily.length;
    const barW = Math.min(72, slot * 0.55);
    daily.forEach((day, index) => {
      const h = (day.steps / niceMax) * plotH;
      const x = pad.left + slot * index + (slot - barW) / 2;
      const gradient = ctx.createLinearGradient(0, pad.top + plotH - h, 0, pad.top + plotH);
      gradient.addColorStop(0, "#4ade80");
      gradient.addColorStop(1, "rgba(74,222,128,.35)");
      ctx.fillStyle = gradient;
      ctx.beginPath();
      ctx.roundRect(x, pad.top + plotH - h, barW, h, 4);
      ctx.fill();

      ctx.fillStyle = "#e6edf3";
      ctx.textAlign = "center";
      ctx.fillText(day.steps.toLocaleString(), x + barW / 2, pad.top + plotH - h - 5);
      ctx.fillStyle = "#8b98a5";
      ctx.fillText(day.date.slice(5), x + barW / 2, height - 6);
      ctx.textAlign = "left";
    });
  };
  requestAnimationFrame(draw);
  window.addEventListener("resize", () => requestAnimationFrame(draw));
  document.getElementById("stepScale").textContent = daily.length
    ? `合计 ${DATA.steps.total.toLocaleString()} 步`
    : "";
}

function renderSpo2Sleep() {
  const s = DATA.spo2_sleep || {points: [], count: 0};
  const scale = document.getElementById("spo2SleepScale");
  if (!s.points.length) {
    const canvas = document.getElementById("spo2SleepChart");
    if (canvas) canvas.style.display = "none";
    scale.textContent = "该窗口内没有睡眠血氧记录（需 --sync-sleep-spo2，或提供 spo2_sleep.jsonl）";
    return;
  }
  // Fixed upper bound keeps the night readable; a lower minimum than 80 only
  // widens the axis when the band actually reported something below it.
  lineChart("spo2SleepChart", s.points, {
    color: "#38bdf8",
    min: Math.min(80, Math.floor(s.min - 2)),
    max: 100,
    maxGap: 300
  });
  const origin = s.source === "sqlite" ? "SQLite spo2_sleep 表" : "spo2_sleep.jsonl";
  scale.textContent = `${s.count} 条 · ${s.min}–${s.max}% · 均值 ${s.avg}% · 来源：${origin}`;
}