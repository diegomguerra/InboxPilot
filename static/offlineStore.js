const DB_NAME = 'inboxpilot_offline';
const DB_VERSION = 1;
let _db = null;

function openDB() {
    return new Promise((resolve, reject) => {
        if (_db) { resolve(_db); return; }
        const req = indexedDB.open(DB_NAME, DB_VERSION);
        req.onupgradeneeded = (e) => {
            const db = e.target.result;
            if (!db.objectStoreNames.contains('snapshots')) {
                const s = db.createObjectStore('snapshots', { keyPath: 'snapshot_id' });
                s.createIndex('created_at', 'created_at');
                s.createIndex('filter_hash', 'filter_hash');
            }
            if (!db.objectStoreNames.contains('command_queue')) {
                const c = db.createObjectStore('command_queue', { keyPath: 'id' });
                c.createIndex('status', 'status');
                c.createIndex('snapshot_id', 'snapshot_id');
                c.createIndex('created_at', 'created_at');
            }
            if (!db.objectStoreNames.contains('cognitive_queue')) {
                const cq = db.createObjectStore('cognitive_queue', { keyPath: 'id' });
                cq.createIndex('status', 'status');
                cq.createIndex('snapshot_id', 'snapshot_id');
                cq.createIndex('created_at', 'created_at');
            }
            if (!db.objectStoreNames.contains('cognitive_results')) {
                const cr = db.createObjectStore('cognitive_results', { keyPath: 'result_id' });
                cr.createIndex('key', 'key');
                cr.createIndex('snapshot_id', 'snapshot_id');
                cr.createIndex('lookup', ['key', 'task', 'content_hash']);
            }
        };
        req.onsuccess = (e) => { _db = e.target.result; resolve(_db); };
        req.onerror = (e) => reject(e.target.error);
    });
}

async function _tx(storeName, mode, fn) {
    const db = await openDB();
    return new Promise((resolve, reject) => {
        const tx = db.transaction(storeName, mode);
        const store = tx.objectStore(storeName);
        try {
            const result = fn(store);
            if (result && result.onsuccess !== undefined) {
                result.onsuccess = () => resolve(result.result);
                result.onerror = () => reject(result.error);
            } else {
                tx.oncomplete = () => resolve(result);
                tx.onerror = () => reject(tx.error);
            }
        } catch (e) { reject(e); }
    });
}

async function _getAll(storeName, indexName, value) {
    const db = await openDB();
    return new Promise((resolve, reject) => {
        const tx = db.transaction(storeName, 'readonly');
        const store = tx.objectStore(storeName);
        let req;
        if (indexName && value !== undefined) {
            req = store.index(indexName).getAll(value);
        } else {
            req = store.getAll();
        }
        req.onsuccess = () => resolve(req.result);
        req.onerror = () => reject(req.error);
    });
}

function _genId(prefix) {
    return `${prefix}_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
}

function _now() { return new Date().toISOString(); }

async function _simpleHash(text) {
    const data = new TextEncoder().encode(text);
    const buf = await crypto.subtle.digest('SHA-256', data);
    return Array.from(new Uint8Array(buf)).map(b => b.toString(16).padStart(2, '0')).join('');
}

async function computeFilterHash(filters) {
    const str = JSON.stringify({
        providers: (filters.providers || []).sort(),
        folders: (filters.folders || []).sort(),
        rangeType: filters.rangeType || '',
        n: filters.n || 0,
        startDate: filters.startDate || '',
        endDate: filters.endDate || '',
        unreadOnly: !!filters.unreadOnly,
    });
    return _simpleHash(str);
}

async function computeContentHash(email) {
    const parts = [
        email.subject || '',
        email.from || email.from_addr || '',
        email.snippet || '',
        email.date || '',
    ];
    if (email.body_text || email.body) {
        const body = (email.body_text || email.body || '').slice(0, 2000);
        parts.push(body);
    }
    return _simpleHash(parts.join('|'));
}

async function saveSnapshot(snapshotData) {
    const db = await openDB();
    return new Promise((resolve, reject) => {
        const tx = db.transaction('snapshots', 'readwrite');
        tx.objectStore('snapshots').put(snapshotData);
        tx.oncomplete = () => resolve();
        tx.onerror = () => reject(tx.error);
    });
}

async function getLatestSnapshotByFilter(filterHash) {
    const all = await _getAll('snapshots', 'filter_hash', filterHash);
    if (all.length === 0) return null;
    all.sort((a, b) => b.created_at.localeCompare(a.created_at));
    return all[0];
}

async function getLatestSnapshot() {
    const all = await _getAll('snapshots');
    if (all.length === 0) return null;
    all.sort((a, b) => b.created_at.localeCompare(a.created_at));
    return all[0];
}

async function addCommand(snapshotId, key, action, body, mode, subject) {
    const item = {
        id: _genId('cmd'),
        snapshot_id: snapshotId || '',
        key: key,
        action: action,
        body: body || null,
        mode: mode || 'execute',
        subject: subject || null,
        status: 'queued',
        error: null,
        created_at: _now(),
        updated_at: _now(),
    };
    const db = await openDB();
    return new Promise((resolve, reject) => {
        const tx = db.transaction('command_queue', 'readwrite');
        tx.objectStore('command_queue').put(item);
        tx.oncomplete = () => resolve(item);
        tx.onerror = () => reject(tx.error);
    });
}

async function addCognitiveTask(snapshotId, key, task, params, contentHash) {
    const item = {
        id: _genId('cog'),
        snapshot_id: snapshotId || '',
        key: key,
        task: task,
        params: params || {},
        content_hash: contentHash || null,
        status: 'queued',
        result: null,
        error: null,
        created_at: _now(),
        updated_at: _now(),
    };
    const db = await openDB();
    return new Promise((resolve, reject) => {
        const tx = db.transaction('cognitive_queue', 'readwrite');
        tx.objectStore('cognitive_queue').put(item);
        tx.oncomplete = () => resolve(item);
        tx.onerror = () => reject(tx.error);
    });
}

async function listQueued(storeName) {
    return _getAll(storeName, 'status', 'queued');
}

async function listAll(storeName) {
    return _getAll(storeName);
}

async function updateQueueStatus(storeName, id, status, errorMsg, result) {
    const db = await openDB();
    return new Promise((resolve, reject) => {
        const tx = db.transaction(storeName, 'readwrite');
        const store = tx.objectStore(storeName);
        const req = store.get(id);
        req.onsuccess = () => {
            const item = req.result;
            if (!item) { resolve(null); return; }
            item.status = status;
            item.updated_at = _now();
            if (errorMsg !== undefined) item.error = errorMsg;
            if (result !== undefined) item.result = result;
            store.put(item);
        };
        tx.oncomplete = () => resolve(true);
        tx.onerror = () => reject(tx.error);
    });
}

async function removeQueueItem(storeName, id) {
    const db = await openDB();
    return new Promise((resolve, reject) => {
        const tx = db.transaction(storeName, 'readwrite');
        tx.objectStore(storeName).delete(id);
        tx.oncomplete = () => resolve();
        tx.onerror = () => reject(tx.error);
    });
}

async function clearExecuted(storeName) {
    const all = await _getAll(storeName);
    const db = await openDB();
    return new Promise((resolve, reject) => {
        const tx = db.transaction(storeName, 'readwrite');
        const store = tx.objectStore(storeName);
        for (const item of all) {
            if (item.status === 'executed' || item.status === 'done') {
                store.delete(item.id);
            }
        }
        tx.oncomplete = () => resolve();
        tx.onerror = () => reject(tx.error);
    });
}

async function saveCognitiveResult(snapshotId, key, task, contentHash, result) {
    const item = {
        result_id: _genId('res'),
        snapshot_id: snapshotId || '',
        key: key,
        task: task,
        content_hash: contentHash,
        result: result,
        created_at: _now(),
    };
    const db = await openDB();
    return new Promise((resolve, reject) => {
        const tx = db.transaction('cognitive_results', 'readwrite');
        tx.objectStore('cognitive_results').put(item);
        tx.oncomplete = () => resolve(item);
        tx.onerror = () => reject(tx.error);
    });
}

async function getCognitiveResultByHash(key, task, contentHash) {
    const db = await openDB();
    return new Promise((resolve, reject) => {
        const tx = db.transaction('cognitive_results', 'readonly');
        const idx = tx.objectStore('cognitive_results').index('lookup');
        const req = idx.get([key, task, contentHash]);
        req.onsuccess = () => resolve(req.result || null);
        req.onerror = () => reject(req.error);
    });
}

async function getQueueCounts() {
    const cmds = await _getAll('command_queue');
    const cogs = await _getAll('cognitive_queue');
    return {
        commands_queued: cmds.filter(c => c.status === 'queued').length,
        commands_total: cmds.length,
        cognitive_queued: cogs.filter(c => c.status === 'queued').length,
        cognitive_total: cogs.length,
    };
}

async function cleanOldSnapshots(keepCount) {
    keepCount = keepCount || 5;
    const all = await _getAll('snapshots');
    if (all.length <= keepCount) return;
    all.sort((a, b) => b.created_at.localeCompare(a.created_at));
    const toDelete = all.slice(keepCount);
    const db = await openDB();
    return new Promise((resolve, reject) => {
        const tx = db.transaction('snapshots', 'readwrite');
        const store = tx.objectStore('snapshots');
        for (const snap of toDelete) store.delete(snap.snapshot_id);
        tx.oncomplete = () => resolve();
        tx.onerror = () => reject(tx.error);
    });
}

window.offlineStore = {
    openDB,
    computeFilterHash,
    computeContentHash,
    saveSnapshot,
    getLatestSnapshotByFilter,
    getLatestSnapshot,
    addCommand,
    addCognitiveTask,
    listQueued,
    listAll,
    updateQueueStatus,
    removeQueueItem,
    clearExecuted,
    saveCognitiveResult,
    getCognitiveResultByHash,
    getQueueCounts,
    cleanOldSnapshots,
};
