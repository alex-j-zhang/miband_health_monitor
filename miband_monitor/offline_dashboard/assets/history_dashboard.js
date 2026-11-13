const DATA = __DATA__;
const STAGE_COLORS = __STAGE_STYLE__;
const STAGE_LABELS = {deep: "深睡", light: "浅睡", awake: "清醒", unknown: "无心率"};

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

function renderHeader() {
  const meta = DATA.meta;
  const range = meta.range[0] && meta.range[1]
    ? `${meta.range[0]} → ${meta.range[1]}`
    : "暂无数据";
  document.getElementById("range").innerHTML =
    `<b>${range}</b> · 近 ${meta.days} 天 · 活动样本 ${meta.activity_samples} 分钟 · 生成于 ${meta.generated_at}`;
}

function renderKpis() {
  const host = document.getElementById("kpis");
  DATA.kpi.forEach(item => {
    const card = el("div", "card");
    card.appendChild(el("div", "label", item.label));
    const value = el("div", "value", item.value);
    if (item.unit && item.value !== "--") value.appendChild(el("span", "unit", item.unit));
    card.appendChild(value);
    card.appendChild(el("div", "hint", item.hint));
    host.appendChild(card);
  });
}

function renderSleep() {
  const host = document.getElementById("sleepPanel");
  const sessions = DATA.sleep.sessions;
  if (!sessions.length) { host.appendChild(el("div", "empty", "该窗口内没有识别到睡眠时段。")); return; }

  sessions.slice().reverse().forEach(session => {
    const box = el("div", "night");

    const head = el("div", "night-head");
    head.appendChild(el("span", "title", `${session.date} 夜`));
    const asleep = (session.minutes_asleep / 60).toFixed(1);
    const inBed = (session.minutes_in_bed / 60).toFixed(1);
    const efficiency = session.heart_rate_available
      ? ` · 效率 ${(session.efficiency * 100).toFixed(0)}%`
      : "";
    head.appendChild(el("span", "meta",
      `${session.start.slice(11, 16)} → ${session.end.slice(11, 16)} · 卧床 ${inBed}h · 睡眠 ${asleep}h${efficiency}`));
    box.appendChild(head);

    // hypnogram: pre-rendered SVG (one run per stage stretch), injected as-is
    const hypno = el("div");
    hypno.innerHTML = session.svg;
    box.appendChild(hypno);

    const scaleRow = el("div", "scales");
    const duration = session.minutes_in_bed;
    const tickCount = duration > 240 ? 4 : 2;
    for (let i = 0; i <= tickCount; i++) {
      const ts = session.start_ts + (duration * i / tickCount) * 60;
      const label = new Date(ts * 1000).toLocaleTimeString("zh-CN",
        {hour: "2-digit", minute: "2-digit", hour12: false});
      scaleRow.appendChild(el("span", "", label));
    }
    box.appendChild(scaleRow);

    if (session.truncated_start) {
      box.appendChild(el("div", "hint",
        `这一夜的开头在手环里已经没有了：数据最早只到 ${session.start.slice(11, 16)}。` +
        `卧床与睡眠时长都是被截断后的值，不要直接和 App 的数字比。`));
    }

    if (!session.heart_rate_available) {
      box.appendChild(el("div", "hint", "该夜手环未记录心率，无法推导深睡/浅睡，仅显示卧床时段。"));
      host.appendChild(box);
      return;
    }

    // stage proportions
    const shares = session.stage_share || {};
    const legend = el("div", "legend");
    ["deep", "light"].forEach(stage => {
      const item = el("span");
      const swatch = el("span", "swatch");
      swatch.style.background = STAGE_COLORS[stage];
      item.appendChild(swatch);
      const minutes = (session.stage_minutes || {})[stage] || 0;
      item.appendChild(document.createTextNode(`${STAGE_LABELS[stage]} ${minutes} 分 (${((shares[stage] || 0) * 100).toFixed(0)}%)`));
      legend.appendChild(item);
    });
    const awake = el("span");
    const awakeSwatch = el("span", "swatch");
    awakeSwatch.style.background = STAGE_COLORS.awake;
    awake.appendChild(awakeSwatch);
    awake.appendChild(document.createTextNode(`清醒 ${(session.stage_minutes || {}).awake || 0} 分`));
    legend.appendChild(awake);
    box.appendChild(legend);

    const bar = el("div", "bars");
    const total = session.minutes_in_bed || 1;
    ["deep", "light", "awake", "unknown"].forEach(stage => {
      const minutes = (session.stage_minutes || {})[stage] || 0;
      if (!minutes) return;
      const seg = el("div");
      seg.style.width = `${(minutes / total) * 100}%`;
      seg.style.background = STAGE_COLORS[stage];
      seg.title = `${STAGE_LABELS[stage]} ${minutes} 分`;
      bar.appendChild(seg);
    });
    box.appendChild(bar);

    host.appendChild(box);
  });
}