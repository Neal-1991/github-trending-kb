/* 本地数据同步状态条:轮询 /api/sync/status(只读本地状态,不触碰 GitHub)。
 * 运行中 2 秒、空闲 30 秒刷新;页面隐藏时暂停,恢复可见立即拉取。
 * 动态文本一律 textContent,不使用 innerHTML;不自动刷新页面,
 * 更新完成后提供"刷新查看"按钮(保留当前 URL 与查询参数)。
 */
(function () {
  "use strict";

  var statusText = document.getElementById("sync-status-text");
  var button = document.getElementById("sync-button");
  var preparingText = document.getElementById("preparing-text");
  var preparingRetry = document.getElementById("preparing-retry");
  var preparingPanel = document.getElementById("preparing-panel");
  var csrfToken = "";
  var timer = null;

  var PHASE_LABELS = {
    checking: "正在检查更新…",
    downloading: "正在下载数据…",
    validating: "正在校验数据…",
    building: "正在构建数据库…",
    publishing: "正在发布新版本…"
  };

  function setText(el, value) {
    if (el) {
      el.textContent = value;
    }
  }

  function dataDate(st) {
    return st.active_latest_date ? "数据更新至 " + st.active_latest_date : "";
  }

  function render(st) {
    if (st && st.csrf_token) {
      csrfToken = st.csrf_token;
    }
    var running = !!(st && st.running);
    if (button) {
      if (running) {
        button.hidden = false;
        button.disabled = true;
        button.textContent = PHASE_LABELS[st.phase] || "正在同步…";
        button.dataset.action = "sync";
      } else if (st && st.last_result === "updated") {
        button.hidden = false;
        button.disabled = false;
        button.textContent = "刷新查看";
        button.dataset.action = "reload";
      } else if (st && st.enabled) {
        button.hidden = false;
        button.disabled = false;
        button.textContent = "立即同步";
        button.dataset.action = "sync";
      } else {
        button.hidden = true;
      }
    }

    if (!st) {
      setText(statusText, "同步状态暂时不可用");
    } else if (!st.enabled) {
      setText(statusText, join(["自动同步已关闭", dataDate(st)]));
    } else if (running) {
      setText(statusText, PHASE_LABELS[st.phase] || "正在同步…");
    } else if (st.last_result === "failed") {
      setText(statusText, join([
        st.message || "同步失败,请稍后重试。", "当前仍可查询" + datePart(st)]));
    } else if (st.last_result === "updated") {
      setText(statusText, join(["新数据已就绪", st.active_latest_date
        ? "更新至 " + st.active_latest_date : "", "刷新查看。"]));
    } else if (st.last_result === "up_to_date") {
      setText(statusText, join(["与 GitHub 已同步", datePart(st)]));
    } else if (st.last_result === "never_synced") {
      setText(statusText, join(["尚未同步", st.active_latest_date
        ? "本地数据更新至 " + st.active_latest_date : "正在等待首次检查…"]));
    } else {
      setText(statusText, join(["自动同步待命", dataDate(st)]));
    }

    if (preparingPanel && st) {
      if (!st.running && st.active_latest_date && st.last_result !== "failed") {
        window.location.reload();  // 首次准备完成 → 自动进入首页(计划允许)
        return;
      }
      if (st.last_result === "failed" && st.last_error_message) {
        setText(preparingText, "准备失败:" + st.last_error_message + " 可点击重试。");
      }
    }
  }

  function datePart(st) {
    return st.active_latest_date ? "截至 " + st.active_latest_date + " 的数据" : "已有数据";
  }

  function join(parts) {
    return parts.filter(Boolean).join(" · ");
  }

  function poll() {
    fetch("/api/sync/status", { credentials: "same-origin" })
      .then(function (resp) {
        if (!resp.ok) {
          throw new Error("HTTP " + resp.status);
        }
        return resp.json();
      })
      .then(function (st) {
        render(st);
        schedule(st && st.running ? 2000 : 30000);
      })
      .catch(function () {
        setText(statusText, "同步状态暂时不可用(服务本地运行中)");
        schedule(30000);
      });
  }

  function schedule(delay) {
    if (timer) {
      clearTimeout(timer);
    }
    timer = setTimeout(function () {
      if (document.hidden) {
        schedule(delay);  // 页面隐藏时暂停轮询,恢复可见立即拉取
        return;
      }
      poll();
    }, delay);
  }

  function triggerSync() {
    setText(statusText, "正在安排同步…");
    fetch("/api/sync", {
      method: "POST",
      credentials: "same-origin",
      headers: { "X-CSRF-Token": csrfToken }
    }).then(function (resp) {
      if (!resp.ok) {
        throw new Error("HTTP " + resp.status);
      }
      return resp.json();
    }).then(function (st) {
      if (st.trigger && st.trigger.reason === "disabled") {
        setText(statusText, "自动同步已关闭");
      } else if (st.trigger && st.trigger.reason === "running") {
        setText(statusText, "同步已在进行中");
      }
      poll();
    }).catch(function () {
      setText(statusText, "触发同步失败,请稍后重试");
      schedule(30000);
    });
  }

  if (button) {
    button.addEventListener("click", function () {
      if (button.dataset.action === "reload") {
        window.location.reload();  // 保留当前 URL 与查询参数
      } else if (!button.disabled) {
        triggerSync();
      }
    });
  }
  if (preparingRetry) {
    preparingRetry.addEventListener("click", triggerSync);
  }
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) {
      poll();
    }
  });

  poll();
})();
