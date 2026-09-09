"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const STORAGE_KEY = "proxysql-demo.run_id";
  const TERMINAL = new Set(["completed", "cancelled", "failed"]);
  const SCENARIOS = new Set([
    "qps_below", "qps_above", "concurrency_below", "concurrency_above", "distributed", "failover",
  ]);
  const state = {
    config: null, snapshot: null, restoring: true, starting: false, cancelling: false,
    unavailable: false, source: null, pollTimer: null, pollController: null,
    epoch: 0, fallback: false, configBusy: false, healthTimer: null,
  };
  const fields = [
    ["duration", "duration_seconds", "duration-bound", "秒"],
    ["qps", "qps", "qps-bound", "请求/秒"],
    ["concurrency", "concurrency", "concurrency-bound", "请求"],
  ];
  const colors = ["#135bb4", "#08764e", "#bd3e10", "#b32347", "#7140a1", "#007d86"];
  const count = (value) => Number.isFinite(Number(value)) ? Number(value) : 0;
  const number = (value) => value == null ? "—" : count(value).toLocaleString("zh-CN", { maximumFractionDigits: 2 });
  const percent = (value) => value == null ? "—" : `${(count(value) * 100).toFixed(1)}%`;
  const set = (id, value) => { $(id).textContent = String(value); };
  const running = () => state.snapshot?.status === "running" && !state.unavailable;
  const selectedScenario = () => state.config?.scenarios.find((item) => item.id === $("scenario").value);
  const runPath = (id) => `/demo/runs/${encodeURIComponent(id)}`;

  function message(text) {
    set("error-message", text);
    $("error-message").hidden = !text;
  }

  function storage(action, value) {
    try {
      if (action === "get") return localStorage.getItem(STORAGE_KEY);
      if (action === "set") localStorage.setItem(STORAGE_KEY, value);
      if (action === "remove") localStorage.removeItem(STORAGE_KEY);
    } catch {
      set("storage-message", "浏览器禁止本地存储：运行仍可继续，但刷新后无法自动恢复。请保留运行 ID 并下载结果。");
      $("storage-message").hidden = false;
    }
    return null;
  }

  function detailText(detail) {
    if (typeof detail === "string") return detail.slice(0, 800);
    if (Array.isArray(detail)) return detail.slice(0, 6).map(detailText).filter(Boolean).join("；");
    if (detail && typeof detail === "object") {
      // Validation input values are deliberately excluded from error messages.
      const location = Array.isArray(detail.loc) ? detail.loc.map(String).join(".") : "";
      const reason = detail.msg ?? detail.message ?? detail.reason ?? detail.error ?? detail.detail;
      return [location, reason === detail ? "" : detailText(reason)].filter(Boolean).join(": ") || "服务端拒绝此请求";
    }
    return "";
  }

  async function api(path, options = {}) {
    const controller = new AbortController();
    const abort = () => controller.abort();
    options.signal?.addEventListener("abort", abort, { once: true });
    if (options.signal?.aborted) controller.abort();
    const timeout = window.setTimeout(abort, 12000);
    try {
      const response = await fetch(path, {
        ...options,
        signal: controller.signal,
        credentials: "same-origin",
        cache: "no-store",
        headers: { Accept: "application/json", ...(options.body ? { "Content-Type": "application/json" } : {}) },
      });
      let body;
      try { body = await response.json(); } catch { body = null; }
      if (!response.ok) {
        const hint = response.status === 422 ? "请检查场景与参数边界。"
          : response.status === 401 || response.status === 403 ? "需要服务端管理授权；页面不会绕过权限。"
          : response.status === 429 ? "控制请求过于频繁，请稍后重试。"
          : response.status >= 500 ? "请检查服务节点、ProxySQL、MySQL 与 Redis 状态。" : "请刷新状态后重试。";
        const error = new Error(`HTTP ${response.status} · ${detailText(body?.detail ?? body) || response.statusText} ${hint}`);
        error.status = response.status;
        throw error;
      }
      if (!body) throw new Error("服务端未返回有效 JSON；请检查 API 与代理配置。");
      return body;
    } catch (error) {
      if (error.name === "AbortError" && !options.signal?.aborted) {
        throw new Error("请求超时，请检查网络与服务状态。");
      }
      if (error instanceof TypeError) throw new Error("无法连接 API，请检查网络、统一入口与节点就绪状态。");
      throw error;
    } finally {
      clearTimeout(timeout);
      options.signal?.removeEventListener("abort", abort);
    }
  }

  function controls() {
    const locked = state.starting || state.restoring || running();
    $("run-controls").disabled = !state.config || locked;
    $("start-run").disabled = !state.config || locked || !selectedScenario()?.enabled;
    $("cancel-run").disabled = !running() || state.cancelling;
    $("download-run").disabled = !state.snapshot;
    set("start-run", state.starting ? "正在启动…" : "开始运行");
    set("cancel-run", state.cancelling ? "正在取消…" : "取消运行");
  }

  function applyDefaults() {
    const scenario = selectedScenario();
    if (!scenario) return;
    fields.forEach(([id, key]) => {
      const bound = state.config.bounds[key];
      const value = Number(scenario.defaults[key]);
      $(id).value = String(Math.min(bound.max, Math.max(bound.min, Number.isFinite(value) ? value : bound.min)));
    });
    if (scenario.id === "distributed") $("mode").value = "distributed";
    set("expected", scenario.expected);
    controls();
  }

  function healthValue(value) {
    if (value === true || ["ready", "ok", "healthy", "up"].includes(value)) return ["✓ 正常", "health-good"];
    if (value === false || ["unready", "error", "unhealthy", "down"].includes(value)) return ["✕ 不可用", "health-bad"];
    return ["? 未知", "health-unknown"];
  }

  function renderHealth(config) {
    // Never display URL credentials, paths, query strings, or connection strings.
    let publicOrigin = "未配置统一入口（本地或直连）";
    if (config.public_url) {
      try {
        const url = new URL(config.public_url);
        publicOrigin = ["http:", "https:"].includes(url.protocol) ? url.origin : "入口配置不是 HTTP(S)";
      } catch { publicOrigin = "入口配置无效，请检查服务端 public_url"; }
    }
    set("public-url", publicOrigin);
    set("serving-node", config.node_id);
    set("backend-mode", config.limiter_mode);
    const fragment = document.createDocumentFragment();
    config.nodes.forEach((node) => {
      const card = document.createElement("article");
      card.className = "node-card";
      const title = document.createElement("h3");
      title.textContent = `${node.node_id} · ${node.limiter_mode}`;
      card.append(title);
      const list = document.createElement("ul");
      list.className = "health-list";
      [["ready", "Ready"], ["proxysql", "ProxySQL"], ["mysql", "MySQL"], ["redis", "Redis"]].forEach(([key, label]) => {
        const item = document.createElement("li");
        const [text, className] = healthValue(node[key]);
        item.className = className;
        item.textContent = `${label} ${text}`;
        list.append(item);
      });
      card.append(list);
      fragment.append(card);
    });
    $("node-health").replaceChildren(fragment);
    if (!config.nodes.length) set("node-health", "尚无有效节点心跳；请检查后端与共享状态存储。");
    const limits = config.limits;
    set("limit-config", `QPS 配额 ${limits.qps_demo.rate_per_second} 请求/秒 · burst ${limits.qps_demo.burst} · SQL 并发上限 ${limits.concurrency_demo.max_concurrency} · 排队超时 ${limits.concurrency_demo.queue_timeout_ms} ms`);
    set("health-updated", `状态更新于 ${new Date().toLocaleTimeString("zh-CN")} · 每 15 秒刷新`);
  }

  async function refreshConfig() {
    if (state.configBusy) return;
    state.configBusy = true;
    $("refresh-health").disabled = true;
    try {
      const config = await api("/demo/config");
      if (!Array.isArray(config.scenarios) || !Array.isArray(config.nodes) || !config.limits?.qps_demo || !config.limits?.concurrency_demo) {
        throw new Error("配置格式不完整；请检查后端 /demo/config 契约。");
      }
      for (const [, key] of fields) {
        const bound = config.bounds?.[key];
        if (!Number.isFinite(bound?.min) || !Number.isFinite(bound?.max) || bound.min > bound.max) {
          throw new Error("服务端未提供有效安全边界，已禁止启动新运行。");
        }
      }
      config.scenarios = config.scenarios.filter((item) => SCENARIOS.has(item.id));
      const first = !state.config;
      const previous = $("scenario").value;
      state.config = config;
      const options = config.scenarios.map((scenario) => {
        const option = document.createElement("option");
        option.value = scenario.id;
        option.textContent = `${scenario.title}${scenario.enabled ? "" : " · 已禁用"}`;
        option.disabled = !scenario.enabled;
        return option;
      });
      $("scenario").replaceChildren(...options);
      $("scenario").value = config.scenarios.some((s) => s.id === previous && s.enabled)
        ? previous : config.scenarios.find((s) => s.enabled)?.id ?? "";
      fields.forEach(([id, key, hint, unit]) => {
        $(id).min = String(config.bounds[key].min);
        $(id).max = String(config.bounds[key].max);
        set(hint, `${config.bounds[key].min}–${config.bounds[key].max} ${unit}`);
      });
      if (first) $("mode").value = config.limiter_mode === "distributed" ? "distributed" : "local";
      if (first || previous !== $("scenario").value) applyDefaults();
      else set("expected", selectedScenario()?.expected ?? "没有可用场景。");
      const fault = config.scenarios.find((s) => s.id === "failover");
      set("fault-note", fault?.enabled
        ? "故障场景由服务端显式启用；仅执行已授权的预定义动作。未获授权时服务端应拒绝请求。"
        : "节点故障转移已禁用：默认没有管理授权，不提供停止服务或其他故障注入动作。");
      renderHealth(config);
    } catch (error) {
      set("health-updated", `状态获取失败（现有数据显示可能已过期）：${error.message} 请使用“刷新状态”重试。`);
      if (!state.config) message(error.message);
    } finally {
      state.configBusy = false;
      $("refresh-health").disabled = false;
      controls();
    }
  }

  function renderMap(id, values, empty) {
    const entries = Object.entries(values ?? {}).sort(([a], [b]) => a.localeCompare(b));
    const items = entries.map(([key, value]) => {
      const item = document.createElement("li");
      const label = document.createElement("span");
      label.textContent = key;
      const total = document.createElement("strong");
      total.textContent = number(value);
      item.append(label, total);
      return item;
    });
    if (!items.length) {
      const item = document.createElement("li");
      item.textContent = empty;
      items.push(item);
    }
    $(id).replaceChildren(...items);
  }

  function svgElement(tag, attributes = {}, text = null) {
    const element = document.createElementNS("http://www.w3.org/2000/svg", tag);
    Object.entries(attributes).forEach(([key, value]) => element.setAttribute(key, String(value)));
    if (text !== null) element.textContent = text;
    return element;
  }

  function chart(id, title, buckets, series) {
    const width = 520, height = 230, left = 46, right = 14, top = 16, bottom = 35;
    const plotWidth = width - left - right, plotHeight = height - top - bottom;
    const svg = svgElement("svg", { viewBox: `0 0 ${width} ${height}`, role: "img", "aria-labelledby": `${id}-title` });
    svg.append(svgElement("title", { id: `${id}-title` }, `${title}。完整数值请展开下方每秒数据表。`));
    const maxX = Math.max(1, ...buckets.map((bucket) => count(bucket.second)));
    const maxY = Math.max(1, ...series.flatMap((line) => buckets.map((bucket) => count(line.value(bucket)))));
    const x = (value) => left + count(value) / maxX * plotWidth;
    const y = (value) => top + plotHeight - count(value) / maxY * plotHeight;
    for (let i = 0; i <= 4; i += 1) {
      const tick = maxY * i / 4;
      svg.append(svgElement("line", { x1: left, x2: width - right, y1: y(tick), y2: y(tick), stroke: "#dfe6ef" }));
      svg.append(svgElement("text", { x: left - 7, y: y(tick) + 4, "text-anchor": "end" }, number(tick)));
    }
    [...new Set([0, Math.floor(maxX / 2), maxX])].forEach((tick) => {
      svg.append(svgElement("text", { x: x(tick), y: height - 14, "text-anchor": "middle" }, `${tick}s`));
    });
    series.forEach((line, index) => {
      const color = line.color ?? colors[index % colors.length];
      const points = buckets.map((bucket) => `${x(bucket.second)},${y(line.value(bucket))}`).join(" ");
      if (buckets.length) {
        svg.append(svgElement("polyline", {
          points, fill: "none", stroke: color, "stroke-width": 2.4,
          "stroke-dasharray": index % 2 ? "6 3" : "none", "vector-effect": "non-scaling-stroke",
        }));
        if (buckets.length === 1) svg.append(svgElement("circle", { cx: x(buckets[0].second), cy: y(line.value(buckets[0])), r: 3, fill: color }));
      }
    });
    if (!buckets.length) svg.append(svgElement("text", { x: width / 2, y: height / 2, "text-anchor": "middle" }, "等待 1 秒桶数据"));
    $(`${id}-chart`).replaceChildren(svg);
    $(`${id}-legend`).replaceChildren(...series.map((line, index) => {
      const item = document.createElement("li");
      const swatch = document.createElement("span");
      swatch.className = "swatch";
      swatch.style.borderColor = line.color ?? colors[index % colors.length];
      swatch.style.borderTopStyle = index % 2 ? "dashed" : "solid";
      swatch.setAttribute("aria-hidden", "true");
      const label = document.createElement("span");
      label.textContent = line.label;
      item.append(swatch, label);
      return item;
    }));
  }

  function renderCharts(buckets) {
    chart("requests", "总请求、成功、429 与不可用（5xx 或无响应），每秒请求数", buckets, [
      { label: "总请求", value: (b) => b.total },
      { label: "成功 200", value: (b) => b.success },
      { label: "拒绝 429", value: (b) => b.rate_limited },
      { label: "不可用（5xx / 无响应）", value: (b) => b.unavailable },
    ]);
    chart("concurrency", "每秒客户端在途并发峰值，不代表 SQL 执行数", buckets, [
      { label: "客户端在途请求峰值", value: (b) => b.concurrency },
    ]);
    chart("latency", "请求延迟百分位，毫秒", buckets, [
      { label: "P50 ms", value: (b) => b.latency_ms?.p50 },
      { label: "P95 ms", value: (b) => b.latency_ms?.p95, color: "#7140a1" },
      { label: "P99 ms", value: (b) => b.latency_ms?.p99, color: "#bd3e10" },
    ]);
    const nodes = [...new Set(buckets.flatMap((b) => Object.keys(b.nodes ?? {})))].sort();
    chart("nodes", "各节点每秒处理请求数", buckets, nodes.map((node) => ({
      label: node, value: (b) => b.nodes?.[node] ?? 0,
    })));
    if (!buckets.length) {
      const row = document.createElement("tr");
      const cell = document.createElement("td");
      cell.colSpan = 10;
      cell.textContent = "尚无数据";
      row.append(cell);
      $("bucket-table").replaceChildren(row);
      return;
    }
    $("bucket-table").replaceChildren(...buckets.map((b) => {
      const row = document.createElement("tr");
      [
        b.second, b.total, b.success, b.rate_limited, b.unavailable, b.concurrency,
        b.latency_ms?.p50, b.latency_ms?.p95, b.latency_ms?.p99,
      ].forEach((value) => {
        const cell = document.createElement("td");
        cell.textContent = number(value);
        row.append(cell);
      });
      const nodesCell = document.createElement("td");
      nodesCell.textContent = Object.entries(b.nodes ?? {}).map(([node, value]) => `${node}: ${number(value)}`).join(" · ") || "—";
      row.append(nodesCell);
      return row;
    }));
  }

  function renderSnapshot(snapshot) {
    const summary = snapshot.summary;
    const statusLabels = { running: "运行中", completed: "已完成", cancelled: "已取消", failed: "失败" };
    set("run-status", `${snapshot.status} · ${statusLabels[snapshot.status] ?? "未知状态"}`);
    $("run-status").dataset.status = snapshot.status;
    set("run-id", snapshot.run_id);
    set("run-scenario", `${snapshot.scenario_id} / ${snapshot.limiter_mode}`);
    set("run-expected", snapshot.expected);
    const parameters = snapshot.parameters;
    set("run-parameters", `${parameters.duration_seconds} 秒 · 目标 ${parameters.qps} 请求/秒 · 客户端并发上限 ${parameters.concurrency}`);
    const date = (value) => value ? `${new Date(value).toLocaleString("zh-CN")} (${value})` : "—";
    set("started-at", date(snapshot.started_at));
    set("finished-at", date(snapshot.finished_at));
    set("total-requests", count(summary.total));
    set("success-count", count(summary.success));
    set("rejected-count", count(summary.rate_limited));
    set("unavailable-count", count(summary.unavailable));
    set("unavailable-detail", `HTTP 503：${count(summary.status_codes?.["503"])} · 无 HTTP 响应（0）：${count(summary.status_codes?.["0"])}`);
    set("success-rate", `成功率 ${percent(summary.success_rate)}`);
    set("rate-limit-rate", `限流率 ${percent(summary.rate_limit_rate)}`);
    set("peak-concurrency", `${number(summary.peak_concurrency)} 请求`);
    set("peak-sql-concurrency", summary.peak_sql_concurrency == null
      ? "未提供" : `${number(summary.peak_sql_concurrency)} 请求（已接受的执行阶段）`);
    set("concurrency-note", `并发曲线为每秒客户端在途请求峰值（含等待），不等于 SQL 正在执行数。${summary.peak_sql_concurrency == null
      ? "结果未提供 SQL 执行阶段峰值。"
      : "SQL 执行阶段峰值来自服务端成功响应采样，包含受控延迟；仅 distributed 并发场景为全局配额占用，其他情况为单节点观测值，不是跨节点总和。"}当前 SQL 执行并发未由结果 API 提供；不能用客户端并发、历史峰值或 ProxySQL 连接数代替。`);
    set("latency-summary", `${number(summary.latency_ms?.p50)} / ${number(summary.latency_ms?.p95)} / ${number(summary.latency_ms?.p99)} ms`);
    set("recovery-time", summary.recovery_time_ms == null ? "未观测 / 不适用（null）" : `${number(summary.recovery_time_ms)} ms`);
    set("transient-failures", number(summary.transient_failures));
    renderMap("status-codes", summary.status_codes, "尚无状态码");
    renderMap("node-counts", summary.nodes, "尚无节点请求");
    renderMap("error-counts", summary.errors, "未记录错误");
    const nodes = Object.entries(summary.nodes ?? {}).filter(([, total]) => count(total) > 0).length;
    set("measured", `已记录 ${number(summary.total)} 个请求：成功 ${percent(summary.success_rate)}，限流 ${percent(summary.rate_limit_rate)}，不可用 ${number(summary.unavailable)}；${nodes} 个节点处理过流量。${snapshot.status === "running" ? "运行尚未结束，以上为部分结果。" : "请对照预期、状态码及错误原因判断，完成状态不代表所有请求成功。"}${snapshot.scenario_id === "distributed" && nodes < 2 ? " 尚未证明双节点流量分布。" : ""}`);
    renderCharts([...snapshot.buckets].sort((a, b) => a.second - b.second));
    controls();
  }

  function stopTracking() {
    state.epoch += 1;
    state.source?.close();
    state.source = null;
    clearTimeout(state.pollTimer);
    state.pollTimer = null;
    state.pollController?.abort();
    state.pollController = null;
  }

  function acceptSnapshot(snapshot, id) {
    if (!snapshot || snapshot.schema_version !== 1 || snapshot.run_id !== id
      || !snapshot.summary || !snapshot.parameters || !Array.isArray(snapshot.buckets)
      || !(snapshot.status === "running" || TERMINAL.has(snapshot.status))) {
      throw new Error("运行快照格式不符合 schema_version: 1；请检查后端版本。");
    }
    if (state.snapshot?.run_id === id) {
      if (TERMINAL.has(state.snapshot.status)) return;
      // A slow polling response must not rewind a newer SSE snapshot.
      if (snapshot.status === "running" && count(snapshot.summary.total) < count(state.snapshot.summary.total)) return;
    }
    state.snapshot = snapshot;
    state.unavailable = false;
    storage("set", id);
    renderSnapshot(snapshot);
    if (TERMINAL.has(snapshot.status)) {
      stopTracking();
      set("connection-status", "最终结果已获取；实时连接已关闭。可下载完整 JSON，刷新后自动恢复。");
      if (snapshot.status === "failed") message("运行失败，而非成功完成。请查看错误原因、状态码及节点健康状态，再决定是否重试。");
      else message("");
    }
  }

  function expired(error) {
    stopTracking();
    state.unavailable = true;
    storage("remove");
    set("connection-status", "运行结果已过期或不存在，已停止恢复。");
    message(`${error.message} 最近运行可能已过期；可启动新运行。${state.snapshot ? "页面保留的快照不是已确认的最终结果，可下载留存。" : ""}`);
    controls();
  }

  function track(id) {
    stopTracking();
    const epoch = state.epoch;
    state.fallback = typeof EventSource === "undefined";
    const active = () => epoch === state.epoch && running() && state.snapshot.run_id === id;
    const schedule = (delay) => {
      if (!active()) return;
      clearTimeout(state.pollTimer);
      state.pollTimer = window.setTimeout(poll, delay);
    };
    async function poll() {
      if (!active() || state.pollController) return;
      const controller = new AbortController();
      state.pollController = controller;
      try {
        const snapshot = await api(runPath(id), { signal: controller.signal });
        if (!active()) return;
        acceptSnapshot(snapshot, id);
        if (active() && state.fallback) set("connection-status", "轮询已同步最新结果 · SSE 保持原连接自动重试（保留 Last-Event-ID）。");
      } catch (error) {
        if (!active() || controller.signal.aborted) return;
        if ([404, 410].includes(error.status)) { expired(error); return; }
        set("connection-status", `结果同步失败，2 秒后继续重试；不会把失联当作完成。${error.message}`);
        state.fallback = true;
      } finally {
        if (state.pollController === controller) state.pollController = null;
        if (active()) schedule(state.fallback ? 2000 : 10000);
      }
    }
    if (!state.fallback) {
      const source = new EventSource(`${runPath(id)}/events`);
      state.source = source;
      const receive = (event) => {
        if (!active()) return;
        try {
          acceptSnapshot(JSON.parse(event.data), id);
          if (active()) {
            state.fallback = false;
            set("connection-status", "SSE 实时连接 · 每 1 秒桶更新 · 定期 GET 核对最终状态");
            if (event.type === "complete") schedule(0);
          }
        } catch (error) {
          state.fallback = true;
          set("connection-status", `实时快照无法读取，已回退 GET 轮询。${error.message}`);
          schedule(0);
        }
      };
      source.addEventListener("snapshot", receive);
      source.addEventListener("complete", receive);
      source.onopen = () => {
        if (!active()) return;
        state.fallback = false;
        set("connection-status", "SSE 已连接；定期核对完整快照，防止丢失最终结果。");
      };
      source.onerror = () => {
        if (!active()) return;
        // Keep the same EventSource so native retries retain its Last-Event-ID.
        state.fallback = true;
        set("connection-status", "SSE 断线：已启动 GET 轮询；原 SSE 连接自动重连并保留 Last-Event-ID。");
        schedule(0);
      };
    } else {
      set("connection-status", "浏览器不支持 SSE，使用每 2 秒 GET 轮询直到最终结果。");
    }
    schedule(state.fallback ? 0 : 10000);
  }

  async function startRun(event) {
    event.preventDefault();
    if (state.starting || state.restoring || running() || !state.config) return;
    if (!$("run-form").reportValidity()) return;
    const scenario = selectedScenario();
    if (!scenario?.enabled) { message("该场景未启用；故障演示需要服务端管理授权。"); return; }
    const payload = { scenario_id: scenario.id, limiter_mode: $("mode").value };
    for (const [id, key] of fields) {
      const value = Number($(id).value), bound = state.config.bounds[key];
      if (!Number.isInteger(value) || value < bound.min || value > bound.max) {
        message(`参数 ${key} 必须为 ${bound.min}–${bound.max} 范围内的整数。`);
        $(id).focus();
        return;
      }
      payload[key] = value;
    }
    state.starting = true;
    message("");
    controls();
    let createdId = null;
    try {
      const result = await api("/demo/runs", { method: "POST", body: JSON.stringify(payload) });
      if (typeof result.run_id !== "string" || !result.run_id) throw new Error("启动响应缺少 run_id；请检查服务端日志，勿连续重复启动。");
      createdId = result.run_id;
      storage("set", createdId);
      stopTracking();
      state.snapshot = null;
      state.unavailable = false;
      const snapshot = result.summary ? result : await api(runPath(createdId));
      acceptSnapshot(snapshot, createdId);
      if (running()) track(createdId);
    } catch (error) {
      message(`${error.message} ${createdId ? "服务端已创建运行，刷新页面将按保存的 run_id 恢复；请勿重复启动。" : "若启动请求已发送，服务端可能仍在运行；为避免重复负载，本页面不会自动重试 POST。请等待最大运行时长后再试。"}`);
      if (createdId) {
        // A created but unreadable run must not be replaced by another start.
        state.restoring = true;
        set("run-status", "unknown · 已创建，等待恢复");
        set("run-id", createdId);
        set("connection-status", "无法取得初始快照。请刷新页面恢复该运行；不要重复启动。");
      }
    } finally {
      state.starting = false;
      controls();
    }
  }

  async function cancelRun() {
    if (!running() || state.cancelling) return;
    const id = state.snapshot.run_id;
    state.cancelling = true;
    message("");
    controls();
    try {
      const result = await api(`${runPath(id)}/cancel`, { method: "POST" });
      if (state.snapshot?.run_id !== id) return;
      if (result.summary) acceptSnapshot(result, id);
      else acceptSnapshot(await api(runPath(id)), id);
      if (running()) set("connection-status", "取消请求已提交，等待服务端确认最终状态。");
    } catch (error) {
      if ([404, 410].includes(error.status)) expired(error);
      else message(`取消尚未确认：${error.message} 实时同步仍将继续，可再次取消。`);
    } finally {
      state.cancelling = false;
      controls();
    }
  }

  function downloadRun() {
    if (!state.snapshot) return;
    const blob = new Blob([JSON.stringify(state.snapshot, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `demo-${state.snapshot.run_id.replace(/[^a-zA-Z0-9_-]/g, "_")}.json`;
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  async function restoreRun() {
    const id = storage("get");
    if (!id) { state.restoring = false; controls(); return; }
    set("connection-status", "正在恢复最近一次运行…");
    try {
      const snapshot = await api(runPath(id));
      acceptSnapshot(snapshot, id);
      if (running()) track(id);
      state.restoring = false;
    } catch (error) {
      if ([404, 410].includes(error.status)) {
        state.restoring = false;
        expired(error);
      } else {
        set("run-status", "unknown · 恢复受阻");
        set("run-id", id);
        message(`无法恢复最近运行：${error.message} 为避免重复负载，暂不允许新运行；请修复网络后刷新页面。运行 ID 已保留。`);
      }
    } finally {
      controls();
    }
  }

  $("run-form").addEventListener("submit", startRun);
  $("scenario").addEventListener("change", applyDefaults);
  $("cancel-run").addEventListener("click", cancelRun);
  $("download-run").addEventListener("click", downloadRun);
  $("refresh-health").addEventListener("click", refreshConfig);
  set("browser-origin", window.location.origin);
  renderCharts([]);
  Promise.all([refreshConfig(), restoreRun()]).then(controls);
  state.healthTimer = window.setInterval(refreshConfig, 15000);
  window.addEventListener("pagehide", () => {
    stopTracking();
    clearInterval(state.healthTimer);
  });
  window.addEventListener("pageshow", (event) => {
    if (!event.persisted) return;
    refreshConfig();
    if (running()) track(state.snapshot.run_id);
    clearInterval(state.healthTimer);
    state.healthTimer = window.setInterval(refreshConfig, 15000);
  });
})();
