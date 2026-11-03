"use strict";
const el = (id) => document.getElementById(id);
const limit = 20;
let token = "";
let offset = 0;
let generation = 0;
let receiptTimer;

function item(tag, value) {
  const node = document.createElement(tag);
  node.textContent = value;
  return node;
}

async function api(path, options = {}) {
  const result = await fetch("/api/v1/agent/approvals" + path, {
    ...options,
    cache: "no-store",
    headers: { Authorization: "Bearer " + token, ...options.headers }
  });
  const data = await result.json();
  if (!result.ok) {
    throw Error(data.error?.message || "请求失败");
  }
  return data;
}

function card(row) {
  const article = document.createElement("article");
  article.append(
    item("h2", row.action_summary),
    item("p", row.business_id + " · " + row.step_id + " · 申请人 " + row.subject_id),
    item("p", "变更字段：value（内容已隐藏），version " +
      row.field_diff.version.before + " → " + row.field_diff.version.after),
    item("p", "到期时间 " + new Date(row.expires_at).toLocaleString()),
    item("small", "提案摘要 " + row.proposal_digest)
  );
  if (row.receipt_id) {
    article.append(item("p", "账本回执 " + row.receipt_id));
  }
  article.append(item("p", "执行状态 " + row.commit_status));
  if (row.can_decide) {
    const note = document.createElement("input");
    note.placeholder = "复核备注（可选）";
    note.maxLength = 300;
    article.append(note);
    const controls = document.createElement("div");
    for (const [choice, label] of [["approve", "批准模拟变更"], ["reject", "拒绝提案"]]) {
      const button = item("button", label);
      // 每个决定按钮保留自己的幂等键，同一决定重试时沿用它。
      const key = crypto.randomUUID();
      button.onclick = async () => {
        const active = generation;
        controls.querySelectorAll("button").forEach(control => {
          control.disabled = true;
        });
        try {
          await api("/" + encodeURIComponent(row.approval_id) + "/decision", {
            method: "POST",
            headers: { "Content-Type": "application/json", "Idempotency-Key": key },
            body: JSON.stringify({
              decision: choice,
              proposal_digest: row.proposal_digest,
              comment: note.value
            })
          });
          if (active === generation) {
            await load("决定已记录。");
          }
        } catch (error) {
          if (active === generation) {
            el("feedback").textContent = error.message;
          }
          controls.querySelectorAll("button").forEach(control => {
            control.disabled = false;
          });
        }
      };
      controls.append(button);
    }
    article.append(controls);
  }
  return article;
}

async function load(message = "") {
  clearTimeout(receiptTimer);
  // 重载队列或退出登录都会更新编号；只显示当前身份最后一次请求的结果。
  const active = ++generation;
  try {
    const data = await api("?status=" + el("filter").value + "&limit=" + limit + "&offset=" + offset);
    if (active !== generation) {
      return;
    }
    el("access").hidden = true;
    el("queueControls").hidden = false;
    el("queue").replaceChildren(...data.approvals.map(card));
    el("feedback").textContent = message + (data.approvals.length ? "" : " 当前没有可见的审批单。");
    el("pages").hidden = offset === 0 && data.approvals.length < limit;
    el("prev").disabled = offset === 0;
    el("next").disabled = data.approvals.length < limit;
    // 审批决定可能早于 Worker 回执，只在当前页面还有待提交项时刷新。
    if (el("filter").value === "approved" && data.approvals.some(row =>
      ["not_started", "in_progress"].includes(row.commit_status) &&
      !["completed", "partial", "failed", "cancelled"].includes(row.task_status)
    )) {
      receiptTimer = setTimeout(() => load(message), 1500);
    }
  } catch (error) {
    if (active === generation) {
      el("feedback").textContent = error.message;
    }
  }
}
el("access").onsubmit = (e) => {
  e.preventDefault();
  token = el("credential").value;
  el("credential").value = "";
  offset = 0;
  load();
};
el("exit").onclick = () => {
  clearTimeout(receiptTimer);
  generation++;
  token = "";
  offset = 0;
  el("queue").replaceChildren();
  el("feedback").textContent = "";
  el("queueControls").hidden = el("pages").hidden = true;
  el("access").hidden = false;
};
el("reload").onclick = () => load();
el("filter").onchange = () => {
  offset = 0;
  load();
};
el("prev").onclick = () => {
  offset = Math.max(0, offset - limit);
  load();
};
el("next").onclick = () => {
  offset += limit;
  load();
};
