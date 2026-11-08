(() => {
  "use strict";

  const TOKEN = new URLSearchParams(window.location.search).get("t") || "";
  const ACTIVE_STATUSES = new Set(["queued", "preparing", "downloading", "pausing", "merging"]);
  const VISIBLE_ACTIVE_STATUSES = new Set([...ACTIVE_STATUSES, "paused"]);

  const state = {
    token: TOKEN,
    ready: false,
    page: "home",
    settings: {
      download_dir: "",
      connections: 12,
      save_cover: false,
      open_folder_when_done: false,
      auto_download: false,
      theme: "light",
      prefer_codec: "avc",
    },
    defaults: {},
    account: { is_login: false },
    tasks: [],
    history: [],
    stats: { active: 0, speed: 0, history_count: 0, today_completed: 0 },
    parse: null,
    parseStatus: "idle",
    parseError: "",
    selectedQualityId: null,
    selectedPage: 1,
    mode: "video",
    saveCover: false,
    lastParsedUrl: "",
    parseController: null,
    parseSeq: 0,
    themeMode: "light",
    themeMedia: window.matchMedia("(prefers-color-scheme: dark)"),
    loginKey: "",
    loginTimer: null,
    loginPolling: false,
    loginClosed: false,
    confirmResolver: null,
    taskRenderedSignature: "",
    previousTaskStatuses: {},
    pollTimer: null,
    pollInFlight: false,
    apiOffline: false,
    autoDownloadTimer: null,
    autoDownloadCountdown: null,
    historyQuery: "",
    historyLoading: false,
    bootedAt: Date.now(),
  };

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

  class AppError extends Error {
    constructor(message, code = "error", status = 0) {
      super(message);
      this.name = "AppError";
      this.code = code;
      this.status = status;
    }
  }

  /* ------------------------------------------------------------------ */
  /* Utilities                                                          */
  /* ------------------------------------------------------------------ */
  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (char) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    }[char]));
  }

  function icon(name, className = "") {
    return `<svg class="icon ${className}" aria-hidden="true"><use href="#i-${name}"></use></svg>`;
  }

  function formatBytes(bytes, digits = 1) {
    const value = Number(bytes) || 0;
    if (value <= 0) return "0 B";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let index = 0;
    let current = value;
    while (current >= 1024 && index < units.length - 1) {
      current /= 1024;
      index += 1;
    }
    const precision = index === 0 ? 0 : digits;
    return `${current.toFixed(precision).replace(/\.0$/, "")} ${units[index]}`;
  }

  function formatSpeed(bytesPerSecond) {
    const value = Number(bytesPerSecond) || 0;
    if (value <= 0) return "0 KB/s";
    return `${formatBytes(value, 1)}/s`;
  }

  function formatDuration(seconds) {
    let total = Math.max(0, Math.round(Number(seconds) || 0));
    const hours = Math.floor(total / 3600);
    total %= 3600;
    const minutes = Math.floor(total / 60);
    const secs = total % 60;
    if (hours > 0) return `${hours}:${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
    return `${minutes}:${String(secs).padStart(2, "0")}`;
  }

  function formatEta(seconds) {
    const value = Number(seconds);
    if (!Number.isFinite(value) || value <= 0) return "--";
    if (value < 60) return `${Math.max(1, Math.round(value))} 秒`;
    if (value < 3600) return `${Math.floor(value / 60)} 分 ${Math.round(value % 60)} 秒`;
    return `${Math.floor(value / 3600)} 小时 ${Math.round((value % 3600) / 60)} 分`;
  }

  function formatCount(value) {
    const number = Number(value) || 0;
    if (number >= 100000000) return `${(number / 100000000).toFixed(1).replace(/\.0$/, "")}亿`;
    if (number >= 10000) return `${(number / 10000).toFixed(1).replace(/\.0$/, "")}万`;
    return String(number);
  }

  function parseDate(value) {
    if (!value) return null;
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? null : date;
  }

  function sameDay(a, b) {
    return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
  }

  function formatDateTime(value) {
    const date = parseDate(value);
    if (!date) return "";
    const now = new Date();
    const time = `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
    if (sameDay(date, now)) return `今天 ${time}`;
    const yesterday = new Date(now);
    yesterday.setDate(now.getDate() - 1);
    if (sameDay(date, yesterday)) return `昨天 ${time}`;
    if (date.getFullYear() === now.getFullYear()) return `${date.getMonth() + 1}月${date.getDate()}日 ${time}`;
    return `${date.getFullYear()}年${date.getMonth() + 1}月${date.getDate()}日`;
  }

  function formatDateOnly(value) {
    const date = parseDate(value);
    if (!date) return "未知日期";
    const now = new Date();
    if (sameDay(date, now)) return "今天";
    const yesterday = new Date(now);
    yesterday.setDate(now.getDate() - 1);
    if (sameDay(date, yesterday)) return "昨天";
    if (date.getFullYear() === now.getFullYear()) return `${date.getMonth() + 1}月${date.getDate()}日`;
    return `${date.getFullYear()}年${date.getMonth() + 1}月${date.getDate()}日`;
  }

  function imageUrl(rawUrl) {
    if (!rawUrl) return "";
    return `/api/image?u=${encodeURIComponent(rawUrl)}&t=${encodeURIComponent(state.token)}`;
  }

  function bindImageFallbacks(root = document) {
    $$("img[data-fallback]", root).forEach((img) => {
      if (img.dataset.bound) return;
      img.dataset.bound = "1";
      img.addEventListener("error", () => {
        img.style.opacity = "0";
        img.closest(".recent-thumb, .history-thumb, .task-cover, .result-cover")?.classList.add("image-missing");
      });
    });
  }

  function normalizeError(error) {
    if (error instanceof AppError) return error;
    if (error?.name === "AbortError") return new AppError("操作超时，请稍后重试。", "timeout");
    return new AppError("操作失败，请稍后重试。", "unknown_error");
  }

  async function api(path, options = {}) {
    const headers = { Accept: "application/json", "X-App-Token": state.token };
    let body;
    if (options.body !== undefined) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(options.body);
    }
    const controller = new AbortController();
    const timeoutMs = options.timeout ?? 60000;
    const timer = window.setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(path, {
        method: options.method || "GET",
        headers,
        body,
        cache: "no-store",
        signal: controller.signal,
      });
      const raw = await response.text();
      let data = {};
      if (raw) {
        try {
          data = JSON.parse(raw);
        } catch {
          data = { error: raw.slice(0, 200) };
        }
      }
      if (!response.ok) {
        throw new AppError(data.error || `请求失败（${response.status}）`, data.code || "http_error", response.status);
      }
      return data;
    } catch (error) {
      if (error?.name === "AbortError") throw new AppError("操作超时，请稍后重试。", "timeout");
      if (error instanceof AppError) throw error;
      throw new AppError("无法连接到本地服务，请确认应用仍在运行。", "local_service_unavailable");
    } finally {
      window.clearTimeout(timer);
    }
  }

  /* ------------------------------------------------------------------ */
  /* Toast / confirm                                                    */
  /* ------------------------------------------------------------------ */
  function dismissToast(node) {
    if (!node || node.classList.contains("is-leaving")) return;
    node.classList.add("is-leaving");
    window.setTimeout(() => node.remove(), 260);
  }

  function showToast(title, message = "", type = "info", duration = 3800) {
    const container = $("#toast-container");
    if (!container) return;
    const toast = document.createElement("div");
    toast.className = `toast ${type === "info" ? "" : type}`.trim();
    const iconName = type === "success" ? "check-circle" : type === "error" ? "alert" : "sparkles";
    toast.innerHTML = `
      <span class="toast-icon">${icon(iconName)}</span>
      <div class="toast-content">
        <strong>${escapeHtml(title)}</strong>
        ${message ? `<p>${escapeHtml(message)}</p>` : ""}
      </div>
      <button class="toast-close" type="button" aria-label="关闭提示">${icon("close")}</button>`;
    $(".toast-close", toast).addEventListener("click", () => dismissToast(toast));
    container.appendChild(toast);
    while (container.children.length > 4) dismissToast(container.firstElementChild);
    if (duration > 0) window.setTimeout(() => dismissToast(toast), duration);
  }

  function confirmAction({ title = "确认操作", message = "", okText = "确认", danger = true }) {
    return new Promise((resolve) => {
      if (state.confirmResolver) {
        state.confirmResolver(false);
        state.confirmResolver = null;
      }
      state.confirmResolver = resolve;
      $("#confirm-title").textContent = title;
      $("#confirm-message").textContent = message;
      const ok = $("#confirm-ok");
      ok.textContent = okText;
      ok.className = `btn ${danger ? "btn-danger" : "btn-primary"}`;
      $("#confirm-modal").classList.add("open");
      window.setTimeout(() => ok.focus(), 60);
    });
  }

  function settleConfirm(value) {
    $("#confirm-modal").classList.remove("open");
    const resolver = state.confirmResolver;
    state.confirmResolver = null;
    if (resolver) resolver(value);
  }

  /* ------------------------------------------------------------------ */
  /* Theme                                                              */
  /* ------------------------------------------------------------------ */
  function resolvedTheme(mode) {
    if (mode === "system") return state.themeMedia.matches ? "dark" : "light";
    return mode === "dark" ? "dark" : "light";
  }

  function applyTheme(mode, persistLocal = true) {
    state.themeMode = ["light", "dark", "system"].includes(mode) ? mode : "light";
    const resolved = resolvedTheme(state.themeMode);
    document.documentElement.dataset.theme = resolved;
    document.documentElement.style.colorScheme = resolved;
    if (persistLocal) {
      try {
        localStorage.setItem("hwz-theme", state.themeMode);
      } catch {
        /* ignore */
      }
    }
    $$("[data-theme-choice]").forEach((button) => {
      button.classList.toggle("active", button.dataset.themeChoice === state.themeMode);
    });
  }

  async function setTheme(mode, persist = true) {
    applyTheme(mode);
    if (!persist || !state.ready) return;
    const previous = state.settings.theme;
    state.settings.theme = mode;
    try {
      const data = await api("/api/settings", { method: "POST", body: { theme: mode }, timeout: 15000 });
      state.settings = { ...state.settings, ...data.settings };
    } catch (error) {
      state.settings.theme = previous;
      showToast("主题保存失败", normalizeError(error).message, "error");
    }
  }

  function initTheme() {
    let stored = "light";
    try {
      stored = localStorage.getItem("hwz-theme") || "light";
    } catch {
      stored = "light";
    }
    applyTheme(stored);
    state.themeMedia.addEventListener("change", () => {
      if (state.themeMode === "system") applyTheme("system", false);
    });
  }

  /* ------------------------------------------------------------------ */
  /* Boot                                                               */
  /* ------------------------------------------------------------------ */
  document.addEventListener("DOMContentLoaded", init);

  async function init() {
    initTheme();
    updateGreeting();
    bindWindowBridge();
    bindCoreEvents();
    bindFeatureEvents();

    if (!state.token) {
      showBootFailure("缺少应用访问令牌，请从桌面应用或启动脚本重新打开。");
      return;
    }

    try {
      const boot = await api("/api/bootstrap", { timeout: 25000 });
      hydrateBootstrap(boot);
    } catch (error) {
      showBootFailure(normalizeError(error).message);
      return;
    }

    state.ready = true;
    hideBootOverlay();
    window.setTimeout(() => $("#url-input")?.focus(), 520);
    scheduleTaskPoll(0);
    window.setTimeout(() => {
      loadHistory({ silent: true }).catch(() => {});
    }, 700);
  }

  function hydrateBootstrap(boot) {
    state.settings = { ...state.settings, ...(boot.settings || {}) };
    state.defaults = boot.defaults || {};
    state.account = boot.account || { is_login: false };
    state.tasks = Array.isArray(boot.tasks) ? boot.tasks : [];
    state.history = Array.isArray(boot.history) ? boot.history : [];
    state.stats = { ...state.stats, ...(boot.stats || {}) };
    state.previousTaskStatuses = Object.fromEntries(state.tasks.map((task) => [task.id, task.status]));
    state.taskRenderedSignature = "";

    applyTheme(state.settings.theme || "light", false);
    updateVersionLabels(boot.app);
    renderAccount();
    renderStats();
    renderRecent();
    renderTaskArea(true);
    renderSettingsForm();
    updateEngineLabels();
    hideLoginHintsIfLoggedIn();
    bindImageFallbacks();
  }

  function updateVersionLabels(app = {}) {
    const version = app.version || "1.0.0";
    $("#version-label").textContent = `v${version}`;
    $("#about-version").textContent = `v${version}`;
  }

  function hideBootOverlay() {
    const overlay = $("#boot-overlay");
    if (!overlay) return;
    window.setTimeout(() => {
      overlay.classList.add("is-hidden");
      window.setTimeout(() => overlay.remove(), 450);
    }, 180);
  }

  function showBootFailure(message) {
    const overlay = $("#boot-overlay");
    if (!overlay) return;
    overlay.innerHTML = `
      <span class="boot-error-mark">${icon("alert")}</span>
      <div class="boot-error-copy">
        <strong>无法启动界面</strong>
        <p>${escapeHtml(message)}</p>
        <button class="btn btn-primary" type="button" id="boot-retry">重新加载</button>
      </div>`;
    $("#boot-retry")?.addEventListener("click", () => window.location.reload());
  }

  function updateGreeting() {
    const hour = new Date().getHours();
    const text = hour < 6 ? "夜深了" : hour < 11 ? "早上好" : hour < 14 ? "中午好" : hour < 18 ? "下午好" : "晚上好";
    const target = $("#greeting");
    if (target) target.textContent = text;
  }

  /* ------------------------------------------------------------------ */
  /* Window bridge                                                      */
  /* ------------------------------------------------------------------ */
  function bindWindowBridge() {
    const markDesktop = () => {
      if (window.pywebview?.api) {
        document.documentElement.classList.add("is-desktop");
      }
    };
    window.addEventListener("pywebviewready", markDesktop);
    markDesktop();
    window.setTimeout(markDesktop, 700);
    window.setTimeout(markDesktop, 1800);

    const call = (name, ...args) => {
      const method = window.pywebview?.api?.[name];
      if (typeof method === "function") {
        Promise.resolve(method(...args)).catch(() => {});
        return true;
      }
      return false;
    };

    $("#win-min")?.addEventListener("click", () => call("minimize"));
    $("#win-max")?.addEventListener("click", () => call("toggle_maximize"));
    $("#win-close")?.addEventListener("click", async () => {
      const active = state.tasks.filter((task) => ["queued", "preparing", "downloading", "pausing", "merging", "paused"].includes(task.status));
      if (active.length > 0) {
        const accepted = await confirmAction({
          title: "仍在下载，确定退出？",
          message: `当前还有 ${active.length} 个任务没有完成。退出后本次进度会停止，建议先暂停并保留文件。`,
          okText: "退出应用",
        });
        if (!accepted) return;
      }
      call("close");
    });
    $("#drag-surface")?.addEventListener("dblclick", () => call("toggle_maximize"));
  }

  /* ------------------------------------------------------------------ */
  /* Core events                                                        */
  /* ------------------------------------------------------------------ */
  function bindCoreEvents() {
    document.addEventListener("click", (event) => {
      const navTarget = event.target.closest("[data-nav]");
      if (navTarget) {
        event.preventDefault();
        navigate(navTarget.dataset.nav);
        return;
      }
      const authAction = event.target.closest("[data-auth-action]");
      if (authAction) {
        event.preventDefault();
        if (authAction.dataset.authAction === "login") openLoginModal();
        if (authAction.dataset.authAction === "logout") logoutAccount();
      }
    });

    $("#theme-btn")?.addEventListener("click", () => {
      const current = resolvedTheme(state.themeMode);
      setTheme(current === "dark" ? "light" : "dark");
    });

    $("#sidebar-account")?.addEventListener("click", () => {
      if (state.account?.is_login) navigate("settings");
      else openLoginModal();
    });

    $("#login-hint")?.addEventListener("click", () => {
      if (state.account?.is_login) {
        navigate("settings");
      } else {
        openLoginModal();
      }
    });

    $("#confirm-cancel")?.addEventListener("click", () => settleConfirm(false));
    $("#confirm-ok")?.addEventListener("click", () => settleConfirm(true));
    $("#confirm-modal")?.addEventListener("click", (event) => {
      if (event.target === $("#confirm-modal")) settleConfirm(false);
    });

    $("#login-close")?.addEventListener("click", closeLoginModal);
    $("#login-modal")?.addEventListener("click", (event) => {
      if (event.target === $("#login-modal")) closeLoginModal();
    });
    $("#qr-refresh")?.addEventListener("click", () => refreshLoginQr());

    $("#pause-all-btn")?.addEventListener("click", pauseOrResumeAll);
    $("#open-downloads-btn")?.addEventListener("click", () => openDownloadsFolder().catch(() => {}));
    $("#open-downloads-history")?.addEventListener("click", () => openDownloadsFolder().catch(() => {}));

    $("#downloads-list")?.addEventListener("click", handleTaskClick);
    $("#history-list")?.addEventListener("click", handleHistoryClick);
    $("#recent-list")?.addEventListener("click", handleRecentClick);

    $("#history-search")?.addEventListener("input", (event) => {
      state.historyQuery = event.target.value.trim();
      renderHistory();
    });

    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        if ($("#confirm-modal")?.classList.contains("open")) {
          settleConfirm(false);
          return;
        }
        if ($("#login-modal")?.classList.contains("open")) {
          closeLoginModal();
          return;
        }
      }
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "v") {
        if (document.activeElement === $("#url-input")) return;
        const target = event.target;
        if (target instanceof HTMLElement && target.closest("input, textarea, [contenteditable='true']")) return;
      }
    });
  }

  /* ------------------------------------------------------------------ */
  /* Navigation                                                         */
  /* ------------------------------------------------------------------ */
  function navigate(page) {
    const allowed = ["home", "downloads", "history", "settings"];
    if (!allowed.includes(page)) return;
    if (page !== "home") cancelAutoDownload();
    state.page = page;
    $$(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.nav === page));
    $$(".page").forEach((section) => section.classList.toggle("active", section.dataset.page === page));
    const main = $("#main-scroll");
    if (main) main.scrollTo({ top: 0, behavior: "smooth" });

    if (page === "downloads") {
      renderTaskArea(true);
      refreshTasksNow().catch(() => {});
    } else if (page === "history") {
      renderHistory();
      loadHistory().catch(() => {});
    } else if (page === "settings") {
      renderSettingsForm();
    } else if (page === "home") {
      renderRecent();
    }
  }

  /* ------------------------------------------------------------------ */
  /* Account                                                            */
  /* ------------------------------------------------------------------ */
  function hideLoginHintsIfLoggedIn() {
    const loginHint = $("#login-hint");
    if (loginHint) loginHint.hidden = Boolean(state.account?.is_login);
  }

  function renderAccount() {
    const account = state.account || { is_login: false };
    const sidebarAvatar = $("#sidebar-avatar");
    const sidebarName = $("#sidebar-account-name");
    const sidebarSub = $("#sidebar-account-sub");
    if (account.is_login) {
      sidebarAvatar.innerHTML = account.face
        ? `<img src="${escapeHtml(imageUrl(account.face))}" alt="" data-fallback>`
        : icon("user");
      sidebarName.textContent = account.uname || "B站用户";
      sidebarSub.textContent = account.vip ? `${account.vip_label || "大会员"} · 已登录` : "已登录 · 解锁高画质";
    } else {
      sidebarAvatar.innerHTML = icon("user");
      sidebarName.textContent = "未登录";
      sidebarSub.textContent = "登录解锁更高画质";
    }
    hideLoginHintsIfLoggedIn();
    renderSettingsAccount();
    bindImageFallbacks();
  }

  function renderSettingsAccount() {
    const target = $("#settings-account");
    if (!target) return;
    const account = state.account || { is_login: false };
    if (account.is_login) {
      target.innerHTML = `
        <span class="settings-account-avatar">
          ${account.face ? `<img src="${escapeHtml(imageUrl(account.face))}" alt="" data-fallback>` : icon("user")}
        </span>
        <div class="settings-account-copy">
          <strong>${escapeHtml(account.uname || "B站用户")}</strong>
          ${account.vip ? `<span class="vip-badge">${escapeHtml(account.vip_label || "大会员")}</span>` : `<span>UID ${escapeHtml(account.mid || "—")}</span>`}
        </div>
        <button class="account-login-btn" type="button" data-auth-action="logout">退出登录</button>`;
    } else {
      target.innerHTML = `
        <span class="settings-account-avatar">${icon("user")}</span>
        <div class="settings-account-copy">
          <strong>未登录</strong>
          <span>登录后可解析 1080P+ / 4K</span>
        </div>
        <button class="account-login-btn" type="button" data-auth-action="login">扫码登录</button>`;
    }
    bindImageFallbacks(target);
  }

  async function logoutAccount() {
    const accepted = await confirmAction({
      title: "退出 B 站账号？",
      message: "退出后，只有公开画质可用。已在本机的登录 Cookie 将被清除。",
      okText: "退出登录",
    });
    if (!accepted) return;
    try {
      const data = await api("/api/logout", { method: "POST", body: {}, timeout: 15000 });
      state.account = data.account || { is_login: false };
      state.parse = null;
      state.parseStatus = "idle";
      state.selectedQualityId = null;
      renderAccount();
      renderParseStage();
      showToast("已退出登录", "", "success");
    } catch (error) {
      showToast("退出失败", normalizeError(error).message, "error");
    }
  }

  /* ------------------------------------------------------------------ */
  /* Stats / engine labels                                              */
  /* ------------------------------------------------------------------ */
  function updateEngineLabels() {
    const connections = Number(state.settings.connections) || 12;
    $$("#engine-connections, .hint-connections, .feature-connections").forEach((el) => {
      el.textContent = String(connections);
    });
  }

  function renderStats() {
    const activeCount = state.tasks.filter((task) => VISIBLE_ACTIVE_STATUSES.has(task.status)).length;
    const speed = state.tasks
      .filter((task) => task.status === "downloading")
      .reduce((sum, task) => sum + (Number(task.speed) || 0), 0);
    const today = Number(state.stats?.today_completed) || 0;

    const badge = $("#nav-task-badge");
    if (badge) {
      const visible = activeCount > 0;
      badge.hidden = !visible;
      badge.textContent = activeCount > 99 ? "99+" : String(activeCount);
    }
    const todayNode = $("#today-completed");
    if (todayNode) todayNode.textContent = String(today);
    const todayDownloads = $("#today-completed-downloads");
    if (todayDownloads) todayDownloads.textContent = String(today);
    const activeNode = $("#active-count");
    if (activeNode) activeNode.textContent = String(activeCount);
    const speedNode = $("#global-speed");
    if (speedNode) speedNode.textContent = formatSpeed(speed);
    const historyCount = $("#history-count");
    if (historyCount) historyCount.textContent = String(Math.max(state.history.length || 0, Number(state.stats?.history_count) || 0));
    updateEngineLabels();
    updatePauseAllButton();
  }

  function updatePauseAllButton() {
    const button = $("#pause-all-btn");
    if (!button) return;
    const pausable = state.tasks.filter((task) => ["queued", "preparing", "downloading"].includes(task.status));
    const resumable = state.tasks.filter((task) => task.status === "paused");
    if (pausable.length > 0) {
      button.innerHTML = `${icon("pause")}全部暂停`;
      button.dataset.mode = "pause";
      button.disabled = false;
    } else if (resumable.length > 0) {
      button.innerHTML = `${icon("play")}全部继续`;
      button.dataset.mode = "resume";
      button.disabled = false;
    } else {
      button.innerHTML = `${icon("pause")}全部暂停`;
      button.dataset.mode = "none";
      button.disabled = true;
    }
  }

  /* ------------------------------------------------------------------ */
  /* Task polling                                                       */
  /* ------------------------------------------------------------------ */
  function scheduleTaskPoll(delay = 1800) {
    window.clearTimeout(state.pollTimer);
    state.pollTimer = window.setTimeout(async () => {
      await refreshTasksNow();
      const hasActive = state.tasks.some((task) => ACTIVE_STATUSES.has(task.status));
      const nextDelay = document.hidden ? 5000 : hasActive ? 900 : 4200;
      scheduleTaskPoll(nextDelay);
    }, delay);
  }

  async function refreshTasksNow() {
    if (!state.ready) return;
    if (state.pollInFlight) return;
    state.pollInFlight = true;
    try {
      const data = await api("/api/tasks", { timeout: 15000 });
      processTaskData(data);
      state.apiOffline = false;
    } catch (error) {
      if (!state.apiOffline) {
        state.apiOffline = true;
        showToast("任务状态获取失败", normalizeError(error).message, "error");
      }
    } finally {
      state.pollInFlight = false;
    }
  }

  function processTaskData(data) {
    const incoming = Array.isArray(data.tasks) ? data.tasks : [];
    const completions = [];
    const failures = [];
    for (const task of incoming) {
      const previous = state.previousTaskStatuses[task.id];
      if (previous && previous !== task.status) {
        if (task.status === "completed") completions.push(task);
        if (task.status === "error") failures.push(task);
      }
      state.previousTaskStatuses[task.id] = task.status;
    }
    state.tasks = incoming;
    if (data.stats) state.stats = { ...state.stats, ...data.stats };
    renderTaskArea();
    renderStats();
    if (completions.length > 0) {
      for (const task of completions) {
        showToast("下载完成", task.title || "视频已保存到本地", "success", 4600);
      }
      loadHistory({ silent: true }).catch(() => {});
      window.setTimeout(() => loadHistory({ silent: true }).catch(() => {}), 800);
    }
    for (const task of failures) {
      showToast("下载失败", task.error || task.message || "请重试", "error", 5000);
    }
  }

  /* ------------------------------------------------------------------ */
  /* Task rendering                                                     */
  /* ------------------------------------------------------------------ */
  function visibleTasks() {
    return state.tasks.filter((task) => task.status !== "cancelled");
  }

  function taskProgressClass(task) {
    if (["queued", "preparing"].includes(task.status)) return "is-queued";
    if (task.status === "merging") return "is-merging";
    if (task.status === "completed") return "is-completed";
    return "";
  }

  function buildTaskStats(task) {
    if (task.status === "downloading") {
      return `
        <span class="speed-value">${formatSpeed(task.speed)}</span>
        <span><strong>${formatBytes(task.received)}</strong> / ${formatBytes(task.total)}</span>
        <span>剩余 ${formatEta(task.eta)}</span>
        <span>${Number(task.connections) || 12} 线程</span>`;
    }
    if (task.status === "pausing") {
      return `<span>正在安全暂停，已下载 <strong>${formatBytes(task.received)}</strong></span>`;
    }
    if (task.status === "paused") {
      return `<span>已下载 <strong>${formatBytes(task.received)}</strong></span><span>共 ${formatBytes(task.total)}</span><span>继续时自动从断点恢复</span>`;
    }
    if (task.status === "merging") {
      return `<span>正在无损合并音视频…</span><span>已下载 ${formatBytes(task.total)}</span>`;
    }
    if (task.status === "queued" || task.status === "preparing") {
      return `<span>${escapeHtml(task.message || "等待开始")}</span><span>${escapeHtml(task.quality_label || "")}</span>`;
    }
    if (task.status === "error") {
      return `<span class="task-error" title="${escapeHtml(task.error || task.message || "下载失败")}">${escapeHtml(task.error || task.message || "下载失败")}</span>`;
    }
    if (task.status === "completed") {
      return `<span>${formatBytes(task.output_size || task.total)}</span><span>${escapeHtml(task.quality_label || "")}</span><span>已无损合并</span><span>${escapeHtml(formatDateTime(task.completed_at))}</span>`;
    }
    return "";
  }

  function buildTaskActions(task) {
    const actions = [];
    if (["queued", "preparing", "downloading"].includes(task.status)) {
      actions.push(`<button class="icon-action" type="button" data-task-action="pause" title="暂停" aria-label="暂停">${icon("pause")}</button>`);
      actions.push(`<button class="icon-action danger-action" type="button" data-task-action="cancel" title="取消下载" aria-label="取消下载">${icon("close")}</button>`);
    } else if (task.status === "pausing" || task.status === "merging") {
      actions.push(`<button class="icon-action danger-action" type="button" data-task-action="cancel" title="取消下载" aria-label="取消下载">${icon("close")}</button>`);
    } else if (task.status === "paused") {
      actions.push(`<button class="icon-action primary-action" type="button" data-task-action="resume" title="继续下载" aria-label="继续下载">${icon("play")}</button>`);
      actions.push(`<button class="icon-action danger-action" type="button" data-task-action="cancel" title="取消下载" aria-label="取消下载">${icon("close")}</button>`);
    } else if (task.status === "error") {
      actions.push(`<button class="small-open-btn" type="button" data-task-action="retry">${icon("refresh")}重试</button>`);
      actions.push(`<button class="icon-action danger-action" type="button" data-task-action="remove" title="移除任务" aria-label="移除任务">${icon("trash")}</button>`);
    } else if (task.status === "completed") {
      actions.push(`<div class="completed-actions">
        <button class="small-open-btn primary" type="button" data-task-action="open">${icon("play")}打开</button>
        <button class="small-open-btn" type="button" data-task-action="folder">${icon("folder")}目录</button>
        <button class="icon-action" type="button" data-task-action="remove" title="移除任务" aria-label="移除任务">${icon("close")}</button>
      </div>`);
    }
    return actions.join("");
  }

  function buildTaskCard(task) {
    const status = task.status;
    const classNames = [`is-${status}`];
    if (status === "paused") classNames.push("is-paused");
    if (status === "completed") classNames.push("is-completed");
    if (status === "error") classNames.push("is-error");
    const progress = Math.max(0, Math.min(100, Number(task.progress) || 0));
    const percent = status === "completed" ? "100%" : status === "queued" || status === "preparing" ? "—" : `${Math.round(progress)}%`;
    const cover = task.cover
      ? `<img src="${escapeHtml(imageUrl(task.cover))}" alt="" loading="lazy" data-fallback>`
      : "";
    const page = Number(task.page_count) > 1 ? `<span class="dot-separator"></span><span>P${Number(task.page) || 1}</span>` : "";
    const mode = task.mode === "audio" ? `<span class="dot-separator"></span><span>音频</span>` : "";
    const playOverlay = status === "completed" ? `<span class="task-cover-play">${icon("play")}</span>` : "";
    const badge = task.duration ? `<span class="task-cover-badge">${escapeHtml(formatDuration(task.duration))}</span>` : "";
    return `
      <article class="task-card ${classNames.join(" ")}" data-task-id="${escapeHtml(task.id)}">
        <div class="task-cover">
          ${cover}${badge}${playOverlay}
        </div>
        <div class="task-main">
          <div class="task-title-row">
            <h3 class="task-title" title="${escapeHtml(task.title || "")}">${escapeHtml(task.title || "B站视频")}</h3>
            <span class="task-status ${escapeHtml(status)}">${escapeHtml(task.status_label || status)}</span>
          </div>
          <div class="task-sub">
            <span>${escapeHtml(task.uploader || "B站视频")}</span>
            <span class="quality-badge">${escapeHtml(task.quality_label || "视频")}</span>
            ${page}${mode}
          </div>
          <div class="task-progress-row">
            <div class="task-progress ${taskProgressClass(task)}"><span class="task-progress-fill" style="width:${progress}%"></span></div>
            <span class="task-percent">${percent}</span>
          </div>
          <div class="task-stats">${buildTaskStats(task)}</div>
        </div>
        <div class="task-actions">${buildTaskActions(task)}</div>
      </article>`;
  }

  function renderTaskArea(force = false) {
    const list = $("#downloads-list");
    const empty = $("#downloads-empty");
    if (!list) return;
    const tasks = visibleTasks();
    const signature = tasks.map((task) => `${task.id}:${task.status}`).join("|");
    if (force || signature !== state.taskRenderedSignature) {
      state.taskRenderedSignature = signature;
      list.innerHTML = tasks.map(buildTaskCard).join("");
      bindImageFallbacks(list);
    } else {
      tasks.forEach(updateTaskCard);
    }
    if (empty) empty.hidden = tasks.length !== 0;
    renderStats();
  }

  function updateTaskCard(task) {
    const card = $(`.task-card[data-task-id="${CSS.escape(task.id)}"]`);
    if (!card) return;
    const fill = $(".task-progress-fill", card);
    if (fill) fill.style.width = `${Math.max(0, Math.min(100, Number(task.progress) || 0))}%`;
    const percent = $(".task-percent", card);
    if (percent) {
      percent.textContent = task.status === "completed"
        ? "100%"
        : ["queued", "preparing"].includes(task.status)
          ? "—"
          : `${Math.round(Number(task.progress) || 0)}%`;
    }
    const stats = $(".task-stats", card);
    if (stats) stats.innerHTML = buildTaskStats(task);
  }

  async function handleTaskClick(event) {
    const button = event.target.closest("[data-task-action]");
    if (!button) return;
    const card = button.closest("[data-task-id]");
    if (!card) return;
    const taskId = card.dataset.taskId;
    const action = button.dataset.taskAction;
    const task = state.tasks.find((item) => item.id === taskId);
    if (!task) return;

    if (action === "open" || action === "folder") {
      try {
        await api("/api/open", {
          method: "POST",
          body: { task_id: taskId, reveal: action === "open" },
          timeout: 15000,
        });
      } catch (error) {
        showToast("无法打开文件", normalizeError(error).message, "error");
      }
      return;
    }

    if (action === "cancel") {
      const accepted = await confirmAction({
        title: "取消这个下载？",
        message: "已下载的临时分片将被清理，此操作无法撤销。",
        okText: "取消下载",
      });
      if (!accepted) return;
    }

    if (action === "remove") {
      try {
        await api(`/api/tasks/${encodeURIComponent(taskId)}/remove`, { method: "POST", body: {}, timeout: 15000 });
        state.tasks = state.tasks.filter((item) => item.id !== taskId);
        delete state.previousTaskStatuses[taskId];
        state.taskRenderedSignature = "";
        renderTaskArea(true);
      } catch (error) {
        showToast("移除失败", normalizeError(error).message, "error");
      }
      return;
    }

    const endpoint = `/api/tasks/${encodeURIComponent(taskId)}/${action}`;
    button.disabled = true;
    try {
      await api(endpoint, { method: "POST", body: {}, timeout: 15000 });
      await refreshTasksNow();
    } catch (error) {
      showToast("操作失败", normalizeError(error).message, "error");
    } finally {
      button.disabled = false;
    }
  }

  async function pauseOrResumeAll() {
    const button = $("#pause-all-btn");
    if (!button || button.disabled) return;
    const mode = button.dataset.mode;
    if (mode === "none") return;
    const targets = mode === "pause"
      ? state.tasks.filter((task) => ["queued", "preparing", "downloading"].includes(task.status))
      : state.tasks.filter((task) => task.status === "paused");
    if (targets.length === 0) return;
    button.disabled = true;
    try {
      await Promise.allSettled(targets.map((task) => api(`/api/tasks/${encodeURIComponent(task.id)}/${mode}`, {
        method: "POST",
        body: {},
        timeout: 15000,
      })));
      showToast(mode === "pause" ? "已暂停全部任务" : "已继续全部任务", "", "success");
    } catch (error) {
      showToast("批量操作失败", normalizeError(error).message, "error");
    } finally {
      await refreshTasksNow();
      updatePauseAllButton();
    }
  }

  async function openDownloadsFolder() {
    try {
      await api("/api/open", { method: "POST", body: { target: "downloads" }, timeout: 15000 });
    } catch (error) {
      showToast("无法打开文件夹", normalizeError(error).message, "error");
      throw error;
    }
  }

  /* ------------------------------------------------------------------ */
  /* Recent                                                             */
  /* ------------------------------------------------------------------ */
  function renderRecent() {
    const target = $("#recent-list");
    if (!target) return;
    const items = state.history.slice(0, 3);
    if (items.length === 0) {
      target.innerHTML = `
        <div class="recent-empty">
          <span class="recent-empty-icon">${icon("sparkles")}</span>
          <div>
            <strong>还没有下载记录</strong>
            <p>粘贴第一个链接，让喜欢的视频留在这里。</p>
          </div>
          <button class="text-link" type="button" data-focus-url>开始收藏 ${icon("arrow-right")}</button>
        </div>`;
      $("[data-focus-url]", target)?.addEventListener("click", focusUrlInput);
      return;
    }
    target.innerHTML = items.map((item) => {
      const cover = item.cover
        ? `<img src="${escapeHtml(imageUrl(item.cover))}" alt="" loading="lazy" data-fallback>`
        : "";
      return `
        <button class="recent-card" type="button" data-history-file="${escapeHtml(item.id)}">
          <span class="recent-thumb">
            ${cover}
            <span class="recent-duration">${escapeHtml(formatDuration(item.duration))}</span>
          </span>
          <span class="recent-info">
            <strong>${escapeHtml(item.title || item.output_name || "B站视频")}</strong>
            <small>${escapeHtml(item.quality_label || "视频")} · ${escapeHtml(formatBytes(item.file_size))}${item.exists === false ? " · 文件已移动" : ""}</small>
          </span>
          <span class="recent-open">${icon("arrow-up-right")}</span>
        </button>`;
    }).join("");
    bindImageFallbacks(target);
  }

  function handleRecentClick(event) {
    const button = event.target.closest("[data-history-file]");
    if (!button) return;
    const item = state.history.find((history) => history.id === button.dataset.historyFile);
    if (!item) return;
    if (item.exists === false) {
      showToast("文件已被移动或删除", "打开下载记录页面查看详情。", "error");
      return;
    }
    openHistoryItem(item.id, true).catch(() => {});
  }

  async function openHistoryItem(historyId, reveal) {
    try {
      await api("/api/open", { method: "POST", body: { history_id: historyId, reveal }, timeout: 15000 });
    } catch (error) {
      showToast("无法打开", normalizeError(error).message, "error");
      throw error;
    }
  }

  /* ------------------------------------------------------------------ */
  /* History                                                            */
  /* ------------------------------------------------------------------ */
  async function loadHistory({ silent = false } = {}) {
    if (state.historyLoading) return;
    state.historyLoading = true;
    try {
      const data = await api("/api/history", { timeout: 15000 });
      state.history = Array.isArray(data.items) ? data.items : [];
      renderHistory();
      renderRecent();
      renderStats();
    } catch (error) {
      if (!silent) showToast("下载记录加载失败", normalizeError(error).message, "error");
    } finally {
      state.historyLoading = false;
    }
  }

  function renderHistory() {
    const target = $("#history-list");
    const empty = $("#history-empty");
    if (!target) return;
    const query = state.historyQuery.toLowerCase();
    const filtered = state.history.filter((item) => {
      if (!query) return true;
      return [item.title, item.uploader, item.bvid, item.quality_label, item.output_name]
        .filter(Boolean)
        .some((value) => String(value).toLowerCase().includes(query));
    });

    const summary = $("#history-summary");
    if (summary) {
      summary.textContent = query
        ? `找到 ${filtered.length} 条记录`
        : state.history.length > 0
          ? `共 ${state.history.length} 条记录`
          : "";
    }
    const count = $("#history-count");
    if (count) count.textContent = String(state.history.length);

    if (filtered.length === 0) {
      target.innerHTML = "";
      if (empty) {
        empty.hidden = false;
        const title = $("h3", empty);
        const copy = $("p", empty);
        if (query) {
          if (title) title.textContent = "没有找到匹配的记录";
          if (copy) copy.textContent = "换一个关键词试试，或清空搜索重新浏览。";
        } else {
          if (title) title.textContent = "还没有下载记录";
          if (copy) copy.textContent = "完成的视频会安全地保存在本地，并在这里留下记录。";
        }
      }
      return;
    }
    if (empty) empty.hidden = true;

    const groups = new Map();
    filtered.forEach((item) => {
      const date = parseDate(item.completed_at || item.created_at) || new Date(0);
      const key = `${date.getFullYear()}-${date.getMonth()}-${date.getDate()}`;
      if (!groups.has(key)) groups.set(key, { label: formatDateOnly(date.toISOString()), items: [] });
      groups.get(key).items.push(item);
    });

    target.innerHTML = Array.from(groups.values()).map((group) => `
      <section class="history-group">
        <div class="history-date">${escapeHtml(group.label)}</div>
        ${group.items.map(buildHistoryRow).join("")}
      </section>`).join("");
    bindImageFallbacks(target);
  }

  function buildHistoryRow(item) {
    const cover = item.cover
      ? `<img src="${escapeHtml(imageUrl(item.cover))}" alt="" loading="lazy" data-fallback>`
      : "";
    const exists = item.exists !== false;
    return `
      <article class="history-row" data-history-id="${escapeHtml(item.id)}">
        <div class="history-thumb">
          ${cover}
          <span class="history-thumb-duration">${escapeHtml(formatDuration(item.duration))}</span>
        </div>
        <div class="history-main">
          <div class="history-title" title="${escapeHtml(item.title || "")}">${escapeHtml(item.title || item.output_name || "B站视频")}</div>
          <div class="history-meta">
            <span class="quality">${escapeHtml(item.quality_label || "视频")}</span>
            <span>${escapeHtml(formatBytes(item.file_size))}</span>
            <span>${escapeHtml(item.uploader || "B站视频")}</span>
            ${item.bvid ? `<span>${escapeHtml(item.bvid)}</span>` : ""}
            ${exists ? "" : `<span class="missing">文件已移动</span>`}
          </div>
        </div>
        <div class="history-actions">
          <button class="icon-action primary-action" type="button" data-history-action="open" title="${exists ? "打开视频" : "文件已被移动"}" aria-label="打开视频" ${exists ? "" : "disabled"}>${icon("play")}</button>
          <button class="icon-action" type="button" data-history-action="folder" title="打开所在文件夹" aria-label="打开所在文件夹" ${exists ? "" : "disabled"}>${icon("folder")}</button>
          <button class="icon-action danger-action" type="button" data-history-action="delete" title="删除记录" aria-label="删除记录">${icon("trash")}</button>
        </div>
      </article>`;
  }

  async function handleHistoryClick(event) {
    const button = event.target.closest("[data-history-action]");
    if (!button) return;
    const row = button.closest("[data-history-id]");
    if (!row) return;
    const historyId = row.dataset.historyId;
    const item = state.history.find((entry) => entry.id === historyId);
    if (!item) return;
    const action = button.dataset.historyAction;

    if (action === "open" || action === "folder") {
      await openHistoryItem(historyId, action === "open").catch(() => {});
      return;
    }
    if (action === "delete") {
      const accepted = await confirmAction({
        title: "删除这条下载记录？",
        message: "仅从列表中移除，不会删除已经下载的视频文件。",
        okText: "删除记录",
      });
      if (!accepted) return;
      try {
        await api("/api/history/delete", { method: "POST", body: { id: historyId }, timeout: 15000 });
        state.history = state.history.filter((entry) => entry.id !== historyId);
        renderHistory();
        renderRecent();
        renderStats();
        showToast("记录已删除", "", "success");
      } catch (error) {
        showToast("删除失败", normalizeError(error).message, "error");
      }
    }
  }

  /* ------------------------------------------------------------------ */
  /* Placeholder: features are appended below                           */
  /* ------------------------------------------------------------------ */
  /* ------------------------------------------------------------------ */
  /* Feature events                                                     */
  /* ------------------------------------------------------------------ */
  function bindFeatureEvents() {
    $("#url-form")?.addEventListener("submit", (event) => {
      event.preventDefault();
      parseVideo({ auto: false });
    });

    const urlInput = $("#url-input");
    urlInput?.addEventListener("input", updateUrlInputUI);
    urlInput?.addEventListener("paste", () => {
      window.setTimeout(() => {
        updateUrlInputUI();
        if (looksLikeBili(urlInput.value)) {
          parseVideo({ auto: true }).catch(() => {});
        }
      }, 40);
    });
    $("#clear-url")?.addEventListener("click", () => {
      if (urlInput) {
        urlInput.value = "";
        urlInput.focus();
      }
      updateUrlInputUI();
    });
    $("#paste-btn")?.addEventListener("click", pasteFromClipboard);
    document.addEventListener("paste", handleGlobalPaste);

    $("#parse-stage")?.addEventListener("click", handleParseStageClick);
    $("#parse-stage")?.addEventListener("change", handleParseStageChange);

    $("#save-dir")?.addEventListener("click", () => saveDownloadDir());
    $("#browse-dir")?.addEventListener("click", browseDownloadDir);
    $("#open-dir")?.addEventListener("click", () => openDownloadsFolder().catch(() => {}));
    $("#restore-dir")?.addEventListener("click", restoreDefaultDir);
    $("#download-dir")?.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        saveDownloadDir();
      }
    });

    $("#connection-options")?.addEventListener("click", (event) => {
      const button = event.target.closest("[data-connections]");
      if (!button) return;
      applyConnectionSetting(Number(button.dataset.connections));
    });

    $("#codec-options")?.addEventListener("click", (event) => {
      const button = event.target.closest("[data-codec]");
      if (!button) return;
      applyCodecSetting(button.dataset.codec);
    });

    $("#theme-options")?.addEventListener("click", (event) => {
      const button = event.target.closest("[data-theme-choice]");
      if (!button) return;
      setTheme(button.dataset.themeChoice);
      state.settings.theme = button.dataset.themeChoice;
      $$("[data-theme-choice]").forEach((item) => item.classList.toggle("active", item === button));
    });

    $("#toggle-save-cover")?.addEventListener("change", async (event) => {
      const checked = Boolean(event.target.checked);
      state.settings.save_cover = checked;
      state.settings = (await patchSettings({ save_cover: checked }, { silent: true })) || state.settings;
    });
    $("#toggle-open-folder")?.addEventListener("change", async (event) => {
      const checked = Boolean(event.target.checked);
      state.settings.open_folder_when_done = checked;
      state.settings = (await patchSettings({ open_folder_when_done: checked }, { silent: true })) || state.settings;
    });
    $("#toggle-auto-download")?.addEventListener("change", async (event) => {
      const checked = Boolean(event.target.checked);
      state.settings.auto_download = checked;
      state.settings = (await patchSettings({ auto_download: checked }, { silent: true })) || state.settings;
    });

    $("#open-data-dir")?.addEventListener("click", async () => {
      try {
        await api("/api/open", { method: "POST", body: { target: "data" }, timeout: 15000 });
      } catch (error) {
        showToast("无法打开数据目录", normalizeError(error).message, "error");
      }
    });

    $("#clear-cache")?.addEventListener("click", async () => {
      const accepted = await confirmAction({
        title: "清理本地缓存？",
        message: "临时分片、封面缓存和解析缓存会被删除，不会影响已经下载的视频。",
        okText: "清理缓存",
        danger: false,
      });
      if (!accepted) return;
      try {
        await api("/api/cache/clear", { method: "POST", body: {}, timeout: 30000 });
        showToast("缓存已清理", "", "success");
      } catch (error) {
        showToast("清理失败", normalizeError(error).message, "error");
      }
    });

    $("#clear-history")?.addEventListener("click", async () => {
      const accepted = await confirmAction({
        title: "清空全部下载记录？",
        message: "只会清除记录列表，已经下载的视频文件不会被删除。",
        okText: "清空记录",
      });
      if (!accepted) return;
      try {
        await api("/api/history/clear", { method: "POST", body: {}, timeout: 15000 });
        state.history = [];
        state.stats.history_count = 0;
        state.stats.today_completed = 0;
        renderHistory();
        renderRecent();
        renderStats();
        showToast("下载记录已清空", "", "success");
      } catch (error) {
        showToast("清空失败", normalizeError(error).message, "error");
      }
    });
  }

  function focusUrlInput() {
    navigate("home");
    const input = $("#url-input");
    if (input) {
      input.focus();
      input.select();
      input.scrollIntoView({ behavior: "smooth", block: "center" });
    }
  }

  function updateUrlInputUI() {
    const input = $("#url-input");
    const clear = $("#clear-url");
    if (!input || !clear) return;
    clear.hidden = input.value.trim().length === 0;
  }

  function looksLikeBili(text) {
    const value = String(text || "").trim();
    if (!value) return false;
    return /(?:https?:\/\/)?(?:[\w-]+\.)*(?:bilibili\.com|b23\.tv|bili2233\.cn)\//i.test(value)
      || /\bBV[0-9A-Za-z]{10}\b/.test(value)
      || /\bav\d{4,}\b/i.test(value);
  }

  function acceptLink(text, { auto = true } = {}) {
    const input = $("#url-input");
    if (!input) return;
    input.value = String(text || "").trim();
    updateUrlInputUI();
    navigate("home");
    parseVideo({ auto }).catch(() => {});
  }

  function handleGlobalPaste(event) {
    if (!state.ready) return;
    const target = event.target;
    if (target instanceof HTMLElement && target.closest("input, textarea, [contenteditable='true']")) return;
    const text = event.clipboardData?.getData("text") || "";
    if (!looksLikeBili(text)) return;
    event.preventDefault();
    acceptLink(text, { auto: true });
  }

  async function pasteFromClipboard() {
    let text = "";
    try {
      text = await navigator.clipboard.readText();
    } catch {
      try {
        if (window.pywebview?.api?.get_clipboard) {
          text = await window.pywebview.api.get_clipboard();
        }
      } catch {
        text = "";
      }
    }
    if (!text || !looksLikeBili(text)) {
      showToast("剪贴板里没有 B 站链接", "复制视频链接后再试一次，也可以直接按 Ctrl + V。", "error");
      $("#url-input")?.focus();
      return;
    }
    const input = $("#url-input");
    if (input) {
      input.value = text.trim();
      updateUrlInputUI();
    }
    navigate("home");
    parseVideo({ auto: true }).catch(() => {});
  }

  /* ------------------------------------------------------------------ */
  /* Parsing                                                            */
  /* ------------------------------------------------------------------ */
  async function parseVideo({ url = "", page = null, auto = false, preserveOptions = false } = {}) {
    if (!state.ready) return;
    const input = $("#url-input");
    const source = String(url || input?.value || state.lastParsedUrl || "").trim();
    if (!source) {
      showToast("请先粘贴视频链接", "支持 BV / av / b23.tv 链接。", "error");
      input?.focus();
      return;
    }
    if (!looksLikeBili(source)) {
      showToast("没有识别到 B 站链接", "请粘贴 bilibili.com 视频链接、BV 号或 b23.tv 短链。", "error");
      input?.focus();
      return;
    }

    cancelAutoDownload();
    if (state.parseController) {
      try {
        state.parseController.abort();
      } catch {
        /* ignore */
      }
    }
    if (input && source !== input.value && !preserveOptions) input.value = source;
    updateUrlInputUI();
    if (state.page !== "home") navigate("home");

    const previousMode = state.mode;
    const previousSaveCover = state.saveCover;
    const controller = new AbortController();
    state.parseController = controller;
    const sequence = ++state.parseSeq;
    state.parseStatus = "loading";
    state.parseError = "";
    setParseButtonLoading(true);
    renderParseLoading();
    scrollParseIntoView();

    try {
      const data = await api("/api/parse", {
        method: "POST",
        body: { url: source, page: page ?? null },
        timeout: 120000,
      });
      if (sequence !== state.parseSeq) return;
      state.parse = data;
      state.parseStatus = "success";
      state.lastParsedUrl = source;
      state.selectedQualityId = data.default_quality_id;
      state.selectedPage = Number(data.page) || 1;
      state.mode = preserveOptions && data.audio ? previousMode : "video";
      state.saveCover = preserveOptions ? previousSaveCover : Boolean(state.settings.save_cover);
      if (data.account) state.account = data.account;
      renderAccount();
      renderParseResult();
      window.setTimeout(() => {
        $(".result-card")?.scrollIntoView({ behavior: "auto", block: "start" });
      }, 120);
      if (auto && state.settings.auto_download) scheduleAutoDownload();
    } catch (error) {
      if (sequence !== state.parseSeq) return;
      if (error?.name === "AbortError") return;
      const normalized = normalizeError(error);
      state.parse = null;
      state.parseStatus = "error";
      state.parseError = normalized.message;
      renderParseError(normalized.message);
      if (auto) showToast("解析失败", normalized.message, "error");
    } finally {
      if (sequence === state.parseSeq) {
        setParseButtonLoading(false);
        if (state.parseController === controller) state.parseController = null;
      }
    }
  }

  function setParseButtonLoading(loading) {
    const button = $("#parse-btn");
    if (!button) return;
    button.classList.toggle("is-loading", loading);
    button.disabled = loading;
  }

  function scrollParseIntoView() {
    window.setTimeout(() => {
      const stage = $("#parse-stage");
      stage?.scrollIntoView({ behavior: "auto", block: "nearest" });
    }, 120);
  }

  function renderParseCase() {
    if (state.parseStatus === "loading") renderParseLoading();
    else if (state.parseStatus === "success" && state.parse) renderParseResult();
    else if (state.parseStatus === "error") renderParseError(state.parseError);
    else renderParseStage();
  }

  function renderParseStage() {
    const stage = $("#parse-stage");
    if (!stage) return;
    stage.innerHTML = "";
  }

  function renderParseLoading() {
    const stage = $("#parse-stage");
    if (!stage) return;
    stage.innerHTML = `
      <div class="parse-loading">
        <div class="loading-cover"></div>
        <div class="loading-content">
          <div class="loading-badge"><span class="spinner"></span>正在解析视频信息</div>
          <div class="skeleton-line title"></div>
          <div class="skeleton-line short"></div>
          <div class="skeleton-line meta"></div>
          <div class="loading-progress-note">
            <span class="loading-dot"></span><span class="loading-dot"></span><span class="loading-dot"></span>
            <span>正在读取清晰度、分P与封面信息…</span>
          </div>
        </div>
      </div>`;
    scrollParseIntoView();
  }

  function renderParseError(message) {
    const stage = $("#parse-stage");
    if (!stage) return;
    stage.innerHTML = `
      <div class="parse-error">
        <span class="error-icon">${icon("alert")}</span>
        <div class="error-copy">
          <strong>解析失败</strong>
          <p>${escapeHtml(message || "请检查链接是否有效，或稍后再试。")}</p>
        </div>
        <div class="error-actions">
          <button class="text-btn" type="button" data-parse-retry>重新解析</button>
        </div>
      </div>`;
  }

  function selectedQuality() {
    if (!state.parse) return null;
    return state.parse.qualities?.find((item) => item.id === state.selectedQualityId)
      || state.parse.qualities?.[0]
      || null;
  }

  function qualityEstimate() {
    if (!state.parse) return 0;
    if (state.mode === "audio") return Number(state.parse.audio?.size) || 0;
    return Number(selectedQuality()?.size) || 0;
  }

  function renderParseResult() {
    const stage = $("#parse-stage");
    const data = state.parse;
    if (!stage || !data) return;
    const quality = selectedQuality();
    const isAudio = state.mode === "audio" && data.audio;
    const cover = data.cover
      ? `<img src="${escapeHtml(imageUrl(data.cover))}" alt="" data-fallback>`
      : "";
    const tags = (data.tags || []).slice(0, 4);
    const duration = formatDuration(data.duration);
    const guestTip = !state.account?.is_login;
    const pageSection = Number(data.page_count) > 1 ? `
      <div class="option-section">
        <div class="option-label">分P <small>共 ${Number(data.page_count)} P</small></div>
        <div class="option-body">
          <div class="page-select-row">
            <div class="select-wrap">
              <select id="page-select" aria-label="选择分P">
                ${(data.pages || []).map((page) => `<option value="${Number(page.page)}" ${Number(page.page) === Number(data.page) ? "selected" : ""}>P${Number(page.page)} · ${escapeHtml(page.title || "")}${page.duration ? ` (${formatDuration(page.duration)})` : ""}</option>`).join("")}
              </select>
              ${icon("chevron-down")}
            </div>
            <span class="page-meta">当前 P${Number(data.page)} · ${escapeHtml(data.page_title || "")}</span>
          </div>
        </div>
      </div>` : "";

    const audioChoice = data.audio ? `
      <div class="audio-choice">
        <span class="audio-choice-icon">${icon("music")}</span>
        <span class="audio-choice-copy">
          <strong>M4A · ${Number(data.audio.abr) || "最佳"} kbps</strong>
          <span>自动提取最佳音轨，不进行二次压缩</span>
        </span>
        <span class="audio-choice-size">${formatBytes(data.audio.size)}</span>
      </div>` : "";

    const qualityOptions = (data.qualities || []).map((item) => `
      <button class="quality-option ${item.id === state.selectedQualityId ? "active" : ""}" type="button"
              data-quality-id="${escapeHtml(item.id)}" aria-pressed="${item.id === state.selectedQualityId}">
        ${item.best ? `<span class="q-best">BEST</span>` : ""}
        <span class="q-dot"></span>
        <span class="q-title">${escapeHtml(item.label)}</span>
        <span class="q-sub">${escapeHtml(item.codec)}${item.fps ? ` · ${Number(item.fps)}fps` : ""} · ${formatBytes(item.size)}</span>
      </button>`).join("");

    stage.innerHTML = `
      <div class="result-card">
        <button class="result-close" type="button" data-parse-close aria-label="关闭解析结果">${icon("close")}</button>
        <div class="result-top">
          <div class="result-cover">
            ${cover}
            <span class="cover-duration">${escapeHtml(duration)}</span>
            ${Number(data.page_count) > 1 ? `<span class="cover-page">P${Number(data.page)}</span>` : ""}
          </div>
          <div class="result-info">
            <span class="result-status">${icon("check-circle")}解析成功</span>
            <h2 class="result-title" title="${escapeHtml(data.title || "")}">${escapeHtml(data.title || "B站视频")}</h2>
            <div class="result-meta">
              <span class="meta-item">
                <span class="up-avatar">${data.uploader?.face ? `<img src="${escapeHtml(imageUrl(data.uploader.face))}" alt="" data-fallback>` : icon("user")}</span>
                ${escapeHtml(data.uploader?.name || "未知UP主")}
              </span>
              ${data.publish_date ? `<span class="meta-item">${escapeHtml(data.publish_date)}</span>` : ""}
              <span class="meta-item">${formatCount(data.view_count)} 播放</span>
              <span class="meta-item">${escapeHtml(data.bvid || "")}</span>
            </div>
            ${tags.length ? `<div class="result-tags">${tags.map((tag) => `<span>${escapeHtml(tag)}</span>`).join("")}</div>` : ""}
          </div>
        </div>
        <div class="result-options">
          ${pageSection}
          <div class="option-section">
            <div class="option-label">下载内容</div>
            <div class="option-body">
              <div class="segment-control" role="group" aria-label="下载内容类型">
                <button class="segment-btn ${!isAudio ? "active" : ""}" type="button" data-mode="video">${icon("film")}视频 MP4</button>
                <button class="segment-btn ${isAudio ? "active" : ""}" type="button" data-mode="audio" ${data.audio ? "" : "disabled"}>${icon("music")}音频 M4A</button>
              </div>
            </div>
          </div>
          <div class="option-section">
            <div class="option-label">${isAudio ? "音质" : "清晰度"}${isAudio ? "" : `<small>${escapeHtml(quality?.codec || "")}</small>`}</div>
            <div class="option-body">
              ${isAudio ? audioChoice : `<div class="quality-grid">${qualityOptions}</div>`}
            </div>
          </div>
          ${guestTip ? `
            <div class="login-nudge">
              ${icon("sparkles")}
              <span>登录 B 站账号，可解锁 1080P+ / 4K 等更高画质。</span>
              <button type="button" data-open-login>扫码登录</button>
            </div>` : ""}
          <div class="download-bar">
            <div class="download-left">
              <label class="check-control">
                <input type="checkbox" id="opt-save-cover" ${state.saveCover ? "checked" : ""}>
                <span class="check-box">${icon("check")}</span>
                同时保存封面
              </label>
              <span class="estimate">预计 <strong>${formatBytes(qualityEstimate())}</strong>${isAudio ? " · 无二次压缩" : " · 含高清音轨"}</span>
            </div>
            <div class="download-actions">
              <button class="primary-btn download-btn" type="button" data-start-download>
                ${icon("download")}开始下载
              </button>
            </div>
          </div>
        </div>
      </div>`;
    bindImageFallbacks(stage);
  }

  function cancelAutoDownload() {
    if (state.autoDownloadTimer) {
      window.clearTimeout(state.autoDownloadTimer);
      state.autoDownloadTimer = null;
    }
    if (state.autoDownloadCountdown) {
      window.clearInterval(state.autoDownloadCountdown);
      state.autoDownloadCountdown = null;
    }
  }

  function scheduleAutoDownload() {
    cancelAutoDownload();
    if (!state.settings.auto_download) return;
    showToast("已按偏好自动准备下载", "将在 3 秒后使用推荐清晰度开始，点击任意选项可取消。", "info", 3300);
    state.autoDownloadTimer = window.setTimeout(() => {
      state.autoDownloadTimer = null;
      if (state.parse && state.parseStatus === "success") {
        startDownload({ auto: true }).catch(() => {});
      }
    }, 3000);
  }

  async function startDownload({ auto = false } = {}) {
    if (!state.parse || state.parseStatus !== "success") return;
    if (!auto) cancelAutoDownload();
    const quality = selectedQuality();
    if (state.mode === "video" && !quality) {
      showToast("请选择一个清晰度", "", "error");
      return;
    }
    if (state.mode === "audio" && !state.parse.audio) {
      showToast("该视频没有可用的独立音轨", "", "error");
      return;
    }
    const button = $("[data-start-download]", $("#parse-stage"));
    if (button) {
      button.disabled = true;
      button.innerHTML = `<span class="spinner"></span>正在创建任务…`;
    }
    try {
      const data = await api("/api/download", {
        method: "POST",
        body: {
          parse_id: state.parse.parse_id,
          quality_id: state.selectedQualityId,
          mode: state.mode,
          save_cover: state.saveCover,
        },
        timeout: 30000,
      });
      const task = data.task;
      if (task) {
        state.tasks = [task, ...state.tasks.filter((item) => item.id !== task.id)];
        state.previousTaskStatuses[task.id] = task.status;
        state.taskRenderedSignature = "";
        renderTaskArea(true);
        renderStats();
        scheduleTaskPoll(300);
      }
      state.parse = null;
      state.parseStatus = "idle";
      state.selectedQualityId = null;
      renderParseStage();
      navigate("downloads");
      showToast("已加入下载队列", task?.title || "任务正在后台加速下载", "success");
    } catch (error) {
      const normalized = normalizeError(error);
      showToast("无法开始下载", normalized.message, "error");
      if (button) {
        button.disabled = false;
        button.innerHTML = `${icon("download")}开始下载`;
      }
      if (normalized.code === "parse_expired") {
        state.parse = null;
        state.parseStatus = "error";
        state.parseError = normalized.message;
        renderParseError(normalized.message);
      }
    }
  }

  function handleParseStageClick(event) {
    const close = event.target.closest("[data-parse-close]");
    if (close) {
      cancelAutoDownload();
      state.parse = null;
      state.parseStatus = "idle";
      state.selectedQualityId = null;
      renderParseStage();
      return;
    }
    const retry = event.target.closest("[data-parse-retry]");
    if (retry) {
      parseVideo({ auto: false }).catch(() => {});
      return;
    }
    const login = event.target.closest("[data-open-login]");
    if (login) {
      openLoginModal();
      return;
    }
    const modeButton = event.target.closest("[data-mode]");
    if (modeButton && !modeButton.disabled) {
      cancelAutoDownload();
      state.mode = modeButton.dataset.mode === "audio" ? "audio" : "video";
      renderParseResult();
      return;
    }
    const qualityButton = event.target.closest("[data-quality-id]");
    if (qualityButton) {
      cancelAutoDownload();
      state.selectedQualityId = qualityButton.dataset.qualityId;
      renderParseResult();
      return;
    }
    const downloadButton = event.target.closest("[data-start-download]");
    if (downloadButton) {
      startDownload({ auto: false }).catch(() => {});
    }
  }

  function handleParseStageChange(event) {
    if (event.target.id === "page-select") {
      cancelAutoDownload();
      const page = Number(event.target.value) || 1;
      const current = state.parse;
      const source = current?.source_url || state.lastParsedUrl;
      if (!source) return;
      parseVideo({
        url: source,
        page,
        auto: false,
        preserveOptions: true,
      }).catch(() => {});
      return;
    }
    if (event.target.id === "opt-save-cover") {
      cancelAutoDownload();
      state.saveCover = Boolean(event.target.checked);
    }
  }

  /* ------------------------------------------------------------------ */
  /* Settings                                                           */
  /* ------------------------------------------------------------------ */
  function renderSettingsForm() {
    const settings = state.settings || {};
    const directory = $("#download-dir");
    if (directory && document.activeElement !== directory) {
      directory.value = settings.download_dir || "";
    }
    $$("#connection-options [data-connections]").forEach((button) => {
      button.classList.toggle("active", Number(button.dataset.connections) === Number(settings.connections));
    });
    $$("#codec-options [data-codec]").forEach((button) => {
      button.classList.toggle("active", button.dataset.codec === (settings.prefer_codec || "avc"));
    });
    const saveCover = $("#toggle-save-cover");
    if (saveCover) saveCover.checked = Boolean(settings.save_cover);
    const openFolder = $("#toggle-open-folder");
    if (openFolder) openFolder.checked = Boolean(settings.open_folder_when_done);
    const autoDownload = $("#toggle-auto-download");
    if (autoDownload) autoDownload.checked = Boolean(settings.auto_download);
    $$("[data-theme-choice]").forEach((button) => {
      button.classList.toggle("active", button.dataset.themeChoice === state.themeMode);
    });
    renderSettingsAccount();
    updateEngineLabels();
  }

  async function patchSettings(patch, { silent = false } = {}) {
    try {
      const data = await api("/api/settings", { method: "POST", body: patch, timeout: 20000 });
      state.settings = { ...state.settings, ...data.settings };
      renderSettingsForm();
      if (!silent) showToast("设置已保存", "", "success", 2200);
      return state.settings;
    } catch (error) {
      showToast("设置保存失败", normalizeError(error).message, "error");
      renderSettingsForm();
      return null;
    }
  }

  async function saveDownloadDir() {
    const input = $("#download-dir");
    const button = $("#save-dir");
    if (!input) return;
    const requested = input.value.trim();
    if (!requested) {
      showToast("下载位置不能为空", "", "error");
      return;
    }
    if (button) {
      button.disabled = true;
      button.textContent = "保存中";
    }
    const previous = state.settings.download_dir;
    try {
      const data = await api("/api/settings", {
        method: "POST",
        body: { download_dir: requested },
        timeout: 20000,
      });
      state.settings = { ...state.settings, ...data.settings };
      input.value = state.settings.download_dir || requested;
      const changed = String(state.settings.download_dir || "").replace(/[\\/]+$/, "").toLowerCase()
        !== requested.replace(/[\/]+$/, "").toLowerCase();
      if (changed && requested !== previous) {
        showToast("该路径不可用", "已保留原来的下载位置，请检查路径或权限。", "error");
      } else {
        showToast("保存位置已更新", state.settings.download_dir, "success");
      }
    } catch (error) {
      showToast("保存失败", normalizeError(error).message, "error");
      input.value = previous || requested;
    } finally {
      if (button) {
        button.disabled = false;
        button.textContent = "保存";
      }
    }
  }

  async function browseDownloadDir() {
    const bridge = window.pywebview?.api;
    if (!bridge?.choose_folder) {
      showToast("请直接输入路径", "桌面版支持文件夹选择器；当前可在输入框中粘贴完整路径。", "info");
      $("#download-dir")?.focus();
      return;
    }
    try {
      const selected = await bridge.choose_folder($("#download-dir")?.value || "");
      if (selected) {
        $("#download-dir").value = selected;
        await saveDownloadDir();
      }
    } catch (error) {
      showToast("无法打开文件夹选择器", "", "error");
    }
  }

  async function restoreDefaultDir() {
    const fallback = state.defaults?.download_dir;
    if (!fallback) {
      showToast("无法获取默认位置", "请手动设置下载目录。", "error");
      return;
    }
    const input = $("#download-dir");
    if (input) input.value = fallback;
    await saveDownloadDir();
  }

  async function applyConnectionSetting(value) {
    if (![4, 8, 12, 16].includes(value)) return;
    if (Number(state.settings.connections) === value) return;
    const previous = Number(state.settings.connections) || 12;
    state.settings.connections = value;
    renderSettingsForm();
    const saved = await patchSettings({ connections: value }, { silent: true });
    if (saved) {
      showToast(`已切换至 ${value} 线程`, "新任务会使用这个并发数。", "success", 2400);
    } else {
      state.settings.connections = previous;
      renderSettingsForm();
    }
  }

  async function applyCodecSetting(codec) {
    if (!["avc", "hevc", "av1"].includes(codec)) return;
    if (state.settings.prefer_codec === codec) return;
    const previous = state.settings.prefer_codec;
    state.settings.prefer_codec = codec;
    renderSettingsForm();
    const saved = await patchSettings({ prefer_codec: codec }, { silent: true });
    if (!saved) {
      state.settings.prefer_codec = previous;
      renderSettingsForm();
      return;
    }
    if (state.parse) {
      cancelAutoDownload();
      state.parse = null;
      state.parseStatus = "idle";
      state.selectedQualityId = null;
      renderParseStage();
      showToast("编码偏好已更新", "请重新解析视频以查看新的编码选项。", "info", 3600);
    } else {
      showToast("编码偏好已更新", "下次解析视频时生效。", "success", 2400);
    }
  }

  /* ------------------------------------------------------------------ */
  /* Login QR                                                           */
  /* ------------------------------------------------------------------ */
  function openLoginModal() {
    cancelAutoDownload();
    if (state.account?.is_login) {
      showToast("你已经登录", state.account.uname || "当前账号已连接", "success");
      navigate("settings");
      return;
    }
    state.loginClosed = false;
    $("#login-modal").classList.add("open");
    refreshLoginQr();
    window.setTimeout(() => $("#login-close")?.focus(), 100);
  }

  function closeLoginModal() {
    state.loginClosed = true;
    state.loginKey = "";
    state.loginGeneration = (state.loginGeneration || 0) + 1;
    stopLoginPolling();
    $("#login-modal").classList.remove("open");
  }

  function stopLoginPolling() {
    if (state.loginTimer) {
      window.clearTimeout(state.loginTimer);
      state.loginTimer = null;
    }
  }

  function setQrStatus(message, mode = "pending") {
    const container = $("#qr-status");
    if (!container) return;
    container.classList.remove("is-scanned", "is-expired");
    if (mode === "scanned") container.classList.add("is-scanned");
    if (mode === "expired" || mode === "error") container.classList.add("is-expired");
    const text = container.querySelector("span:last-child");
    if (text) text.textContent = message || "";
  }

  async function refreshLoginQr() {
    stopLoginPolling();
    state.loginGeneration = (state.loginGeneration || 0) + 1;
    const generation = state.loginGeneration;
    state.loginKey = "";
    const loading = $("#qr-loading");
    const box = $("#qr-box");
    const refresh = $("#qr-refresh");
    if (refresh) refresh.hidden = true;
    if (loading) loading.hidden = false;
    if (box) box.innerHTML = "";
    setQrStatus("正在生成二维码…", "pending");
    try {
      const data = await api("/api/login/qr", { timeout: 20000 });
      if (generation !== state.loginGeneration || state.loginClosed) return;
      state.loginKey = data.key;
      if (box) box.innerHTML = data.svg || "";
      if (loading) loading.hidden = true;
      setQrStatus("请使用哔哩哔哩 App 扫码", "pending");
      scheduleLoginPoll(1400);
    } catch (error) {
      if (generation !== state.loginGeneration) return;
      if (loading) loading.hidden = true;
      setQrStatus("二维码生成失败，请刷新", "error");
      if (refresh) refresh.hidden = false;
    }
  }

  function scheduleLoginPoll(delay = 1500) {
    stopLoginPolling();
    const generation = state.loginGeneration;
    state.loginTimer = window.setTimeout(() => pollLoginQr(generation), delay);
  }

  async function pollLoginQr(generation) {
    if (state.loginClosed || generation !== state.loginGeneration || !state.loginKey) return;
    if (state.loginPolling) {
      scheduleLoginPoll(1500);
      return;
    }
    state.loginPolling = true;
    try {
      const data = await api(
        `/api/login/poll?key=${encodeURIComponent(state.loginKey)}`,
        { timeout: 18000 },
      );
      if (state.loginClosed || generation !== state.loginGeneration) return;
      if (data.state === "success") {
        state.loginKey = "";
        state.loginGeneration += 1;
        stopLoginPolling();
        state.account = data.account || state.account;
        renderAccount();
        showToast("登录成功", `欢迎回来，${state.account.uname || "B站用户"}`, "success", 3200);
        const pendingParse = state.parse;
        const shouldRefreshParse = state.page === "home" && pendingParse;
        const shouldClearParse = Boolean(pendingParse) && !shouldRefreshParse;
        closeLoginModal();
        if (shouldRefreshParse) {
          window.setTimeout(() => {
            parseVideo({
              url: pendingParse.source_url || state.lastParsedUrl,
              page: pendingParse.page,
              preserveOptions: true,
              auto: false,
            }).catch(() => {});
          }, 400);
        } else if (shouldClearParse) {
          state.parse = null;
          state.parseStatus = "idle";
          state.selectedQualityId = null;
          renderParseStage();
        }
        return;
      }
      if (data.state === "expired") {
        state.loginKey = "";
        setQrStatus(data.message || "二维码已过期，请刷新", "expired");
        $("#qr-refresh").hidden = false;
        return;
      }
      setQrStatus(data.message || "等待扫码", data.state === "scanned" ? "scanned" : "pending");
      scheduleLoginPoll(1500);
    } catch (error) {
      if (state.loginClosed || generation !== state.loginGeneration) return;
      setQrStatus(normalizeError(error).message || "登录状态查询失败", "error");
      scheduleLoginPoll(2200);
    } finally {
      state.loginPolling = false;
    }
  }

  /* ------------------------------------------------------------------ */
  /* Initial helpers                                                    */
  /* ------------------------------------------------------------------ */
  function initializeStaticWidgets() {
    updateUrlInputUI();
  }

  window.addEventListener("load", initializeStaticWidgets);



})();


