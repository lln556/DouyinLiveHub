/**
 * 历史数据页面逻辑
 */
const app = new Vue({
    el: '#app',
    data: {
        loading: true,
        rooms: [],
        error: ''
    },
    computed: {
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
        async restoreRoom(liveId) {
            if (!confirm('确定要把这个直播间恢复到主页吗？恢复后可重新启动监控。')) return;

            try {
                const response = await fetch(`/api/rooms/${encodeURIComponent(liveId)}/restore`, {
                    method: 'POST'
                });
                const data = await response.json();
                if (!response.ok) {
                    throw new Error(data.error || '恢复失败');
                }
                await this.loadArchivedRooms();
            } catch (error) {
                alert('恢复失败: ' + error.message);
            }
        },
        async hardDeleteRoom(liveId) {
            const confirmedLiveId = prompt(`永久删除会删除直播间 ${liveId} 的所有弹幕、礼物、统计、用户贡献和场次数据，无法恢复。\n\n请输入直播间ID确认永久删除：`);
            if (confirmedLiveId !== liveId) {
                if (confirmedLiveId !== null) {
                    alert('输入的直播间ID不匹配，已取消永久删除');
                }
                return;
            }

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
                await this.loadArchivedRooms();
            } catch (error) {
                alert('永久删除失败: ' + error.message);
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
