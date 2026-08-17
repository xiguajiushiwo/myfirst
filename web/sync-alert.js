(function () {
  const POLL_MS = 30000;
  let timer = null;
  let refreshing = false;

  function formatTime(timestamp) {
    if (!timestamp) return "无";
    return new Date(timestamp * 1000).toLocaleString("zh-CN", { hour12: false });
  }

  function modeName() {
    return "增量同步";
  }

  function ensureAlert() {
    let alert = document.getElementById("kingdee-sync-alert");
    if (alert) return alert;
    const style = document.createElement("style");
    style.textContent = `
      #kingdee-sync-alert{position:fixed;right:20px;bottom:20px;z-index:10000;width:min(520px,calc(100vw - 32px));display:none;background:#fff;border:1px solid #f0aaa5;border-left:5px solid #d93025;border-radius:8px;box-shadow:0 16px 42px rgba(91,22,18,.24);padding:15px 16px;color:#1d2330;font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}
      #kingdee-sync-alert.on{display:block}
      #kingdee-sync-alert .ksa-head{display:flex;align-items:center;gap:9px;margin-bottom:7px}
      #kingdee-sync-alert .ksa-icon{width:24px;height:24px;display:grid;place-items:center;border-radius:50%;background:#fdecea;color:#b42318;font-size:15px;font-weight:900;flex:none}
      #kingdee-sync-alert .ksa-title{font-size:15px;font-weight:800;color:#b42318}
      #kingdee-sync-alert .ksa-count{margin-left:auto;color:#b42318;background:#fdecea;border-radius:999px;padding:2px 8px;font-size:12px;font-weight:750}
      #kingdee-sync-alert .ksa-message{font-size:13px;line-height:1.5;color:#4f2630;overflow-wrap:anywhere;margin-bottom:8px}
      #kingdee-sync-alert .ksa-meta{font-size:12px;line-height:1.55;color:#667085}
      #kingdee-sync-alert .ksa-actions{display:flex;gap:8px;justify-content:flex-end;margin-top:11px}
      #kingdee-sync-alert button,#kingdee-sync-alert a{height:34px;display:inline-flex;align-items:center;justify-content:center;border-radius:7px;padding:0 12px;font:inherit;font-size:12px;font-weight:750;text-decoration:none;cursor:pointer}
      #kingdee-sync-alert a{border:1px solid #d0d5dd;color:#344054;background:#fff}
      #kingdee-sync-alert button{border:1px solid #b42318;color:#fff;background:#b42318}
      #kingdee-sync-alert button:disabled{opacity:.6;cursor:wait}
      @media(max-width:640px){#kingdee-sync-alert{right:16px;bottom:16px}}
    `;
    document.head.appendChild(style);
    alert = document.createElement("aside");
    alert.id = "kingdee-sync-alert";
    alert.setAttribute("role", "alert");
    alert.setAttribute("aria-live", "assertive");
    alert.innerHTML = `
      <div class="ksa-head"><span class="ksa-icon">!</span><span class="ksa-title"></span><span class="ksa-count"></span></div>
      <div class="ksa-message"></div>
      <div class="ksa-meta"></div>
      <div class="ksa-actions"><a href="/orders">查看采购订单</a><button type="button">立即重试</button></div>
    `;
    alert.querySelector("button").addEventListener("click", retrySync);
    document.body.appendChild(alert);
    return alert;
  }

  function render(status) {
    const alert = ensureAlert();
    if (!status.last_error) {
      alert.classList.remove("on");
      return;
    }
    const count = Number(status.consecutive_failures || 1);
    alert.querySelector(".ksa-title").textContent = `金蝶${modeName(status.last_mode)}失败`;
    alert.querySelector(".ksa-count").textContent = `连续 ${count} 次`;
    alert.querySelector(".ksa-message").textContent = status.last_error;
    alert.querySelector(".ksa-meta").textContent =
      `失败时间：${formatTime(status.last_error_at)}　上次成功：${formatTime(status.last_success)}`;
    alert.dataset.syncMode = "incremental";
    alert.classList.add("on");
  }

  async function refresh() {
    if (refreshing) return;
    refreshing = true;
    try {
      const response = await fetch("/api/order-sync/status", { cache: "no-store" });
      if (response.ok) render(await response.json());
    } catch (_) {
    } finally {
      refreshing = false;
    }
  }

  async function retrySync(event) {
    const button = event.currentTarget;
    button.disabled = true;
    button.textContent = "重试中...";
    try {
      const form = new FormData();
      form.append("force", "true");
      await fetch("/api/orders/sync_kingdee", { method: "POST", body: form });
    } finally {
      button.disabled = false;
      button.textContent = "立即重试";
      await refresh();
    }
  }

  function start() {
    ensureAlert();
    refresh();
    timer = window.setInterval(refresh, POLL_MS);
  }

  window.kingdeeSyncAlert = { refresh };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
  window.addEventListener("beforeunload", function () { if (timer) window.clearInterval(timer); });
}());
