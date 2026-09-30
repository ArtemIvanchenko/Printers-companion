function _getWorkstationId() {
    const key = 'printer-companion-workstation-id';
    let value = localStorage.getItem(key);
    if (!value) {
        const suffix = (globalThis.crypto?.randomUUID?.() || Math.random().toString(36).slice(2)).slice(0, 8);
        value = `operator-${location.hostname || 'pc'}-${suffix}`;
        localStorage.setItem(key, value);
    }
    return value;
}

function _jsonHeaders() {
    return {
        'Content-Type': 'application/json',
        'X-Workstation-ID': _getWorkstationId(),
    };
}

function _esc(value) {
    return String(value ?? '')
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#39;');
}

// Non-blocking feedback keeps the operator in context. Browser alerts
// stop the whole dashboard and make repeated import errors hard to read.
function showToast(message, type = 'info', duration = 4200) {
    const region = document.getElementById('app-toast-region');
    if (!region) return;
    const toast = document.createElement('div');
    toast.className = `app-toast ${type}`;
    toast.setAttribute('role', type === 'error' ? 'alert' : 'status');
    toast.textContent = message;
    region.appendChild(toast);
    window.setTimeout(() => {
        toast.style.opacity = '0';
        toast.style.transform = 'translateY(4px) scale(.98)';
        toast.style.transition = 'opacity 140ms var(--ease-out), transform 140ms var(--ease-out)';
        window.setTimeout(() => toast.remove(), 160);
    }, duration);
}

let _interactiveEnhanceQueued = false;
function enhanceInteractiveElements() {
    if (_interactiveEnhanceQueued) return;
    _interactiveEnhanceQueued = true;
    requestAnimationFrame(() => {
        _interactiveEnhanceQueued = false;
        document.querySelectorAll('a[onclick]:not([href]), div[onclick], span[onclick]')
            .forEach(el => {
                if (el.tagName === 'A' || el.matches('.stat-card, .recent-card, .model-card, .tel-card')) {
                    el.tabIndex = 0;
                    if (!el.getAttribute('role')) el.setAttribute('role', 'button');
                }
            });
    });
}

document.addEventListener('keydown', (event) => {
    const target = event.target;
    if (target && (target.matches('input, textarea, select') || target.isContentEditable)) return;
    if (event.key === 'Escape') {
        if (document.getElementById('viewer-overlay')?.classList.contains('open')) closeViewer(null);
        if (document.getElementById('changelog-modal')?.classList.contains('open')) closeChangelog();
        return;
    }
    if ((event.key === 'Enter' || event.key === ' ') && target?.matches('a[role="button"], div[role="button"], span[role="button"]')) {
        event.preventDefault();
        target.click();
    }
});
enhanceInteractiveElements();
new MutationObserver(enhanceInteractiveElements).observe(document.body, { childList: true, subtree: true });

const dashboardBootstrap = JSON.parse(document.getElementById('dashboard-bootstrap').textContent);
const chartColors = {blue:'#60a5fa', green:'#10b981', yellow:'#f59e0b', red:'#ef4444',
    purple:'#8b5cf6', pink:'#ec4899', cyan:'#06b6d4', orange:'#f97316'};
// Optional visualizers must never prevent catalog/navigation startup.
function createDashboardChart(canvas, config) {
    if (!canvas) return null;
    if (typeof Chart === 'undefined') {
        let notice = canvas.parentElement.querySelector('.chart-unavailable');
        if (!notice) {
            notice = document.createElement('p');
            notice.className = 'chart-unavailable';
            notice.textContent = 'График недоступен. Данные и карточки остаются доступны.';
            canvas.parentElement.appendChild(notice);
        }
        return null;
    }
    Chart.defaults.color = '#a0aec0';
    Chart.defaults.borderColor = '#2d3748';
    Chart.getChart(canvas)?.destroy();
    return new Chart(canvas, config);
}
const _telCharts = {currentSessionId: null};
function ensureTelemetryCharts() {
    for (const [key, id] of [['o2','o2Chart'],['temp','tempChart'],['hum','humChart'],['press','pressChart']]) {
        if (!_telCharts[key]) _telCharts[key] = createDashboardChart(document.getElementById(id), {
            type:'line', data:{labels:[], datasets:[]},
            options:{responsive:true, interaction:{mode:'index',intersect:false},
                plugins:{legend:{position:'bottom'}}, scales:{x:{ticks:{maxTicksLimit:8}}}},
        });
    }
    if (!_telCharts.burn) _telCharts.burn = createDashboardChart(document.getElementById('burnLayerChart'), {
        type:'bar', data:{labels:[],datasets:[{label:'Сек/слой',data:[],backgroundColor:'#f97316',borderRadius:4}]},
        options:{responsive:true,scales:{y:{beginAtZero:true},x:{ticks:{maxTicksLimit:20}}}},
    });
}

// =========================================================
