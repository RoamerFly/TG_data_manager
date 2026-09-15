/**
 * Telegram 缓存管理器 — 前端交互
 */

// ==================== 全局状态 ====================
const state = {
    files: [],
    currentPage: 1,
    perPage: 50,
    totalFiles: 0,
    category: 'all',
    dlCategory: 'all',     // 下载文件分类筛选
    cacheType: '',         // 分组: '' (全部) / 'cache' / 'media_cache'
    search: '',
    sort: 'size_desc',
    selectedIds: new Set(),
    currentPreviewId: null,
    scanPollTimer: null,
    currentLottie: null,
    viewMode: 'cache',     // 'cache' 或 'downloads'
    theme: 'light',        // 'light' 或 'dark'
};

// ==================== DOM 引用 ====================
const $ = (id) => document.getElementById(id);

const el = {
    btnScan: $('btn-scan'),
    btnExports: $('btn-exports'),
    btnSettings: $('btn-settings'),
    scanProgress: $('scan-progress'),
    scanProgressText: $('scan-progress-text'),
    scanProgressCount: $('scan-progress-count'),
    scanProgressFill: $('scan-progress-fill'),
    statsBar: $('stats-bar'),
    dlStatsBar: $('dl-stats-bar'),
    toolbar: $('toolbar'),
    fileGrid: $('file-grid'),
    emptyState: $('empty-state'),
    searchInput: $('search-input'),
    sortSelect: $('sort-select'),
    fileCount: $('file-count'),
    btnSelectAll: $('btn-select-all'),
    btnExportSelected: $('btn-export-selected'),
    btnDeleteSelected: $('btn-delete-selected'),
    btnTheme: $('btn-theme'),
    iconTheme: $('icon-theme'),
    toast: $('toast'),
};

// ==================== Toast 通知 ====================
function toast(msg, type = '') {
    el.toast.textContent = msg;
    el.toast.className = 'toast' + (type ? ' ' + type : '');
    el.toast.classList.remove('hidden');
    clearTimeout(toast._timer);
    // error 类型延长到 5s, 其他 3s
    const duration = type === 'error' ? 5000 : 3000;
    toast._timer = setTimeout(() => el.toast.classList.add('hidden'), duration);
}

// ==================== 主题切换 ====================
function applyTheme(theme) {
    state.theme = theme;
    document.documentElement.setAttribute('data-theme', theme);
    try { localStorage.setItem('tgcm_theme', theme); } catch(e) {}
    // 切换图标 (浅色显示月亮, 深色显示太阳)
    if (el.iconTheme) {
        el.iconTheme.innerHTML = theme === 'dark'
            ? '<path fill="currentColor" d="M12 7c-2.76 0-5 2.24-5 5s2.24 5 5 5 5-2.24 5-5-2.24-5-5-5zM2 13h2c.55 0 1-.45 1-1s-.45-1-1-1H2c-.55 0-1 .45-1 1s.45 1 1 1zm18 0h2c.55 0 1-.45 1-1s-.45-1-1-1h-2c-.55 0-1 .45-1 1s.45 1 1 1zM11 2v2c0 .55.45 1 1 1s1-.45 1-1V2c0-.55-.45-1-1-1s-1 .45-1 1zm0 18v2c0 .55.45 1 1 1s1-.45 1-1v-2c0-.55-.45-1-1-1s-1 .45-1 1zM5.99 4.58a.996.996 0 0 0-1.41 0 .996.996 0 0 0 0 1.41l1.06 1.06c.39.39 1.03.39 1.41 0s.39-1.03 0-1.41L5.99 4.58zm12.37 12.37a.996.996 0 0 0-1.41 0 .996.996 0 0 0 0 1.41l1.06 1.06c.39.39 1.03.39 1.41 0a.996.996 0 0 0 0-1.41l-1.06-1.06zm1.06-10.96a.996.996 0 0 0 0-1.41.996.996 0 0 0-1.41 0l-1.06 1.06c-.39.39-.39 1.03 0 1.41s1.03.39 1.41 0l1.06-1.06zM7.05 18.36a.996.996 0 0 0-1.41 0 .996.996 0 0 0 0 1.41l1.06 1.06c.39.39 1.03.39 1.41 0s.39-1.03 0-1.41l-1.06-1.06z"/>'
            : '<path fill="currentColor" d="M12 3a9 9 0 1 0 9 9c0-.46-.04-.92-.1-1.36a5.389 5.389 0 0 1-4.4 2.26 5.403 5.403 0 0 1-3.14-9.8c-.44-.06-.9-.1-1.36-.1z"/>';
    }
}

function toggleTheme() {
    applyTheme(state.theme === 'light' ? 'dark' : 'light');
}

// ==================== 选中计数徽标 ====================
function updateSelectedBadge() {
    const badge = $('selected-badge');
    const count = $('selected-count');
    if (!badge || !count) return;
    const n = state.selectedIds.size;
    badge.classList.toggle('hidden', n === 0);
    count.textContent = n;
    // 显示选中文件总大小
    const sizeEl = $('selected-size');
    if (sizeEl) {
        let totalSize = 0;
        state.selectedIds.forEach(id => {
            const card = document.querySelector(`.file-card[data-id="${id}"]`);
            if (card) totalSize += parseInt(card.dataset.size || '0', 10);
        });
        sizeEl.textContent = totalSize > 0 ? ` · ${formatSize(totalSize)}` : '';
    }
}

// ==================== 工具函数 ====================
function formatSize(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    if (bytes < 1024 * 1024 * 1024) return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
    return (bytes / (1024 * 1024 * 1024)).toFixed(2) + ' GB';
}

function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = String(str);
    return div.innerHTML.replace(/'/g, '&#39;').replace(/"/g, '&quot;');
}

// ==================== API 调用 ====================
async function api(url, options = {}) {
    const resp = await fetch(url, {
        headers: { 'Content-Type': 'application/json' },
        ...options,
    });
    const data = await resp.json();
    if (!resp.ok) {
        const err = new Error(data.error || '请求失败');
        // 把后端的结构化原因带出来, 便于在界面上说明"为什么失败"
        err.payload = data;
        err.status = resp.status;
        throw err;
    }
    return data;
}

// 把导出结果整理成一句人话 (含实际时长与是否只是局部片段)
function describeExportResult(result) {
    const parts = [];
    if (result.size) parts.push(formatSize(result.size));
    if (result.duration) parts.push('时长 ' + Number(result.duration).toFixed(1) + 's');
    if (result.truncated) parts.push('局部片段');
    return parts.join(' · ');
}

// 导出失败时把后端给的原因 (含缺失区间) 拼成可读文本
function describeExportError(e) {
    const p = e && e.payload ? e.payload : null;
    let text = (e && e.message) ? e.message : '未知错误';
    if (p && Array.isArray(p.reasons) && p.reasons.length) {
        text = p.reasons.join('<br>');
    }
    if (p && Array.isArray(p.missing_blocks) && p.missing_blocks.length) {
        const shown = p.missing_blocks.slice(0, 20).join(', ');
        const more = p.missing_blocks.length > 20 ? ` ... 共 ${p.missing_blocks.length} 个` : '';
        text += `<br>缺失分片: [${shown}${more}]`;
    }
    return text;
}

// ==================== 视图模式切换 ====================
function switchViewMode(mode) {
    if (state.viewMode === mode) return;
    state.viewMode = mode;
    state.selectedIds.clear();
    state.currentPage = 1;
    state.search = '';
    state.category = 'all';
    state.dlCategory = 'all';
    state.cacheType = '';

    // 更新按钮状态
    document.querySelectorAll('.view-mode-btn').forEach(b => b.classList.remove('active'));
    document.querySelector(`.view-mode-btn[data-mode="${mode}"]`).classList.add('active');

    // 切换 UI 元素显示
    if (mode === 'cache') {
        el.statsBar.classList.remove('hidden');
        el.dlStatsBar.classList.add('hidden');
        el.btnScan.classList.remove('hidden');
        el.btnExports.classList.remove('hidden');
        document.querySelectorAll('.cache-only').forEach(e => e.style.display = '');
        document.querySelectorAll('.dl-only').forEach(e => e.style.display = 'none');
        // 重置统计卡片 active
        document.querySelectorAll('#stats-bar .stat-card').forEach(c => c.classList.remove('active'));
        document.querySelector('#stats-bar .stat-card[data-category="all"]').classList.add('active');
    } else {
        el.statsBar.classList.add('hidden');
        el.dlStatsBar.classList.remove('hidden');
        el.scanProgress.classList.add('hidden');
        document.querySelectorAll('.cache-only').forEach(e => e.style.display = 'none');
        document.querySelectorAll('.dl-only').forEach(e => e.style.display = '');
        // 重置统计卡片 active
        document.querySelectorAll('#dl-stats-bar .stat-card').forEach(c => c.classList.remove('active'));
        document.querySelector('#dl-stats-bar .stat-card[data-dl-category="all"]').classList.add('active');
    }

    // 隐藏按钮
    el.btnExportSelected.classList.add('hidden');
    el.btnDeleteSelected.classList.add('hidden');

    // 搜索框清空
    el.searchInput.value = '';

    // 加载对应文件列表
    if (mode === 'cache') {
        // 如果已有扫描结果, 加载文件列表; 否则显示空状态提示用户先扫描
        if (state.totalFiles > 0 || state.files.length > 0) {
            loadFiles(1);
        } else {
            el.fileGrid.innerHTML = '<div class="loading-spinner"><p style="color:var(--text-muted);">尚未扫描, 请点击「扫描缓存」按钮</p></div>';
            el.toolbar.classList.add('hidden');
        }
    } else {
        loadDownloadFiles(1);
    }
}

// ==================== 扫描 ====================
async function startScan() {
    el.btnScan.disabled = true;
    el.btnScan.innerHTML = '<span class="btn-spinner"></span> 扫描中...';
    el.scanProgress.classList.remove('hidden');
    el.emptyState.classList.add('hidden');
    el.fileGrid.innerHTML = '';

    try {
        await api('/api/scan', { method: 'POST' });
        pollScanStatus();
    } catch (e) {
        toast('扫描启动失败: ' + e.message, 'error');
        el.btnScan.disabled = false;
        el.btnScan.textContent = '扫描缓存';
        el.scanProgress.classList.add('hidden');
    }
}

function pollScanStatus() {
    if (state.scanPollTimer) clearInterval(state.scanPollTimer);

    state.scanPollTimer = setInterval(async () => {
        try {
            const status = await api('/api/scan/status');
            updateScanProgress(status);
            if (status.finished) {
                clearInterval(state.scanPollTimer);
                state.scanPollTimer = null;
                onScanFinished(status);
            }
        } catch (e) {
            console.error('轮询失败:', e);
        }
    }, 500);
}

function updateScanProgress(status) {
    const pct = status.total > 0 ? (status.progress / status.total * 100) : 0;
    el.scanProgressFill.style.width = pct + '%';
    el.scanProgressText.textContent = status.error ? '扫描出错: ' + status.error : '扫描中...';
    el.scanProgressCount.textContent = status.total > 0
        ? `${status.progress} / ${status.total}`
        : '';
}

async function onScanFinished(status) {
    el.btnScan.disabled = false;
    el.btnScan.textContent = '重新扫描';

    if (status.error) {
        toast('扫描失败: ' + status.error, 'error');
        el.scanProgress.classList.add('hidden');
        return;
    }

    toast('扫描完成', 'success');
    el.scanProgress.classList.add('hidden');

    // binlog 锁定/可用提示
    const banner = $('binlog-banner');
    if (banner) {
        if (status.binlog_locked) {
            banner.textContent = '⚠ 检测到 Telegram Desktop 正在运行，binlog 被锁定，无法获取真实文件名称与精确跳转信息。建议关闭 Telegram 后重新扫描。';
            banner.classList.remove('hidden');
        } else if (status.binlog_available) {
            banner.textContent = '';
            banner.classList.add('hidden');
        } else {
            banner.textContent = '';
            banner.classList.add('hidden');
        }
    }

    // 加载统计和文件列表
    await loadStats();
    await loadFiles();
}

// ==================== 统计 ====================
async function loadStats() {
    try {
        const stats = await api('/api/stats');
        $('stat-total').textContent = stats.total_files;
        $('stat-image').textContent = stats.by_category.image || 0;
        $('stat-video').textContent = stats.by_category.video || 0;
        $('stat-slice').textContent = stats.by_category.slice || 0;
        $('stat-sticker').textContent = stats.by_category.sticker || 0;
        $('stat-audio').textContent = stats.by_category.audio || 0;
        $('stat-unknown').textContent = stats.by_category.unknown || 0;
        $('stat-time').textContent = stats.scan_time + 's';
        el.statsBar.classList.remove('hidden');

        // 渲染分组标签
        renderGroupTabs(stats.by_cache_type || {});
    } catch (e) {
        console.error('加载统计失败:', e);
    }
}

function renderGroupTabs(byCacheType) {
    const tabs = $('group-tabs');
    const cacheCount = byCacheType.cache || 0;
    const mediaCount = byCacheType.media_cache || 0;
    
    if (cacheCount === 0 && mediaCount === 0) {
        tabs.classList.add('hidden');
        return;
    }
    
    let html = `<div class="group-tab ${state.cacheType === '' ? 'active' : ''}" data-cache-type="" onclick="selectGroup('')">全部 (${cacheCount + mediaCount})</div>`;
    if (cacheCount > 0) {
        html += `<div class="group-tab ${state.cacheType === 'cache' ? 'active' : ''}" data-cache-type="cache" onclick="selectGroup('cache')">缩略图缓存 (${cacheCount})</div>`;
    }
    if (mediaCount > 0) {
        html += `<div class="group-tab ${state.cacheType === 'media_cache' ? 'active' : ''}" data-cache-type="media_cache" onclick="selectGroup('media_cache')">媒体缓存 (${mediaCount})</div>`;
    }
    
    tabs.innerHTML = html;
    tabs.classList.remove('hidden');
}

function selectGroup(cacheType) {
    state.cacheType = cacheType;
    // 切换分组时重置分类筛选，避免 cache_type + category 交叉筛选导致空列表
    state.category = 'all';
    document.querySelectorAll('.stat-card').forEach(c => c.classList.remove('active'));
    document.querySelector('[data-category="all"]').classList.add('active');
    // 更新标签状态
    document.querySelectorAll('.group-tab').forEach(t => t.classList.remove('active'));
    const activeTab = document.querySelector(`.group-tab[data-cache-type="${cacheType}"]`);
    if (activeTab) activeTab.classList.add('active');
    loadFiles(1);
}

// ==================== 文件列表 ====================
async function loadFiles(page = 1) {
    state.currentPage = page;

    const params = new URLSearchParams({
        page: page,
        per_page: state.perPage,
        sort: state.sort,
    });
    if (state.category !== 'all') params.set('category', state.category);
    if (state.cacheType) params.set('cache_type', state.cacheType);
    if (state.search) params.set('search', state.search);

    try {
        el.fileGrid.innerHTML = '<div class="loading-spinner"><div class="spinner"></div></div>';

        const data = await api('/api/files?' + params.toString());
        state.files = data.files;
        state.totalFiles = data.total;

        renderFiles(data);
        el.toolbar.classList.remove('hidden');
    } catch (e) {
        toast('加载文件列表失败: ' + e.message, 'error');
    }
}

function renderFiles(data) {
    el.fileCount.textContent = `共 ${data.total} 个文件`;
    el.btnDeleteSelected.classList.toggle('hidden', state.selectedIds.size === 0);
    el.btnExportSelected.classList.toggle('hidden', state.selectedIds.size === 0);
    updateSelectedBadge();

    if (data.files.length === 0) {
        el.fileGrid.innerHTML = '';
        el.emptyState.classList.remove('hidden');
        el.emptyState.querySelector('p').textContent = '没有符合条件的文件';
        return;
    }

    el.emptyState.classList.add('hidden');

    let html = '';
    for (const f of data.files) {
        const isSelected = state.selectedIds.has(f.file_id);
        const badgeClass = `badge-${f.category}`;
        const isSlice = f.file_type === 'video_slice';
        const isLargeVideo = f.is_large_video;
        const showPlayIcon = f.category === 'video' && !isSlice;

        // 分片: 有父 header 的可以显示缩略图 (从父 header 生成)
        const showThumb = (f.category === 'image' || f.category === 'video' || f.category === 'sticker' ||
                          (isSlice && f.parent_header_id));
        const thumbUrl = showThumb ? `/api/thumbnail/${f.file_id}` : null;

        // 分片和大视频的特殊标识
        let extraBadge = '';
        if (isLargeVideo) {
            // 合并展示后一个视频一条记录: 完整的标"完整"排最前,
            // 其余标"合成率 X.XX%"(覆盖率口径) 按合成率降序排在后面
            const tags = [];
            if (f.is_rebuilt) {
                // 已重建但要如实区分"完整"与"局部片段" —— 否则用户会以为
                // 拿到的是完整视频, 播到一半停住时完全摸不着头脑
                tags.push(f.is_complete_large_video
                    ? '<span class="file-card-tag tag-rebuilt">已重建</span>'
                    : '<span class="file-card-tag tag-rebuilt">已重建 · 局部</span>');
            }
            if (f.is_complete_large_video) {
                tags.push('<span class="file-card-tag tag-complete">完整</span>');
            } else if (f.synthesis_rate !== null && f.synthesis_rate !== undefined) {
                tags.push(`<span class="file-card-tag tag-rate">合成率 ${Number(f.synthesis_rate).toFixed(2)}%</span>`);
            } else {
                tags.push('<span class="file-card-tag tag-large">大视频</span>');
            }
            extraBadge = tags.join('');
        } else if (isSlice) {
            if (f.parent_header_id) {
                if (f.parent_is_complete) {
                    extraBadge = '<span class="file-card-tag tag-complete">可合成</span>';
                } else {
                    extraBadge = '<span class="file-card-tag tag-slice">分片</span>';
                }
            } else {
                extraBadge = '<span class="file-card-tag tag-orphan">孤立</span>';
            }
        }

        const hasDisplayName = !!f.display_name;
        const nameClass = hasDisplayName ? 'file-card-name has-display-name' : 'file-card-name';
        html += `
        <div class="file-card ${isSelected ? 'selected' : ''}" data-id="${f.file_id}" data-size="${f.decrypted_size || 0}" onclick="onCardClick(event, '${f.file_id}')">
            <input type="checkbox" class="file-card-checkbox" ${isSelected ? 'checked' : ''}
                onchange="onCardSelect(event, '${f.file_id}')">
            <div class="file-card-thumb">
                ${thumbUrl
                    ? `<img class="lazy-img" data-src="${thumbUrl}" data-category="${f.category}" onerror="this.style.display='none';this.parentElement.innerHTML=getPlaceholder('${f.category}')">`
                    : getPlaceholder(f.category)
                }
                ${showPlayIcon ? '<div class="play-overlay"><svg viewBox="0 0 24 24" width="32" height="32"><path fill="white" d="M8 5v14l11-7z"/></svg></div>' : ''}
            </div>
            <span class="file-card-badge ${badgeClass}">${f.category}</span>
            ${extraBadge}
            <div class="file-card-info">
                <div class="${nameClass}" title="${escapeHtml(f.display_name || f.file_name)}">${escapeHtml(f.display_name || f.file_name)}</div>
                <div class="file-card-meta">
                    <span class="file-card-type">${f.file_type_label}</span>
                    <span class="file-card-size">${formatSize(f.decrypted_size)}</span>
                </div>
            </div>
        </div>`;
    }

    // 分页 (页码按钮)
    html += renderPagination(data.page, data.pages, 'loadFiles');

    el.fileGrid.innerHTML = html;

    // 立即加载缩略图 (不等滚动)
    lazyLoadImages();
}

// ==================== 页码分页组件 ====================
function renderPagination(currentPage, totalPages, loadFnName) {
    if (totalPages <= 1) return '';

    let html = '<div class="pagination">';

    // 上一页
    html += `<button class="page-btn ${currentPage === 1 ? 'disabled' : ''}" ${currentPage > 1 ? `onclick="${loadFnName}(${currentPage - 1})"` : ''}>&lt;</button>`;

    // 计算页码范围 (显示最多 7 个页码 + 省略号)
    let startPage = Math.max(1, currentPage - 3);
    let endPage = Math.min(totalPages, currentPage + 3);

    if (startPage > 1) {
        html += `<button class="page-btn" onclick="${loadFnName}(1)">1</button>`;
        if (startPage > 2) html += '<span class="page-ellipsis">...</span>';
    }

    for (let i = startPage; i <= endPage; i++) {
        html += `<button class="page-btn ${i === currentPage ? 'active' : ''}" onclick="${loadFnName}(${i})">${i}</button>`;
    }

    if (endPage < totalPages) {
        if (endPage < totalPages - 1) html += '<span class="page-ellipsis">...</span>';
        html += `<button class="page-btn" onclick="${loadFnName}(${totalPages})">${totalPages}</button>`;
    }

    // 下一页
    html += `<button class="page-btn ${currentPage === totalPages ? 'disabled' : ''}" ${currentPage < totalPages ? `onclick="${loadFnName}(${currentPage + 1})"` : ''}>&gt;</button>`;

    html += '</div>';
    return html;
}

function getPlaceholder(category) {
    const icons = {
        image: `<svg viewBox="0 0 24 24" class="placeholder"><path fill="currentColor" d="M21 19V5c0-1.1-.9-2-2-2H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2zM8.5 13.5l2.5 3.01L14.5 12l4.5 6H5l3.5-4.5z"/></svg>`,
        video: `<svg viewBox="0 0 24 24" class="placeholder"><path fill="currentColor" d="M17 10.5V7c0-.55-.45-1-1-1H4c-.55 0-1 .45-1 1v10c0 .55.45 1 1 1h12c.55 0 1-.45 1-1v-3.5l4 4v-11l-4 4z"/></svg>`,
        sticker: `<svg viewBox="0 0 24 24" class="placeholder"><path fill="currentColor" d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-2 15l-5-5 1.41-1.41L10 14.17l7.59-7.59L19 8l-9 9z"/></svg>`,
        audio: `<svg viewBox="0 0 24 24" class="placeholder"><path fill="currentColor" d="M12 3v10.55c-.59-.34-1.27-.55-2-.55-2.21 0-4 1.79-4 4s1.79 4 4 4 4-1.79 4-4V7h4V3h-6z"/></svg>`,
        unknown: `<svg viewBox="0 0 24 24" class="placeholder"><path fill="currentColor" d="M14 2H6c-1.1 0-1.99.9-1.99 2L4 20c0 1.1.89 2 1.99 2H18c1.1 0 2-.9 2-2V8l-6-6zm2 16H8v-2h8v2zm0-4H8v-2h8v2zm-3-5V3.5L18.5 9H13z"/></svg>`,
    };
    return icons[category] || icons.unknown;
}

// ==================== 下载文件列表 ====================
async function loadDownloadFiles(page = 1) {
    state.currentPage = page;

    const params = new URLSearchParams({
        page: page,
        per_page: state.perPage,
        sort: state.sort,
    });
    if (state.dlCategory !== 'all') params.set('category', state.dlCategory);
    if (state.search) params.set('search', state.search);

    try {
        el.fileGrid.innerHTML = '<div class="loading-spinner"><div class="spinner"></div></div>';

        const data = await api('/api/download_files?' + params.toString());
        state.files = data.files;
        state.totalFiles = data.total;

        // 更新下载统计栏
        const byCat = data.by_category || {};
        $('dl-stat-total').textContent = data.total;
        $('dl-stat-image').textContent = byCat.image || 0;
        $('dl-stat-video').textContent = byCat.video || 0;
        $('dl-stat-audio').textContent = byCat.audio || 0;
        $('dl-stat-unknown').textContent = byCat.unknown || 0;
        el.dlStatsBar.classList.remove('hidden');

        renderDownloadFiles(data);
        el.toolbar.classList.remove('hidden');
    } catch (e) {
        toast('加载下载文件失败: ' + e.message, 'error');
    }
}

function renderDownloadFiles(data) {
    el.fileCount.textContent = `共 ${data.total} 个文件`;
    el.btnDeleteSelected.classList.toggle('hidden', state.selectedIds.size === 0);
    updateSelectedBadge();

    if (data.files.length === 0) {
        el.fileGrid.innerHTML = '';
        el.emptyState.classList.remove('hidden');
        el.emptyState.querySelector('p').textContent = '下载目录中没有文件';
        return;
    }

    el.emptyState.classList.add('hidden');

    let html = '';
    for (const f of data.files) {
        const isSelected = state.selectedIds.has(f.file_id);
        const badgeClass = `badge-${f.category}`;
        const showThumb = (f.category === 'image' || f.category === 'video');
        const thumbUrl = showThumb ? `/api/download_thumbnail/${encodeURIComponent(f.file_id)}` : null;
        const showPlayIcon = f.category === 'video';

        html += `
        <div class="file-card ${isSelected ? 'selected' : ''}" data-id="${escapeHtml(f.file_id)}" data-size="${f.file_size || 0}" onclick="onCardClick(event, '${escapeHtml(f.file_id)}')">
            <input type="checkbox" class="file-card-checkbox" ${isSelected ? 'checked' : ''}
                onchange="onCardSelect(event, '${escapeHtml(f.file_id)}')">
            <div class="file-card-thumb">
                ${thumbUrl
                    ? `<img class="lazy-img" data-src="${thumbUrl}" data-category="${f.category}">`
                    : getPlaceholder(f.category)
                }
                ${showPlayIcon ? '<div class="play-overlay"><svg viewBox="0 0 24 24" width="32" height="32"><path fill="white" d="M8 5v14l11-7z"/></svg></div>' : ''}
            </div>
            <span class="file-card-badge ${badgeClass}">${f.category}</span>
            <span class="file-card-tag tag-download">下载</span>
            <div class="file-card-info">
                <div class="file-card-name has-display-name" title="${escapeHtml(f.file_name)}">${escapeHtml(f.file_name)}</div>
                <div class="file-card-meta">
                    <span class="file-card-type">${f.category}</span>
                    <span class="file-card-size">${formatSize(f.file_size)}</span>
                </div>
            </div>
        </div>`;
    }

    // 分页
    html += renderPagination(data.page, data.pages, 'loadDownloadFiles');

    el.fileGrid.innerHTML = html;
    lazyLoadImages();
}

// ==================== 下载文件预览 ====================
async function openDownloadPreview(filename) {
    state.currentPreviewId = filename;
    const modal = $('preview-modal');
    const body = $('preview-body');
    const info = $('preview-info');

    modal.classList.remove('hidden');
    body.scrollTop = 0;
    body.innerHTML = '<div class="loading-spinner"><div class="spinner"></div></div>';
    info.textContent = '';

    // 从 state.files 中找到文件信息
    const f = state.files.find(x => x.file_id === filename);
    if (!f) {
        body.innerHTML = `<div class="preview-placeholder"><p>文件信息未找到</p></div>`;
        return;
    }

    $('preview-title').textContent = f.file_name;

    // 下载文件模式: 隐藏不适用按钮, 修改导出按钮文案
    $('btn-preview-telegram').style.display = 'none';
    $('btn-preview-export').textContent = '在浏览器打开';

    const previewUrl = `/api/download_preview/${encodeURIComponent(filename)}`;

    if (f.category === 'image') {
        body.innerHTML = `<img class="preview-image" src="${previewUrl}" alt="预览" onclick="toggleImageZoom(this)">`;
        info.textContent = `${f.category} · ${formatSize(f.file_size)}`;
    } else if (f.category === 'video') {
        body.innerHTML = `<video class="preview-video" src="${previewUrl}" controls autoplay></video>`;
        info.textContent = `${f.category} · ${formatSize(f.file_size)}`;
    } else if (f.category === 'audio') {
        body.innerHTML = `<audio controls autoplay style="width:100%;"><source src="${previewUrl}"></audio>`;
        info.textContent = `${f.category} · ${formatSize(f.file_size)}`;
    } else {
        body.innerHTML = `<div class="preview-placeholder"><svg viewBox="0 0 24 24"><path fill="currentColor" d="M14 2H6c-1.1 0-1.99.9-1.99 2L4 20c0 1.1.89 2 1.99 2H18c1.1 0 2-.9 2-2V8l-6-6zm2 16H8v-2h8v2zm0-4H8v-2h8v2zm-3-5V3.5L18.5 9H13z"/></svg><p>无法预览此文件类型<br>可直接下载查看</p><div style="margin-top:12px;"><a class="btn btn-primary" href="${previewUrl}" download="${escapeHtml(filename)}">下载文件</a></div></div>`;
        info.textContent = `${f.category} · ${formatSize(f.file_size)}`;
    }
}

// ==================== 删除下载文件 ====================
async function deleteDownloadFile(filename) {
    if (!confirm(`确认删除下载文件 "${filename}" ？\n此操作将从磁盘上移除文件，不可恢复。`)) return;
    try {
        await api('/api/download_files', {
            method: 'DELETE',
            body: JSON.stringify({ file_ids: [filename] }),
        });
        toast('文件已删除', 'success');
        closePreview();
        await loadDownloadFiles(state.currentPage);
    } catch (e) {
        toast('删除失败: ' + e.message, 'error');
    }
}

async function deleteSelectedDownloadFiles() {
    const ids = Array.from(state.selectedIds);
    if (!ids.length) return;

    if (!confirm(`确认删除选中的 ${ids.length} 个文件？\n此操作不可恢复。`)) return;

    el.btnDeleteSelected.disabled = true;
    el.btnDeleteSelected.textContent = '删除中...';

    try {
        let result;
        if (state.viewMode === 'downloads') {
            // 下载文件批量删除
            result = await api('/api/download_files', {
                method: 'DELETE',
                body: JSON.stringify({ file_ids: ids }),
            });
        } else {
            // 缓存文件批量删除
            result = await api('/api/files/batch_delete', {
                method: 'POST',
                body: JSON.stringify({ file_ids: ids }),
            });
        }
        toast(`删除完成: 成功 ${result.success}/${result.total}`, 'success');
        state.selectedIds.clear();
        el.btnDeleteSelected.classList.add('hidden');
        el.btnExportSelected.classList.add('hidden');
        updateSelectedBadge();
        el.btnDeleteSelected.disabled = false;
        el.btnDeleteSelected.textContent = '删除选中';
        if (state.viewMode === 'downloads') {
            await loadDownloadFiles(state.currentPage);
        } else {
            await loadFiles(state.currentPage);
        }
    } catch (e) {
        toast('删除失败: ' + e.message, 'error');
        el.btnDeleteSelected.disabled = false;
        el.btnDeleteSelected.textContent = '删除选中';
    }
}

// ==================== 懒加载 ====================
function lazyLoadImages() {
    const imgs = document.querySelectorAll('.lazy-img[data-src]');
    if (!imgs.length) return;

    if ('IntersectionObserver' in window) {
        const observer = new IntersectionObserver((entries) => {
            entries.forEach(entry => {
                if (entry.isIntersecting) {
                    const img = entry.target;
                    img.src = img.dataset.src;
                    img.onload = () => img.classList.add('loaded');
                    img.onerror = () => {
                        // 缩略图加载失败: 替换为占位符
                        const category = img.dataset.category || 'unknown';
                        const placeholder = getPlaceholder(category);
                        img.parentElement.innerHTML = placeholder;
                    };
                    img.removeAttribute('data-src');
                    observer.unobserve(img);
                }
            });
        }, { rootMargin: '200px' });
        imgs.forEach(img => observer.observe(img));
    } else {
        imgs.forEach(img => {
            img.src = img.dataset.src;
            img.onload = () => img.classList.add('loaded');
            img.removeAttribute('data-src');
        });
    }
}

// ==================== 文件卡片交互 ====================
function onCardClick(event, fileId) {
    // 点击 checkbox 不触发预览
    if (event.target.classList.contains('file-card-checkbox')) return;
    if (state.viewMode === 'downloads') {
        openDownloadPreview(fileId);
    } else {
        openPreview(fileId);
    }
}

function onCardSelect(event, fileId) {
    event.stopPropagation();
    if (event.target.checked) {
        state.selectedIds.add(fileId);
    } else {
        state.selectedIds.delete(fileId);
    }
    const card = event.target.closest('.file-card');
    card.classList.toggle('selected', event.target.checked);
    el.btnDeleteSelected.classList.toggle('hidden', state.selectedIds.size === 0);
    el.btnExportSelected.classList.toggle('hidden', state.selectedIds.size === 0);
    updateSelectedBadge();
}

// ==================== 视频错误处理 ====================
//
// 旧实现只监听 `error` 事件。而"缓存里缺一段数据"这类故障通常不会触发致命的
// error 事件 —— 视频只是播到某个位置就**悄悄停住**, 界面上没有任何提示,
// 这正是"恢复的视频无法持久播放"最难定位的地方。
//
// 现在补齐: error / stalled / waiting / timeupdate / ended / durationchange。
// 判定"播放中断"的做法: 在 stalled/waiting 之后起一个定时器, 若到点后
// currentTime 仍未推进, 就认定中断并显示已播到的位置。
const VIDEO_ICON = '<svg viewBox="0 0 24 24" style="width:24px;height:24px;vertical-align:middle;"><path fill="currentColor" d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-2h2v2zm0-4h-2V7h2v6z"/></svg>';

function handleVideoError(videoEl, body, message, opts = {}) {
    if (!videoEl) return;
    const v = videoEl;
    const b = body;
    const STALL_MS = opts.stallMs || 6000;

    let stallTimer = null;
    let lastTime = v.currentTime || 0;
    let failed = false;

    function noticeNode() {
        let n = b.querySelector('.video-error-msg');
        if (!n) {
            n = document.createElement('div');
            n.className = 'video-error-msg preview-placeholder';
            n.style.padding = '12px';
            const vid = b.querySelector('video');
            if (vid && vid.parentNode === b) b.insertBefore(n, vid.nextSibling);
            else b.appendChild(n);
        }
        return n;
    }

    function show(html, kind) {
        // 致命错误优先级最高, 不被后续的 stall 提示覆盖
        const n = noticeNode();
        if (n.dataset.kind === 'error' && kind !== 'error') return;
        n.dataset.kind = kind || 'info';
        n.classList.toggle('video-warn', kind === 'stall');
        n.innerHTML = VIDEO_ICON + '<p style="margin-top:4px;">' + html + '</p>';
    }

    function clearStall() {
        const n = b.querySelector('.video-error-msg');
        if (n && n.dataset.kind === 'stall') n.remove();
    }

    function armStall() {
        clearTimeout(stallTimer);
        // 以"起算时刻的播放位置"为基准, 而不是某个可能已经过期的 lastTime ——
        // 否则 seek 之后立即 waiting 之类的情况会被误判为"还在推进"
        const armedAt = v.currentTime || 0;
        stallTimer = setTimeout(function () {
            if (failed || v.paused || v.ended || v.seeking) return;
            if ((v.currentTime || 0) > armedAt + 0.05) return;   // 相对起算点已推进
            const dur = (v.duration && isFinite(v.duration)) ? v.duration.toFixed(1) + 's' : '?';
            show('播放中断: 已播到 ' + (v.currentTime || 0).toFixed(1) + 's / ' + dur + '<br>' +
                 '数据不再到达 —— 可能是缓存里这一段缺失, 或文件被截断。<br>' +
                 '可尝试"重新重建", 或用播放器打开导出的文件确认。', 'stall');
        }, STALL_MS);
    }

    v.addEventListener('error', function () {
        failed = true;
        clearTimeout(stallTimer);
        const code = v.error ? v.error.code : 0;
        const detail = code === 4 ? '格式不受支持或数据损坏'
            : code === 3 ? '数据已损坏'
                : code === 2 ? '网络/读取中断'
                    : code === 1 ? '加载被中止' : '';
        show(message + (detail ? '<br><span style="opacity:.75;">（' + detail + '）</span>' : ''), 'error');
    });

    v.addEventListener('loadedmetadata', function () {
        lastTime = v.currentTime || 0;
    });

    v.addEventListener('durationchange', function () {
        lastTime = v.currentTime || 0;
        if (v.duration && isFinite(v.duration) && opts.onDuration) {
            opts.onDuration(v.duration);
        }
    });

    v.addEventListener('stalled', armStall);
    v.addEventListener('waiting', armStall);

    v.addEventListener('timeupdate', function () {
        clearTimeout(stallTimer);
        if (v.currentTime > lastTime + 0.05) {
            lastTime = v.currentTime;
            clearStall();
            armStall();
        }
    });

    v.addEventListener('playing', function () { clearStall(); armStall(); });

    v.addEventListener('ended', function () {
        clearTimeout(stallTimer);
        clearStall();
    });
}

// ==================== 预览 ====================
async function openPreview(fileId) {
    state.currentPreviewId = fileId;
    const modal = $('preview-modal');
    const body = $('preview-body');
    const info = $('preview-info');

    modal.classList.remove('hidden');
    body.scrollTop = 0;
    body.innerHTML = '<div class="loading-spinner"><div class="spinner"></div></div>';
    info.textContent = '';

    // 缓存文件模式: 恢复按钮显示和文案
    $('btn-preview-telegram').style.display = '';
    $('btn-preview-export').textContent = '导出文件';

    try {
        const f = await api(`/api/file/${fileId}`);

        $('preview-title').textContent = f.display_name || f.file_type_label;
        renderTelegramBindBox(f);

        if (f.category === 'image') {
            body.innerHTML = `<img class="preview-image" src="/api/preview/${fileId}" alt="预览" onclick="toggleImageZoom(this)">`;
            info.textContent = `${f.file_type_label} · ${formatSize(f.decrypted_size)}`;
            if (f.file_name) info.textContent += ` · ${f.file_name}`;
        } else if (f.category === 'video') {
            if (f.is_incomplete && !f.is_large_video) {
                body.innerHTML = `<div class="preview-placeholder"><svg viewBox="0 0 24 24"><path fill="currentColor" d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-2h2v2zm0-4h-2V7h2v6z"/></svg><p>此视频缓存不完整 (缺少元数据 moov)<br>可能是 Telegram 未完成下载<br>可尝试导出后用 VLC 播放器修复</p></div>`;
            } else if (f.is_large_video) {
                // 大视频: 根据状态显示不同界面
                const largeInfo = f.large_video_info || {};
                const estSize = formatSize(largeInfo.estimated_size || 0);
                const slicesNeeded = largeInfo.slices_needed || 0;
                const availableSlices = largeInfo.available_slices || 0;
                const partsCount = largeInfo.parts_count || 0;
                const missing = f.missing_slices || 0;
                const keyHigh = largeInfo.key_high || '';
                const sliceDetails = largeInfo.slice_details || [];
                const missingIndices = largeInfo.missing_indices || [];
                const isComplete = f.is_complete_large_video;
                const isRebuilt = f.is_rebuilt;

                // 如果已重建: 直接用预览 API 播放 (会自动检测导出文件, moov 已前置)
                if (isRebuilt) {
                    const bust = Date.now();
                    body.innerHTML = `<video class="preview-video" src="/api/preview/${encodeURIComponent(fileId)}?t=${bust}" controls autoplay></video>
                        <div style="margin-top:8px; display:flex; gap:8px; justify-content:center;">
                            <button class="btn btn-small btn-secondary" onclick="reRebuildVideo('${fileId}', this)">重新重建</button>
                        </div>`;
                    handleVideoError(body.querySelector('video'), body,
                        '视频无法播放<br>可能是缓存不完整或重建文件损坏<br>请尝试重新重建或重新导出');
                    // 可播时长统一由下面的通用块追加, 这里不再重复
                    info.textContent = `大视频 · ${formatSize(f.decrypted_size)} · 已重建 (moov 前置)`;
                    if (!isComplete) info.textContent += ' · 局部片段';
                    if (f.display_name) info.textContent += ` · ${f.display_name}`;
                    else if (f.file_name) info.textContent += ` · ${f.file_name}`;
                } else if (isComplete) {
                    // 分片完整但未重建: 显示重建按钮
                    // 构建分片可视化图
                    let sliceMapHtml = '';
                    if (sliceDetails.length > 0) {
                        const cells = [];
                        cells.push(`<div class="slice-cell slice-header" title="Header (分片 0)">0</div>`);
                        for (const sd of sliceDetails) {
                            const cls = sd.on_disk ? 'slice-cell slice-ok' : 'slice-cell slice-missing';
                            const tip = sd.on_disk 
                                ? `分片 ${sd.slice_index} · ${formatSize(sd.size)} · ${sd.file_name}`
                                : `分片 ${sd.slice_index} · 缺失`;
                            cells.push(`<div class="${cls}" title="${escapeHtml(tip)}">${sd.slice_index}</div>`);
                        }
                        sliceMapHtml = `
                            <div class="slice-map">
                                <div class="slice-map-legend">
                                    <span class="slice-legend-item"><span class="slice-dot slice-header-dot"></span>Header</span>
                                    <span class="slice-legend-item"><span class="slice-dot slice-ok-dot"></span>已缓存 (${availableSlices})</span>
                                    <span class="slice-legend-item"><span class="slice-dot slice-missing-dot"></span>缺失 (${missing})</span>
                                </div>
                                <div class="slice-map-grid">${cells.join('')}</div>
                            </div>
                        `;
                    }

                    const keyHighHtml = keyHigh ? `<div class="slice-key-high">Cache Key: <code>${keyHigh}</code></div>` : '';

                    body.innerHTML = `<div class="preview-placeholder">
                        <svg viewBox="0 0 24 24"><path fill="currentColor" d="M17 10.5V7c0-.55-.45-1-1-1H4c-.55 0-1 .45-1 1v10c0 .55.45 1 1 1h12c.55 0 1-.45 1-1v-3.5l4 4v-11l-4 4z"/></svg>
                        <p>大视频缓存 (分片完整, 可重建)</p>
                        <div class="slice-info-grid">
                            <div class="slice-info-item"><span class="slice-info-label">估算大小</span><span class="slice-info-value">${estSize}</span></div>
                            <div class="slice-info-item"><span class="slice-info-label">已知分块</span><span class="slice-info-value">${partsCount}</span></div>
                            <div class="slice-info-item"><span class="slice-info-label">需要分片</span><span class="slice-info-value">${slicesNeeded}</span></div>
                            <div class="slice-info-item"><span class="slice-info-label">已缓存</span><span class="slice-info-value">${availableSlices}</span></div>
                        </div>
                        ${keyHighHtml}
                        ${sliceMapHtml}
                        <div class="slice-ok-msg">所有分片齐全, 点击下方按钮重建并播放</div>
                        <div style="margin-top:12px;">
                            <button class="btn btn-primary" onclick="rebuildAndPreview('${fileId}')">重建并播放</button>
                        </div>
                    </div>`;
                    info.textContent = `大视频 · ${formatSize(f.decrypted_size)}`;
                } else {
                    // 分片不完整: 显示分片覆盖图和警告
                    let sliceMapHtml = '';
                    if (sliceDetails.length > 0) {
                        const cells = [];
                        cells.push(`<div class="slice-cell slice-header" title="Header (分片 0)">0</div>`);
                        for (const sd of sliceDetails) {
                            const cls = sd.on_disk ? 'slice-cell slice-ok' : 'slice-cell slice-missing';
                            const tip = sd.on_disk 
                                ? `分片 ${sd.slice_index} · ${formatSize(sd.size)} · ${sd.file_name}`
                                : `分片 ${sd.slice_index} · 缺失`;
                            cells.push(`<div class="${cls}" title="${escapeHtml(tip)}">${sd.slice_index}</div>`);
                        }
                        sliceMapHtml = `
                            <div class="slice-map">
                                <div class="slice-map-legend">
                                    <span class="slice-legend-item"><span class="slice-dot slice-header-dot"></span>Header</span>
                                    <span class="slice-legend-item"><span class="slice-dot slice-ok-dot"></span>已缓存 (${availableSlices})</span>
                                    <span class="slice-legend-item"><span class="slice-dot slice-missing-dot"></span>缺失 (${missing})</span>
                                </div>
                                <div class="slice-map-grid">${cells.join('')}</div>
                            </div>
                        `;
                    }

                    let missingListHtml = '';
                    if (missingIndices.length > 0 && missingIndices.length <= 20) {
                        missingListHtml = `<div class="slice-missing-list">缺失分片: [${missingIndices.join(', ')}]</div>`;
                    } else if (missingIndices.length > 20) {
                        missingListHtml = `<div class="slice-missing-list">缺失分片: [${missingIndices.slice(0, 20).join(', ')} ... 共 ${missingIndices.length} 个]</div>`;
                    }

                    const keyHighHtml = keyHigh ? `<div class="slice-key-high">Cache Key: <code>${keyHigh}</code></div>` : '';
                    const playableDur = f.playable_duration || largeInfo.playable_duration || 0;
                    const durItem = playableDur > 0
                        ? `<div class="slice-info-item"><span class="slice-info-label">可播时长</span><span class="slice-info-value">${playableDur.toFixed(1)}s</span></div>`
                        : '';
                    const reasonTxt = (f.incomplete_reasons || []).length
                        ? `<div class="slice-hint" style="margin-top:4px; font-size:12px; color:var(--text-muted);">${escapeHtml((f.incomplete_reasons || []).join(' · '))}</div>`
                        : '';

                    body.innerHTML = `<div class="preview-placeholder">
                        <svg viewBox="0 0 24 24"><path fill="currentColor" d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-2h2v2zm0-4h-2V7h2v6z"/></svg>
                        <p>大视频缓存 (分片不完整)</p>
                        <div class="slice-info-grid">
                            <div class="slice-info-item"><span class="slice-info-label">估算大小</span><span class="slice-info-value">${estSize}</span></div>
                            <div class="slice-info-item"><span class="slice-info-label">已知分块</span><span class="slice-info-value">${partsCount}</span></div>
                            <div class="slice-info-item"><span class="slice-info-label">需要分片</span><span class="slice-info-value">${slicesNeeded}</span></div>
                            <div class="slice-info-item"><span class="slice-info-label">已缓存</span><span class="slice-info-value">${availableSlices}</span></div>
                            ${durItem}
                        </div>
                        ${keyHighHtml}
                        ${sliceMapHtml}
                        ${missingListHtml}
                        <div class="slice-warning">缓存分片不足 (缺 ${missing} 分片)</div>
                        <div class="slice-hint" style="margin-top:8px; font-size:13px; color:var(--text-muted);">
                            导出时会**只输出从文件开头连续覆盖到的那一段**, 时长约 ${playableDur > 0 ? playableDur.toFixed(1) + 's' : '未知'};
                            缺失的区段不会被写成零数据, 因此播放不会在中途卡死或黑屏。
                        </div>
                        ${reasonTxt}
                        <div class="slice-hint" style="margin-top:8px; font-size:13px; color:var(--text-muted);">
                            想在 Telegram Desktop 里播放该视频以补全缓存分片, 重新扫描后即可导出完整时长
                        </div>
                        <div style="margin-top:12px; display:flex; gap:8px; justify-content:center;">
                            <button class="btn btn-primary" onclick="exportIncompleteVideo('${fileId}')">导出可播放片段</button>
                        </div>
                    </div>`;
                    info.textContent = `大视频 (缺 ${missing} 分片) · ${formatSize(f.decrypted_size)}`;
                }
            } else if (f.file_type === 'video_slice') {
                // 8MB 视频分片
                const si = f.slice_info || {};
                const sliceIdx = si.slice_index !== undefined ? si.slice_index : -1;
                const parentHeader = si.parent_header_id || '';
                const keyHigh = si.key_high || '';

                let parentHtml = '';
                if (parentHeader) {
                    parentHtml = `<div class="slice-parent-info">
                        <span>归属大视频: <code>${escapeHtml(parentHeader)}</code></span>
                        <button class="btn btn-small" style="margin-left:8px;" onclick="closePreview(); openPreview('${parentHeader}')">查看大视频</button>
                    </div>`;
                } else if (keyHigh) {
                    parentHtml = `<div class="slice-parent-info"><span>Cache Key: <code>${keyHigh}</code></span><span style="color:var(--text-muted); margin-left:8px;">(未找到对应 header)</span></div>`;
                }

                const idxText = sliceIdx >= 0 ? `分片 #${sliceIdx}` : '分片';
                body.innerHTML = `<div class="preview-placeholder"><svg viewBox="0 0 24 24"><path fill="currentColor" d="M4 6H2v14c0 1.1.9 2 2 2h14v-2H4V6zm16-4H8c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zm-6 12l-4-4 4-4v3h4v2h-4v3z"/></svg><p>8MB 视频分片<br>${idxText} · ${formatSize(si.size || f.decrypted_size)}</p>${parentHtml}</div>`;
            } else {
                body.innerHTML = `<video class="preview-video" src="/api/preview/${fileId}" controls autoplay></video>`;
                handleVideoError(body.querySelector('video'), body, '视频无法播放<br>可能是缓存不完整或格式不支持<br>可尝试导出后用 VLC 播放器打开');
            }
            // 只有在上面分支还没写过信息栏时才用通用文案覆盖 ——
            // 旧代码是无条件赋值, 于是大视频分支精心拼好的
            // "已重建 (moov 前置) · 可播 Ns" 会被这一行悄悄抹掉。
            if (!info.textContent) {
                info.textContent = `${f.file_type_label} · ${formatSize(f.decrypted_size)}`;
            }
            if (f.playable_duration) {
                info.textContent += ` · 可播 ${Number(f.playable_duration).toFixed(1)}s`;
            }
            if (f.is_serialized && f.video_info) {
                info.textContent += ` · 估算 ${formatSize(f.video_info.estimated_size)}`;
            }
        } else if (f.category === 'audio') {
            body.innerHTML = `<audio controls autoplay style="width:100%;"><source src="/api/preview/${fileId}"></audio>`;
            info.textContent = `${f.file_type_label} · ${formatSize(f.decrypted_size)}`;
        } else if (f.category === 'sticker') {
            // TGS 动画贴片: 用 lottie-web 渲染动画
            if (f.lottie_json) {
                try {
                    const animationData = JSON.parse(f.lottie_json);
                    const containerId = 'lottie-container';
                    body.innerHTML = `<div id="${containerId}" style="width:256px;height:256px;margin:0 auto;"></div>`;
                    const anim = lottie.loadAnimation({
                        container: document.getElementById(containerId),
                        renderer: 'svg',
                        loop: true,
                        autoplay: true,
                        animationData: animationData,
                    });
                    // 存储动画实例以便关闭时销毁
                    state.currentLottie = anim;
                } catch (e) {
                    body.innerHTML = `<div class="preview-placeholder"><p>Lottie 渲染失败: ${escapeHtml(e.message)}</p></div>`;
                }
                const nameText = f.sticker_name ? ` · ${f.sticker_name}` : '';
                info.textContent = `${f.file_type_label} · ${formatSize(f.decrypted_size)}${nameText}`;
            } else {
                body.innerHTML = `<div class="preview-placeholder"><svg viewBox="0 0 24 24"><path fill="currentColor" d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-2 15l-5-5 1.41-1.41L10 14.17l7.59-7.59L19 8l-9 9z"/></svg><p>动画贴片解压失败<br>可尝试导出查看</p></div>`;
                info.textContent = `${f.file_type_label} · ${formatSize(f.decrypted_size)}`;
            }
        } else {
            // 未知文件/碎片: 显示十六进制预览和碎片分析
            const hexPreview = f.hex_preview || '无法读取';
            const fragmentHint = f.fragment_hint || '';
            let hintHtml = '';
            if (fragmentHint) {
                hintHtml = `<p style="color:var(--text-muted);font-size:13px;margin-top:8px;">${escapeHtml(fragmentHint)}</p>`;
            }
            body.innerHTML = `<div class="preview-placeholder"><svg viewBox="0 0 24 24"><path fill="currentColor" d="M14 2H6c-1.1 0-1.99.9-1.99 2L4 20c0 1.1.89 2 1.99 2H18c1.1 0 2-.9 2-2V8l-6-6zm2 16H8v-2h8v2zm0-4H8v-2h8v2zm-3-5V3.5L18.5 9H13z"/></svg><p>未知文件类型<br>可能是未完成下载的碎片数据</p><div class="hex-preview-box">头部十六进制:<br>${escapeHtml(hexPreview)}</div>${hintHtml}</div>`;
            info.textContent = `${f.file_type_label} · ${formatSize(f.decrypted_size)}`;
        }
    } catch (e) {
        body.innerHTML = `<div class="preview-placeholder"><p>加载失败: ${escapeHtml(e.message)}</p></div>`;
    }
}

function closePreview() {
    // 销毁 lottie 动画实例
    if (state.currentLottie) {
        try { state.currentLottie.destroy(); } catch(e) {}
        state.currentLottie = null;
    }
    $('preview-modal').classList.add('hidden');
    $('preview-body').innerHTML = '';
    state.currentPreviewId = null;
}

// ==================== 图片预览缩放 ====================
function toggleImageZoom(img) {
    img.classList.toggle('zoomed');
}

// ==================== 大视频重建 ====================
async function rebuildAndPreview(fileId) {
    const body = $('preview-body');
    body.innerHTML = '<div class="loading-spinner"><div class="spinner"></div></div><p style="text-align:center;margin-top:8px;color:var(--text-muted);">正在重建大视频... (可能需要数秒)</p>';
    
    try {
        // 触发导出 (导出会执行重建 + ffmpeg remux moov 前置)
        const result = await api(`/api/export/${fileId}`, { method: 'POST' });
        toast(`重建成功: ${describeExportResult(result)}`, 'success');

        // 用 /api/preview 播放 (会自动检测导出文件, 支持 Range 请求)
        // 加 cache-busting 参数避免浏览器缓存重建前的旧响应
        const bust = Date.now();
        body.innerHTML = `<video class="preview-video" src="/api/preview/${encodeURIComponent(fileId)}?t=${bust}" controls autoplay></video>
            <div style="margin-top:8px; display:flex; gap:8px; justify-content:center;">
                <button class="btn btn-small btn-secondary" onclick="reRebuildVideo('${fileId}', this)">重新重建</button>
            </div>`;
        handleVideoError(body.querySelector('video'), body, '视频无法播放<br>重建可能未完全成功<br>请尝试用 VLC 播放器打开导出的文件');
        const infoEl = $('preview-info');
        if (infoEl) infoEl.textContent = `大视频 · ${describeExportResult(result)} · 已重建 (moov 前置)`;

        // 刷新文件列表以更新卡片上的"已重建"标记
        await loadFiles(state.currentPage);
    } catch (e) {
        body.innerHTML = `<div class="preview-placeholder"><svg viewBox="0 0 24 24"><path fill="currentColor" d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-2h2v2zm0-4h-2V7h2v6z"/></svg><p>重建失败<br>${describeExportError(e)}</p><p style="margin-top:8px;font-size:13px;color:var(--text-muted);">提示: 在 Telegram Desktop 中播放该视频可以补全缓存分片, 重新扫描后再试</p></div>`;
    }
}

// ==================== 单个视频重新重建 ====================
async function reRebuildVideo(fileId, btn) {
    const body = $('preview-body');
    if (btn) { btn.disabled = true; btn.textContent = '重建中...'; }

    try {
        // 1. 删除旧导出文件
        await api(`/api/delete_export/${fileId}`, { method: 'POST' });

        // 2. 重新重建+导出
        const result = await api(`/api/export/${fileId}`, { method: 'POST' });
        toast(`重新重建成功: ${describeExportResult(result)}`, 'success');

        // 3. 用新文件播放 (cache-busting 避免浏览器缓存旧文件)
        const bust = Date.now();
        body.innerHTML = `<video class="preview-video" src="/api/preview/${encodeURIComponent(fileId)}?t=${bust}" controls autoplay></video>
            <div style="margin-top:8px; display:flex; gap:8px; justify-content:center;">
                <button class="btn btn-small btn-secondary" onclick="reRebuildVideo('${fileId}', this)">重新重建</button>
            </div>`;
        handleVideoError(body.querySelector('video'), body, '视频无法播放<br>可能是缓存不完整或重建文件损坏<br>请尝试重新导出');
        const infoEl = $('preview-info');
        if (infoEl) infoEl.textContent = `大视频 · ${describeExportResult(result)} · 已重建 (moov 前置)`;

        // 刷新文件列表
        await loadFiles(state.currentPage);
    } catch (e) {
        toast('重新重建失败', 'error');
        body.innerHTML = `<div class="preview-placeholder"><svg viewBox="0 0 24 24"><path fill="currentColor" d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-2h2v2zm0-4h-2V7h2v6z"/></svg><p>重新重建失败<br>${describeExportError(e)}</p></div>`;
    }
}

// ==================== 不完整大视频导出 (局部可播片段) ====================
async function exportIncompleteVideo(fileId) {
    const body = $('preview-body');
    body.innerHTML = '<div class="loading-spinner"><div class="spinner"></div></div><p style="text-align:center;margin-top:8px;color:var(--text-muted);">正在重建可播放片段... (只写出连续覆盖的部分, 不做零填充)</p>';

    try {
        const result = await api(`/api/export/${fileId}`, { method: 'POST' });
        toast(`导出成功: ${describeExportResult(result)}`, 'success');

        // 用 /api/preview 播放 (会自动检测导出文件)
        const bust = Date.now();
        const durTxt = result.duration ? `实际可播 ${Number(result.duration).toFixed(1)}s` : '';
        body.innerHTML = `<video class="preview-video" src="/api/preview/${encodeURIComponent(fileId)}?t=${bust}" controls></video>
            <div style="margin-top:8px; display:flex; gap:8px; justify-content:center; flex-wrap:wrap;">
                <span style="color:var(--text-muted);font-size:13px;line-height:28px;">已导出局部片段${durTxt ? ' · ' + durTxt : ''}, 缺失区段未写入, 播放会在此结束</span>
                <button class="btn btn-small btn-secondary" onclick="reRebuildVideo('${fileId}', this)">重新重建</button>
            </div>`;
        handleVideoError(body.querySelector('video'), body, '视频无法播放<br>导出文件可能损坏<br>请尝试用 VLC 播放器打开导出的文件');
        const infoEl = $('preview-info');
        if (infoEl) infoEl.textContent = `大视频 (局部片段) · ${describeExportResult(result)}`;

        // 刷新文件列表
        await loadFiles(state.currentPage);
    } catch (e) {
        body.innerHTML = `<div class="preview-placeholder"><svg viewBox="0 0 24 24"><path fill="currentColor" d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-2h2v2zm0-4h-2V7h2v6z"/></svg><p>导出失败<br>${describeExportError(e)}</p><p style="margin-top:8px;font-size:13px;color:var(--text-muted);">若提示 moov 未缓存, 说明连文件的索引都没缓存下来, 目前无法生成可播放文件</p></div>`;
    }
}

// ==================== 一键重建完整大视频 ====================
async function rebuildAllCompleteVideos() {
    const btn = $('btn-rebuild-all');
    if (!btn) return;
    
    // 从当前已加载的文件列表中找出所有可重建的大视频
    const completeIds = state.files
        .filter(f => f.is_complete_large_video && !f.is_rebuilt)
        .map(f => f.file_id);
    
    if (completeIds.length === 0) {
        toast('没有可重建的完整大视频', '');
        return;
    }

    btn.disabled = true;
    btn.innerHTML = '<span class="btn-spinner"></span> 重建中...';
    toast(`正在重建 ${completeIds.length} 个大视频, 请稍候...`, '');

    let taskId = null;
    let cancelled = false;

    // 把按钮变成取消按钮
    btn.textContent = `重建中 0/${completeIds.length} (点击取消)`;
    btn.disabled = false;
    btn.onclick = async () => {
        if (taskId) {
            try { await api(`/api/task/cancel/${taskId}`, { method: 'POST' }); } catch(e) {}
            cancelled = true;
        }
    };

    try {
        const startResult = await api('/api/export_batch_async', {
            method: 'POST',
            body: JSON.stringify({ file_ids: completeIds }),
        });
        taskId = startResult.task_id;

        // 轮询任务进度
        const pollTask = async () => {
            const status = await api(`/api/task/status/${taskId}`);
            if (!cancelled) {
                btn.textContent = `重建中 ${status.progress}/${status.total} (点击取消)`;
            }
            if (status.status === 'running' && !cancelled) {
                await new Promise(r => setTimeout(r, 500));
                return pollTask();
            }
            return status;
        };
        const finalStatus = await pollTask();

        if (finalStatus.status === 'cancelled' || cancelled) {
            toast('重建已取消', '');
        } else if (finalStatus.status === 'error') {
            toast('重建出错', 'error');
        } else if (finalStatus.failed > 0) {
            // 如实报告: 有失败项时不要显示成"全部成功"
            toast(`重建完成: 成功 ${finalStatus.success}/${finalStatus.total}, 失败 ${finalStatus.failed} 个 (缓存数据不足)`, 'error');
        } else {
            toast(`重建完成: 成功 ${finalStatus.success}/${finalStatus.total}`, 'success');
        }
    } catch (e) {
        toast('重建失败: ' + e.message, 'error');
    } finally {
        // 恢复按钮
        btn.disabled = false;
        btn.textContent = '一键重建';
        btn.onclick = rebuildAllCompleteVideos;
        // 刷新文件列表
        await loadFiles(state.currentPage);
    }
}

// ==================== 一键重新重建已重建过的大视频 ====================
async function reRebuildAllCompleteVideos() {
    const btn = $('btn-rebuild-redo');
    if (!btn) return;

    // 找出所有已重建过的大视频 (is_rebuilt=true, is_large_video=true)
    const rebuiltIds = state.files
        .filter(f => f.is_rebuilt && f.is_large_video)
        .map(f => f.file_id);

    if (rebuiltIds.length === 0) {
        toast('没有已重建过的大视频可重新重建', '');
        return;
    }

    btn.disabled = true;
    btn.innerHTML = '<span class="btn-spinner"></span> 重建中...';
    toast(`正在重新重建 ${rebuiltIds.length} 个大视频, 先删除旧文件...`, '');

    // 1. 删除所有旧的导出文件
    let deletedCount = 0;
    for (const fid of rebuiltIds) {
        try {
            await api(`/api/delete_export/${fid}`, { method: 'POST' });
            deletedCount++;
        } catch (e) {
            // 忽略删除失败, 继续重建
        }
    }

    // 2. 重新重建
    let taskId = null;
    let cancelled = false;

    btn.textContent = `重建中 0/${rebuiltIds.length} (点击取消)`;
    btn.disabled = false;
    btn.onclick = async () => {
        if (taskId) {
            try { await api(`/api/task/cancel/${taskId}`, { method: 'POST' }); } catch(e) {}
            cancelled = true;
        }
    };

    try {
        const startResult = await api('/api/export_batch_async', {
            method: 'POST',
            body: JSON.stringify({ file_ids: rebuiltIds }),
        });
        taskId = startResult.task_id;

        const pollTask = async () => {
            const status = await api(`/api/task/status/${taskId}`);
            if (!cancelled) {
                btn.textContent = `重建中 ${status.progress}/${status.total} (点击取消)`;
            }
            if (status.status === 'running' && !cancelled) {
                await new Promise(r => setTimeout(r, 500));
                return pollTask();
            }
            return status;
        };
        const finalStatus = await pollTask();

        if (finalStatus.status === 'cancelled' || cancelled) {
            toast('重新重建已取消', '');
        } else if (finalStatus.status === 'error') {
            toast('重新重建出错', 'error');
        } else if (finalStatus.failed > 0) {
            toast(`重新重建完成: 成功 ${finalStatus.success}/${finalStatus.total}, 失败 ${finalStatus.failed} 个 (缓存数据不足)`, 'error');
        } else {
            toast(`重新重建完成: 成功 ${finalStatus.success}/${finalStatus.total}`, 'success');
        }
    } catch (e) {
        toast('重新重建失败: ' + e.message, 'error');
    } finally {
        btn.disabled = false;
        btn.textContent = '一键重新重建';
        btn.onclick = reRebuildAllCompleteVideos;
        await loadFiles(state.currentPage);
    }
}

// ==================== 刷新当前列表 ====================
async function refreshCurrentList() {
    const btn = $('btn-refresh');
    if (btn) { btn.disabled = true; btn.textContent = '刷新中...'; }
    try {
        if (state.viewMode === 'downloads') {
            // 下载文件: 强制重新扫描
            await api('/api/download_files/refresh', { method: 'POST' });
            await loadDownloadFiles(state.currentPage);
            toast('下载列表已刷新', 'success');
        } else {
            // 缓存文件: 重新加载
            await loadFiles(state.currentPage);
            toast('列表已刷新', 'success');
        }
    } catch (e) {
        toast('刷新失败: ' + e.message, 'error');
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = '刷新'; }
    }
}

// ==================== 导出 ====================
async function exportFile(fileId) {
    toast('正在导出...', '');
    try {
        const result = await api(`/api/export/${fileId}`, { method: 'POST' });
        toast(`导出成功: ${result.filename} (${describeExportResult(result)})`, 'success');
    } catch (e) {
        toast('导出失败', 'error');
        // 失败时把后端给的原因 (含缺失分片) 展示出来, 而不是只说"导出失败"
        const body = $('preview-body');
        if (body) {
            const box = document.createElement('div');
            box.className = 'video-error-msg preview-placeholder video-warn';
            box.style.padding = '12px';
            box.innerHTML = VIDEO_ICON + '<p style="margin-top:4px;">导出失败<br>' + describeExportError(e) + '</p>';
            body.appendChild(box);
        }
    }
}

// ==================== 删除缓存文件 ====================
async function deleteCacheFile(fileId) {
    if (!confirm('确认删除此缓存文件？\n删除后将从磁盘上移除原始缓存数据，此操作不可恢复。')) return;
    try {
        const result = await api(`/api/file/${fileId}`, { method: 'DELETE' });
        toast('缓存文件已删除', 'success');
        // 关闭预览并刷新列表
        closePreview();
        await loadFiles(state.currentPage);
        await loadStats();
    } catch (e) {
        toast('删除失败: ' + e.message, 'error');
    }
}

// ==================== Telegram 来源链接绑定 ====================
// 仅缓存未下载的视频, 本地没有消息关联 (后端取证结论), 无法自动定位原消息。
// 绑定一次 t.me 链接后, "前往 Telegram 播放"即可精确跳转回去补全分片。
function renderTelegramBindBox(f) {
    const box = $('telegram-bind-box');
    if (!box) return;
    // 只在缓存视图下、对视频类文件 (非分片) 显示
    if (state.viewMode === 'downloads' || f.category !== 'video' || f.file_type === 'video_slice') {
        box.classList.add('hidden');
        box.innerHTML = '';
        return;
    }
    const link = f.telegram_link;
    if (link && link.tg_url) {
        box.classList.remove('hidden');
        box.innerHTML = `
            <div class="tg-bind-row">
                <span class="tg-bind-label">Telegram 来源</span>
                <span class="tg-bind-link" title="${escapeHtml(link.source_url || link.display || '')}">${escapeHtml(link.display || link.source_url || '')}</span>
                <button class="btn btn-small" onclick="unbindTelegram('${f.file_id}')">解绑</button>
            </div>`;
    } else {
        box.classList.remove('hidden');
        box.innerHTML = `
            <div class="tg-bind-row">
                <span class="tg-bind-label">绑定来源</span>
                <input id="tg-bind-input" class="tg-bind-input" type="text"
                       placeholder="https://t.me/频道名/消息号" spellcheck="false">
                <button class="btn btn-small btn-secondary" onclick="bindTelegram('${f.file_id}')">绑定</button>
            </div>
            <div class="tg-bind-hint">仅缓存未下载的视频无法自动定位原消息 —— 粘贴原视频的 t.me 链接绑定一次, 之后可一键跳转回去播放、补全缓存分片</div>`;
    }
}

async function bindTelegram(fileId) {
    const input = $('tg-bind-input');
    const url = input ? input.value.trim() : '';
    if (!url) {
        toast('请先粘贴原视频的 t.me 链接', 'error');
        return;
    }
    try {
        await api(`/api/file/${fileId}/bind_telegram`, {
            method: 'POST',
            body: JSON.stringify({ url }),
        });
        toast('已绑定来源, 现在可以精确跳转了', 'success');
        await refreshBindBox(fileId);
    } catch (e) {
        toast('绑定失败: ' + e.message, 'error');
    }
}

async function unbindTelegram(fileId) {
    try {
        await api(`/api/file/${fileId}/bind_telegram`, { method: 'DELETE' });
        toast('已解除绑定', 'info');
        await refreshBindBox(fileId);
    } catch (e) {
        toast('解绑失败: ' + e.message, 'error');
    }
}

async function refreshBindBox(fileId) {
    try {
        const f = await api(`/api/file/${fileId}`);
        renderTelegramBindBox(f);
    } catch (e) {
        // 刷新失败不影响主流程
    }
}

// ==================== 前往 Telegram 播放 ====================
async function openInTelegram() {
    const fileId = state.currentPreviewId;
    if (!fileId) return;

    const btn = $('btn-preview-telegram');
    const originalText = btn.textContent;
    btn.disabled = true;
    btn.textContent = '正在打开...';

    try {
        const result = await api(`/api/file/${fileId}/open_in_telegram`, { method: 'POST' });
        if (result.ok) {
            // hint 由后端按定位状态给出 (绑定跳转 / 自动定位 / 仅缓存未下载 / 私聊)
            let msg = result.hint || '已启动 Telegram Desktop';
            if (result.peer_info) {
                const pi = result.peer_info;
                msg += `\n来源: ${pi.peer_type}`;
                if (pi.path) msg += `\n文件: ${pi.path}`;
            }
            toast(msg, result.tg_url ? 'success' : 'info');
        } else {
            toast('启动失败: ' + (result.error || result.hint || '未知错误'), 'error');
        }
    } catch (e) {
        toast('启动失败: ' + e.message, 'error');
    } finally {
        btn.disabled = false;
        btn.textContent = originalText;
    }
}

async function exportSelected() {
    const ids = Array.from(state.selectedIds);
    if (!ids.length) return;

    el.btnExportSelected.disabled = true;
    el.btnExportSelected.textContent = '导出中...';
    el.btnDeleteSelected.classList.add('hidden');
    toast(`正在导出 ${ids.length} 个文件...`, '');

    let taskId = null;
    let cancelled = false;

    // 临时把导出按钮变成取消按钮
    const btnCancel = el.btnExportSelected;
    btnCancel.textContent = '取消导出';
    btnCancel.disabled = false;
    btnCancel.onclick = async () => {
        if (taskId) {
            try { await api(`/api/task/cancel/${taskId}`, { method: 'POST' }); } catch(e) {}
            cancelled = true;
        }
    };

    try {
        const startResult = await api('/api/export_batch_async', {
            method: 'POST',
            body: JSON.stringify({ file_ids: ids }),
        });
        taskId = startResult.task_id;

        // 轮询任务进度
        const pollTask = async () => {
            const status = await api(`/api/task/status/${taskId}`);
            if (!cancelled) {
                btnCancel.textContent = `导出中 ${status.progress}/${status.total} (点击取消)`;
            }
            if (status.status === 'running' && !cancelled) {
                await new Promise(r => setTimeout(r, 500));
                return pollTask();
            }
            return status;
        };
        const finalStatus = await pollTask();

        if (finalStatus.status === 'cancelled' || cancelled) {
            toast('导出已取消', '');
        } else if (finalStatus.status === 'error') {
            toast('导出出错', 'error');
        } else {
            toast(`批量导出完成: 成功 ${finalStatus.success}/${finalStatus.total}`, 'success');
        }
    } catch (e) {
        toast('批量导出失败: ' + e.message, 'error');
    } finally {
        // 恢复按钮
        state.selectedIds.clear();
        el.btnExportSelected.classList.add('hidden');
        el.btnExportSelected.disabled = false;
        el.btnExportSelected.textContent = '导出选中';
        el.btnExportSelected.onclick = exportSelected;
        updateSelectedBadge();
        await loadFiles(state.currentPage);
    }
}

// ==================== 导出管理 ====================
async function openExports() {
    const modal = $('exports-modal');
    const list = $('exports-list');
    modal.classList.remove('hidden');
    list.innerHTML = '<div class="loading-spinner"><div class="spinner"></div></div>';

    try {
        const data = await api('/api/exports');
        renderExportsList(data.files);
    } catch (e) {
        list.innerHTML = `<p>加载失败: ${escapeHtml(e.message)}</p>`;
    }
}

function renderExportsList(files) {
    const list = $('exports-list');
    if (!files.length) {
        list.innerHTML = '<p style="text-align:center; color:var(--text-muted); padding:40px;">暂无导出文件</p>';
        return;
    }

    list.innerHTML = files.map(f => `
        <div class="export-item">
            <div class="export-item-info">
                <div class="export-item-name">${escapeHtml(f.filename)}</div>
                <div class="export-item-meta">${formatSize(f.size)} · ${new Date(f.modified * 1000).toLocaleString()}</div>
            </div>
            <div class="export-item-actions">
                <a class="btn btn-small" href="/api/exports/${encodeURIComponent(f.filename)}" download>下载</a>
                <button class="btn btn-small btn-danger" onclick="deleteExport('${escapeHtml(f.filename)}')">删除</button>
            </div>
        </div>
    `).join('');
}

async function deleteExport(filename) {
    if (!confirm(`确认删除 ${filename}？`)) return;
    try {
        await api(`/api/exports/${encodeURIComponent(filename)}`, { method: 'DELETE' });
        toast('已删除', 'success');
        openExports();
    } catch (e) {
        toast('删除失败: ' + e.message, 'error');
    }
}

async function clearExports() {
    if (!confirm('确认清空所有导出文件？此操作不可恢复。')) return;
    try {
        await api('/api/exports/clear', { method: 'POST' });
        toast('已清空', 'success');
        openExports();
    } catch (e) {
        toast('清空失败: ' + e.message, 'error');
    }
}

function closeExports() {
    $('exports-modal').classList.add('hidden');
}

// ==================== 设置 ====================
async function openSettings() {
    const modal = $('settings-modal');
    modal.classList.remove('hidden');
    $('passcode-input').value = '';
    try {
        const config = await api('/api/config');
        $('tdata-path-input').value = config.tdata_path;
        $('download-path-input').value = config.download_path || '';
        const hint = $('passcode-hint');
        if (hint) {
            hint.textContent = config.has_passcode
                ? '当前已设置本地密码。如需清除，保存时留空即可。'
                : '留空表示无密码。设置后用于解密缓存密钥，改动需重新扫描。';
        }
    } catch (e) {
        toast('加载配置失败: ' + e.message, 'error');
    }
}

async function saveSettings() {
    const tdataPath = $('tdata-path-input').value.trim();
    const downloadPath = $('download-path-input').value.trim();
    if (!tdataPath) { toast('tdata 路径不能为空', 'error'); return; }

    const body = { tdata_path: tdataPath };
    body.download_path = downloadPath;  // 始终发送, 空字符串表示清除
    // passcode: 只有用户主动输入了内容才标记修改, 空输入不触发清除
    const passcodeVal = $('passcode-input').value;
    if (passcodeVal) {
        body.passcode = passcodeVal;
        body.passcode_change = true;
    }

    try {
        await api('/api/config', {
            method: 'POST',
            body: JSON.stringify(body),
        });
        toast('设置已保存', 'success');
        closeSettings();
        resetUIForRescan();
    } catch (e) {
        toast('保存失败: ' + e.message, 'error');
    }
}

function closeSettings() {
    $('settings-modal').classList.add('hidden');
}

// 扫描结果作废后把界面拉回初始态 (保存设置 / 清空缓存共用)
function resetUIForRescan() {
    el.statsBar.classList.add('hidden');
    el.dlStatsBar.classList.add('hidden');
    el.toolbar.classList.add('hidden');
    el.fileGrid.innerHTML = '';
    el.emptyState.classList.remove('hidden');
    el.btnScan.textContent = '扫描缓存';
    const banner = $('binlog-banner');
    if (banner) banner.classList.add('hidden');
}

// ==================== 清理孤儿文件 ====================
async function cleanupOrphans() {
    const btn = $('btn-cleanup');
    if (!btn) return;
    if (!confirm('确认清理孤儿文件？\n将删除不属于当前扫描结果的缩略图和导出文件。此操作不可恢复。')) return;
    btn.disabled = true;
    const orig = btn.textContent;
    btn.textContent = '清理中...';
    try {
        const result = await api('/api/cleanup', { method: 'POST' });
        toast(`清理完成: 缩略图 ${result.thumb_deleted} 个, 导出文件 ${result.export_deleted} 个`, 'success');
    } catch (e) {
        toast('清理失败: ' + e.message, 'error');
    } finally {
        btn.disabled = false;
        btn.textContent = orig;
    }
}

// ==================== 打开导出目录 ====================
async function openExportFolder() {
    const btn = $('btn-open-export-folder');
    if (!btn) return;
    try {
        await api('/api/open_export_folder', { method: 'POST' });
    } catch (e) {
        toast('打开失败: ' + e.message, 'error');
    }
}

// ==================== 清空缓存 / 下载目录 ====================
// 两步走: 先向后端要一份预演统计 (文件数/体积/目录), 用户确认后才真正删除。
const CLEAR_BTN_IDS = {
    telegram: 'btn-clear-telegram-cache',
    download: 'btn-clear-download-dir',
    app: 'btn-clear-app-cache',
};

async function clearCacheTarget(target) {
    const btn = $(CLEAR_BTN_IDS[target]);
    if (!btn) return;
    const orig = btn.textContent;
    const restore = () => { btn.disabled = false; btn.textContent = orig; };

    // ---- 第一步: 预演 ----
    let preview;
    btn.disabled = true;
    btn.textContent = '统计中...';
    try {
        preview = await api('/api/clear_cache', {
            method: 'POST',
            body: JSON.stringify({ target, confirm: false }),
        });
    } catch (e) {
        toast('无法预览: ' + e.message, 'error');
        restore();
        return;
    }

    if (!preview.file_count) {
        restore();
        toast(`「${preview.label}」已经是空的，无需清理`, 'success');
        return;
    }

    const dirLines = (preview.dirs || []).map(d => '  · ' + d.path).join('\n');
    const sampleLine = (preview.sample || []).length
        ? '\n示例文件: ' + preview.sample.join(', ')
        : '';
    const ok = window.confirm(
        `确认清空「${preview.label}」？\n\n` +
        `将清空以下目录:\n${dirLines}\n\n` +
        `共 ${preview.file_count} 个文件，约 ${formatSize(preview.total_bytes)}${sampleLine}\n\n` +
        `此操作不可恢复。账号数据 (key_datas / D877F783D5D3EF8C)、聊天记录与登录态不会被删除。`
    );
    if (!ok) {
        restore();
        return;
    }

    // ---- 第二步: 执行 ----
    btn.textContent = '清空中...';
    try {
        const r = await api('/api/clear_cache', {
            method: 'POST',
            body: JSON.stringify({ target, confirm: true }),
        });
        let msg = `已删除 ${r.deleted} 个文件，释放 ${formatSize(r.freed_bytes)}`;
        if (r.failed) msg += `；${r.failed} 个失败（可能正被 Telegram 占用）`;
        toast(msg, r.failed ? 'error' : 'success');
        if (r.errors && r.errors.length) {
            console.warn('[clear_cache] 失败明细:', r.errors);
        }
        if (r.need_rescan) {
            resetUIForRescan();
            closeSettings();
            toast('缓存已清空，请重新扫描', 'success');
        } else if (target === 'download') {
            if (state.viewMode === 'downloads') loadDownloadFiles(1);
        } else if (target === 'app') {
            refreshCurrentList();
        }
    } catch (e) {
        toast('清空失败: ' + e.message, 'error');
    } finally {
        restore();
    }
}

// ==================== 重复文件检测 ====================
async function openDuplicates() {
    const modal = $('duplicates-modal');
    const list = $('duplicates-list');
    const summary = $('duplicates-summary');
    modal.classList.remove('hidden');
    list.innerHTML = '<div class="loading-spinner"><div class="spinner"></div></div>';
    summary.textContent = '';

    try {
        const data = await api('/api/duplicates');
        if (data.total_groups === 0) {
            summary.textContent = '未检测到重复文件';
            list.innerHTML = '';
            return;
        }
        summary.textContent = `发现 ${data.total_groups} 组重复, 涉及 ${data.total_dup_files} 个文件, 浪费空间 ${formatSize(data.waste_size)}`;

        let html = '';
        for (const g of data.groups) {
            let cards = '';
            for (const f of g.files) {
                const showThumb = (f.category === 'image' || f.category === 'video');
                const thumbUrl = showThumb ? `/api/thumbnail/${f.file_id}` : '';
                cards += `<div class="dup-file-card" onclick="closeDuplicates(); openPreview('${f.file_id}')">
                    <div class="dup-file-thumb">${showThumb ? `<img src="${thumbUrl}" onerror="this.style.display='none'">` : getPlaceholder(f.category)}</div>
                    <div class="dup-file-info">
                        <div class="dup-file-name" title="${escapeHtml(f.display_name || f.file_name)}">${escapeHtml(f.display_name || f.file_name)}</div>
                        <div class="dup-file-size">${formatSize(f.decrypted_size)}</div>
                    </div>
                </div>`;
            }
            html += `<div class="dup-group">
                <div class="dup-group-header">
                    <span>重复 ${g.count} 份 · ${formatSize(g.size)}/份</span>
                    <span style="color:var(--danger);">浪费 ${formatSize(g.waste)}</span>
                </div>
                <div class="dup-group-files">${cards}</div>
            </div>`;
        }
        list.innerHTML = html;
    } catch (e) {
        list.innerHTML = `<p>检测失败: ${escapeHtml(e.message)}</p>`;
    }
}

function closeDuplicates() {
    $('duplicates-modal').classList.add('hidden');
}

// ==================== 事件绑定 ====================
el.btnScan.addEventListener('click', startScan);
el.btnExports.addEventListener('click', openExports);
el.btnSettings.addEventListener('click', openSettings);
el.btnSelectAll.addEventListener('click', () => {
    const checkboxes = document.querySelectorAll('.file-card-checkbox');
    const allChecked = Array.from(checkboxes).every(cb => cb.checked);
    checkboxes.forEach(cb => {
        cb.checked = !allChecked;
        const card = cb.closest('.file-card');
        const id = card.dataset.id;
        if (!allChecked) {
            state.selectedIds.add(id);
            card.classList.add('selected');
        } else {
            state.selectedIds.delete(id);
            card.classList.remove('selected');
        }
    });
    el.btnDeleteSelected.classList.toggle('hidden', state.selectedIds.size === 0);
    el.btnExportSelected.classList.toggle('hidden', state.selectedIds.size === 0);
    updateSelectedBadge();
});

el.btnExportSelected.addEventListener('click', exportSelected);
el.btnDeleteSelected.addEventListener('click', deleteSelectedDownloadFiles);
$('btn-rebuild-all').onclick = rebuildAllCompleteVideos;
$('btn-rebuild-redo').onclick = reRebuildAllCompleteVideos;
$('btn-preview-export').addEventListener('click', () => {
    if (state.viewMode === 'downloads' && state.currentPreviewId) {
        // 下载模式: 下载文件直接打开
        window.open(`/api/download_preview/${encodeURIComponent(state.currentPreviewId)}`, '_blank');
    } else if (state.currentPreviewId) {
        exportFile(state.currentPreviewId);
    }
});
$('btn-preview-telegram').addEventListener('click', openInTelegram);
$('btn-preview-delete').addEventListener('click', () => {
    if (state.viewMode === 'downloads' && state.currentPreviewId) {
        deleteDownloadFile(state.currentPreviewId);
    } else if (state.currentPreviewId) {
        deleteCacheFile(state.currentPreviewId);
    }
});
$('btn-clear-exports').addEventListener('click', clearExports);
$('btn-save-settings').addEventListener('click', saveSettings);
$('btn-cleanup').addEventListener('click', cleanupOrphans);
$('btn-open-export-folder').addEventListener('click', openExportFolder);
$('btn-clear-telegram-cache').addEventListener('click', () => clearCacheTarget('telegram'));
$('btn-clear-download-dir').addEventListener('click', () => clearCacheTarget('download'));
$('btn-clear-app-cache').addEventListener('click', () => clearCacheTarget('app'));
$('btn-refresh').addEventListener('click', refreshCurrentList);
$('btn-duplicates').addEventListener('click', openDuplicates);
el.btnTheme.addEventListener('click', toggleTheme);

// 搜索 (防抖)
let searchTimer = null;
el.searchInput.addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
        state.search = el.searchInput.value.trim();
        if (state.viewMode === 'downloads') {
            loadDownloadFiles(1);
        } else {
            loadFiles(1);
        }
    }, 300);
});

// 排序
el.sortSelect.addEventListener('change', () => {
    state.sort = el.sortSelect.value;
    if (state.viewMode === 'downloads') {
        loadDownloadFiles(1);
    } else {
        loadFiles(1);
    }
});

// 缓存文件统计卡片筛选
document.querySelectorAll('#stats-bar .stat-card').forEach(card => {
    card.addEventListener('click', () => {
        const category = card.dataset.category;
        document.querySelectorAll('#stats-bar .stat-card').forEach(c => c.classList.remove('active'));
        if (category === 'all' || state.category === category) {
            state.category = 'all';
        } else {
            card.classList.add('active');
            state.category = category;
        }
        if (state.category === 'all') {
            document.querySelector('#stats-bar .stat-card[data-category="all"]').classList.add('active');
        }
        loadFiles(1);
    });
});

// 下载文件统计卡片筛选
document.querySelectorAll('#dl-stats-bar .stat-card').forEach(card => {
    card.addEventListener('click', () => {
        const category = card.dataset.dlCategory;
        document.querySelectorAll('#dl-stats-bar .stat-card').forEach(c => c.classList.remove('active'));
        if (category === 'all' || state.dlCategory === category) {
            state.dlCategory = 'all';
        } else {
            card.classList.add('active');
            state.dlCategory = category;
        }
        if (state.dlCategory === 'all') {
            document.querySelector('#dl-stats-bar .stat-card[data-dl-category="all"]').classList.add('active');
        }
        loadDownloadFiles(1);
    });
});

// 模态框点击外部关闭 (只关闭被点击的 overlay 所属的模态)
document.querySelectorAll('.modal-overlay').forEach(overlay => {
    overlay.addEventListener('click', (e) => {
        if (e.target !== overlay) return;  // 只响应 overlay 自身点击, 不响应子元素冒泡
        const modal = overlay.closest('.modal');
        if (!modal) return;
        const id = modal.id;
        if (id === 'preview-modal') closePreview();
        else if (id === 'exports-modal') closeExports();
        else if (id === 'settings-modal') closeSettings();
        else if (id === 'duplicates-modal') closeDuplicates();
    });
});

// ESC 关闭模态框 + 快捷键
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
        closePreview();
        closeExports();
        closeSettings();
        closeDuplicates();
    }
    // / 聚焦搜索框 (不在 input 中时)
    if (e.key === '/' && document.activeElement !== el.searchInput && !e.ctrlKey && !e.metaKey) {
        // 检查是否有模态框打开
        const anyModalOpen = document.querySelectorAll('.modal:not(.hidden)').length > 0;
        if (!anyModalOpen) {
            e.preventDefault();
            el.searchInput.focus();
        }
    }
    // Ctrl+A 全选当前页 (不在 input/textarea 中时)
    if ((e.ctrlKey || e.metaKey) && e.key === 'a') {
        const tag = document.activeElement.tagName;
        if (tag !== 'INPUT' && tag !== 'TEXTAREA') {
            const anyModalOpen = document.querySelectorAll('.modal:not(.hidden)').length > 0;
            if (!anyModalOpen && el.toolbar && !el.toolbar.classList.contains('hidden')) {
                e.preventDefault();
                $('btn-select-all').click();
            }
        }
    }
});

// 页面加载时初始化
(async function init() {
    // 初始化主题 (从 localStorage 读取, 默认浅色)
    let savedTheme = 'light';
    try { savedTheme = localStorage.getItem('tgcm_theme') || 'light'; } catch(e) {}
    applyTheme(savedTheme === 'dark' ? 'dark' : 'light');

    try {
        const config = await api('/api/config');
        $('tdata-path-input').value = config.tdata_path;
        $('download-path-input').value = config.download_path || '';
    } catch (e) {
        console.error('初始化失败:', e);
    }
    // 标记 "全部" 为 active
    document.querySelector('#stats-bar .stat-card[data-category="all"]').classList.add('active');
    document.querySelector('#dl-stats-bar .stat-card[data-dl-category="all"]').classList.add('active');
    // 默认隐藏下载模式专属按钮
    document.querySelectorAll('.dl-only').forEach(e => e.style.display = 'none');
})();
