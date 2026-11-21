/**
 * 首页 - 直播监控控制台逻辑
 */
const app = new Vue({
    el: '#app',
    data: {
        showAddModal: false,
        showProxyModal: false,
        showCookieModal: false,
        addingRoom: false,
        savingProxy: false,
        cookieSaving: false,
        cookieChecking: false,
        busyRooms: {},
        refreshTimer: null,
        refreshInFlight: false,
        statusFilter: 'all',  // 'all' | 'monitoring' | 'waiting' | 'stopped'
        searchKeyword: '',
        newRoom: {
            live_id: ''
        },
        rooms: [],
        stats: {
            total_rooms: 0,
            monitoring_rooms: 0,
            waiting_rooms: 0,
            stopped_rooms: 0,
            archived_rooms: 0
        },
        proxy: {
            enabled: false,
            host: '127.0.0.1',
            port: 7890,
            type: 'http'
        },
        douyinCookie: {
            configured: false,
            length: 0,
            health: {
                status: 'unknown',
                last_check_time: null,
                last_ok_time: null,
                last_error: null
            }
        },
        cookieForm: {
            cookie: '',
            reconnect_active: true
        }
    },
    mounted() {
        this.refreshDashboard();
        this.loadProxyConfig();

        this._visibilityHandler = () => {
            if (!document.hidden) {
                this.refreshDashboard();
            }
        };
        document.addEventListener('visibilitychange', this._visibilityHandler);

        this._keydownHandler = (e) => {
            if (e.key === 'Escape') {
                this.closeAddModal();
                this.closeProxyModal();
                this.closeCookieModal();
            }
        };
        document.addEventListener('keydown', this._keydownHandler);

        // 定时刷新状态（页面在后台时暂停请求，切回前台立即同步）
        this.refreshTimer = setInterval(() => {
            if (!document.hidden) {
                this.refreshDashboard();
            }
        }, 5000);
    },
    beforeDestroy() {
        if (this.refreshTimer) {
            clearInterval(this.refreshTimer);
            this.refreshTimer = null;
        }
        if (this._visibilityHandler) {
            document.removeEventListener('visibilitychange', this._visibilityHandler);
        }
        if (this._keydownHandler) {
            document.removeEventListener('keydown', this._keydownHandler);
        }
    },
    computed: {
        cookieHealth() {
            return (this.douyinCookie && this.douyinCookie.health) || { status: 'unknown' };
        },
        cookieHealthText() {
            if (!this.douyinCookie.configured) return '未配置';
            const map = { healthy: '正常', suspect: '确认中', dead: '已失活', unknown: '未知' };
            return map[this.cookieHealth.status] || '未知';
        },
        cookiePillClass() {
            if (!this.douyinCookie.configured) return '';
            const map = { healthy: 'is-ready', suspect: 'is-warn', dead: 'is-danger' };
            return map[this.cookieHealth.status] || '';
        },
        extractedLiveIdPreview() {
            return window.HubUI.extractLiveId(this.newRoom.live_id);
        },
        statusFilterLabel() {
            const labels = {
                all: '全部',
                monitoring: '直播监控中',
                waiting: '等待开播',
                stopped: '已停止 / 异常'
            };
            return labels[this.statusFilter] || '全部';
        },
        filteredRooms() {
            const kw = (this.searchKeyword || '').trim().toLowerCase();
            const list = this.rooms.filter((room) => {
                if (this.statusFilter === 'monitoring') {
                    if (room.status !== 'monitoring' && room.live_status !== 'live') return false;
                } else if (this.statusFilter === 'waiting') {
                    if (!['offline', 'waiting'].includes(room.status) || room.live_status === 'live') return false;
                } else if (this.statusFilter === 'stopped') {
                    if (!['stopped', 'error'].includes(room.status)) return false;
                }

                if (kw) {
                    const name = String(room.anchor_name || '').toLowerCase();
                    const lid = String(room.live_id || '').toLowerCase();
                    if (!name.includes(kw) && !lid.includes(kw)) return false;
                }
                return true;
            });

            const weight = (room) => {
                if (room.live_status === 'live' || room.status === 'monitoring') return 0;
                if (['offline', 'waiting'].includes(room.status)) return 1;
                if (room.status === 'error') return 2;
                return 3;
            };
            return list.slice().sort((a, b) => weight(a) - weight(b));
        }
    },
    methods: {
        setStatusFilter(target) {
            if (target !== 'all' && this.statusFilter === target) {
                this.statusFilter = 'all';
            } else {
                this.statusFilter = target;
            }
        },
        formatNumber(value) {
            const num = Number(value || 0);
            return Number.isFinite(num) ? Math.round(num).toLocaleString() : '0';
        },
        isRoomBusy(liveId) {
            return Boolean(this.busyRooms[liveId]);
        },
        setRoomBusy(liveId, busy) {
            if (busy) {
                this.$set(this.busyRooms, liveId, true);
            } else {
                this.$delete(this.busyRooms, liveId);
            }
        },
        async refreshDashboard() {
            if (this.refreshInFlight) return;
            this.refreshInFlight = true;
            try {
                await Promise.all([
                    this.loadRooms(),
                    this.loadStats(),
                    this.loadCookieConfig()
                ]);
            } finally {
                this.refreshInFlight = false;
            }
        },
        async loadRooms() {
            try {
                const response = await fetch('/api/rooms');
                const data = await response.json();
                if (data.rooms) {
                    this.rooms = data.rooms;
                }
            } catch (error) {
                console.error('加载房间列表失败:', error);
            }
        },
        async loadStats() {
            try {
                const response = await fetch('/api/rooms/stats/summary');
                const data = await response.json();
                if (response.ok && data) {
                    this.stats = data;
                }
            } catch (error) {
                console.error('加载统计数据失败:', error);
            }
        },
        async loadProxyConfig() {
            try {
                const response = await fetch('/api/proxy');
                const data = await response.json();
                if (response.ok && data) {
                    this.proxy = data;
                }
            } catch (error) {
                console.error('加载代理配置失败:', error);
            }
        },
        async loadCookieConfig() {
            try {
                const response = await fetch('/api/douyin-cookie');
                const data = await response.json();
                if (!data.health) {
                    data.health = { status: 'unknown', last_check_time: null, last_ok_time: null, last_error: null };
                }
                this.douyinCookie = data;
            } catch (error) {
                console.error('加载Cookie配置失败:', error);
            }
        },
        async checkCookieHealth() {
            if (this.cookieChecking) return;
            this.cookieChecking = true;
            try {
                const response = await fetch('/api/douyin-cookie/check', { method: 'POST' });
                const data = await response.json();
                if (response.ok) {
                    this.douyinCookie = Object.assign({}, this.douyinCookie, { health: data });
                    window.HubUI.toast(
                        data.status === 'healthy' ? 'Cookie 测活通过' : 'Cookie 当前不可用，请检查',
                        data.status === 'healthy' ? 'success' : 'warning'
                    );
                } else {
                    window.HubUI.toast(data.error || '检测失败', 'error');
                }
            } catch (error) {
                window.HubUI.toast('检测失败: ' + error.message, 'error');
            } finally {
                this.cookieChecking = false;
            }
        },
        openAddModal() {
            this.showAddModal = true;
            this.newRoom = {
                live_id: ''
            };
        },
        closeAddModal() {
            if (!this.addingRoom) {
                this.showAddModal = false;
            }
        },
        openProxyModal() {
            this.showProxyModal = true;
        },
        closeProxyModal() {
            if (!this.savingProxy) {
                this.showProxyModal = false;
            }
        },
        openCookieModal() {
            this.showCookieModal = true;
            this.cookieForm = {
                cookie: '',
                reconnect_active: true
            };
            this.loadCookieConfig();
        },
        closeCookieModal() {
            if (!this.cookieSaving) {
                this.showCookieModal = false;
            }
        },
        async updateProxyConfig() {
            if (this.savingProxy) return;
            this.savingProxy = true;
            try {
                const response = await fetch('/api/proxy', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(this.proxy)
                });
                const data = await response.json();

                if (response.ok) {
                    this.proxy = data;
                    this.savingProxy = false;
                    this.closeProxyModal();
                    window.HubUI.toast('代理配置已更新（新连接立即生效，已接入房间需重启监控）', 'success', 4200);
                } else {
                    window.HubUI.toast(data.error || '更新失败', 'error');
                }
            } catch (error) {
                window.HubUI.toast('更新失败: ' + error.message, 'error');
            } finally {
                this.savingProxy = false;
            }
        },
        async updateCookieConfig() {
            if (this.cookieSaving) return;
            const cookie = (this.cookieForm.cookie || '').trim();
            if (!cookie) {
                window.HubUI.toast('请粘贴完整 Cookie；如需清空请点击“清空 Cookie”', 'warning');
                return;
            }

            this.cookieSaving = true;
            try {
                const response = await fetch('/api/douyin-cookie', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        cookie,
                        reconnect_active: this.cookieForm.reconnect_active
                    })
                });
                const data = await response.json();

                if (response.ok) {
                    this.douyinCookie = {
                        configured: data.configured,
                        length: data.length,
                        health: data.health || this.douyinCookie.health
                    };
                    this.cookieForm.cookie = '';
                    this.cookieSaving = false;
                    this.closeCookieModal();
                    const persistText = data.persisted ? '' : '；但写入 .env 失败';
                    const healthText = data.health && data.health.status === 'healthy'
                        ? '，测活通过' : (data.health && data.health.status === 'dead' ? '，但测活未通过' : '');
                    const toastType = data.health && data.health.status === 'dead' ? 'warning' : 'success';
                    window.HubUI.toast(`Cookie 已更新（同步 ${data.updated_rooms} 个房间${healthText}${persistText}）`, toastType, 4500);
                } else {
                    window.HubUI.toast(data.error || '更新失败', 'error');
                }
            } catch (error) {
                window.HubUI.toast('更新失败: ' + error.message, 'error');
            } finally {
                this.cookieSaving = false;
            }
        },
        async clearCookieConfig() {
            if (this.cookieSaving) return;
            const confirmed = await window.HubUI.confirm({
                title: '清空抖音 Cookie',
                message: '清空后可能无法接收直播间礼物消息，确定要清空吗？',
                confirmText: '确认清空',
                danger: true
            });
            if (!confirmed) return;

            this.cookieSaving = true;
            try {
                const response = await fetch('/api/douyin-cookie', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        cookie: '',
                        reconnect_active: this.cookieForm.reconnect_active
                    })
                });
                const data = await response.json();

                if (response.ok) {
                    this.douyinCookie = {
                        configured: data.configured,
                        length: data.length,
                        health: data.health || this.douyinCookie.health
                    };
                    this.cookieForm.cookie = '';
                    this.cookieSaving = false;
                    this.closeCookieModal();
                    window.HubUI.toast(`Cookie 已清空（同步 ${data.updated_rooms} 个房间）`, 'info');
                } else {
                    window.HubUI.toast(data.error || '清空失败', 'error');
                }
            } catch (error) {
                window.HubUI.toast('清空失败: ' + error.message, 'error');
            } finally {
                this.cookieSaving = false;
            }
        },
        async addRoom() {
            if (this.addingRoom) return;
            const extracted = window.HubUI.extractLiveId(this.newRoom.live_id);
            const liveId = extracted.liveId;
            if (!liveId) {
                window.HubUI.toast('请输入直播间 ID 或粘贴直播间链接', 'warning');
                return;
            }

            this.addingRoom = true;
            try {
                const response = await fetch('/api/rooms', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ live_id: liveId })
                });
                const data = await response.json();

                if (response.ok) {
                    this.addingRoom = false;
                    this.closeAddModal();
                    window.HubUI.toast(`已添加直播间 ${liveId} 并启动监控`, 'success');
                    await this.refreshDashboard();
                } else {
                    window.HubUI.toast(data.error || '添加失败', 'error');
                }
            } catch (error) {
                window.HubUI.toast('添加失败: ' + error.message, 'error');
            } finally {
                this.addingRoom = false;
            }
        },
        async startRoom(liveId) {
            if (this.isRoomBusy(liveId)) return;
            this.setRoomBusy(liveId, true);
            try {
                const response = await fetch(`/api/rooms/${encodeURIComponent(liveId)}/start`, {
                    method: 'POST'
                });
                const data = await response.json();

                if (response.ok) {
                    window.HubUI.toast(`已启动房间 ${liveId} 监控`, 'success');
                    await this.refreshDashboard();
                } else {
                    window.HubUI.toast(data.error || '启动失败', 'error');
                }
            } catch (error) {
                window.HubUI.toast('启动失败: ' + error.message, 'error');
            } finally {
                this.setRoomBusy(liveId, false);
            }
        },
        async stopRoom(liveId) {
            if (this.isRoomBusy(liveId)) return;
            const confirmed = await window.HubUI.confirm({
                title: '停止房间监控',
                message: `确定要停止监控直播间 ${liveId} 吗？`,
                confirmText: '停止监控',
                danger: true
            });
            if (!confirmed) return;

            this.setRoomBusy(liveId, true);
            try {
                const response = await fetch(`/api/rooms/${encodeURIComponent(liveId)}/stop`, {
                    method: 'POST'
                });
                const data = await response.json();

                if (response.ok) {
                    window.HubUI.toast(`已停止房间 ${liveId} 监控`, 'info');
                    await this.refreshDashboard();
                } else {
                    window.HubUI.toast(data.error || '停止失败', 'error');
                }
            } catch (error) {
                window.HubUI.toast('停止失败: ' + error.message, 'error');
            } finally {
                this.setRoomBusy(liveId, false);
            }
        },
        async archiveRoom(roomOrLiveId) {
            const liveId = typeof roomOrLiveId === 'string' ? roomOrLiveId : roomOrLiveId.live_id;
            const label = typeof roomOrLiveId === 'string' ? liveId : (roomOrLiveId.anchor_name || roomOrLiveId.live_id);
            if (this.isRoomBusy(liveId)) return;

            const confirmed = await window.HubUI.confirm({
                title: '归档直播间',
                message: `确定要归档「${label}」吗？归档后会从主页控制台移出，历史弹幕、礼物和统计数据全部保留，可在“归档管理”查看或恢复。`,
                confirmText: '确认归档',
                danger: true
            });
            if (!confirmed) return;

            this.setRoomBusy(liveId, true);
            try {
                const response = await fetch(`/api/rooms/${encodeURIComponent(liveId)}`, {
                    method: 'DELETE'
                });
                const data = await response.json();

                if (response.ok) {
                    window.HubUI.toast(`已归档「${label}」`, 'success');
                    await this.refreshDashboard();
                } else {
                    window.HubUI.toast(data.error || '归档失败', 'error');
                }
            } catch (error) {
                window.HubUI.toast('归档失败: ' + error.message, 'error');
            } finally {
                this.setRoomBusy(liveId, false);
            }
        },
        goToRoom(liveId) {
            window.location.href = `/room/${encodeURIComponent(liveId)}`;
        },
        getMonitorBadgeClass(roomOrStatus) {
            const status = typeof roomOrStatus === 'string'
                ? roomOrStatus
                : (roomOrStatus && roomOrStatus.status);
            switch (status) {
                case 'monitoring': return 'is-live';
                case 'offline':
                case 'waiting': return 'is-waiting';
                case 'error': return 'is-error';
                default: return 'is-stopped';
            }
        },
        getMonitorStatusText(roomOrStatus) {
            const status = typeof roomOrStatus === 'string'
                ? roomOrStatus
                : (roomOrStatus && roomOrStatus.status);
            switch (status) {
                case 'monitoring': return '运行中';
                case 'offline':
                case 'waiting': return '等待开播';
                case 'stopped': return '已停止';
                case 'error': return '异常';
                default: return '未知';
            }
        },
        getRoomStatusNote(room) {
            if (!room || !room.error_message) {
                return '';
            }
            if (['offline', 'waiting', 'error'].includes(room.status)) {
                return room.error_message;
            }
            return '';
        },
        getRoomStatusNoteClass(room) {
            if (!room) {
                return '';
            }
            return room.status === 'error' ? 'is-danger' : 'is-warning';
        },
        getIsMonitoring(room) {
            return room.is_monitor_alive === true;
        }
    }
});
