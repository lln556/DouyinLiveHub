/**
 * 归档管理页面逻辑
 */
const app = new Vue({
    el: '#app',
    data: {
        loading: true,
        rooms: [],
        error: '',
        searchKeyword: '',
        busyRooms: {}
    },
    computed: {
        filteredRooms() {
            const kw = (this.searchKeyword || '').trim().toLowerCase();
            if (!kw) return this.rooms;
            return this.rooms.filter(room => {
                const name = String(room.anchor_name || '').toLowerCase();
                const lid = String(room.live_id || '').toLowerCase();
                return name.includes(kw) || lid.includes(kw);
            });
        },
        summary() {
            return this.rooms.reduce((acc, room) => {
                acc.totalRooms += 1;
                acc.totalSessions += Number(room.total_sessions || 0);
                acc.totalIncome += Number(room.total_income || 0);
                acc.totalMessages += Number(room.total_chat_count || 0);
                return acc;
            }, {
                totalRooms: 0,
                totalSessions: 0,
                totalIncome: 0,
                totalMessages: 0
            });
        }
    },
    mounted() {
        this.loadArchivedRooms();
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
        async loadArchivedRooms() {
            this.loading = true;
            this.error = '';
            try {
                const response = await fetch('/api/rooms/archived');
                const data = await response.json();
                if (!response.ok) {
                    throw new Error(data.error || '加载历史数据失败');
                }
                this.rooms = data.rooms || [];
            } catch (error) {
                console.error('加载历史数据失败:', error);
                this.error = error.message || '加载历史数据失败';
            } finally {
                this.loading = false;
            }
        },
        async restoreRoom(roomOrLiveId) {
            const liveId = typeof roomOrLiveId === 'string' ? roomOrLiveId : roomOrLiveId.live_id;
            const label = typeof roomOrLiveId === 'string' ? liveId : (roomOrLiveId.anchor_name || roomOrLiveId.live_id);
            if (this.isRoomBusy(liveId)) return;

            const confirmed = await window.HubUI.confirm({
                title: '恢复直播间到控制台',
                message: `确定要把「${label}」恢复到首页控制台吗？恢复后可随时启动监控。`,
                confirmText: '确认恢复'
            });
            if (!confirmed) return;

            this.setRoomBusy(liveId, true);
            try {
                const response = await fetch(`/api/rooms/${encodeURIComponent(liveId)}/restore`, {
                    method: 'POST'
                });
                const data = await response.json();
                if (!response.ok) {
                    throw new Error(data.error || '恢复失败');
                }
                window.HubUI.toast(`已恢复「${label}」到控制台`, 'success');
                await this.loadArchivedRooms();
            } catch (error) {
                window.HubUI.toast('恢复失败: ' + error.message, 'error');
            } finally {
                this.setRoomBusy(liveId, false);
            }
        },
        async hardDeleteRoom(roomOrLiveId) {
            const liveId = typeof roomOrLiveId === 'string' ? roomOrLiveId : roomOrLiveId.live_id;
            const anchorName = typeof roomOrLiveId === 'string' ? '' : (roomOrLiveId.anchor_name || '');
            if (this.isRoomBusy(liveId)) return;

            const confirmed = await window.HubUI.confirmHardDelete({ liveId, anchorName });
            if (!confirmed) return;

            this.setRoomBusy(liveId, true);
            try {
                const response = await fetch(`/api/rooms/${encodeURIComponent(liveId)}/hard-delete`, {
                    method: 'DELETE',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ confirm_live_id: liveId })
                });
                const data = await response.json();
                if (!response.ok) {
                    throw new Error(data.error || '永久删除失败');
                }
                window.HubUI.toast(`已永久删除直播间 ${liveId}`, 'success');
                await this.loadArchivedRooms();
            } catch (error) {
                window.HubUI.toast('永久删除失败: ' + error.message, 'error');
            } finally {
                this.setRoomBusy(liveId, false);
            }
        },
        goToRoom(liveId) {
            window.location.href = `/room/${encodeURIComponent(liveId)}`;
        },
        formatNumber(value) {
            const number = Number(value || 0);
            return new Intl.NumberFormat('zh-CN', {
                maximumFractionDigits: 0
            }).format(number);
        },
        formatDateTime(value) {
            if (!value) return '-';
            const date = new Date(value);
            if (Number.isNaN(date.getTime())) return '-';
            return date.toLocaleString('zh-CN', {
                year: 'numeric',
                month: '2-digit',
                day: '2-digit',
                hour: '2-digit',
                minute: '2-digit'
            });
        },
        formatDate(value) {
            if (!value) return '';
            const date = new Date(value);
            if (Number.isNaN(date.getTime())) return '';
            return date.toLocaleDateString('zh-CN', {
                year: 'numeric',
                month: '2-digit',
                day: '2-digit'
            });
        },
        formatDateRange(start, end) {
            if (!start && !end) return '暂无数据';
            const startText = this.formatDate(start);
            const endText = this.formatDate(end);
            if (!startText) return endText || '暂无数据';
            if (!endText || startText === endText) return startText;
            return `${startText} - ${endText}`;
        }
    }
});
