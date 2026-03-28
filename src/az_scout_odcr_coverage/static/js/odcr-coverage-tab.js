// ODCR Coverage Plugin
(function () {
    const PLUGIN = "odcr-coverage";
    const PORTAL = "https://portal.azure.com/#@/resource";
    const container = document.getElementById("plugin-tab-" + PLUGIN);
    if (!container) return;

    fetch(`/plugins/${PLUGIN}/static/html/odcr-coverage-tab.html`)
        .then(r => r.text())
        .then(html => { container.innerHTML = html; init(); })
        .catch(err => {
            container.innerHTML = `<div class="alert alert-danger">Failed to load UI: ${err.message}</div>`;
        });

    function init() {
        const regionEl   = document.getElementById("region-select");
        const tenantEl   = document.getElementById("tenant-select");
        const subFilter  = document.getElementById("odcr-sub-filter");
        const subListEl  = document.getElementById("odcr-sub-list");
        const subCountEl = document.getElementById("odcr-sub-count");
        const btn        = document.getElementById("odcr-analyze-btn");
        const lookback   = document.getElementById("odcr-lookback");

        const selectedSubs = new Set();

        function getRegion() { return regionEl?.value || ""; }
        function getTenant() { return tenantEl?.value || ""; }
        function getSubs() { return typeof subscriptions !== "undefined" ? subscriptions : []; }

        // ---- Subscription checklist (same pattern as topology) ----
        function renderSubList(filter) {
            if (!subListEl) return;
            const subs = getSubs();
            const lc = (filter || "").toLowerCase();
            const list = lc
                ? subs.filter(s => s.name.toLowerCase().includes(lc) || s.id.toLowerCase().includes(lc))
                : subs;

            if (!list.length && !lc) {
                subListEl.innerHTML = '<span class="text-body-secondary small">No subscriptions</span>';
                return;
            }
            subListEl.innerHTML = list.map(s => {
                const checked = selectedSubs.has(s.id) ? "checked" : "";
                return `<label title="${s.name}" data-id="${s.id}">
                    <input type="checkbox" class="form-check-input me-1" value="${s.id}" ${checked}>
                    ${s.name}
                </label>`;
            }).join("");

            subListEl.querySelectorAll("input[type=checkbox]").forEach(cb => {
                cb.addEventListener("change", () => {
                    if (cb.checked) selectedSubs.add(cb.value);
                    else selectedSubs.delete(cb.value);
                    updateSubCount();
                    updateBtn();
                });
            });
            updateSubCount();
        }

        function selectAllVisible() {
            subListEl?.querySelectorAll("input[type=checkbox]").forEach(cb => {
                cb.checked = true;
                selectedSubs.add(cb.value);
            });
            updateSubCount();
            updateBtn();
        }

        function deselectAll() {
            selectedSubs.clear();
            subListEl?.querySelectorAll("input[type=checkbox]").forEach(cb => { cb.checked = false; });
            updateSubCount();
            updateBtn();
        }

        function updateSubCount() {
            if (subCountEl) subCountEl.textContent = `${selectedSubs.size} selected`;
        }

        function populateSubs() {
            selectedSubs.clear();
            renderSubList("");
            if (subFilter) subFilter.value = "";
            updateBtn();
        }

        subFilter?.addEventListener("input", () => renderSubList(subFilter.value));
        document.getElementById("odcr-select-all")?.addEventListener("click", selectAllVisible);
        document.getElementById("odcr-deselect-all")?.addEventListener("click", deselectAll);

        function updateBtn() {
            btn.disabled = !(selectedSubs.size > 0 && getRegion());
        }

        document.addEventListener("azscout:subscriptions-loaded", populateSubs);
        document.addEventListener("azscout:tenant-changed", populateSubs);
        document.addEventListener("azscout:region-changed", updateBtn);
        populateSubs();

        btn?.addEventListener("click", analyze);

        // ---- Progress ----
        let dbgStartTime = 0;

        function setProgressLabel(label) {
            const lbl = document.getElementById("odcr-progress-label");
            if (lbl) lbl.textContent = label;
        }
        function setProgress(pct, label) {
            const bar = document.getElementById("odcr-progress-bar");
            if (bar) {
                bar.style.width = `${pct}%`;
                if (pct >= 100) bar.classList.add("bg-success");
            }
            setProgressLabel(label);
        }

        // ---- Analyze ----
        async function analyze() {
            const reg = getRegion();
            const subIds = [...selectedSubs];
            if (!reg || !subIds.length) return;

            hide("odcr-empty"); hide("odcr-error");
            show("odcr-progress"); show("odcr-results");

            // Reset debug stats
            const dbgPerSub = {};  // subId → latest stats from backend
            dbgStartTime = performance.now();
            let dbgTimerHandle = null;
            show("odcr-debug-panel");
            updateDebugPanel({ pages: 0, raw_events: 0, filtered_events: 0, vms_with_events: 0 });

            // Live elapsed timer
            function tickTimer() {
                const el = document.getElementById("odcr-dbg-elapsed");
                if (el) el.textContent = fmtElapsed(performance.now() - dbgStartTime);
            }
            dbgTimerHandle = setInterval(tickTimer, 200);
            tickTimer();

            // Reset progress bar
            const bar = document.getElementById("odcr-progress-bar");
            if (bar) { bar.style.width = "0%"; bar.classList.remove("bg-success"); }

            // Show a spinner in the table while loading
            const tbody = document.getElementById("odcr-vm-tbody");
            if (tbody) {
                tbody.innerHTML = `<tr><td colspan="12" class="text-center py-4">
                    <div class="spinner-border spinner-border-sm text-primary" role="status"></div>
                    <span class="text-body-secondary ms-2">Loading…</span>
                </td></tr>`;
            }
            setProgressLabel(`Listing VMs for subscription 1/${subIds.length}…`);
            await new Promise(r => setTimeout(r, 0));

            const subs = getSubs();
            const subMap = Object.fromEntries(subs.map(s => [s.id, s.name]));
            const days = lookback?.value || "7";
            const tid = getTenant();

            const allVms = [];
            const merged = {
                total_vms: 0, covered: 0, uncovered_critical: 0,
                uncovered_high: 0, uncovered_medium: 0, uncovered_low: 0,
                total_allocation_attempts: 0, total_allocation_failures: 0,
                odcr_total_reserved: 0, odcr_total_used: 0, odcr_total_unused: 0,
            };
            const allRes = [];
            const errors = [];
            const ro = { critical: 0, high: 1, medium: 2, low: 3, covered: 4 };

            // Track per-subscription state for two-phase merging
            const subData = {};  // subId → { vms, summary, reservations }
            let completedSubs = 0;

            for (let i = 0; i < subIds.length; i++) {
                const subId = subIds[i];
                const subName = subMap[subId] || subId.slice(0, 8) + "…";
                subData[subId] = { vms: [], summary: null, reservations: [] };

                const qs = `region=${encodeURIComponent(reg)}&subscription_id=${encodeURIComponent(subId)}&lookback_days=${days}`
                    + (tid ? `&tenant_id=${encodeURIComponent(tid)}` : "");

                try {
                    await new Promise((resolve, reject) => {
                        const es = new EventSource(`/plugins/${PLUGIN}/coverage/stream?${qs}`);

                        es.addEventListener("vms", (evt) => {
                            const data = JSON.parse(evt.data);
                            // Store phase-1 data for this subscription
                            subData[subId].vms = data.vms.map(vm => ({
                                ...vm, subscription_name: subName, subscription_id: subId,
                                _preliminary: true,
                            }));
                            subData[subId].summary = data.summary;
                            subData[subId].reservations = (data.odcr_utilization?.reservations || []).map(
                                r => ({ ...r, subscription_name: subName })
                            );
                            rebuildMerged();
                            const subTag = subIds.length > 1 ? ` — sub ${i + 1}/${subIds.length}` : "";
                            setProgress(
                                Math.round(((completedSubs + 0.3) / subIds.length) * 100),
                                `${subName}: loading events…${subTag}`
                            );
                        });

                        es.addEventListener("progress", (evt) => {
                            try {
                                const data = JSON.parse(evt.data);
                                const dc = data.days_covered || 0;
                                const lb = data.lookback_days || parseInt(days, 10);
                                // Progress within this subscription: VMs loaded (~30%) + event days
                                const subPct = 0.3 + 0.7 * (dc / lb);
                                const subTag = subIds.length > 1 ? ` — sub ${i + 1}/${subIds.length}` : "";
                                setProgress(
                                    Math.round(((completedSubs + subPct) / subIds.length) * 100),
                                    `${subName}: events ${dc}/${lb}d…${subTag}`
                                );
                                // Update debug panel (stats are cumulative per sub)
                                dbgPerSub[subId] = {
                                    pages: data.pages || 0,
                                    raw_events: data.raw_events || 0,
                                    filtered_events: data.filtered_events || 0,
                                };
                                rebuildDebugPanel(dbgPerSub);
                            } catch { /* ignore */ }
                        });

                        es.addEventListener("enriched", (evt) => {
                            const data = JSON.parse(evt.data);
                            // Replace data with progressively enriched results
                            subData[subId].vms = data.vms.map(vm => ({
                                ...vm, subscription_name: subName, subscription_id: subId,
                            }));
                            subData[subId].summary = data.summary;
                            subData[subId].reservations = (data.odcr_utilization?.reservations || []).map(
                                r => ({ ...r, subscription_name: subName })
                            );
                            rebuildMerged();
                            rebuildDebugPanel(dbgPerSub);
                        });

                        es.addEventListener("error", (evt) => {
                            // SSE error event with data (server-sent error)
                            if (evt.data) {
                                try {
                                    const err = JSON.parse(evt.data);
                                    errors.push(`${subName}: ${err.message}`);
                                } catch { /* ignore parse errors */ }
                            }
                        });

                        es.addEventListener("done", () => {
                            es.close();
                            resolve();
                        });

                        es.onerror = () => {
                            es.close();
                            errors.push(`${subName}: Connection lost`);
                            reject(new Error("SSE connection failed"));
                        };
                    });
                } catch {
                    // Error already collected above; continue to next sub
                }
                completedSubs++;
                setProgress(
                    Math.round((completedSubs / subIds.length) * 100),
                    completedSubs < subIds.length
                        ? `Listing VMs for subscription ${completedSubs + 1}/${subIds.length}…`
                        : "Finalizing…"
                );
            }

            function rebuildMerged() {
                // Rebuild merged state from all subscriptions' current data
                allVms.length = 0;
                allRes.length = 0;
                Object.keys(merged).forEach(k => { merged[k] = 0; });

                for (const sd of Object.values(subData)) {
                    for (const vm of sd.vms) allVms.push(vm);
                    if (sd.summary) {
                        for (const k of Object.keys(merged)) merged[k] += sd.summary[k] || 0;
                    }
                    for (const r of sd.reservations) allRes.push(r);
                }

                allVms.sort((a, b) => (ro[a.risk] ?? 99) - (ro[b.risk] ?? 99));
                renderResults({
                    summary: merged,
                    odcr_utilization: {
                        total_reserved: allRes.reduce((s, r) => s + r.capacity, 0),
                        total_used: allRes.reduce((s, r) => s + r.used, 0),
                        total_unused: allRes.reduce((s, r) => s + r.unused, 0),
                        reservations: allRes,
                    },
                    vms: allVms,
                });
            }

            setProgress(100, "Done");
            if (dbgTimerHandle) { clearInterval(dbgTimerHandle); dbgTimerHandle = null; }
            tickTimer();  // final tick
            setTimeout(() => hide("odcr-progress"), 800);

            if (!allVms.length && errors.length) {
                const errEl = document.getElementById("odcr-error");
                if (errEl) { errEl.textContent = errors.join("\n"); errEl.style.display = "block"; }
            } else if (errors.length) {
                const errEl = document.getElementById("odcr-error");
                if (errEl) {
                    errEl.innerHTML = `<strong>Partial errors:</strong> ${errors.join("; ")}`;
                    errEl.style.display = "block";
                }
            }
        }

        // ---- Rendering ----
        let allVmsForFilter = [];

        function renderResults(data) {
            show("odcr-results");
            allVmsForFilter = data.vms;
            renderSummaryCards(data.summary);
            renderOdcrUtilization(data.odcr_utilization);
            renderRiskBar(data.summary);
            applyRiskFilter();
        }

        // ---- Risk filter ----
        const riskFilter = document.getElementById("odcr-risk-filter");
        riskFilter?.addEventListener("change", applyRiskFilter);

        function applyRiskFilter() {
            const val = riskFilter?.value || "";
            const filtered = val
                ? allVmsForFilter.filter(vm => vm.risk === val)
                : allVmsForFilter;
            renderVmTable(filtered);
        }

        // ---- CSV export ----
        document.getElementById("odcr-export-csv")?.addEventListener("click", () => {
            if (!allVmsForFilter.length) return;
            const headers = ["Risk", "VM Name", "Subscription", "RG", "Size", "Zone", "State", "Uptime%", "AllocSucceeded", "AllocFailed", "ODCR", "ODCRGroup", "Reason"];
            const rows = allVmsForFilter.map(vm => [
                vm.risk,
                vm.name,
                vm.subscription_name || "",
                vm.resource_group || "",
                vm.vm_size,
                vm.zone || "",
                vm.power_state,
                vm.uptime_pct,
                vm.allocation_summary?.succeeded || 0,
                vm.allocation_summary?.failed || 0,
                vm.has_odcr ? "Yes" : "No",
                vm.odcr_group_name || "",
                vm.risk_reason,
            ]);
            const csv = [headers, ...rows].map(r => r.map(c => `"${String(c).replace(/"/g, '""')}"`).join(",")).join("\n");
            const blob = new Blob([csv], { type: "text/csv" });
            const url = URL.createObjectURL(blob);
            const a = document.createElement("a");
            a.href = url;
            a.download = `odcr-coverage-${new Date().toISOString().slice(0, 10)}.csv`;
            a.click();
            URL.revokeObjectURL(url);
        });

        function renderSummaryCards(s) {
            const el = document.getElementById("odcr-summary-cards");
            if (!el) return;
            const cards = [
                { cls: "card-total", value: s.total_vms, label: "Total VMs", icon: "pc-display" },
                { cls: "card-covered", value: s.covered, label: "Covered", icon: "shield-check" },
                { cls: "card-critical", value: s.uncovered_critical, label: "Critical", icon: "exclamation-triangle-fill" },
                { cls: "card-high", value: s.uncovered_high, label: "High", icon: "arrow-up-circle-fill" },
                { cls: "card-medium", value: s.uncovered_medium, label: "Medium", icon: "dash-circle" },
                { cls: "card-low", value: s.uncovered_low, label: "Low", icon: "arrow-down-circle" },
                { cls: "card-failures", value: s.total_allocation_failures, label: `Failures / ${s.total_allocation_attempts}`, icon: "x-octagon" },
            ];
            el.innerHTML = cards.map(c => `
                <div class="col-6 col-md-3 col-xl">
                    <div class="odcr-summary-card ${c.cls}">
                        <div class="odcr-card-value">${c.value}</div>
                        <div class="odcr-card-label"><i class="bi bi-${c.icon}"></i> ${c.label}</div>
                    </div>
                </div>`).join("");
        }

        function renderOdcrUtilization(u) {
            const section = document.getElementById("odcr-utilization-section");
            const el = document.getElementById("odcr-utilization-cards");
            if (!section || !el) return;
            if (!u.reservations.length) { section.style.display = "none"; return; }
            section.style.display = "block";

            // Group reservations by ODCR group name
            const groups = new Map();
            for (const r of u.reservations) {
                const key = r.group;
                if (!groups.has(key)) groups.set(key, []);
                groups.get(key).push(r);
            }

            el.innerHTML = Array.from(groups.entries()).map(([groupName, items]) => {
                const totalUsed = items.reduce((s, r) => s + r.used, 0);
                const totalCap = items.reduce((s, r) => s + r.capacity, 0);
                const totalPct = totalCap > 0 ? Math.round(totalUsed / totalCap * 100) : 0;
                const cardCls = totalPct === 100 ? "full-util" : totalPct === 0 ? "no-util" : "partial-util";
                const subName = items[0].subscription_name || "";

                const zoneRows = items.map(r => {
                    const pct = r.capacity > 0 ? Math.round(r.used / r.capacity * 100) : 0;
                    const fillCls = pct === 100 ? "full-util" : pct === 0 ? "no-util" : "partial-util";
                    const unusedTag = r.unused > 0
                        ? ` · <span class="text-warning fw-medium">${r.unused} unused</span>`
                        : "";
                    return `
                        <div class="odcr-zone-row">
                            <div class="odcr-zone-label">${r.sku} · Zone ${r.zone || "—"}</div>
                            <div class="odcr-util-gauge"><div class="gauge-fill ${fillCls}" style="width:${pct}%"></div></div>
                            <div class="odcr-util-meta">${r.used}/${r.capacity} used${unusedTag}</div>
                        </div>`;
                }).join("");

                return `
                <div class="col-6 col-md-4 col-xl-3">
                    <div class="odcr-util-card ${cardCls} odcr-highlight-target" data-odcr-group="${groupName}">
                        ${zoneRows}
                        <div class="odcr-util-meta">${groupName}${subName ? " · " + subName : ""}</div>
                    </div>
                </div>`;
            }).join("");

            // Cross-highlight: ODCR card ↔ VM rows
            el.querySelectorAll("[data-odcr-group]").forEach(card => {
                card.addEventListener("mouseenter", () => highlightGroup(card.dataset.odcrGroup, true));
                card.addEventListener("mouseleave", () => highlightGroup(card.dataset.odcrGroup, false));
            });
        }

        function highlightGroup(group, on) {
            if (!group) return;
            // Highlight matching VM rows
            document.querySelectorAll(`#odcr-vm-tbody tr[data-odcr-group="${group}"]`).forEach(row => {
                row.classList.toggle("odcr-highlight", on);
            });
            // Highlight matching ODCR cards
            document.querySelectorAll(`.odcr-util-card[data-odcr-group="${group}"]`).forEach(card => {
                card.classList.toggle("odcr-highlight", on);
            });
        }

        function renderRiskBar(s) {
            const bar = document.getElementById("odcr-risk-bar");
            const legend = document.getElementById("odcr-risk-legend");
            if (!bar) return;
            const total = s.total_vms || 1;
            const segs = [
                { cls: "risk-critical", count: s.uncovered_critical, label: "Critical", color: "#dc3545" },
                { cls: "risk-high", count: s.uncovered_high, label: "High", color: "#fd7e14" },
                { cls: "risk-medium", count: s.uncovered_medium, label: "Medium", color: "#ffc107" },
                { cls: "risk-low", count: s.uncovered_low, label: "Low", color: "#6c757d" },
                { cls: "risk-covered", count: s.covered, label: "Covered", color: "#198754" },
            ];
            bar.innerHTML = segs.filter(sg => sg.count > 0).map(sg => {
                const pct = (sg.count / total * 100).toFixed(1);
                return `<div class="risk-segment ${sg.cls}" style="flex:${sg.count}" title="${sg.label}: ${sg.count} (${pct}%)">${sg.count}</div>`;
            }).join("");
            if (legend) {
                legend.innerHTML = segs.map(sg =>
                    `<span class="legend-item"><span class="legend-dot" style="background:${sg.color}"></span>${sg.label}: ${sg.count}</span>`
                ).join("");
            }
        }

        // Store VMs for modal access
        let currentVms = [];

        function renderVmTable(vms) {
            currentVms = vms;
            const tbody = document.getElementById("odcr-vm-tbody");
            if (!tbody) return;
            tbody.innerHTML = vms.map((vm, idx) => {
                const badge = riskBadgeHtml(vm.risk);
                const uptime = uptimeBarHtml(vm.uptime_pct);
                const alloc = allocBadgeHtml(vm.allocation_summary);
                const odcrGroup = vm.odcr_group_name || "";
                const odcrCell = vm.has_odcr
                    ? `<span class="odcr-group-label" data-odcr-group="${odcrGroup}"><i class="bi bi-shield-fill-check odcr-status-icon protected"></i> ${odcrGroup}</span>`
                    : '<i class="bi bi-shield-slash odcr-status-icon unprotected"></i>';
                const state = vm.power_state === "running"
                    ? '<span class="text-success"><i class="bi bi-circle-fill" style="font-size:0.5rem"></i> running</span>'
                    : `<span class="text-secondary"><i class="bi bi-circle" style="font-size:0.5rem"></i> ${vm.power_state}</span>`;
                const link = vm.resource_id
                    ? `<a href="${PORTAL}${vm.resource_id}" target="_blank" rel="noopener" class="text-primary" onclick="event.stopPropagation()"><i class="bi bi-box-arrow-up-right"></i></a>`
                    : "";
                const groupAttr = odcrGroup ? `data-odcr-group="${odcrGroup}"` : "";
                return `<tr class="odcr-vm-row" data-vm-idx="${idx}" ${groupAttr} style="cursor:pointer">
                    <td>${badge}</td>
                    <td class="fw-medium">${vm.name}</td>
                    <td class="small text-body-secondary">${vm.subscription_name || ""}</td>
                    <td class="small text-body-secondary">${vm.resource_group || ""}</td>
                    <td><code>${vm.vm_size}</code></td>
                    <td>${vm.zone || "—"}</td>
                    <td>${state}</td>
                    <td>${uptime}</td>
                    <td>${alloc}</td>
                    <td>${odcrCell}</td>
                    <td class="text-body-secondary small">${vm.risk_reason}</td>
                    <td>${link}</td>
                </tr>`;
            }).join("");

            // Attach click + hover handlers
            tbody.querySelectorAll(".odcr-vm-row").forEach(row => {
                row.addEventListener("click", () => {
                    const idx = parseInt(row.dataset.vmIdx, 10);
                    if (currentVms[idx]) showVmModal(currentVms[idx]);
                });
                // Cross-highlight: VM row ↔ ODCR card
                const group = row.dataset.odcrGroup;
                if (group) {
                    row.addEventListener("mouseenter", () => highlightGroup(group, true));
                    row.addEventListener("mouseleave", () => highlightGroup(group, false));
                }
            });
        }

        // ---- VM Detail Modal ----
        function showVmModal(vm) {
            const title = document.getElementById("odcr-vm-modal-title");
            const body = document.getElementById("odcr-vm-modal-body");
            const portalLink = document.getElementById("odcr-vm-modal-portal");

            if (title) title.textContent = vm.name;
            if (portalLink) {
                if (vm.resource_id) {
                    portalLink.href = `${PORTAL}${vm.resource_id}`;
                    portalLink.style.display = "";
                } else {
                    portalLink.style.display = "none";
                }
            }

            if (body) body.innerHTML = buildModalContent(vm);

            const modalEl = document.getElementById("odcr-vm-modal");
            if (modalEl && typeof bootstrap !== "undefined") {
                const modal = bootstrap.Modal.getOrCreateInstance(modalEl);
                modal.show();
            }
        }

        function buildModalContent(vm) {
            const s = vm.allocation_summary || {};
            const events = vm.allocation_events || [];

            // ---- VM Profile section ----
            let html = `
            <div class="row g-3 mb-3">
                <div class="col-6 col-md-3">
                    <div class="text-body-secondary small">Risk</div>
                    <div>${riskBadgeHtml(vm.risk)}</div>
                </div>
                <div class="col-6 col-md-3">
                    <div class="text-body-secondary small">ODCR</div>
                    <div>${vm.has_odcr
                        ? '<span class="text-success fw-medium"><i class="bi bi-shield-fill-check"></i> Protected</span>'
                        : '<span class="text-danger fw-medium"><i class="bi bi-shield-slash"></i> Not covered</span>'}</div>
                </div>
                <div class="col-6 col-md-3">
                    <div class="text-body-secondary small">VM Size</div>
                    <div><code>${vm.vm_size}</code></div>
                </div>
                <div class="col-6 col-md-3">
                    <div class="text-body-secondary small">Zone</div>
                    <div>${vm.zone || "—"}</div>
                </div>
            </div>
            <div class="row g-3 mb-3">
                <div class="col-6 col-md-3">
                    <div class="text-body-secondary small">Subscription</div>
                    <div class="small">${vm.subscription_name || "—"}</div>
                </div>
                <div class="col-6 col-md-3">
                    <div class="text-body-secondary small">Resource Group</div>
                    <div class="small">${vm.resource_group || "—"}</div>
                </div>
                <div class="col-6 col-md-3">
                    <div class="text-body-secondary small">Power State</div>
                    <div>${vm.power_state === "running"
                        ? '<span class="text-success">running</span>'
                        : `<span class="text-secondary">${vm.power_state}</span>`}</div>
                </div>
                <div class="col-6 col-md-3">
                    <div class="text-body-secondary small">Uptime</div>
                    <div>${uptimeBarHtml(vm.uptime_pct)}</div>
                </div>
            </div>`;

            // ---- Risk reason ----
            html += `<div class="alert alert-${vm.risk === "covered" ? "success" : vm.risk === "critical" ? "danger" : vm.risk === "high" ? "warning" : "secondary"} py-2 small">
                <i class="bi bi-info-circle me-1"></i> ${vm.risk_reason}
            </div>`;

            // ---- Allocation Summary ----
            html += `<h6 class="mt-3 mb-2"><i class="bi bi-bar-chart"></i> Allocation Summary</h6>
            <div class="row g-3 mb-3">
                <div class="col-3 text-center">
                    <div class="fs-4 fw-bold">${s.total_attempts || 0}</div>
                    <div class="text-body-secondary small">Total Attempts</div>
                </div>
                <div class="col-3 text-center">
                    <div class="fs-4 fw-bold text-success">${s.succeeded || 0}</div>
                    <div class="text-body-secondary small">Succeeded</div>
                </div>
                <div class="col-3 text-center">
                    <div class="fs-4 fw-bold ${s.failed > 0 ? "text-danger" : ""}">${s.failed || 0}</div>
                    <div class="text-body-secondary small">Failed</div>
                </div>
                <div class="col-3 text-center">
                    <div class="fs-4 fw-bold ${s.failure_rate_pct > 0 ? "text-danger" : ""}">${s.failure_rate_pct || 0}%</div>
                    <div class="text-body-secondary small">Failure Rate</div>
                </div>
            </div>`;

            // ---- Allocation Events Timeline (latest first) ----
            if (events.length > 0) {
                const sortedEvents = [...events].reverse();
                html += `<h6 class="mt-3 mb-2"><i class="bi bi-clock-history"></i> Allocation Events (${events.length})</h6>
                <div class="odcr-timeline">`;
                for (const e of sortedEvents) {
                    const isFail = (e.status || "").toLowerCase().includes("fail");
                    const icon = isFail ? "x-circle-fill" : e.operation === "start" ? "play-circle-fill" : "stop-circle-fill";
                    const color = isFail ? "text-danger" : e.operation === "start" ? "text-success" : "text-warning";
                    const ts = formatTimestamp(e.timestamp);
                    const errorTag = e.error_code ? ` <code class="text-danger">${e.error_code}</code>` : "";
                    const tooltip = `correlationId: ${e.correlation_id || "—"}\noperationName: ${e.operation_name || "—"}`;
                    html += `<div class="odcr-timeline-item" title="${tooltip}">
                        <div class="odcr-timeline-rail">
                            <div class="rail-line"></div>
                            <i class="bi bi-${icon} ${color} rail-dot"></i>
                            <div class="rail-line"></div>
                        </div>
                        <div class="odcr-timeline-content">
                            <span class="fw-medium">${e.operation}</span>
                            <span class="badge ${isFail ? "bg-danger" : "bg-success"} ms-1">${e.status}</span>${errorTag}
                            <div class="text-body-secondary small">${ts}</div>
                        </div>
                    </div>`;
                }
                html += `</div>`;
            } else {
                html += `<p class="text-body-secondary small mt-3"><i class="bi bi-info-circle"></i> No allocation events found in the lookback period.</p>`;
            }

            return html;
        }

        function formatTimestamp(ts) {
            if (!ts) return "—";
            try {
                const d = new Date(ts);
                return d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
            } catch { return ts; }
        }

        function riskBadgeHtml(risk) {
            const icons = { critical: "exclamation-triangle-fill", high: "arrow-up-circle-fill", medium: "dash-circle", low: "arrow-down-circle", covered: "shield-fill-check" };
            return `<span class="odcr-risk-badge risk-${risk}"><i class="bi bi-${icons[risk] || "question-circle"} risk-icon"></i>${risk}</span>`;
        }
        function uptimeBarHtml(pct) {
            const cls = pct >= 90 ? "high" : pct >= 50 ? "medium" : "low";
            return `<span class="odcr-uptime-bar"><span class="odcr-uptime-track"><span class="odcr-uptime-fill ${cls}" style="width:${pct}%"></span></span>${pct}%</span>`;
        }
        function allocBadgeHtml(s) {
            if (!s.total_attempts) return '<span class="text-body-secondary">—</span>';
            const fail = s.failed > 0 ? ` <span class="alloc-fail">${s.failed}✗</span>` : "";
            return `<span class="odcr-alloc-badge">${s.succeeded}✓${fail}</span>`;
        }

        function show(id) { const e = document.getElementById(id); if (e) e.style.display = ""; }
        function hide(id) { const e = document.getElementById(id); if (e) e.style.display = "none"; }

        function fmtElapsed(ms) {
            const s = Math.floor(ms / 1000);
            if (s < 60) return `${s}s`;
            return `${Math.floor(s / 60)}m ${s % 60}s`;
        }

        function updateDebugPanel(stats) {
            const set = (id, v) => { const e = document.getElementById(id); if (e) e.textContent = v; };
            const fmt = (n) => n.toLocaleString();
            set("odcr-dbg-pages", fmt(stats.pages));
            set("odcr-dbg-raw", fmt(stats.raw_events));
            set("odcr-dbg-filtered", fmt(stats.filtered_events));
            set("odcr-dbg-vms", fmt(stats.vms_with_events));
            // Events per second
            const elapsed = (performance.now() - (dbgStartTime || performance.now())) / 1000;
            set("odcr-dbg-eps", elapsed > 0.5 ? Math.round(stats.raw_events / elapsed).toLocaleString() : "—");
        }

        function rebuildDebugPanel(perSub) {
            const totals = { pages: 0, raw_events: 0, filtered_events: 0, vms_with_events: 0 };
            for (const s of Object.values(perSub)) {
                totals.pages += s.pages;
                totals.raw_events += s.raw_events;
                totals.filtered_events += s.filtered_events;
            }
            // Count VMs with events from current merged data
            totals.vms_with_events = allVmsForFilter.filter(
                vm => vm.allocation_events && vm.allocation_events.length > 0
            ).length;
            updateDebugPanel(totals);
        }
    }
})();
