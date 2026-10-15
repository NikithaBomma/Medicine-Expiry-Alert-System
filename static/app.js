const state = { medicines: [], alerts: [], editingId: null, searchTimer: null };
const userRole = document.body.dataset.role;
const isManagement = userRole === "management";
const isGlobalManagement = document.body.dataset.globalManagement === "true";
const isBranchStaff = isManagement && !isGlobalManagement;
const voicePreferenceKey = `meditrack-voice-${document.body.dataset.userId}`;
const spokenAlertsKey = `${voicePreferenceKey}-spoken`;

const elements = {
  rows: document.getElementById("medicine-rows"),
  alerts: document.getElementById("alert-list"),
  modal: document.getElementById("medicine-modal"),
  form: document.getElementById("medicine-form"),
  error: document.getElementById("form-error"),
};

const escapeHtml = (value) =>
  String(value).replace(/[&<>"']/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[character]);

function friendlyRequestError(error, fallback) {
  if (error instanceof TypeError && error.message.toLowerCase().includes("fetch")) {
    return "Can’t reach MediTrack. Make sure the Flask server is running, then try again.";
  }
  return error.message || fallback;
}

async function loadBranches() {
  try {
    const response = await fetch("/api/branches");
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Could not load branches.");
    const branches = data.branches || [];
    const branchSelects = [
      document.getElementById("medicine-branch"),
      document.getElementById("staff-branch"),
      document.getElementById("branch-filter"),
    ].filter(Boolean);
    branchSelects.forEach((select) => {
      const currentValue = select.value;
      const isStaffSelect = select.id === "staff-branch";
      const isFilter = select.id === "branch-filter";
      const first = isStaffSelect
        ? '<option value="">Select a branch</option>'
        : isFilter ? '<option value="">All branches</option>'
          : '<option value="">Unassigned inventory</option>';
      select.innerHTML = first + branches.map((branch) =>
        `<option value="${branch.id}">${escapeHtml(branch.name)}</option>`
      ).join("");
      if (branches.some((branch) => String(branch.id) === currentValue)) {
        select.value = currentValue;
      }
      if (isStaffSelect) select.disabled = branches.length === 0;
    });
    const count = document.getElementById("branch-count");
    if (count) count.textContent = `${branches.length} ${branches.length === 1 ? "branch" : "branches"}`;
    const list = document.getElementById("branch-list");
    if (list) {
      list.innerHTML = branches.length
        ? branches.map((branch) => `<article class="branch-list-item">
            <span class="branch-avatar">${escapeHtml(initials(branch.name))}</span>
            <span><strong>${escapeHtml(branch.name)}</strong><small>${escapeHtml(branch.notification_email)}</small></span>
            <span class="branch-staff-count">${branch.staff_count || 0} staff</span>
          </article>`).join("")
        : "<p>No branches yet. Create a branch to start organizing inventory.</p>";
    }
  } catch (error) {
    const list = document.getElementById("branch-list");
    if (list) list.textContent = friendlyRequestError(error, "Could not load branches.");
  }
}

async function submitBranchForm(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const error = document.getElementById("branch-error");
  error.hidden = true;
  const payload = Object.fromEntries(new FormData(form));
  try {
    const response = await fetch("/api/branches", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Could not create branch.");
    form.reset();
    showToast(`Branch ${data.branch.name} created.`);
    await loadBranches();
  } catch (exception) {
    error.textContent = friendlyRequestError(exception, "Could not create branch.");
    error.hidden = false;
  }
}

async function submitStaffForm(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const error = document.getElementById("staff-error");
  error.hidden = true;
  const payload = Object.fromEntries(new FormData(form));
  payload.branch_id = Number(payload.branch_id);
  try {
    const response = await fetch("/api/branch-staff", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Could not create staff account.");
    form.reset();
    showToast(`Staff account created for ${data.staff.name} (${data.staff.branch_name}).`);
    await loadBranches();
  } catch (exception) {
    error.textContent = friendlyRequestError(exception, "Could not create staff account.");
    error.hidden = false;
  }
}

function formatDate(value) {
  return new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", year: "numeric" })
    .format(new Date(`${value}T00:00:00`));
}

function statusTone(medicine) {
  if (medicine.status === "expired" || medicine.status === "7") return "red";
  if (medicine.status === "15" || medicine.status === "30" || medicine.status === "90") return "amber";
  return "blue";
}

function initials(name) {
  return name.trim().split(/\s+/).slice(0, 2).map((part) => part[0]).join("").toUpperCase();
}

function renderStats(stats) {
  document.getElementById("stat-total").textContent = stats.total;
  document.getElementById("stat-expiring").textContent = stats.expiring_soon;
  document.getElementById("stat-expired").textContent = stats.expired;
  if (isManagement) {
    document.getElementById("stat-low-stock").textContent = stats.low_stock;
    const alertCount = stats.expiring_soon + stats.expired + stats.low_stock;
    document.getElementById("alert-nav-count").textContent = alertCount;
  }
}

function renderAlerts(medicines) {
  const attention = medicines
    .filter((medicine) => medicine.status !== "healthy" || (isManagement && medicine.low_stock))
    .sort((a, b) => {
      const rank = (medicine) => medicine.status === "expired" ? 0
        : medicine.status === "7" ? 1 : medicine.status === "15" ? 2
          : medicine.status === "30" ? 3 : medicine.status === "90" ? 4 : 5;
      return rank(a) - rank(b) || a.days_remaining - b.days_remaining;
    })
    .slice(0, 3);
  const count = medicines.filter((medicine) => medicine.status !== "healthy" || medicine.low_stock).length;
  const priorityCount = document.getElementById("priority-count");
  if (priorityCount) priorityCount.textContent = `${count} ${count === 1 ? "alert" : "alerts"}`;
  if (!attention.length) {
    elements.alerts.innerHTML = '<div class="alerts-empty">You’re all caught up — no medicines need attention right now.</div>';
    return;
  }
  elements.alerts.innerHTML = attention.map((medicine) => {
    const tone = medicine.status === "expired" || medicine.status === "7" ? "red"
      : medicine.status === "healthy" ? "purple" : "amber";
    const reason = medicine.status === "expired"
      ? `<strong>Expired ${Math.abs(medicine.days_remaining)} days ago</strong>`
      : medicine.status === "healthy" && isManagement
        ? `<strong>Only ${medicine.quantity} units left</strong> · Low stock`
        : `<strong>${medicine.status_label}</strong> · ${medicine.days_remaining} days remaining`;
    const label = medicine.status === "healthy" ? "Low stock"
      : medicine.status === "expired" ? "Expired"
        : medicine.days_remaining === 0 ? "Expires today"
          : `${medicine.days_remaining} days left`;
    return `<div class="alert-row ${isManagement ? "" : "personal-alert-row"}">
      <div class="alert-medicine"><span class="medicine-initial initial-${tone}">${escapeHtml(initials(medicine.name))}</span><span class="alert-name">${escapeHtml(medicine.name)}</span></div>
      ${isManagement ? `<span class="alert-batch">${escapeHtml(medicine.batch_number)}</span>` : ""}
      <span class="alert-reason">${isManagement ? reason : `<strong>${escapeHtml(medicine.status_label)}</strong> · ${medicine.days_remaining} days remaining`}</span>
      <span class="alert-pill pill-${tone}">${escapeHtml(label)}</span>
    </div>`;
  }).join("");
}

function renderMedicines(medicines) {
  document.getElementById("inventory-count").textContent = `${medicines.length} ${medicines.length === 1 ? "item" : "items"}`;
  document.getElementById("table-summary").textContent = `Showing ${medicines.length} ${medicines.length === 1 ? "medicine" : "medicines"}`;
  document.getElementById("empty-state").hidden = medicines.length !== 0;
  elements.rows.innerHTML = medicines.map((medicine) => {
    const tone = statusTone(medicine);
    if (!isManagement) {
      const expiryLabel = medicine.status === "expired"
        ? `Expired ${Math.abs(medicine.days_remaining)} days ago`
        : medicine.days_remaining === 0 ? "Expires today" : formatDate(medicine.expiry_date);
      return `<tr class="medicine-row personal-medicine-row">
        <td><div class="medicine-cell"><span class="medicine-initial table-initial initial-${tone}">${escapeHtml(initials(medicine.name))}</span><span class="medicine-copy"><strong>${escapeHtml(medicine.name)}</strong></span></div></td>
        <td class="expiry-cell">${escapeHtml(expiryLabel)}</td>
        <td><span class="status-pill pill-${tone}">${escapeHtml(medicine.days_remaining === 0 ? "Expires today" : medicine.status_label)}</span></td>
        <td class="action-cell"><div class="row-actions"><button class="row-action delete" data-action="delete" data-id="${medicine.id}" aria-label="Remove ${escapeHtml(medicine.name)}" title="Remove">×</button></div></td>
      </tr>`;
    }
    const uploaderLabel = medicine.uploader_name
      ? `Uploaded by ${escapeHtml(medicine.uploader_name)}`
      : "Uploader details needed for alerts";
    const branchLabel = medicine.branch_name
      ? `<small class="medicine-branch-label">${escapeHtml(medicine.branch_name)}</small>`
      : "";
    const expiryLabel = medicine.status === "expired"
      ? `Expired ${Math.abs(medicine.days_remaining)} days ago`
      : medicine.days_remaining === 0 ? "Expires today" : formatDate(medicine.expiry_date);
    const statusLabel = medicine.status === "expired" ? "Expired"
      : medicine.status === "healthy" ? "In date"
        : medicine.days_remaining === 0 ? "Expires today" : `Expires in ${medicine.status} days`;
    return `<tr class="medicine-row">
      <td><div class="medicine-cell"><span class="medicine-initial table-initial initial-${tone}">${escapeHtml(initials(medicine.name))}</span><span class="medicine-copy"><strong>${escapeHtml(medicine.name)}</strong><small class="${medicine.uploader_name ? "" : "contact-missing"}">${uploaderLabel}</small>${branchLabel}</span></div></td>
      <td class="batch-cell">${escapeHtml(medicine.batch_number)}</td>
      <td class="quantity-cell ${medicine.low_stock ? "quantity-low" : ""}">${medicine.quantity} units${medicine.low_stock ? ' <span title="Low stock">⚑</span>' : ""}</td>
      <td class="expiry-cell">${escapeHtml(expiryLabel)}</td>
      <td><span class="status-pill pill-${tone}">${escapeHtml(statusLabel)}</span></td>
      <td class="action-cell"><div class="row-actions">
        <button class="row-action" data-action="edit" data-id="${medicine.id}" aria-label="Edit ${escapeHtml(medicine.name)}" title="Edit">✎</button>
        <button class="row-action delete" data-action="delete" data-id="${medicine.id}" aria-label="Delete ${escapeHtml(medicine.name)}" title="Delete">×</button>
      </div></td>
    </tr>`;
  }).join("");
}

function renderExpiryFilters(alerts) {
  const expiryAlerts = alerts.filter((medicine) => medicine.status !== "healthy");
  const groups = {
    90: expiryAlerts.filter((medicine) => medicine.status !== "expired" && Number(medicine.status) <= 90),
    15: expiryAlerts.filter((medicine) => medicine.status !== "expired" && Number(medicine.status) <= 15),
    7: expiryAlerts.filter((medicine) => medicine.status !== "expired" && Number(medicine.status) <= 7),
    expired: expiryAlerts.filter((medicine) => medicine.status === "expired"),
  };
  Object.entries(groups).forEach(([key, medicines]) => {
    document.getElementById(`expiry-count-${key}`).textContent = medicines.length;
  });
  const activeFilter = document.getElementById("status-filter").value;
  document.querySelectorAll("[data-expiry-filter]").forEach((button) => {
    button.setAttribute("aria-pressed", String(button.dataset.expiryFilter === activeFilter));
  });
  const list = document.getElementById("expiry-filter-list");
  const upcoming = [...groups.expired, ...groups[7], ...groups[15], ...groups[90]]
    .filter((medicine, index, all) => all.findIndex((item) => item.id === medicine.id) === index)
    .slice(0, 6);
  list.innerHTML = upcoming.length
    ? upcoming.map((medicine) => {
      const summary = medicine.status === "expired"
        ? `Expired ${Math.abs(medicine.days_remaining)} days ago`
        : medicine.days_remaining === 0 ? "Expires today" : `${medicine.days_remaining} days left`;
      return `<div class="expiry-filter-item ${medicine.status === "expired" ? "is-expired" : "is-upcoming"}"><strong>${escapeHtml(medicine.name)}</strong><small>${escapeHtml(summary)}</small></div>`;
    }).join("")
    : "<p>No medicines expiring soon.</p>";
}

function voiceAlert(medicines) {
  if (localStorage.getItem(voicePreferenceKey) === "disabled" || !("speechSynthesis" in window)) return;
  const spoken = new Set(JSON.parse(sessionStorage.getItem(spokenAlertsKey) || "[]"));
  const urgent = medicines.filter((medicine) => medicine.status !== "healthy")
    .sort((a, b) => a.days_remaining - b.days_remaining)
    .filter((medicine) => {
      const key = `${medicine.id}:${medicine.status}:${medicine.expiry_date}`;
      if (spoken.has(key)) return false;
      spoken.add(key);
      return true;
    });
  if (!urgent.length) return;
  const messages = urgent.map((medicine) => medicine.status === "expired"
    ? `Medicine ${medicine.name} expired ${Math.abs(medicine.days_remaining)} days ago.`
    : medicine.days_remaining === 0
      ? `Medicine ${medicine.name} expires today.`
      : `Medicine ${medicine.name} expires in ${medicine.days_remaining} days.`);
  sessionStorage.setItem(spokenAlertsKey, JSON.stringify([...spoken].slice(-100)));
  window.speechSynthesis.cancel();
  const utterance = new SpeechSynthesisUtterance(messages.join(" "));
  utterance.onerror = () => showToast("Could not play spoken alerts. Use the voice control to try again.", true);
  window.speechSynthesis.speak(utterance);
}

function updateVoiceAlertButton() {
  const button = document.getElementById("voice-alerts-toggle");
  const enabled = localStorage.getItem(voicePreferenceKey) !== "disabled";
  button.disabled = !("speechSynthesis" in window);
  button.textContent = !("speechSynthesis" in window)
    ? "Voice alerts unavailable"
    : enabled ? "🔊 Voice alerts on" : "🔈 Voice alerts muted";
  button.setAttribute("aria-pressed", String(enabled && !button.disabled));
}

async function loadInventory() {
  const parameters = new URLSearchParams();
  const search = document.getElementById("search-input").value.trim();
  const status = document.getElementById("status-filter").value;
  const branchFilter = document.getElementById("branch-filter");
  if (search) parameters.set("q", search);
  if (status !== "all") parameters.set("status", status);
  if (branchFilter && branchFilter.value) parameters.set("branch_id", branchFilter.value);
  try {
    const response = await fetch(`/api/medicines?${parameters.toString()}`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Could not load the inventory.");
    state.medicines = data.medicines;
    state.alerts = data.alerts;
    renderStats(data.stats);
    renderAlerts(data.alerts);
    renderMedicines(data.medicines);
    renderExpiryFilters(data.alerts);
    voiceAlert(data.alerts);
    if ("Notification" in window && Notification.permission === "granted") notifyAboutInventory(data.alerts);
  } catch (error) {
    const message = friendlyRequestError(error, "Could not load the inventory.");
    elements.rows.innerHTML = `<tr><td colspan="${isManagement ? 6 : 4}" class="loading-cell">${escapeHtml(message)}</td></tr>`;
    showToast(message, true);
  }
}

function openModal(medicine = null) {
  state.editingId = medicine ? medicine.id : null;
  elements.form.reset();
  elements.error.hidden = true;
  document.getElementById("modal-title").textContent = medicine ? "Edit medicine" : "Add medicine";
  document.getElementById("save-medicine").textContent = medicine ? "Save changes" : "Save medicine";
  if (medicine) {
    const fields = isManagement
      ? ["name", "batch_number", "quantity", "expiry_date", "uploader_name", "uploader_email"]
      : ["name", "expiry_date"];
    for (const field of fields) {
      elements.form.elements[field].value = medicine[field];
    }
  }
  const medicineBranch = document.getElementById("medicine-branch");
  if (medicineBranch) medicineBranch.value = medicine?.branch_id || "";
  elements.modal.hidden = false;
  elements.form.elements.name.focus();
}

function managementContactError() {
  if (!elements.form.elements.uploader_email.value.trim()) {
    return "Enter an email address so the uploader can receive medicine alerts.";
  }
  return "";
}

function closeModal() {
  elements.modal.hidden = true;
  state.editingId = null;
}

async function saveMedicine(event) {
  event.preventDefault();
  const payload = Object.fromEntries(new FormData(elements.form));
  if (isManagement) {
    payload.quantity = Number(payload.quantity);
    payload.notify_email = true;
    payload.notify_whatsapp = false;
    payload.uploader_whatsapp = "";
    if (payload.branch_id) payload.branch_id = Number(payload.branch_id);
  } else {
    payload.notify_whatsapp = false;
    payload.uploader_whatsapp = "";
  }
  if (isManagement) {
    const contactError = managementContactError();
    if (contactError) {
      elements.error.textContent = contactError;
      elements.error.hidden = false;
      return;
    }
  }
  const wasEditing = state.editingId !== null;
  const saveButton = document.getElementById("save-medicine");
  saveButton.disabled = true;
  try {
    const response = await fetch(state.editingId ? `/api/medicines/${state.editingId}` : "/api/medicines", {
      method: state.editingId ? "PUT" : "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Could not save this medicine.");
    closeModal();
    const failed = (data.notifications || []).filter((item) =>
      item.status === "failed" || item.status === "unavailable");
    if (failed.length) {
      showToast(`Medicine saved, but ${failed.map((item) => `${item.channel}: ${item.message}`).join("; ")}`, true);
    } else {
      const sent = (data.notifications || []).filter((item) => item.status === "sent");
      const action = wasEditing ? "updated" : "added";
      showToast(sent.length
        ? `Medicine ${action}; ${sent.length} email alert${sent.length === 1 ? "" : "s"} sent to uploader and branch contacts.`
        : isManagement
          ? `Medicine ${action}. Expiry alerts follow the uploader’s selected channels.`
            : `Medicine ${action}. Expiry reminders will go to ${payload.uploader_email} when due, if email alerts are enabled and configured.`);
    }
    await loadInventory();
    loadNotificationSettings();
  } catch (error) {
    elements.error.textContent = friendlyRequestError(error, "Could not save this medicine.");
    elements.error.hidden = false;
  } finally {
    saveButton.disabled = false;
  }
}

async function deleteMedicine(id) {
  const medicine = state.medicines.find((item) => item.id === id);
  if (!medicine || !window.confirm(`${isManagement ? "Delete" : "Remove"} ${medicine.name}${medicine.batch_number ? ` (batch ${medicine.batch_number})` : ""} ${isManagement ? "from your inventory" : "from your medicines"}?`)) return;
  try {
    const response = await fetch(`/api/medicines/${id}`, { method: "DELETE" });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Could not delete this medicine.");
    showToast("Medicine deleted.");
    await loadInventory();
  } catch (error) {
    showToast(friendlyRequestError(error, "Could not delete this medicine."), true);
  }
}

function showToast(message, isError = false) {
  const toast = document.createElement("div");
  toast.className = `toast${isError ? " error" : ""}`;
  toast.textContent = message;
  document.getElementById("toast-region").append(toast);
  window.setTimeout(() => toast.remove(), 3500);
}

function renderNotificationHistory(history) {
  document.getElementById("history-count").textContent = history.length;
  const container = document.getElementById("notification-history");
  if (!history.length) {
    container.innerHTML = '<p class="history-empty">No notification deliveries yet.</p>';
    return;
  }
  container.innerHTML = history.map((item) => {
    const event = item.event.replace("expiry-", "Expiry ").replaceAll("-", " ");
    return `<div class="history-row">
      <span class="history-status ${escapeHtml(item.status)}">${escapeHtml(item.status)}</span>
      <span class="history-event"><strong>${escapeHtml(item.medicine_name)}</strong><small>${escapeHtml(event)} · ${escapeHtml(item.channel)} · ${escapeHtml(item.recipient)}</small></span>
      <span class="history-time">${escapeHtml(item.created_at)}</span>
      ${item.error ? `<span class="history-error">${escapeHtml(item.error)}</span>` : ""}
    </div>`;
  }).join("");
}

function updateBrowserAlertButtons() {
  const buttons = [
    document.getElementById("enable-notifications"),
    document.getElementById("sidebar-notifications"),
    document.getElementById("enable-personal-browser-alerts"),
  ].filter(Boolean);
  if (!("Notification" in window)) {
    buttons.forEach((button) => {
      button.disabled = true;
      button.title = "This browser does not support browser notifications.";
      if (button.id !== "enable-notifications") button.textContent = "Browser alerts unavailable";
    });
    return;
  }
  const permission = Notification.permission;
  const label = permission === "granted" ? "Browser alerts enabled"
    : permission === "denied" ? "Browser alerts blocked — change site settings"
      : "Enable browser alerts";
  buttons.forEach((button) => {
    button.title = permission === "denied"
      ? "Allow notifications for this site in your browser's site settings, then reload."
      : label;
    if (button.id !== "enable-notifications") button.textContent = label;
    button.setAttribute("aria-label", label);
    button.classList.toggle("notification-blocked", permission === "denied");
    button.classList.toggle("notification-enabled", permission === "granted");
  });
  const status = document.getElementById("my-email-provider-status");
  if (status && !document.body.dataset.role.includes("management")) {
    const emailReady = status.dataset.emailReady === "true";
    const emailMessage = emailReady
      ? "Email provider is configured."
      : "Email delivery is unavailable until SMTP is configured on the server.";
    const browserMessage = permission === "granted"
      ? "Browser alerts are enabled while this page is open."
      : permission === "denied"
        ? "Browser alerts are blocked for this site; allow them in browser site settings."
        : "Select Enable browser alerts while MediTrack is open.";
    status.textContent = `${emailMessage} ${browserMessage}`;
  }
}

async function loadNotificationSettings() {
  if (!isManagement) {
    try {
      const response = await fetch("/api/my/notifications");
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "Could not load expiry notification settings.");
      document.getElementById("my-email-reminders").checked = data.email_enabled;
      document.getElementById("my-weekly-digest").checked = data.weekly_digest_enabled;
      document.getElementById("my-email-address").textContent = data.email;
      document.getElementById("personal-notification-note").textContent =
        `Expiry reminders are sent ${data.reminder_days.map((day) => Number(day) === 0 ? "on expiry day" : `${day} days before expiry`).join(", ")}. Weekly summaries are sent on Mondays. Browser alerts appear while MediTrack is open.`;
      const providerStatus = document.getElementById("my-email-provider-status");
      providerStatus.dataset.emailReady = String(data.email_provider_ready);
      providerStatus.classList.toggle("provider-ready", data.email_provider_ready);
      providerStatus.classList.toggle("provider-missing", !data.email_provider_ready);
      updateBrowserAlertButtons();
    } catch (error) {
      showToast(friendlyRequestError(error, "Could not load expiry notification settings."), true);
    }
    return;
  }
  if (!isGlobalManagement) return;
  try {
    const response = await fetch("/api/notifications/settings");
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Could not load notification settings.");
    document.getElementById("email-enabled").checked = data.settings.email_enabled;
    document.getElementById("whatsapp-enabled").checked = data.settings.whatsapp_enabled;
    document.getElementById("weekly-digest-enabled").checked = data.settings.weekly_digest_enabled;
    document.getElementById("reminder-days").value = data.settings.reminder_days.join(", ");
    const emailStatus = data.providers.email;
    document.getElementById("email-provider-status").textContent = emailStatus.message;
    document.getElementById("email-provider-status").className = emailStatus.ready ? "provider-ready" : "provider-missing";
    const whatsappStatus = data.providers.whatsapp;
    document.getElementById("whatsapp-provider-status").textContent =
      `${whatsappStatus.message}${whatsappStatus.ready && !whatsappStatus.template_ready ? " Add an approved Content SID for scheduled, user-initiated WhatsApp reminders." : ""}`;
    document.getElementById("whatsapp-provider-status").className = whatsappStatus.ready ? "provider-ready" : "provider-missing";
    document.getElementById("schedule-info").textContent =
      `Expiry checks run daily at ${String(data.providers.schedule.hour).padStart(2, "0")}:00 ${data.providers.schedule.timezone} time. Weekly digests run on Mondays.`;
    updateBrowserAlertButtons();
    renderNotificationHistory(data.history);
  } catch (error) {
    document.getElementById("settings-save-status").textContent =
      friendlyRequestError(error, "Could not load notification settings.");
  }
}

async function savePersonalNotificationSettings() {
  const enabled = document.getElementById("my-email-reminders").checked;
  const weeklyDigestEnabled = document.getElementById("my-weekly-digest").checked;
  try {
    const response = await fetch("/api/my/notifications", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        email_enabled: enabled,
        weekly_digest_enabled: weeklyDigestEnabled,
      }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Could not save expiry notification settings.");
    if (enabled && !data.email_provider_ready) {
      showToast("Expiry email reminders are enabled, but can't be delivered until SMTP is configured on the server.", true);
    } else if (weeklyDigestEnabled && !enabled) {
      showToast("Turn on Email expiry reminders to receive the weekly digest.", true);
    } else {
      showToast("Email notification preferences saved.");
    }
  } catch (error) {
    showToast(friendlyRequestError(error, "Could not save expiry notification settings."), true);
    await loadNotificationSettings();
  }
}

async function saveNotificationSettings() {
  const button = document.getElementById("save-notification-settings");
  const channelControls = [
    document.getElementById("email-enabled"),
    document.getElementById("whatsapp-enabled"),
    document.getElementById("weekly-digest-enabled"),
    document.getElementById("reminder-days"),
  ];
  if (button) button.disabled = true;
  channelControls.forEach((control) => { control.disabled = true; });
  try {
    const response = await fetch("/api/notifications/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        email_enabled: document.getElementById("email-enabled").checked,
        whatsapp_enabled: document.getElementById("whatsapp-enabled").checked,
        weekly_digest_enabled: document.getElementById("weekly-digest-enabled").checked,
        reminder_days: document.getElementById("reminder-days").value
          .split(",")
          .map((day) => day.trim())
          .filter(Boolean)
          .map(Number),
      }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Could not save notification settings.");
    renderNotificationHistory(data.history);
    const enabledButUnavailable = [
      ["Email", document.getElementById("email-enabled").checked, data.providers.email],
      ["WhatsApp", document.getElementById("whatsapp-enabled").checked, data.providers.whatsapp],
      ["Weekly digest", document.getElementById("weekly-digest-enabled").checked, data.providers.email],
    ].filter(([, enabled, provider]) => enabled && !provider.ready);
    const message = enabledButUnavailable.length
      ? `Preferences saved, but ${enabledButUnavailable.map(([channel]) => channel).join(" and ")} delivery ${enabledButUnavailable.length === 1 ? "is" : "are"} not configured on the server.`
      : "Settings saved.";
    document.getElementById("settings-save-status").textContent = message;
    showToast(message, enabledButUnavailable.length > 0);
  } catch (error) {
    const message = friendlyRequestError(error, "Could not save notification settings.");
    document.getElementById("settings-save-status").textContent = message;
    showToast(message, true);
  } finally {
    if (button) button.disabled = false;
    channelControls.forEach((control) => { control.disabled = false; });
  }
}

async function sendTestNotification() {
  const button = document.getElementById("send-test-notification");
  button.disabled = true;
  try {
    const response = await fetch("/api/notifications/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        uploader_email: document.getElementById("test-email").value.trim(),
        uploader_whatsapp: document.getElementById("test-whatsapp").value.trim(),
      }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Could not send test notification.");
    const failures = data.results.filter((item) => item.status !== "sent");
    renderNotificationHistory(data.history);
    if (failures.length) throw new Error(failures.map((item) => `${item.channel}: ${item.message}`).join("; "));
    showToast(`Test notification sent through ${data.results.map((item) => item.channel).join(" and ")}.`);
  } catch (error) {
    showToast(friendlyRequestError(error, "Could not send test notification."), true);
  } finally {
    button.disabled = false;
  }
}

function notifyAboutInventory(medicines) {
  if (!("Notification" in window) || Notification.permission !== "granted") return;
  const urgent = medicines.filter((medicine) => medicine.status !== "healthy" || medicine.low_stock);
  if (!urgent.length) return;
  const title = `${urgent.length} inventory ${urgent.length === 1 ? "alert" : "alerts"}`;
  const body = urgent.slice(0, 3).map((medicine) => medicine.status === "expired"
    ? `${medicine.name} has expired`
    : medicine.status === "healthy"
      ? `${medicine.name}: ${medicine.quantity} units left`
      : `${medicine.name}: ${medicine.days_remaining === 0 ? "expires today" : `expires in ${medicine.days_remaining} days`}`
  ).join(" · ");
  new Notification(title, { body, icon: "/static/favicon.svg", tag: "meditrack-inventory-alerts" });
}

async function enableNotifications() {
  if (!("Notification" in window)) {
    showToast("This browser does not support notifications.", true);
    return;
  }
  if (Notification.permission === "denied") {
    showToast("Browser alerts are blocked. Open this site's browser permissions, allow Notifications, then reload MediTrack.", true);
    return;
  }
  try {
    const permission = await Notification.requestPermission();
    if (permission !== "granted") {
      showToast("Notification permission was not granted.", true);
      return;
    }
    document.getElementById("notification-dot").hidden = true;
    updateBrowserAlertButtons();
    showToast("Browser notifications enabled.");
    notifyAboutInventory(state.alerts);
  } catch (error) {
    showToast(`Could not enable notifications: ${error.message}`, true);
  }
}

document.getElementById("add-medicine").addEventListener("click", () => openModal());
document.getElementById("empty-add").addEventListener("click", () => openModal());
document.getElementById("close-modal").addEventListener("click", closeModal);
document.getElementById("cancel-modal").addEventListener("click", closeModal);
elements.modal.addEventListener("click", (event) => {
  if (event.target === elements.modal) closeModal();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !elements.modal.hidden) closeModal();
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
    event.preventDefault();
    document.getElementById("search-input").focus();
  }
});
elements.form.addEventListener("submit", saveMedicine);
elements.rows.addEventListener("click", (event) => {
  const button = event.target.closest("[data-action]");
  if (!button) return;
  const id = Number(button.dataset.id);
  if (button.dataset.action === "edit" && isManagement) {
    const medicine = state.medicines.find((item) => item.id === id);
    if (medicine) openModal(medicine);
  } else if (button.dataset.action === "delete") {
    deleteMedicine(id);
  }
});
document.getElementById("search-input").addEventListener("input", () => {
  window.clearTimeout(state.searchTimer);
  state.searchTimer = window.setTimeout(loadInventory, 180);
});
document.getElementById("status-filter").addEventListener("change", loadInventory);
document.querySelectorAll("[data-expiry-filter]").forEach((button) => {
  button.addEventListener("click", () => {
    document.getElementById("status-filter").value = button.dataset.expiryFilter;
    document.getElementById("inventory").scrollIntoView({ behavior: "smooth" });
    loadInventory();
  });
});
document.getElementById("voice-alerts-toggle").addEventListener("click", () => {
  if (!("speechSynthesis" in window)) {
    showToast("Spoken alerts are not supported by this browser.", true);
    return;
  }
  const enabled = localStorage.getItem(voicePreferenceKey) === "disabled";
  localStorage.setItem(voicePreferenceKey, enabled ? "enabled" : "disabled");
  if (!enabled) window.speechSynthesis.cancel();
  else sessionStorage.removeItem(spokenAlertsKey);
  updateVoiceAlertButton();
  showToast(enabled ? "Spoken expiry alerts enabled." : "Spoken expiry alerts muted.");
  if (enabled) voiceAlert(state.alerts);
});
document.getElementById("clear-filters").addEventListener("click", () => {
  document.getElementById("search-input").value = "";
  document.getElementById("status-filter").value = "all";
  const branchFilter = document.getElementById("branch-filter");
  if (branchFilter) branchFilter.value = "";
  loadInventory();
});
const branchFilter = document.getElementById("branch-filter");
if (branchFilter) branchFilter.addEventListener("change", loadInventory);
if (isManagement) {
  document.getElementById("alerts-nav").addEventListener("click", () => {
    document.getElementById("status-filter").value = "alerts";
    document.getElementById("inventory").scrollIntoView({ behavior: "smooth" });
    loadInventory();
  });
  document.getElementById("view-alerts").addEventListener("click", () => {
    document.getElementById("status-filter").value = "alerts";
    document.getElementById("inventory").scrollIntoView({ behavior: "smooth" });
    loadInventory();
  });
  if (isGlobalManagement) {
    document.getElementById("save-notification-settings").addEventListener("click", saveNotificationSettings);
    document.getElementById("send-test-notification").addEventListener("click", sendTestNotification);
    document.getElementById("email-enabled").addEventListener("change", saveNotificationSettings);
    document.getElementById("whatsapp-enabled").addEventListener("change", saveNotificationSettings);
  }
} else {
  document.getElementById("my-email-reminders").addEventListener("change", savePersonalNotificationSettings);
  document.getElementById("my-weekly-digest").addEventListener("change", savePersonalNotificationSettings);
}
if (isGlobalManagement) {
  document.getElementById("weekly-digest-enabled").addEventListener("change", saveNotificationSettings);
  document.getElementById("reminder-days").addEventListener("change", saveNotificationSettings);
  document.getElementById("branch-form").addEventListener("submit", submitBranchForm);
  document.getElementById("staff-form").addEventListener("submit", submitStaffForm);
  loadBranches();
} else if (isBranchStaff) {
  loadBranches();
}
document.getElementById("enable-notifications").addEventListener("click", enableNotifications);
if (isGlobalManagement) {
  document.getElementById("sidebar-notifications").addEventListener("click", enableNotifications);
} else if (!isManagement) {
  document.getElementById("enable-personal-browser-alerts").addEventListener("click", enableNotifications);
}

if ("Notification" in window && Notification.permission === "granted") {
  document.getElementById("notification-dot").hidden = true;
}
document.getElementById("dashboard-date").textContent =
  `${new Intl.DateTimeFormat(undefined, { weekday: "long", month: "short", day: "numeric" }).format(new Date()).toUpperCase()} · YOUR INVENTORY CHECK-IN`;
updateBrowserAlertButtons();
updateVoiceAlertButton();
loadInventory();
loadNotificationSettings();
