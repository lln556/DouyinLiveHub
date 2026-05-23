/**
 * 首页 - 房间列表页面逻辑
 */
const app = new Vue({
    el: '#app',
    data: {
        loading: true,
        showAddModal: false,
        showProxyModal: false,
        showCookieModal: false,
        cookieSaving: false,
        showEditModal: false,
        newRoom: {
            live_id: ''
        },
        editRoom: {
            live_id: '',
            anchor_name: '',
            status: ''
        },
        rooms: [],
        stats: {
            total_rooms: 0,
            monitoring_rooms: 0,
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
            length: 0
        },
        cookieForm: {
            cookie: '',
            reconnect_active: true
        },
        error: null
    },
    mounted() {
        this.loadRooms();
        this.loadStats();
        this.loadProxyConfig();
        this.loadCookieConfig();
        // 定时刷新状态
        setInterval(() => {
            this.loadRooms();
            this.loadStats();
        }, 5000);
    },
    methods: {
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
                this.stats = data;
            } catch (error) {
                console.error('加载统计数据失败:', error);
            }
        },
        async loadProxyConfig() {
            try {
                const response = await fetch('/api/proxy');
                const data = await response.json();
                this.proxy = data;
            } catch (error) {
                console.error('加载代理配置失败:', error);
            }
        },
        async loadCookieConfig() {
            try {
                const response = await fetch('/api/douyin-cookie');
                const data = await response.json();
                this.douyinCookie = data;
            } catch (error) {
                console.error('加载Cookie配置失败:', error);
            }
        },
        openAddModal() {
            this.showAddModal = true;
            this.newRoom = {
                live_id: ''
            };
        },
        closeAddModal() {
            this.showAddModal = false;
        },
        openProxyModal() {
            this.showProxyModal = true;
        },
        closeProxyModal() {
            this.showProxyModal = false;
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
            this.showCookieModal = false;
        },
        openEditModal(room) {
            this.showEditModal = true;
            this.editRoom = {
                live_id: room.live_id,
                anchor_name: room.anchor_name,
                status: room.status
            };
        },
        closeEditModal() {
            this.showEditModal = false;
        },
        async updateProxyConfig() {
            try {
                const response = await fetch('/api/proxy', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(this.proxy)
                });
                const data = await response.json();

                if (response.ok) {
                    this.proxy = data;
                    this.closeProxyModal();
                    alert('代理配置已更新，重启监控后生效');
                } else {
                    alert(data.error || '更新失败');
                }
            } catch (error) {
                alert('更新失败: ' + error.message);
            }
        },
        async updateCookieConfig() {
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
                        length: data.length
                    };
                    this.cookieForm.cookie = '';
                    this.closeCookieModal();
                    const persistText = data.persisted ? '' : '；但写入 .env 失败，重启后不会保留';
                    alert(`Cookie已更新，已同步 ${data.updated_rooms} 个运行中房间${persistText}`);
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
                        length: data.length
                    };
                    this.cookieForm.cookie = '';
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
            if (!this.newRoom.live_id) {
                alert('请输入直播间ID');
                return;
            }

            try {
                const response = await fetch('/api/rooms', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(this.newRoom)
                });
                const data = await response.json();

                if (response.ok) {
                    this.closeAddModal();
                    this.loadRooms();
                    this.loadStats();
                } else {
                    alert(data.error || '添加失败');
                }
            } catch (error) {
                alert('添加失败: ' + error.message);
            }
        },
        async startRoom(liveId) {
            try {
                const response = await fetch(`/api/rooms/${encodeURIComponent(liveId)}/start`, {
                    method: 'POST'
                });
                const data = await response.json();

                if (response.ok) {
                    this.loadRooms();
                } else {
                    alert(data.error || '启动失败');
                }
            } catch (error) {
                alert('启动失败: ' + error.message);
            }
        },
        async stopRoom(liveId) {
            if (!confirm('确定要停止监控吗？')) return;

            try {
                const response = await fetch(`/api/rooms/${encodeURIComponent(liveId)}/stop`, {
                    method: 'POST'
                });
                const data = await response.json();

                if (response.ok) {
                    this.loadRooms();
                } else {
                    alert(data.error || '停止失败');
                }
            } catch (error) {
                alert('停止失败: ' + error.message);
            }
        },
        async archiveRoom(liveId) {
            if (!confirm('确定要归档此房间吗？归档后会从主页移除，但历史弹幕、礼物、统计和场次数据都会保留，可在“历史数据”中查看或恢复。')) return;

            try {
                const response = await fetch(`/api/rooms/${encodeURIComponent(liveId)}`, {
                    method: 'DELETE'
                });
                const data = await response.json();

                if (response.ok) {
                    this.loadRooms();
                    this.loadStats();
                } else {
                    alert(data.error || '归档失败');
                }
            } catch (error) {
                alert('归档失败: ' + error.message);
            }
        },
        goToRoom(liveId) {
            window.location.href = `/room/${encodeURIComponent(liveId)}`;
        },
        getStatusClass(status) {
            // 兼容旧版，使用 monitor_status
            return this.getMonitorStatusClass(status);
        },
        getStatusText(status) {
            // 兼容旧版，使用 monitor_status
            return this.getMonitorStatusText(status);
        },
        getMonitorStatusClass(room) {
            // 基于 status 字段判断监控状态
            switch (room.status) {
                case 'monitoring': return 'bg-green-100 text-green-800';
                case 'offline': return 'bg-yellow-100 text-yellow-800';
                case 'stopped': return 'bg-gray-100 text-gray-800';
                case 'error': return 'bg-red-100 text-red-800';
                default: return 'bg-gray-100 text-gray-800';
            }
        },
        getMonitorStatusText(room) {
            // 基于 status 字段判断监控状态
            switch (room.status) {
                case 'monitoring': return '监控中';
                case 'offline': return '等待中';
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
            // 判断是否正在监控：监控线程运行中
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
        },
        formatIncome(value) {
            return value ? value.toLocaleString() + ' 钻石' : '0 钻石';
        }
    }
});
