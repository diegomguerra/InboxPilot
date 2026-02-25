const API_BASE = '';
let apiKey = localStorage.getItem('inboxpilot_api_key') || '';
let currentEmail = null;
let emails = [];
let queueItems = [];
let totalAvailableEmails = 0;
let currentLoadLimit = 50;
let rangeInfo = null;
let providerStatus = {};
let triageResults = [];
let replyEmailKey = null;
let emailCounts = {};
let mailboxStats = null;
let actionsExecuted = JSON.parse(localStorage.getItem('inboxpilot_actions_v2') || '{}');

function syncGlobalEmails() {
    window.emails = emails;
    window.currentEmail = currentEmail;
}

function trackAction(action, emailKey) {
    let provider = 'unknown';
    if (emailKey) {
        if (emailKey.startsWith('apple:')) provider = 'apple';
        else if (emailKey.startsWith('gmail:')) provider = 'gmail';
        else if (emailKey.startsWith('outlook:')) provider = 'outlook';
    }
    if (!actionsExecuted[provider]) actionsExecuted[provider] = { total: 0, deleted: 0, read: 0, replied: 0, skipped: 0 };
    actionsExecuted[provider].total++;
    if (action === 'delete') actionsExecuted[provider].deleted++;
    else if (action === 'mark_read') actionsExecuted[provider].read++;
    else if (action === 'send') actionsExecuted[provider].replied++;
    else if (action === 'skip') actionsExecuted[provider].skipped++;
    localStorage.setItem('inboxpilot_actions_v2', JSON.stringify(actionsExecuted));
}

function getActionsForProviders(providerList) {
    const result = { total: 0, deleted: 0, read: 0, replied: 0, skipped: 0 };
    const activeProviders = providerList || Object.keys(actionsExecuted);
    for (const p of activeProviders) {
        const a = actionsExecuted[p];
        if (!a) continue;
        result.total += a.total || 0;
        result.deleted += a.deleted || 0;
        result.read += a.read || 0;
        result.replied += a.replied || 0;
        result.skipped += a.skipped || 0;
    }
    return result;
}
let sessionId = localStorage.getItem('inboxpilot_session_id') || '';
let currentSnapshotId = null;
let isOnline = navigator.onLine;

function ensureSession() {
    if (!sessionId) {
        const now = new Date();
        const ts = now.toISOString().replace(/[-:T]/g, '').slice(0, 14);
        const rand = Math.random().toString(36).slice(2, 8);
        sessionId = `sess_${ts}_${rand}`;
        localStorage.setItem('inboxpilot_session_id', sessionId);
    }
    return sessionId;
}

function getHeaders() {
    const headers = { 'Content-Type': 'application/json' };
    if (apiKey) headers['X-API-Key'] = apiKey;
    return headers;
}

async function apiCall(endpoint, options = {}) {
    const response = await fetch(API_BASE + endpoint, {
        ...options,
        headers: getHeaders()
    });
    if (response.status === 401) {
        showApiKeyModal();
        throw new Error('API Key required');
    }
    if (!response.ok) {
        const text = await response.text();
        throw new Error(text || response.statusText);
    }
    const contentType = response.headers.get('content-type');
    if (contentType && contentType.includes('application/json')) {
        return response.json();
    }
    return response.text();
}

function showToast(message, duration = 3000) {
    const existing = document.querySelector('.toast');
    if (existing) existing.remove();
    const toast = document.createElement('div');
    toast.className = 'toast';
    toast.textContent = message;
    document.body.appendChild(toast);
    setTimeout(() => toast.remove(), duration);
}

function showApiKeyModal() {
    document.getElementById('apiKeyModal').classList.remove('hidden');
}

function hideApiKeyModal() {
    document.getElementById('apiKeyModal').classList.add('hidden');
}

function saveApiKey() {
    apiKey = document.getElementById('apiKeyInput').value.trim();
    localStorage.setItem('inboxpilot_api_key', apiKey);
    hideApiKeyModal();
    loadEmails();
}

function setLoading(element, loading) {
    if (loading) {
        element.innerHTML = '<div class="loading"><div class="spinner"></div>Carregando...</div>';
    }
}

function buildRangeParams() {
    const rangeType = document.getElementById('rangeSelect').value;
    let params = `range=${rangeType}`;
    if (rangeType === 'last_n_days') {
        params += `&n=${document.getElementById('nDaysInput')?.value || 7}`;
    } else if (rangeType === 'custom') {
        const s = document.getElementById('startDateInput')?.value;
        const e = document.getElementById('endDateInput')?.value;
        if (s) params += `&start=${s}`;
        if (e) params += `&end=${e}`;
    }
    params += `&unread_only=${document.getElementById('unreadOnlyCheck')?.checked ? 1 : 0}`;
    return params;
}

function getSelectedProviders() {
    return Array.from(document.querySelectorAll('.provider-check:checked')).map(cb => cb.value);
}

function getCurrentFilters() {
    const rangeType = document.getElementById('rangeSelect').value;
    return {
        providers: getSelectedProviders(),
        folders: Array.from(document.querySelectorAll('.folder-check:checked')).map(cb => cb.value),
        rangeType: rangeType,
        n: parseInt(document.getElementById('nDaysInput')?.value || '7'),
        startDate: document.getElementById('startDateInput')?.value || '',
        endDate: document.getElementById('endDateInput')?.value || '',
        unreadOnly: document.getElementById('unreadOnlyCheck')?.checked || false,
    };
}

function getHomeFilterState() {
    return {
        providers: getSelectedProviders(),
        folders: Array.from(document.querySelectorAll('.folder-check:checked')).map(cb => cb.value),
        rangeType: document.getElementById('rangeSelect')?.value || 'last_n_days',
        n: parseInt(document.getElementById('nDaysInput')?.value || '7'),
        startDate: document.getElementById('startDateInput')?.value || '',
        endDate: document.getElementById('endDateInput')?.value || '',
        unreadOnly: document.getElementById('unreadOnlyCheck')?.checked || false,
    };
}
window.getHomeFilterState = getHomeFilterState;

async function fetchEmailsIsolated() {
    try {
        const filter = getHomeFilterState();
        const providers = filter.providers.join(',');
        const folders = filter.folders.join(',');
        const rangeParams = buildRangeParams();
        const sid = ensureSession();
        const data = await apiCall(`/ui/messages?${rangeParams}&providers=${providers}&folders=${folders}&limit=100&session_id=${encodeURIComponent(sid)}`);
        return data.items || [];
    } catch (err) {
        console.warn('[fetchEmailsIsolated] error:', err);
        return [];
    }
}
window.fetchEmailsIsolated = fetchEmailsIsolated;

async function loadEmails() {
    const listEl = document.getElementById('emailList');
    emails = [];
    currentEmail = null;
    renderEmailList();
    setLoading(listEl, true);

    const rangeParams = buildRangeParams();
    const providers = getSelectedProviders().join(',');
    const folders = Array.from(document.querySelectorAll('.folder-check:checked')).map(cb => cb.value).join(',');

    if (!navigator.onLine) {
        await loadFromSnapshot();
        return;
    }

    try {
        const sid = ensureSession();
        const data = await apiCall(`/ui/messages?${rangeParams}&providers=${providers}&folders=${folders}&limit=${currentLoadLimit}&session_id=${encodeURIComponent(sid)}`);
        emails = data.items || [];
        rangeInfo = data.range_info;
        providerStatus = data.provider_status || {};
        const loaded = data.counts.loaded || emails.length;
        const totalAvail = data.counts.total_available || data.counts.total || loaded;
        totalAvailableEmails = totalAvail;
        const unread = data.counts.unread || 0;
        emailCounts = {
            total: totalAvail,
            loaded: loaded,
            unread: unread,
            read: data.counts.read || (totalAvail - unread),
            by_category: data.counts.by_category || {},
            by_provider: {}
        };
        emails.forEach(e => {
            const p = e.provider || 'unknown';
            emailCounts.by_provider[p] = (emailCounts.by_provider[p] || 0) + 1;
        });
        renderEmailList();
        syncGlobalEmails();
        console.log("[DIAG] Home loaded:", (emails || []).length,
                    "| window.emails:", (window.emails || []).length);
        renderEmailDetail();
        updateRangeDisplay();
        updateProviderBanners();
        updateConnectionsPanel();
        setTimeout(() => loadMailboxStats(), 100);
        if (totalAvail > loaded) {
            updateStatus(`Mostrando ${loaded} de ${totalAvail} e-mails (${unread} não lidos)`);
        } else {
            updateStatus(`${loaded} e-mails (${unread} não lidos)`);
        }

        try {
            const filters = getCurrentFilters();
            const filterHash = await offlineStore.computeFilterHash(filters);
            const snapId = `snap_${Date.now()}`;
            const snapItems = emails.map(e => ({
                key: e.key, provider: e.provider || '', folder: e.folder || 'inbox',
                from: e.from || e.from_addr || '', subject: e.subject || '',
                date: e.date || '', snippet: e.snippet || '',
                classification: e.classification || 'human', status: e.status || '',
                body_text: e.body_text || e.body || '',
            }));
            await offlineStore.saveSnapshot({
                snapshot_id: snapId, created_at: new Date().toISOString(),
                filters: filters, items: snapItems, filter_hash: filterHash,
            });
            currentSnapshotId = snapId;
            await offlineStore.cleanOldSnapshots(5);
            updateSnapshotDisplay();
        } catch (snapErr) {
            console.warn('Snapshot save failed:', snapErr);
        }
    } catch (err) {
        await loadFromSnapshot();
        if (emails.length === 0) {
            listEl.innerHTML = `<div class="loading">Erro: ${err.message}</div>`;
        }
    }
}

async function loadFromSnapshot() {
    const listEl = document.getElementById('emailList');
    try {
        const filters = getCurrentFilters();
        const filterHash = await offlineStore.computeFilterHash(filters);
        let snap = await offlineStore.getLatestSnapshotByFilter(filterHash);
        if (!snap) snap = await offlineStore.getLatestSnapshot();
        if (snap && snap.items && snap.items.length > 0) {
            emails = snap.items;
            currentSnapshotId = snap.snapshot_id;
            const unreadCount = emails.filter(e => e.unread !== false).length;
            emailCounts = {
                total: emails.length,
                loaded: emails.length,
                unread: unreadCount,
                read: emails.length - unreadCount,
                by_category: {},
                by_provider: {}
            };
            emails.forEach(e => {
                const cat = e.classification || 'human';
                emailCounts.by_category[cat] = (emailCounts.by_category[cat] || 0) + 1;
                const p = e.provider || 'unknown';
                emailCounts.by_provider[p] = (emailCounts.by_provider[p] || 0) + 1;
            });
            renderEmailList();
            syncGlobalEmails();
            renderEmailDetail();
            const snapTime = new Date(snap.created_at).toLocaleTimeString('pt-BR', {hour:'2-digit', minute:'2-digit'});
            updateStatus(`${emails.length} emails (snapshot de ${snapTime}) - OFFLINE`);
            updateSnapshotDisplay();
        } else {
            listEl.innerHTML = '<div class="loading">Sem conexão e sem snapshot salvo.</div>';
        }
    } catch (e) {
        listEl.innerHTML = '<div class="loading">Sem conexão.</div>';
    }
}

function updateProviderBanners() {
    const bannerEl = document.getElementById('providerBanner');
    if (!bannerEl) return;
    const selected = getSelectedProviders();
    const banners = [];
    for (const prov of selected) {
        const ps = providerStatus[prov];
        if (ps && !ps.connected) {
            if (prov === 'gmail') {
                banners.push(`<div class="provider-banner warning">Gmail não conectado — <a href="/gmail/login" target="_blank">Conectar Gmail</a></div>`);
            } else if (prov === 'apple') {
                banners.push(`<div class="provider-banner warning">Apple Mail não configurado</div>`);
            }
        }
    }
    bannerEl.innerHTML = banners.join('');
}

function updateConnectionsPanel() {
    const el = document.getElementById('connectionsPanel');
    if (!el) return;
    let html = '';
    if (providerStatus.apple) {
        const c = providerStatus.apple.connected;
        html += `<div class="conn-item"><span class="conn-dot" style="background:${c ? '#1e8e3e' : '#d93025'}"></span> Apple: ${c ? 'Conectado' : 'Não configurado'}</div>`;
    }
    if (providerStatus.gmail) {
        const c = providerStatus.gmail.connected;
        let label = c ? 'Conectado' : 'Não conectado';
        if (!c) label += ` — <a href="/gmail/login" target="_blank" style="color:#1a73e8;">Conectar</a>`;
        html += `<div class="conn-item"><span class="conn-dot" style="background:${c ? '#1e8e3e' : '#d93025'}"></span> Gmail: ${label}</div>`;
    }
    el.innerHTML = html;
}

function updateRangeDisplay() {
    const el = document.getElementById('rangeDisplay');
    if (el && rangeInfo) {
        el.textContent = rangeInfo.description;
        el.title = `${rangeInfo.start_local} -> ${rangeInfo.end_local} (${rangeInfo.tz_name})`;
    }
}

function handleRangeChange() {
    currentLoadLimit = 50;
    totalAvailableEmails = 0;
    const rangeType = document.getElementById('rangeSelect').value;
    const nDays = document.getElementById('nDaysGroup');
    const custom = document.getElementById('customDateGroup');
    if (nDays) nDays.style.display = rangeType === 'last_n_days' ? 'flex' : 'none';
    if (custom) custom.style.display = rangeType === 'custom' ? 'flex' : 'none';
    loadEmails();
}

function removeExecutedEmails(results) {
    if (!results || results.length === 0) return;
    const unreadOnly = document.getElementById('unreadOnlyCheck')?.checked || false;
    const keysToRemove = new Set();
    const keysMarkedRead = new Set();
    const keysProcessed = new Set();
    for (const r of results) {
        const isOk = r.status === 'ok' || r.status === 'skipped' || r.status === 'done';
        if (!isOk) continue;
        const act = r.action || r.decision || '';
        trackAction(act, r.key);
        if (act === 'delete') {
            keysToRemove.add(r.key);
        } else if (act === 'mark_read') {
            if (unreadOnly) {
                keysToRemove.add(r.key);
            } else {
                keysMarkedRead.add(r.key);
            }
        } else if (act === 'send') {
            keysToRemove.add(r.key);
        } else if (act === 'skip') {
            keysProcessed.add(r.key);
        }
    }
    if (keysToRemove.size > 0) {
        emails = emails.filter(e => !keysToRemove.has(e.key));
        if (currentEmail && keysToRemove.has(currentEmail.key)) {
            currentEmail = null;
            renderEmailDetail();
        }
    }
    if (keysMarkedRead.size > 0) {
        for (const e of emails) {
            if (keysMarkedRead.has(e.key)) e.unread = false;
        }
    }
    if (keysProcessed.size > 0) {
        for (const e of emails) {
            if (keysProcessed.has(e.key)) e.classification = 'done';
        }
    }
    if (keysToRemove.size > 0 || keysMarkedRead.size > 0 || keysProcessed.size > 0) {
        const unreadNow = emails.filter(e => e.unread !== false).length;
        emailCounts.total = emails.length;
        emailCounts.loaded = emails.length;
        emailCounts.unread = unreadNow;
        emailCounts.read = emails.length - unreadNow;
        emailCounts.by_category = {};
        emailCounts.by_provider = {};
        emails.forEach(e => {
            const cat = e.classification || 'human';
            emailCounts.by_category[cat] = (emailCounts.by_category[cat] || 0) + 1;
            const p = e.provider || 'unknown';
            emailCounts.by_provider[p] = (emailCounts.by_provider[p] || 0) + 1;
        });
        renderEmailList();
        syncGlobalEmails();
        renderEmailDetail();
        updateStatus(`${emails.length} e-mails na lista`);
    }
}

function renderEmailList() {
    const listEl = document.getElementById('emailList');
    if (emails.length === 0) {
        const selected = getSelectedProviders();
        let msg = 'Nenhum e-mail encontrado';
        for (const prov of selected) {
            if (providerStatus[prov] && !providerStatus[prov].connected && prov === 'gmail') {
                msg = 'Gmail não conectado. Clique em "Conectar" acima.';
                break;
            }
        }
        listEl.innerHTML = `<div class="empty-state">${msg}</div>`;
        return;
    }
    let html = emails.map((email, idx) => `
        <div class="email-item ${currentEmail && currentEmail.key === email.key ? 'selected' : ''} ${email.unread === false ? 'read' : ''}"
             data-key="${email.key}"
             onclick="selectEmail(${idx})">
            <div class="from">
                <span>${escapeHtml((email.from || '').split('<')[0].trim() || email.from)}</span>
                <span class="badge ${email.classification}">${email.classification}</span>
            </div>
            <div class="subject">${escapeHtml(email.subject)}</div>
            <div class="meta">
                <span>${email.provider}</span>
                <span>${email.folder}</span>
            </div>
        </div>
    `).join('');

    if (totalAvailableEmails > emails.length) {
        const remaining = totalAvailableEmails - emails.length;
        html += `<div class="load-more-bar" onclick="loadMoreEmails()">
            <span>Carregar mais ${remaining} e-mail${remaining > 1 ? 's' : ''}</span>
        </div>`;
    }

    listEl.innerHTML = html;
}

async function loadMoreEmails() {
    currentLoadLimit += 50;
    await loadEmails();
}

async function selectEmail(idx) {
    currentEmail = emails[idx];
    syncGlobalEmails();
    renderEmailList();
    const detailEl = document.getElementById('emailDetail');
    setLoading(detailEl, true);
    try {
        const data = await apiCall(`/inbox/message/${encodeURIComponent(currentEmail.key)}`);
        currentEmail = { ...currentEmail, ...data };
        syncGlobalEmails();
        renderEmailDetail();
    } catch (err) {
        detailEl.innerHTML = `<div class="loading">Erro: ${err.message}</div>`;
    }
}

async function loadMailboxStats() {
    if (!navigator.onLine) return;
    try {
        const providers = getSelectedProviders().join(',');
        const folders = Array.from(document.querySelectorAll('.folder-check:checked')).map(cb => cb.value).join(',');
        const data = await apiCall(`/mailbox/stats?providers=${providers}&folders=${folders}`);
        if (data.ok) {
            mailboxStats = data;
            renderEmailDetail();
        }
    } catch (e) {
        console.warn('Mailbox stats failed:', e);
    }
}

function renderStatsDashboard() {
    const total = emailCounts.total || 0;
    const unread = emailCounts.unread || 0;
    const read = emailCounts.read || 0;
    const cats = emailCounts.by_category || {};
    
    const catLabels = {
        human: { label: 'Pessoais', icon: '👤', color: '#1565c0', bg: '#e3f2fd' },
        newsletter: { label: 'Newsletters', icon: '📰', color: '#e65100', bg: '#fff3e0' },
        automated: { label: 'Automáticos', icon: '🤖', color: '#616161', bg: '#f5f5f5' },
        otp: { label: 'Códigos/OTP', icon: '🔑', color: '#c62828', bg: '#fce4ec' }
    };
    
    const provLabels = {
        apple: { label: 'Apple Mail', icon: '🍎', color: '#333' },
        outlook: { label: 'Outlook', icon: '📧', color: '#0078d4' },
        gmail: { label: 'Gmail', icon: '✉️', color: '#ea4335' }
    };

    function makeRing(val, maxVal, color, size) {
        const r = size === 'sm' ? 36 : 54;
        const vb = size === 'sm' ? 80 : 128;
        const cx = vb / 2;
        const sw = size === 'sm' ? 8 : 12;
        const circ = 2 * Math.PI * r;
        const pct = maxVal > 0 ? val / maxVal : 0;
        const len = pct * circ;
        return `<svg viewBox="0 0 ${vb} ${vb}" class="stats-ring" style="width:${size === 'sm' ? 70 : 120}px;height:${size === 'sm' ? 70 : 120}px">
            <circle cx="${cx}" cy="${cx}" r="${r}" fill="none" stroke="#f0f0f0" stroke-width="${sw}"/>
            <circle cx="${cx}" cy="${cx}" r="${r}" fill="none" stroke="${color}" stroke-width="${sw}" 
                stroke-dasharray="${len} ${circ - len}" stroke-dashoffset="0" stroke-linecap="round" 
                style="transition: all 0.6s ease; transform: rotate(-90deg); transform-origin: center"/>
        </svg>`;
    }

    const selectedProviders = getSelectedProviders();
    const filteredActions = getActionsForProviders(selectedProviders);
    const actionTotal = filteredActions.total || 0;
    const actPct = total > 0 ? Math.round((actionTotal / total) * 100) : 0;

    let catCards = '';
    for (const [key, info] of Object.entries(catLabels)) {
        const count = cats[key] || 0;
        const p = total > 0 ? Math.round((count / total) * 100) : 0;
        catCards += `
            <div class="stat-cat-card" style="border-left: 4px solid ${info.color}; background: ${info.bg}">
                <div class="stat-cat-icon">${info.icon}</div>
                <div class="stat-cat-info">
                    <div class="stat-cat-label">${info.label}</div>
                    <div class="stat-cat-value">${count}</div>
                </div>
                <div class="stat-cat-bar-wrap">
                    <div class="stat-cat-bar" style="width: ${p}%; background: ${info.color}"></div>
                </div>
            </div>`;
    }

    let mbSection = '';
    if (mailboxStats && mailboxStats.providers) {
        let provRows = '';
        for (const [key, info] of Object.entries(provLabels)) {
            const ps = mailboxStats.providers[key];
            if (!ps) continue;
            const t = ps.total || 0;
            const u = ps.unseen || 0;
            const r = ps.read || 0;
            if (t === 0) continue;
            provRows += `
                <div class="stat-mailbox-row">
                    <div class="stat-mailbox-prov">
                        <span>${info.icon}</span>
                        <span class="stat-prov-name">${info.label}</span>
                    </div>
                    <div class="stat-mailbox-ring">
                        ${makeRing(u, t, '#1976d2', 'sm')}
                        <div class="stat-mailbox-ring-label">${t}</div>
                    </div>
                    <div class="stat-mailbox-nums">
                        <div><span class="stat-dot" style="background:#1976d2"></span> ${u} não lidos</div>
                        <div><span class="stat-dot" style="background:#66bb6a"></span> ${r} lidos</div>
                    </div>
                </div>`;
        }
        if (provRows) {
            const gt = mailboxStats.totals || {};
            mbSection = `
                <div class="stats-section">
                    <h4>Caixas de Entrada (Total)</h4>
                    ${provRows}
                    <div class="stat-mailbox-grand">
                        Total geral: <strong>${gt.total || 0}</strong> e-mails 
                        (<span style="color:#1976d2">${gt.unseen || 0} não lidos</span>, 
                        <span style="color:#66bb6a">${gt.read || 0} lidos</span>)
                    </div>
                </div>`;
        }
    }

    const filterLabel = rangeInfo ? rangeInfo.description || 'Período selecionado' : 'Período selecionado';

    return `
        <div class="stats-dashboard">
            <div class="stats-header">
                <h3>Painel de Controle</h3>
            </div>

            ${mbSection}

            <div class="stats-section">
                <h4>Filtro: ${escapeHtml(filterLabel)}</h4>
                <div class="stats-top-row">
                    <div class="stats-ring-card">
                        ${makeRing(unread, total || 1, '#1976d2', 'lg')}
                        <div class="stats-ring-center">
                            <div class="stats-ring-number">${total}</div>
                            <div class="stats-ring-label">e-mails</div>
                        </div>
                    </div>
                    <div class="stats-summary-cards">
                        <div class="stats-mini-card stats-unread">
                            <div class="stats-mini-icon">📩</div>
                            <div class="stats-mini-value">${unread}</div>
                            <div class="stats-mini-label">Não lidos</div>
                        </div>
                        <div class="stats-mini-card stats-read-card">
                            <div class="stats-mini-icon">📭</div>
                            <div class="stats-mini-value">${read}</div>
                            <div class="stats-mini-label">Lidos</div>
                        </div>
                        ${selectedProviders.map(p => {
                            const pa = actionsExecuted[p] || { total: 0 };
                            const pLabel = (provLabels[p] || {}).label || p;
                            const pIcon = (provLabels[p] || {}).icon || '⚡';
                            return `<div class="stats-mini-card stats-actions" title="${pLabel}">
                                <div class="stats-mini-icon">${pIcon}</div>
                                <div class="stats-mini-value">${pa.total}</div>
                                <div class="stats-mini-label">Ações ${pLabel.split(' ')[0]}</div>
                            </div>`;
                        }).join('')}
                        <div class="stats-mini-card stats-remaining">
                            <div class="stats-mini-icon">📋</div>
                            <div class="stats-mini-value">${Math.max(0, total - actionTotal)}</div>
                            <div class="stats-mini-label">Restantes</div>
                        </div>
                    </div>
                </div>
            </div>

            ${total > 0 ? `
            <div class="stats-section">
                <h4>Categorias</h4>
                <div class="stats-cat-grid">${catCards}</div>
            </div>` : ''}

            ${actionTotal > 0 ? `
            <div class="stats-section">
                <h4>Progresso</h4>
                <div class="stats-progress-bar-wrap">
                    <div class="stats-progress-bar" style="width: ${actPct}%"></div>
                </div>
                <div class="stats-progress-label">${actPct}% concluído (${actionTotal} de ${total})</div>
                <div class="stats-action-chips">
                    ${filteredActions.replied > 0 ? `<span class="stat-action-chip replied">↩ ${filteredActions.replied} respondidos</span>` : ''}
                    ${filteredActions.read > 0 ? `<span class="stat-action-chip read-done">✓ ${filteredActions.read} lidos</span>` : ''}
                    ${filteredActions.deleted > 0 ? `<span class="stat-action-chip deleted">🗑 ${filteredActions.deleted} apagados</span>` : ''}
                    ${filteredActions.skipped > 0 ? `<span class="stat-action-chip skipped">⏭ ${filteredActions.skipped} ignorados</span>` : ''}
                </div>
            </div>` : ''}

            ${total === 0 && !mailboxStats ? `
            <div class="stats-empty-hint">
                <div class="stats-empty-icon">📮</div>
                <p>Clique em <strong>Atualizar</strong> para carregar seus e-mails</p>
            </div>` : ''}
        </div>
    `;
}

function renderEmailDetail() {
    const detailEl = document.getElementById('emailDetail');
    if (!currentEmail) {
        detailEl.innerHTML = renderStatsDashboard();
        detailEl.className = 'email-detail stats-view';
        return;
    }
    detailEl.className = 'email-detail';
    detailEl.innerHTML = `
        <div class="email-header">
            <h2>${escapeHtml(currentEmail.subject)}</h2>
            <div class="meta">
                <strong>De:</strong> ${escapeHtml(currentEmail.from)}<br>
                <strong>Data:</strong> ${currentEmail.date}<br>
                <strong>Pasta:</strong> ${currentEmail.folder} |
                <strong>Provider:</strong> ${currentEmail.provider}
                <span class="badge ${currentEmail.classification}">${currentEmail.classification}</span>
            </div>
        </div>
        <div class="email-body">${escapeHtml(currentEmail.body || currentEmail.snippet || '')}</div>
        <div class="action-buttons">
            <button class="btn-primary" onclick="openSuggestReply()">Sugerir Resposta</button>
            <button class="btn-secondary" onclick="addToQueueDirect('mark_read')">Marcar como Lido</button>
            <button class="btn-danger" onclick="addToQueueDirect('delete')">Apagar</button>
            <button class="btn-secondary" onclick="addToQueueDirect('skip')">Ignorar</button>
        </div>
    `;
}

async function openSuggestReply() {
    if (!currentEmail) return;
    replyEmailKey = currentEmail.key;
    const modal = document.getElementById('replyModal');
    modal.classList.remove('hidden');
    document.getElementById('replyDraftText').value = '';
    document.getElementById('replyNotes').textContent = 'Gerando resposta...';
    await generateReply();
}

async function generateReply() {
    const tone = document.getElementById('replyTone').value;
    try {
        const data = await apiCall('/llm/suggest-reply', {
            method: 'POST',
            body: JSON.stringify({
                session_id: ensureSession(),
                key: replyEmailKey,
                tone: tone,
                language: 'pt',
                force: false,
            })
        });
        if (data.queued && data.job_id) {
            document.getElementById('replyNotes').textContent = 'Na fila de processamento...';
            const jobResult = await pollJob(data.job_id);
            if (jobResult.ok && jobResult.result) {
                const r = jobResult.result;
                if (r.draft_body) {
                    document.getElementById('replyDraftText').value = r.draft_body;
                    const notes = r.notes && r.notes.length > 0 ? r.notes.join(' | ') : '';
                    document.getElementById('replyNotes').textContent = notes;
                } else {
                    document.getElementById('replyDraftText').value = '';
                    const noteText = r.notes ? r.notes.join(', ') : `Acao sugerida: ${r.suggested_action}`;
                    document.getElementById('replyNotes').textContent = noteText;
                }
            } else {
                let errMsg = jobResult.message || 'Falha ao gerar resposta.';
                if (jobResult.error_code === 'rate_limited') errMsg = 'Assistente ocupado. Tente em 30s.';
                else if (jobResult.error_code === 'auth_or_billing') errMsg = 'Chave OpenAI sem credito.';
                document.getElementById('replyNotes').textContent = errMsg;
            }
        } else if (data.draft_body) {
            document.getElementById('replyDraftText').value = data.draft_body;
            const notes = data.notes && data.notes.length > 0 ? data.notes.join(' | ') : '';
            document.getElementById('replyNotes').textContent = notes + (data.cached ? ' (cache)' : '');
        } else {
            document.getElementById('replyDraftText').value = '';
            let noteText = data.notes ? data.notes.join(', ') : `Acao sugerida: ${data.suggested_action}`;
            if (data.error_code === 'rate_limited') noteText = 'Assistente ocupado. Tente em 30s.';
            else if (data.error_code === 'auth_or_billing') noteText = 'Chave OpenAI sem credito.';
            document.getElementById('replyNotes').textContent = noteText;
        }
    } catch (err) {
        document.getElementById('replyNotes').textContent = 'Erro: ' + err.message;
    }
}

function addReplyToQueue() {
    const body = document.getElementById('replyDraftText').value.trim();
    if (!body) {
        showToast('Escreva a resposta primeiro.');
        return;
    }
    addToQueueItem(replyEmailKey, 'send', body);
    document.getElementById('replyModal').classList.add('hidden');
}

async function addToQueueDirect(action) {
    if (!currentEmail) return;
    await addToQueueItem(currentEmail.key, action);
}

async function addToQueueItem(key, action, body = null) {
    try {
        const emailInfo = emails.find(e => e.key === key);
        const subject = emailInfo?.subject || null;
        await offlineStore.addCommand(currentSnapshotId, key, action, body, 'execute', subject);
        const actionLabels = { send: 'Enviar', delete: 'Deletar', mark_read: 'Marcar lido', skip: 'Ignorar' };
        showToast(`Na fila: ${actionLabels[action] || action} - ${(subject || key).slice(0, 40)}`);
        await loadQueue();
        switchTab('queue');
        updateQueueBadge();
    } catch (err) {
        showToast('Erro: ' + err.message);
    }
}

async function loadQueue() {
    try {
        const cmds = await offlineStore.listAll('command_queue');
        queueItems = cmds.filter(c => c.status === 'queued');
        renderQueue();
    } catch (err) {
        console.error('Queue load error:', err);
    }
}

function renderQueue() {
    const el = document.getElementById('queueList');
    if (queueItems.length === 0) {
        el.innerHTML = '<div class="empty-state">Fila vazia</div>';
        return;
    }
    const actionLabels = { send: 'ENVIAR', delete: 'DELETAR', mark_read: 'MARCAR LIDO', skip: 'IGNORAR' };
    el.innerHTML = queueItems.map(item => {
        const emailInfo = emails.find(e => e.key === item.key);
        const subject = emailInfo?.subject || item.subject || '';
        const displayLine = subject ? `${item.key} — ${subject}` : item.key;
        return `
        <div class="queue-item">
            <div class="qi-info">
                <div class="qi-action">${actionLabels[item.action] || item.action}</div>
                <div class="qi-subject">${escapeHtml(displayLine)}</div>
            </div>
            <button class="qi-remove" onclick="removeFromQueue('${item.id}')" title="Remover">&times;</button>
        </div>`;
    }).join('');
}

async function removeFromQueue(itemId) {
    try {
        await offlineStore.removeQueueItem('command_queue', itemId);
        await loadQueue();
        updateQueueBadge();
    } catch (err) {
        showToast('Erro: ' + err.message);
    }
}

async function executeQueue(mode) {
    if (queueItems.length === 0) {
        showToast('A fila está vazia.');
        return;
    }
    if (!navigator.onLine) {
        showToast('Sem conexão. Clique "Sincronizar" quando voltar online.');
        return;
    }
    const label = mode === 'dry_run' ? 'Validando' : 'Executando';
    showToast(`${label} ${queueItems.length} ações...`);

    try {
        const actions = queueItems.map(item => ({
            key: item.key,
            action: item.action,
            body: item.body || undefined,
        }));
        const data = await apiCall('/llm/dispatch', {
            method: 'POST',
            body: JSON.stringify({
                session_id: ensureSession(),
                actions: actions,
                mode: mode,
            })
        });
        if (data.results) {
            const ok = data.results.filter(r => r.status === 'ok').length;
            const errCount = data.results.filter(r => r.status === 'error').length;
            showToast(`${data.dry_run ? 'Simulação' : 'Executado'}: ${ok} OK, ${errCount} erros`, 5000);

            for (const item of queueItems) {
                const result = data.results.find(r => r.key === item.key);
                if (result) {
                    const status = (mode === 'dry_run') ? 'queued' : (result.status === 'ok' ? 'executed' : 'failed');
                    await offlineStore.updateQueueStatus('command_queue', item.id, status, result.status === 'error' ? result.message : null);
                }
            }
            if (mode !== 'dry_run') {
                await offlineStore.clearExecuted('command_queue');
            }
        }
        await loadQueue();
        updateQueueBadge();
        if (mode !== 'dry_run') {
            removeExecutedEmails(data.results);
            renderEmailDetail();
        }
    } catch (err) {
        showToast('Erro: ' + err.message);
    }
}

async function runTriage() {
    if (emails.length === 0) {
        showToast('Nenhum e-mail para triagem');
        return;
    }
    const keys = emails.map(e => e.key);
    const modal = document.getElementById('triageModal');
    modal.classList.remove('hidden');
    document.getElementById('triageResults').innerHTML = '<div class="loading"><div class="spinner"></div>IA analisando ' + keys.length + ' emails...</div>';

    try {
        const data = await apiCall('/llm/triage', {
            method: 'POST',
            body: JSON.stringify({
                session_id: ensureSession(),
                keys: keys,
                language: 'pt',
            })
        });

        if (data.queued && data.job_id) {
            document.getElementById('triageResults').innerHTML = '<div class="loading"><div class="spinner"></div>Na fila de processamento...</div>';
            const jobResult = await pollJob(data.job_id);
            if (jobResult.ok && jobResult.result && jobResult.result.items) {
                triageResults = jobResult.result.items;
                renderTriageResults();
            } else {
                let errMsg = jobResult.message || 'Falha ao processar.';
                if (jobResult.error_code === 'rate_limited') errMsg = 'Assistente temporariamente ocupado. Tente novamente em 30s.';
                else if (jobResult.error_code === 'auth_or_billing') errMsg = 'Chave OpenAI sem crédito ou inválida.';
                document.getElementById('triageResults').innerHTML = `<div style="color:#e65100; padding:16px;">${escapeHtml(errMsg)}<br><small>Você pode continuar usando o app sem IA.</small></div>`;
            }
        } else if (data.ok && data.items) {
            triageResults = data.items;
            renderTriageResults();
        } else {
            let errMsg = data.message || 'Erro na triagem';
            if (data.error_code === 'rate_limited') errMsg = 'Assistente temporariamente ocupado. Tente novamente em 30s.';
            document.getElementById('triageResults').innerHTML = `<div style="color:#e65100; padding:16px;">${escapeHtml(errMsg)}</div>`;
        }
    } catch (err) {
        document.getElementById('triageResults').innerHTML = '<div style="color:red;">Erro: ' + escapeHtml(err.message) + '</div>';
    }
}

function renderTriageResults() {
    const el = document.getElementById('triageResults');
    const priorityColors = { high: '#d93025', med: '#ff9800', low: '#1e8e3e' };
    const priorityLabels = { high: 'ALTA', med: 'MEDIA', low: 'BAIXA' };
    const actionLabels = { reply: 'Responder', delete: 'Deletar', skip: 'Ignorar', mark_read: 'Marcar lido', send: 'Enviar' };

    let html = `<div style="margin-bottom:10px; font-size:0.85rem; color:#666;">${triageResults.length} emails analisados</div>`;

    triageResults.forEach((item, idx) => {
        const pLabel = priorityLabels[item.priority] || item.priority;
        const pColor = priorityColors[item.priority] || '#666';
        const aLabel = actionLabels[item.suggested_action] || item.suggested_action;
        const emailInfo = emails.find(e => e.key === item.key);
        const subject = emailInfo ? emailInfo.subject : item.key;

        html += `<div class="triage-item priority-${item.priority}">
            <div class="ti-header">
                <span class="ti-priority" style="color:${pColor}">${pLabel}</span>
                <span class="ti-action-label">${aLabel}</span>
            </div>
            <div class="ti-key" title="${escapeHtml(item.key)}">${escapeHtml(subject)}</div>
            <div class="ti-summary">${escapeHtml(item.summary)}</div>
            <div class="ti-actions">
                <button onclick="triageAddToQueue(${idx}, '${item.suggested_action === 'reply' ? 'send' : item.suggested_action}')">Adicionar a Fila</button>
                ${item.suggested_action === 'reply' ? `<button onclick="triageSuggestReply(${idx})">Sugerir Resposta</button>` : ''}
            </div>
        </div>`;
    });

    el.innerHTML = html;
}

function triageAddToQueue(idx, action) {
    const item = triageResults[idx];
    if (!item) return;
    addToQueueItem(item.key, action);
}

function triageSuggestReply(idx) {
    const item = triageResults[idx];
    if (!item) return;
    replyEmailKey = item.key;
    document.getElementById('triageModal').classList.add('hidden');
    const modal = document.getElementById('replyModal');
    modal.classList.remove('hidden');
    document.getElementById('replyDraftText').value = '';
    document.getElementById('replyNotes').textContent = 'Gerando resposta...';
    generateReply();
}

async function triageAddAll() {
    if (!triageResults || triageResults.length === 0) return;
    for (const item of triageResults) {
        const action = item.suggested_action === 'reply' ? 'skip' : item.suggested_action;
        const emailInfo = emails.find(e => e.key === item.key);
        const subject = emailInfo?.subject || item.subject || null;
        await offlineStore.addCommand(currentSnapshotId, item.key, action, null, 'execute', subject);
    }
    showToast(`${triageResults.length} itens adicionados a fila`);
    await loadQueue();
    updateQueueBadge();
    document.getElementById('triageModal').classList.add('hidden');
    switchTab('queue');
}

async function sendChatMessage() {
    const input = document.getElementById('chatInput');
    const message = input.value.trim();
    if (!message) return;

    input.value = '';
    addChatBubble('user', message);

    const lowerMsg = message.toLowerCase().trim();
    const msgWords = lowerMsg.split(/\s+/);
    const strongApprovals = ['aprovado', 'confirmado', 'confirmei', 'confirmo', 'prosseguir'];
    const shortApprovals = ['sim', 'ok', 'aprova', 'aprovar', 'executar', 'execute', 'executa', 'confirma', 'confirmar', 'prossiga'];
    const phraseApprovals = ['pode sim', 'pode enviar', 'pode mandar', 'pode fazer', 'pode executar', 'pode apagar', 'pode deletar', 'manda ver', 'vai em frente', 'faz isso', 'prosseguir apagar', 'prosseguir deletar', 'aprovado pode'];
    const isStrongApproval = msgWords.length <= 4 && strongApprovals.some(w => lowerMsg.includes(w));
    const isShortApproval = msgWords.length <= 2 && shortApprovals.some(w => lowerMsg.includes(w));
    const isPhraseApproval = msgWords.length <= 4 && phraseApprovals.some(p => lowerMsg.includes(p));
    const isApproval = isStrongApproval || isShortApproval || isPhraseApproval;
    let pendingActions = window._proposedActions || [];
    if (pendingActions.length === 0) pendingActions = window._hfProposedActions || [];
    console.log(`[CHAT] approval check: isApproval=${isApproval}, pendingActions=${pendingActions.length}, msg="${lowerMsg}"`);
    if (isApproval && pendingActions.length > 0) {
        window._proposedActions = pendingActions;
        addChatBubble('system', `Executando ${pendingActions.length} ações...`);
        const approved = await chatApproveActions();
        if (approved) return;
    }

    addChatBubble('system', 'Pensando...');

    const visibleKeys = emails.map(e => e.key);

    try {
        const data = await apiCall('/llm/chat', {
            method: 'POST',
            body: JSON.stringify({
                session_id: ensureSession(),
                message: message,
                visible_keys: visibleKeys,
            })
        });

        if (data.queued && data.job_id) {
            addChatBubble('system', 'Aguardando processamento...');
            const jobResult = await pollJob(data.job_id);
            removeChatBubble('system');
            if (jobResult.ok && jobResult.result) {
                addChatBubble('assistant', jobResult.result.answer || 'Processado.');
                if (jobResult.result.proposed_actions && jobResult.result.proposed_actions.length > 0) {
                    renderProposedActions(jobResult.result.proposed_actions);
                }
            } else {
                addChatBubble('assistant', jobResult.message || 'Falha ao processar. Você pode continuar usando o app sem IA.');
            }
            return;
        }

        removeChatBubble('system');

        if (data.ok) {
            addChatBubble('assistant', data.answer);

            if (data.proposed_actions && data.proposed_actions.length > 0) {
                renderProposedActions(data.proposed_actions);
            }
        } else {
            let errMsg = data.answer || data.message || 'Erro no processamento';
            if (data.error_code === 'rate_limited' || errMsg.includes('429')) {
                errMsg = 'Assistente temporariamente ocupado. Tente novamente em 30s.';
            } else if (data.error_code === 'auth_or_billing') {
                errMsg = 'Chave OpenAI sem crédito ou inválida. Verifique Billing/Usage.';
            }
            addChatBubble('assistant', errMsg);
        }
    } catch (err) {
        removeChatBubble('system');
        addChatBubble('assistant', 'Erro: ' + err.message);
    }
}

async function pollJob(jobId, maxAttempts = 30, intervalMs = 2000) {
    for (let i = 0; i < maxAttempts; i++) {
        await new Promise(r => setTimeout(r, intervalMs));
        try {
            const data = await apiCall(`/llm/job/${jobId}`);
            if (data.status === 'done') return { ok: true, result: data.result || {} };
            if (data.status === 'error') {
                let msg = data.error_message || 'Erro desconhecido';
                if (data.error_code === 'rate_limited') msg = 'Assistente temporariamente ocupado. Tente novamente em 30s.';
                if (data.error_code === 'auth_or_billing') msg = 'Chave OpenAI sem crédito ou inválida. Verifique Billing/Usage.';
                return { ok: false, error_code: data.error_code, message: msg };
            }
        } catch (e) {
            continue;
        }
    }
    return { ok: false, error_code: 'timeout', message: 'Tempo esgotado aguardando resposta da IA.' };
}

function addChatBubble(role, text) {
    const history = document.getElementById('chatHistory');
    const div = document.createElement('div');
    div.className = `chat-msg ${role}`;
    div.textContent = text;
    if (role === 'system') div.dataset.temp = 'true';
    history.appendChild(div);
    history.scrollTop = history.scrollHeight;
}

function removeChatBubble(role) {
    const history = document.getElementById('chatHistory');
    const temp = history.querySelector(`.chat-msg.${role}[data-temp]`);
    if (temp) temp.remove();
}

function renderProposedActions(actions) {
    const history = document.getElementById('chatHistory');
    const actionLabels = { send: 'ENVIAR', delete: 'DELETAR', mark_read: 'MARCAR LIDO', skip: 'IGNORAR' };

    actions.forEach((action, idx) => {
        const card = document.createElement('div');
        card.className = 'proposed-action-card';
        const emailInfo = emails.find(e => e.key === action.key);
        const subject = emailInfo ? emailInfo.subject : action.key;
        card.innerHTML = `
            <div class="action-label">${actionLabels[action.action] || action.action}</div>
            <div style="font-size:0.8rem; color:#333;">${escapeHtml(subject)}</div>
            ${action.body ? `<div style="font-size:0.75rem; color:#666; margin-top:2px;">${escapeHtml(action.body.substring(0, 100))}...</div>` : ''}
            <button onclick="executeProposedAction(this, '${escapeHtml(action.key)}', '${action.action}', ${idx})">Executar</button>
        `;
        history.appendChild(card);
    });

    window._proposedActions = actions;
    history.scrollTop = history.scrollHeight;
}

async function executeProposedAction(btn, key, action, idx) {
    const actions = window._proposedActions || [];
    const a = actions[idx];
    btn.disabled = true;
    btn.textContent = 'Executando...';
    try {
        const data = await apiCall('/llm/dispatch', {
            method: 'POST',
            body: JSON.stringify({ session_id: ensureSession(), actions: [{ key, action, body: a ? a.body : null }], mode: 'execute', confirm_delete: true })
        });
        if (data.ok) {
            const r = data.results && data.results[0];
            if (r && r.status === 'ok') {
                btn.textContent = 'Executado';
                btn.style.color = '#00c853';
                removeExecutedEmails(data.results);
            } else {
                btn.textContent = `Falha: ${r?.message || 'erro'}`;
                btn.style.color = '#e53935';
            }
        } else {
            btn.textContent = `Falha: ${data.message || 'erro'}`;
            btn.style.color = '#e53935';
        }
    } catch (err) {
        btn.textContent = `Erro: ${err.message}`;
        btn.style.color = '#e53935';
    }
}

function switchTab(tabName) {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    const activeTab = document.querySelector(`.tab[data-tab="${tabName}"]`);
    if (activeTab) activeTab.classList.add('active');

    document.querySelectorAll('.tab-content').forEach(c => c.classList.add('hidden'));
    const tabContent = document.getElementById(`${tabName}Tab`);
    if (tabContent) tabContent.classList.remove('hidden');

    if (tabName === 'queue') loadQueue();
}

function updateStatus(message) {
    document.getElementById('statusText').textContent = message;
}

function escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

async function exportChatGPT() {
    const range = document.getElementById('rangeSelect').value;
    const providers = getSelectedProviders().join(',');
    const folders = Array.from(document.querySelectorAll('.folder-check:checked')).map(cb => cb.value).join(',');
    try {
        const text = await apiCall(`/export/chatgpt?range=${range}&providers=${providers}&folders=${folders}`);
        await navigator.clipboard.writeText(text);
        showToast('Resumo copiado com sucesso!');
    } catch (err) {
        showToast('Erro: ' + err.message);
    }
}

async function exportDispatch() {
    try {
        const data = await apiCall('/export/dispatch.json');
        await navigator.clipboard.writeText(JSON.stringify(data, null, 2));
        showToast('JSON de despacho copiado com sucesso!');
    } catch (err) {
        showToast('Erro: ' + err.message);
    }
}

async function executeImport(dryRun) {
    const input = document.getElementById('importJsonInput').value.trim();
    const resultEl = document.getElementById('importResult');
    if (!input) {
        resultEl.innerHTML = '<span style="color:red;">Cole o JSON primeiro</span>';
        return;
    }
    let payload;
    try {
        payload = JSON.parse(input);
    } catch (e) {
        resultEl.innerHTML = '<span style="color:red;">JSON invalido: ' + e.message + '</span>';
        return;
    }
    payload.dry_run = dryRun;
    try {
        const data = await apiCall('/dispatch/import', {
            method: 'POST',
            body: JSON.stringify(payload)
        });
        let html = '<div style="margin-bottom:8px;">' + (dryRun ? '<b>SIMULACAO:</b>' : '<b>EXECUTADO:</b>') + '</div>';
        if (data.counts) {
            const c = data.counts;
            html += `<div>Acoes: send=${c.send||0}, delete=${c.delete||0}, mark_read=${c.mark_read||0}</div>`;
        }
        if (data.results && data.results.length > 0) {
            html += '<div style="margin-top:8px;"><b>Acoes:</b></div>';
            data.results.forEach(r => {
                const color = r.status === 'done' ? 'green' : (r.status === 'error' ? 'red' : 'orange');
                html += `<div style="color:${color};">${r.key}: ${r.decision} - ${r.status}</div>`;
            });
        }
        resultEl.innerHTML = html;
        if (!dryRun) { showToast('Import executado!'); removeExecutedEmails(data.results); }
    } catch (err) {
        resultEl.innerHTML = '<span style="color:red;">Erro: ' + err.message + '</span>';
    }
}

function updateNetworkStatus() {
    isOnline = navigator.onLine;
    const el = document.getElementById('networkStatus');
    if (!el) return;
    if (isOnline) {
        el.textContent = 'Online';
        el.className = 'net-online';
    } else {
        el.textContent = 'Offline';
        el.className = 'net-offline';
    }
    const syncBtn = document.getElementById('syncBtn');
    if (syncBtn) syncBtn.disabled = !isOnline;
}

function updateSnapshotDisplay() {
    const el = document.getElementById('snapshotInfo');
    if (!el) return;
    if (currentSnapshotId) {
        const ts = currentSnapshotId.replace('snap_', '');
        const d = new Date(parseInt(ts));
        if (!isNaN(d.getTime())) {
            el.textContent = `Snapshot: ${d.toLocaleTimeString('pt-BR', {hour:'2-digit', minute:'2-digit'})}`;
        } else {
            el.textContent = 'Snapshot ativo';
        }
    } else {
        el.textContent = '';
    }
}

async function updateQueueBadge() {
    const el = document.getElementById('queueBadge');
    const syncBtn = document.getElementById('syncBtn');
    if (!el) return;
    try {
        const counts = await offlineStore.getQueueCounts();
        const total = counts.commands_queued + counts.cognitive_queued;
        if (total > 0) {
            el.textContent = `${total} pendente${total > 1 ? 's' : ''}`;
            el.classList.remove('hidden');
            if (syncBtn) syncBtn.classList.add('has-pending');
        } else {
            el.classList.add('hidden');
            if (syncBtn) syncBtn.classList.remove('has-pending');
        }
    } catch (e) {
        el.classList.add('hidden');
    }
}

async function syncDispatch() {
    if (!navigator.onLine) {
        showToast('Sem conexão. Os itens ficaram na fila.');
        return;
    }

    const syncBtn = document.getElementById('syncBtn');
    if (syncBtn) { syncBtn.disabled = true; syncBtn.textContent = 'Sincronizando...'; }

    try {
        const cogTasks = await offlineStore.listQueued('cognitive_queue');
        let cogProcessed = 0;
        for (const task of cogTasks) {
            await offlineStore.updateQueueStatus('cognitive_queue', task.id, 'processing');
            try {
                let endpoint = '', body = {};
                if (task.task === 'suggest_reply') {
                    endpoint = '/llm/suggest-reply';
                    body = { session_id: ensureSession(), key: task.key, tone: task.params.tone || 'neutral', language: task.params.language || 'pt', force: false };
                } else if (task.task === 'triage_item') {
                    endpoint = '/llm/triage';
                    body = { session_id: ensureSession(), keys: [task.key], language: task.params.language || 'pt' };
                } else if (task.task === 'summarize') {
                    endpoint = '/llm/suggest-reply';
                    body = { session_id: ensureSession(), key: task.key, tone: 'short', language: task.params.language || 'pt', force: false };
                }
                if (endpoint) {
                    const data = await apiCall(endpoint, { method: 'POST', body: JSON.stringify(body) });
                    const hash = task.content_hash;
                    if (data.queued && data.job_id) {
                        const jobResult = await pollJob(data.job_id);
                        if (jobResult.ok) {
                            await offlineStore.updateQueueStatus('cognitive_queue', task.id, 'done', null, jobResult.result);
                            if (hash) {
                                await offlineStore.saveCognitiveResult(currentSnapshotId, task.key, task.task, hash, jobResult.result);
                            }
                        } else {
                            await offlineStore.updateQueueStatus('cognitive_queue', task.id, 'failed', jobResult.message);
                        }
                    } else {
                        await offlineStore.updateQueueStatus('cognitive_queue', task.id, 'done', null, data);
                        if (hash) {
                            await offlineStore.saveCognitiveResult(currentSnapshotId, task.key, task.task, hash, data);
                        }
                    }
                    cogProcessed++;
                }
            } catch (err) {
                await offlineStore.updateQueueStatus('cognitive_queue', task.id, 'queued', err.message);
            }
        }
        if (cogProcessed > 0) showToast(`${cogProcessed} tarefa(s) de IA processada(s)`);

        const cmds = await offlineStore.listQueued('command_queue');
        if (cmds.length > 0) {
            const actions = cmds.map(c => ({ key: c.key, action: c.action, body: c.body || undefined }));
            const data = await apiCall('/llm/dispatch', {
                method: 'POST',
                body: JSON.stringify({ session_id: ensureSession(), actions: actions, mode: 'execute' })
            });
            if (data.results) {
                let okCount = 0, errCount = 0;
                for (const cmd of cmds) {
                    const r = data.results.find(x => x.key === cmd.key);
                    if (r && r.status === 'ok') {
                        await offlineStore.updateQueueStatus('command_queue', cmd.id, 'executed');
                        okCount++;
                    } else if (r && r.status === 'already_done') {
                        await offlineStore.updateQueueStatus('command_queue', cmd.id, 'executed');
                        okCount++;
                    } else {
                        await offlineStore.updateQueueStatus('command_queue', cmd.id, 'failed', r?.message || 'Erro');
                        errCount++;
                    }
                }
                await offlineStore.clearExecuted('command_queue');
                showToast(`Despacho: ${okCount} OK, ${errCount} erros`);
                removeExecutedEmails(data.results);
            }
        }

        await loadQueue();
        updateQueueBadge();

    } catch (err) {
        showToast('Erro na sincronização: ' + err.message);
    } finally {
        if (syncBtn) { syncBtn.disabled = false; syncBtn.textContent = 'Sincronizar'; }
    }
}

document.addEventListener('DOMContentLoaded', () => {
    offlineStore.openDB().catch(e => console.warn('IndexedDB init failed:', e));

    ensureSession();
    updateNetworkStatus();
    handleRangeChange();
    loadQueue();
    updateQueueBadge();
    renderEmailDetail();

    window.addEventListener('online', () => { updateNetworkStatus(); showToast('Conexão restaurada'); });
    window.addEventListener('offline', () => { updateNetworkStatus(); showToast('Sem conexão - modo off-line'); });

    document.getElementById('syncBtn').addEventListener('click', syncDispatch);
    document.getElementById('refreshBtn').addEventListener('click', loadEmails);
    document.getElementById('triageBtn').addEventListener('click', runTriage);
    document.getElementById('closeTriageBtn').addEventListener('click', () => {
        document.getElementById('triageModal').classList.add('hidden');
    });
    document.getElementById('triageAddAllBtn').addEventListener('click', triageAddAll);

    document.getElementById('saveApiKeyBtn').addEventListener('click', saveApiKey);
    document.getElementById('cancelApiKeyBtn').addEventListener('click', hideApiKeyModal);

    document.getElementById('chatSendBtn').addEventListener('click', sendChatMessage);
    document.getElementById('chatInput').addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            sendChatMessage();
        }
    });

    document.getElementById('queueDryRunBtn').addEventListener('click', () => executeQueue('dry_run'));
    document.getElementById('queueExecuteBtn').addEventListener('click', () => {
        if (queueItems.length === 0) { showToast('A fila está vazia.'); return; }
        if (!confirm(`Executar ${queueItems.length} ações?`)) return;
        executeQueue('execute');
    });

    document.getElementById('closeReplyBtn').addEventListener('click', () => {
        document.getElementById('replyModal').classList.add('hidden');
    });
    document.getElementById('addReplyToQueueBtn').addEventListener('click', addReplyToQueue);
    document.getElementById('regenerateReplyBtn').addEventListener('click', () => {
        document.getElementById('replyNotes').textContent = 'Regenerando...';
        generateReply();
    });

    document.getElementById('exportChatGPTBtn').addEventListener('click', exportChatGPT);
    document.getElementById('exportDispatchBtn').addEventListener('click', exportDispatch);
    document.getElementById('importDryRunBtn').addEventListener('click', () => executeImport(true));
    document.getElementById('importExecuteBtn').addEventListener('click', () => executeImport(false));

    document.querySelectorAll('.tab').forEach(tab => {
        tab.addEventListener('click', () => switchTab(tab.dataset.tab));
    });

    document.querySelectorAll('.provider-check, .folder-check').forEach(cb => {
        cb.addEventListener('change', () => { currentLoadLimit = 50; totalAvailableEmails = 0; loadEmails(); });
    });

    document.getElementById('rangeSelect').addEventListener('change', handleRangeChange);

    const nDaysInput = document.getElementById('nDaysInput');
    if (nDaysInput) nDaysInput.addEventListener('change', loadEmails);

    const startDateInput = document.getElementById('startDateInput');
    const endDateInput = document.getElementById('endDateInput');
    if (startDateInput) startDateInput.addEventListener('change', loadEmails);
    if (endDateInput) endDateInput.addEventListener('change', loadEmails);

});

async function chatApproveActions() {
    const pendingActions = window._proposedActions || [];
    if (pendingActions.length === 0) return false;
    const result = await hfDispatch(pendingActions);
    window._proposedActions = [];
    window._hfProposedActions = [];
    addChatBubble('system', result);
    return true;
}

