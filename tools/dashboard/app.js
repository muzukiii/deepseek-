const $ = (id) => document.getElementById(id);
const REFRESH_MS = 10_000;

const esc = (v) =>
  String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const num = (n) => Number(n || 0).toLocaleString("zh-CN");

const money = (n) => {
  const v = Number(n || 0);
  if (v === 0) return "$0";
  if (v < 0.0001) return "$" + v.toFixed(7).replace(/0+$/, "").replace(/\.$/, "");
  return "$" + v.toFixed(5).replace(/0+$/, "").replace(/\.$/, "");
};

const compact = (n) => {
  const v = Number(n || 0);
  if (v >= 1_000_000) return (v / 1_000_000).toFixed(1) + "M";
  if (v >= 1_000) return (v / 1_000).toFixed(1) + "k";
  return String(v);
};

const ago = (sec) => {
  if (sec == null) return "—";
  if (sec < 60) return `${sec}s 前`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m 前`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}h 前`;
  return `${Math.floor(sec / 86400)}d 前`;
};

const clock = (epochSeconds) => new Date(epochSeconds * 1000).toLocaleTimeString("zh-CN", { hour12: false });

function setPill(el, label, state, title) {
  el.className = "pill" + (state ? " " + state : "");
  el.textContent = label;
  if (title) el.title = title;
}

function renderKpis(data) {
  const u = data.usage;
  const log = data.log;
  const lastReply = log.replies[0];
  const connText =
    log.connected === true ? "连接正常" : log.connected === false ? "连接已断开或未建立" : "日志窗口内无连接事件";
  const kpis = [
    { label: "今日花费", value: money(u.today_cost), foot: `今日 ${num(u.today)} 次调用` },
    { label: "累计花费", value: money(u.total_cost), foot: `累计 ${num(u.total_calls)} 次调用` },
    { label: "表情库", value: num(data.stickers.count), foot: "已入库的去重表情" },
    { label: "最近回复", value: lastReply ? lastReply.time.slice(11, 16) : "—", small: !lastReply, foot: lastReply ? `群 ${esc(lastReply.group)} · ${esc(lastReply.time)}` : "本次日志内还没有回复" },
    { label: "本次运行回复数", value: num(log.reply_count), foot: connText + (log.truncated ? " · 日志超 32MB 仅统计最近部分" : "") },
    { label: "日志", value: log.age_s == null ? "—" : ago(log.age_s), small: true, foot: log.file ? esc(log.file) : "未找到日志文件" },
  ];
  $("kpis").innerHTML = kpis
    .map(
      (k) => `
      <div class="kpi">
        <span class="label">${k.label}</span>
        <span class="value${k.small ? " small" : ""}">${k.value}</span>
        <span class="foot">${k.foot}</span>
      </div>`
    )
    .join("");
}

function renderByModel(rows) {
  const body = $("by-model").querySelector("tbody");
  body.innerHTML = rows.length
    ? rows
        .map(
          (r) => `<tr>
            <td>${esc(r.model)}</td>
            <td class="num">${num(r.calls)}</td>
            <td class="num">${compact(r.in)}</td>
            <td class="num">${compact(r.out)}</td>
            <td class="num">${money(r.cost)}</td>
          </tr>`
        )
        .join("")
    : `<tr><td colspan="5" class="empty">没有用量记录</td></tr>`;
}

function renderByKind(rows) {
  const body = $("by-kind").querySelector("tbody");
  body.innerHTML = rows.length
    ? rows
        .map(
          (r) => `<tr><td>${esc(r.kind)}</td><td class="num">${num(r.calls)}</td><td class="num">${money(r.cost)}</td></tr>`
        )
        .join("")
    : `<tr><td colspan="3" class="empty">没有用量记录</td></tr>`;
}

function renderBars(days) {
  const host = $("bars");
  if (!days.length) {
    host.innerHTML = `<div class="empty">没有历史数据</div>`;
    return;
  }
  const max = Math.max(...days.map((d) => d.cost), 0.0000001);
  host.innerHTML = days
    .map((d) => {
      const pct = Math.max(2, Math.round((d.cost / max) * 100));
      const label = d.day.slice(5);
      return `<div class="bar-col" title="${esc(d.day)} · ${money(d.cost)} · ${num(d.calls)} 次">
        <div class="bar" style="height:${pct}%"></div>
        <span class="day">${label}</span>
      </div>`;
    })
    .join("");
}

function renderReplies(rows, total) {
  $("reply-count").textContent = total ? `${total} 条（显示最近 ${rows.length} 条）` : "";
  const host = $("replies");
  host.innerHTML = rows.length
    ? rows
        .map(
          (r) => `<li>
            <div class="meta"><span>${esc(r.time)}</span><span>群 ${esc(r.group)}</span></div>
            <div class="text">${esc(r.text) || "（空回复）"}</div>
          </li>`
        )
        .join("")
    : `<li class="empty">本次日志内还没有回复记录</li>`;
}

function renderErrors(rows) {
  const host = $("errors");
  host.innerHTML = rows.length
    ? rows
        .map(
          (r) => `<li class="${r.level === "ERROR" ? "error" : ""}">
            <div class="meta"><span class="lvl ${esc(r.level)}">${esc(r.level)}</span><span>${esc(r.time)}</span></div>
            <div class="text">${esc(r.text)}</div>
          </li>`
        )
        .join("")
    : `<li class="empty">没有 WARNING / ERROR</li>`;
}

function renderThumbs(items, count) {
  $("sticker-count").textContent = count ? `${count} 张 · 按使用次数排序` : "";
  const host = $("thumbs");
  host.innerHTML = items.length
    ? items
        .map(
          (s) => `<figure class="thumb" title="${esc(s.desc || s.file)}">
            <img loading="lazy" src="/api/sticker/${encodeURIComponent(s.id)}" alt="${esc(s.desc || s.id)}">
            <span class="uses">${num(s.uses)}</span>
            <figcaption class="desc">${esc(s.desc || s.file)}</figcaption>
          </figure>`
        )
        .join("")
    : `<div class="empty">表情库为空</div>`;
}

function renderRecent(rows) {
  const body = $("recent").querySelector("tbody");
  body.innerHTML = rows.length
    ? rows
        .map(
          (r) => `<tr>
            <td>${esc(r.time)}</td>
            <td>${esc(r.kind)}</td>
            <td>${esc(r.model)}</td>
            <td class="num">${num(r.in)}<span class="dim"> / ${num(r.cache)}</span></td>
            <td class="num">${num(r.out)}</td>
            <td class="num">${money(r.cost)}</td>
          </tr>`
        )
        .join("")
    : `<tr><td colspan="6" class="empty">没有用量记录</td></tr>`;
}

function renderCtx(ctx) {
  const host = $("ctx");
  if (!ctx || !ctx.turns) {
    $("ctx-hint").textContent = "还没有取上下文的记录（新版 bot 重启后开始统计）";
    host.innerHTML = `<div class="empty">本段日志里没有 [ctx] 记录</div>`;
    return;
  }
  $("ctx-hint").textContent = `A4 · 本次日志 ${num(ctx.turns)} 次取上下文`;
  const hits = ctx.quote_hit_id + ctx.quote_hit_text;
  const hitTotal = hits + ctx.quote_miss;
  const avgExtra = ctx.triggered ? (ctx.extra_rows / ctx.triggered).toFixed(1) : "0";
  const items = [
    { label: "补充触发", value: `${num(ctx.triggered)} 次`, foot: `占 ${Math.round((ctx.triggered / ctx.turns) * 100)}%` },
    { label: "引用命中", value: `${num(hits)} / ${num(hitTotal)}`, foot: `ID ${num(ctx.quote_hit_id)} · 文本兜底 ${num(ctx.quote_hit_text)}` },
    { label: "引用落空", value: num(ctx.quote_miss), foot: "被引用那条不在库里且文本未匹配" },
    { label: "护栏裁剪", value: `${num(ctx.trimmed)} 次`, foot: "超字符预算，丢弃了部分补充" },
    { label: "平均补量", value: `${avgExtra} 行`, foot: `累计 ${num(ctx.extra_rows)} 行` },
    { label: "最近上下文", value: ctx.chars_last == null ? "—" : `${num(ctx.chars_last)} 字`, foot: "整段 prompt 里聊天记录的长度" },
  ];
  host.innerHTML = items
    .map(
      (k) => `<div class="ctx-item"><span class="label">${k.label}</span><span class="value">${k.value}</span><span class="foot">${k.foot}</span></div>`
    )
    .join("");
}

function render(data) {
  const s = data.status;
  const dot = $("dot-bot");
  dot.className = "dot " + (s.bot_up ? "ok" : "bad");
  dot.title = s.bot_up ? "Bot 在 8080 监听" : "Bot 未监听 8080";
  setPill($("pill-bot"), `Bot :8080 ${s.bot_up ? "在线" : "离线"}`, s.bot_up ? "ok" : "bad");
  setPill($("pill-bridge"), `桥接 :3001 ${s.bridge_up ? "在线" : "离线"}`, s.bridge_up ? "ok" : "bad");
  const log = data.log;
  setPill(
    $("pill-log"),
    `日志 ${log.age_s == null ? "未找到" : ago(log.age_s)}`,
    log.age_s != null && log.age_s < 300 ? "ok" : "",
    log.file || ""
  );

  renderKpis(data);
  renderByModel(data.usage.by_model);
  renderByKind(data.usage.by_kind);
  renderBars(data.usage.by_day);
  renderReplies(log.replies, log.reply_count);
  renderErrors(log.errors);
  renderCtx(log.ctx);
  renderThumbs(data.stickers.top, data.stickers.count);
  renderRecent(data.usage.recent);

  $("updated").textContent = `更新于 ${clock(data.generated_at)}`;
  const banner = $("banner");
  banner.hidden = true;
}

async function load() {
  try {
    const res = await fetch("/api/summary", { cache: "no-store" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    render(await res.json());
  } catch (err) {
    const banner = $("banner");
    banner.hidden = false;
    banner.textContent = `读取数据失败：${err.message}。确认面板进程还活着，并且 bot 的数据目录存在。`;
    $("updated").textContent = "更新失败";
  }
}

let timer = null;

function schedule() {
  if (timer) clearInterval(timer);
  if ($("auto").checked) timer = setInterval(load, REFRESH_MS);
}

$("auto").addEventListener("change", schedule);
$("refresh").addEventListener("click", load);

load();
schedule();
