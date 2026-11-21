/**
 * 首页 - 房间列表页面逻辑
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
        }
    },
    methods: {
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
                } else {
                    alert(data.error || '检测失败');
                }
            } catch (error) {
                alert('检测失败: ' + error.message);
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
                    alert('代理配置已更新（仅当前运行时生效）：新连接与测活立即生效，已接入房间需重启监控后生效');
                } else {
                    alert(data.error || '更新失败');
                }
            } catch (error) {
                alert('更新失败: ' + error.message);
            } finally {
                this.savingProxy = false;
            }
        },
        async updateCookieConfig() {
            if (this.cookieSaving) return;
            const cookie = (this.cookieForm.cookie || '').trim();
            if (!cookie) {
                alert('请粘贴完整 Cookie；如需清空请点击“清空 Cookie”');
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
                    const persistText = data.persisted ? '' : '；但写入 .env 失败，重启后不会保留';
                    const healthText = data.health && data.health.status === 'healthy'
                        ? '，测活通过 ✓' : (data.health && data.health.status === 'dead' ? '，但测活未通过 ✗' : '');
                    alert(`Cookie已更新，已同步 ${data.updated_rooms} 个运行中房间${healthText}${persistText}`);
                } else {
                    alert(data.error || '更新失败');
                }
            } catch (error) {
                alert('更新失败: ' + error.message);
            } finally {
                this.cookieSaving = false;
            }
        },
        async clearCookieConfig() {
            if (this.cookieSaving) return;
            if (!confirm('确定要清空抖音 Cookie 吗？清空后可能无法接收礼物消息。')) return;

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
                    const persistText = data.persisted ? '' : '；但写入 .env 失败，重启后不会保留';
                    alert(`Cookie已清空，已同步 ${data.updated_rooms} 个运行中房间${persistText}`);
                } else {
                    alert(data.error || '清空失败');
                }
            } catch (error) {
                alert('清空失败: ' + error.message);
            } finally {
                this.cookieSaving = false;
            }
        },
        async addRoom() {
            if (this.addingRoom) return;
            const liveId = (this.newRoom.live_id || '').trim();
            if (!liveId) {
                alert('请输入直播间ID');
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
                    await this.refreshDashboard();
                } else {
                    alert(data.error || '添加失败');
                }
            } catch (error) {
                alert('添加失败: ' + error.message);
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
                    await this.loadRooms();
                } else {
                    alert(data.error || '启动失败');
                }
            } catch (error) {
                alert('启动失败: ' + error.message);
            } finally {
                this.setRoomBusy(liveId, false);
            }
        },
        async stopRoom(liveId) {
            if (this.isRoomBusy(liveId)) return;
            if (!confirm('确定要停止记录吗？')) return;

            this.setRoomBusy(liveId, true);
            try {
                const response = await fetch(`/api/rooms/${encodeURIComponent(liveId)}/stop`, {
                    method: 'POST'
                });
                const data = await response.json();

                if (response.ok) {
                    await this.loadRooms();
                } else {
                    alert(data.error || '停止失败');
                }
            } catch (error) {
                alert('停止失败: ' + error.message);
            } finally {
                this.setRoomBusy(liveId, false);
            }
        },
        async archiveRoom(liveId) {
            if (this.isRoomBusy(liveId)) return;
            if (!confirm('确定要归档此房间吗？归档后会从主页移除，但历史弹幕、礼物、统计和场次数据都会保留，可在“历史数据”中查看或恢复。')) return;

            this.setRoomBusy(liveId, true);
            try {
                const response = await fetch(`/api/rooms/${encodeURIComponent(liveId)}`, {
                    method: 'DELETE'
                });
                const data = await response.json();

                if (response.ok) {
                    await this.refreshDashboard();
                } else {
                    alert(data.error || '归档失败');
                }
            } catch (error) {
                alert('归档失败: ' + error.message);
            } finally {
                this.setRoomBusy(liveId, false);
            }
        },
        goToRoom(liveId) {
            window.location.href = `/room/${encodeURIComponent(liveId)}`;
        },
        getMonitorStatusClass(roomOrStatus) {
            const status = typeof roomOrStatus === 'string'
                ? roomOrStatus
                : (roomOrStatus && roomOrStatus.status);
            switch (status) {
                case 'monitoring': return 'bg-green-100 text-green-800';
                case 'offline':
                case 'waiting': return 'bg-yellow-100 text-yellow-800';
                case 'stopped': return 'bg-gray-100 text-gray-800';
                case 'error': return 'bg-red-100 text-red-800';
                default: return 'bg-gray-100 text-gray-800';
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
                case 'error': return '错误';
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
        },
        getLiveStatusClass(status) {
            switch (status) {
                case 'live': return 'bg-green-100 text-green-800';
                case 'offline': return 'bg-gray-100 text-gray-800';
                default: return 'bg-gray-100 text-gray-800';
            }
        },
        getLiveStatusText(status) {
            switch (status) {
                case 'live': return '直播中';
                case 'offline': return '离线';
                default: return '未知';
            }
        }
    }
});
