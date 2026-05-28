/**
 * 房间详情页 - 实时数据页面逻辑
 */
const app = new Vue({
    el: '#app',
    data: {
        liveId: null,
        live_id: null,  // 用于模板显示
        room: null,
        loading: true,
        loadingMessages: false,  // 加载消息时的loading状态（已废弃）
        activeTab: 'all', // all/chat/gift
        activeRankTab: 'gift', // gift/like 右侧栏 tab
        messages: [],
        lastSession: null,  // 上次直播场次
        sessions: [],  // 历史场次列表
        showSessionsModal: false,  // 是否显示历史场次弹窗
        stats: {
            currentUserCount: 0,
            totalUserCount: 0,
            totalLikeCount: 0,
            totalIncome: 0,
            contributorCount: 0,
            contributorInfo: [],
            likeRankInfo: []
        },
        currentSession: null,
        contributors: [],
        socket: null,
        roomInfoRefreshTimer: null,
        isAtBottom: true,  // 用户是否在底部
        unreadCount: 0,  // 未读消息数量
        isUserScrolling: false,  // 用户是否正在手动滚动
        userNameSearch: '',
        userSearchLoading: false,
        userSearchError: '',
        userSearchResults: [],
        userSearchHasSearched: false,
        userSearchFocused: false,
        userSearchDebounceTimer: null,
        userSearchRequestSeq: 0,
        // 用户消息模态框
        showUserMessagesModal: false,
        userMessagesLoading: false,
        userMessagesTab: 'all',
        userMessagesData: {
            user: {},
            stats: {
                total_messages: 0,
                chat_count: 0,
                gift_count: 0,
                like_count: 0,
                total_value: 0
            },
            messages: [],
            pagination: {
                page: 1,
                page_size: 50,
                total: 0,
                total_pages: 1
            }
        },
        userMessagesQuery: {
            user_id: null,
            user_name: null,
            session_id: null  // 用于记录当前查询的场次ID
        },
        maxRealtimeMessages: 500
    },
    computed: {
        isArchived() {
            return this.room && (this.room.is_archived || this.room.status === 'archived' || Boolean(this.room.archived_at));
        },
        canStartMonitoring() {
            return this.room && !this.isArchived && this.room.status !== 'monitoring';
        },
        roomStatusNote() {
            if (!this.room || !this.room.error_message) {
                return '';
            }
            if (['offline', 'waiting', 'error'].includes(this.room.status)) {
                return this.room.error_message;
            }
            return '';
        },
        filteredMessages() {
            if (this.activeTab === 'all') {
                return this.messages;
            } else if (this.activeTab === 'chat') {
                return this.messages.filter(m => m.type === 'chat');
            } else if (this.activeTab === 'gift') {
                return this.messages.filter(m => m.type === 'gift');
            }
            return this.messages;
        }
    },
    watch: {
        userNameSearch(value) {
            if (this.userSearchDebounceTimer) {
                clearTimeout(this.userSearchDebounceTimer);
            }

            const keyword = (value || '').trim();
            this.userSearchError = '';

            if (!keyword) {
                this.userSearchRequestSeq++;
                this.userSearchResults = [];
                this.userSearchHasSearched = false;
                this.userSearchLoading = false;
                return;
            }

            this.userSearchDebounceTimer = setTimeout(() => {
                this.searchUserByName({ silent: true });
            }, 350);
        }
    },
    mounted() {
        console.log('=== Vue mounted 开始 ===');
        this.liveId = document.querySelector('meta[name="live-id"]').content;
        this.live_id = this.liveId;

        console.log('liveId:', this.liveId);
        console.log('Vue 实例:', this);

        // 并行加载数据
        this.loadRoomInfo();
        this.loadCurrentSession();
        // 不再加载历史消息，只显示新推送的
        this.loadSessionContributors();
        this.initSocket();
        this.roomInfoRefreshTimer = setInterval(() => {
            this.loadRoomInfo({ loadHistory: false });
        }, 10000);

        // 添加滚动监听
        this.$nextTick(() => {
            const container = document.querySelector('.messages-container');
            if (container) {
                container.addEventListener('scroll', this.handleScroll);
                // 点击消息中的用户名，打开用户消息模态框
                container.addEventListener('click', (e) => {
                    const userEl = e.target.closest('.user-highlight');
                    if (userEl && userEl.dataset.userId) {
                        this.openUserMessagesModal(userEl.dataset.userId, userEl.dataset.userName || userEl.textContent);
                    }
                });
            }
        });

        console.log('=== Vue mounted 结束 ===');
    },
    beforeDestroy() {
        if (this.socket) {
            this.socket.disconnect();
        }
        if (this.userSearchDebounceTimer) {
            clearTimeout(this.userSearchDebounceTimer);
        }
        if (this.roomInfoRefreshTimer) {
            clearInterval(this.roomInfoRefreshTimer);
        }
        // 移除滚动监听
        const container = document.querySelector('.messages-container');
        if (container) {
            container.removeEventListener('scroll', this.handleScroll);
        }
    },
    methods: {
        async loadRoomInfo(options = {}) {
            try {
                const response = await fetch(`/api/rooms/${encodeURIComponent(this.liveId)}`);
                const data = await response.json();

                if (data.room) {
                    this.room = data.room;
                    if (data.room.stats) {
                        this.mergeStats(data.room.stats, { preserveEmptyContributors: true });
                    }
                    // 更新页面标题
                    const anchorName = data.room.anchor_name || this.liveId;
                    document.title = `${anchorName} - 直播详情`;
                }

                // 加载历史场次数据（用于未开播时显示）
                if (options.loadHistory !== false) {
                    await this.loadHistorySessions();
                }
            } catch (error) {
                console.error('加载房间信息失败:', error);
            } finally {
                this.loading = false;
            }
        },
        async loadCurrentSession() {
            try {
                const response = await fetch(`/api/rooms/${encodeURIComponent(this.liveId)}/current-session`);
                const data = await response.json();
                if (data.session && data.session.status === 'live') {
                    // 只有正在直播的场次才显示为"当场直播"
                    this.currentSession = data.session;
                } else {
                    // 直播已结束或没有场次，设置为 null
                    this.currentSession = null;
                }
            } catch (error) {
                console.error('加载当前直播场次失败:', error);
            }
        },
        async loadHistorySessions() {
            try {
                const response = await fetch(`/api/rooms/${encodeURIComponent(this.liveId)}/sessions?limit=10`);
                const data = await response.json();

                if (data.sessions) {
                    this.sessions = data.sessions;

                    if (data.sessions.length === 0) {
                        return;
                    }

                    // 找出最近已结束的场次作为"上次直播"
                    const endedSessions = data.sessions.filter(s => s.status === 'ended');

                    if (endedSessions.length > 0) {
                        this.lastSession = endedSessions[0];
                    } else {
                        // 如果没有已结束的场次，使用最近的场次（可能是正在进行中的）
                        this.lastSession = data.sessions[0];
                    }
                }
            } catch (error) {
                console.error('加载历史场次失败:', error);
            }
        },
        async startMonitoring() {
            try {
                const response = await fetch(`/api/rooms/${encodeURIComponent(this.liveId)}/start`, {
                    method: 'POST'
                });
                const data = await response.json();

                if (response.ok) {
                    this.loadRoomInfo();
                } else {
                    alert(data.error || '启动失败');
                }
            } catch (error) {
                alert('启动失败: ' + error.message);
            }
        },
        async stopMonitoring() {
            if (!confirm('确定要停止记录吗？')) return;

            try {
                const response = await fetch(`/api/rooms/${encodeURIComponent(this.liveId)}/stop`, {
                    method: 'POST'
                });
                const data = await response.json();

                if (response.ok) {
                    this.loadRoomInfo();
                } else {
                    alert(data.error || '停止失败');
                }
            } catch (error) {
                alert('停止失败: ' + error.message);
            }
        },
        async restoreRoom() {
            if (!confirm('确定要把这个直播间恢复到主页吗？恢复后可重新启动记录。')) return;

            try {
                const response = await fetch(`/api/rooms/${encodeURIComponent(this.liveId)}/restore`, {
                    method: 'POST'
                });
                const data = await response.json();

                if (response.ok) {
                    await this.loadRoomInfo();
                } else {
                    alert(data.error || '恢复失败');
                }
            } catch (error) {
                alert('恢复失败: ' + error.message);
            }
        },
        async loadContributors() {
            try {
                const response = await fetch(`/api/rooms/${encodeURIComponent(this.liveId)}/contributors?limit=100`);
                const data = await response.json();

                if (data.contributors) {
                    this.contributors = data.contributors;
                }
            } catch (error) {
                console.error('加载贡献榜失败:', error);
            }
        },
        async loadSessionContributors() {
            // 首次加载当前场次贡献榜，避免等待 Socket.IO 推送
            try {
                const response = await fetch(`/api/rooms/${encodeURIComponent(this.liveId)}/session-contributors?limit=100`);
                const data = await response.json();

                if (data.contributors && data.contributors.length > 0) {
                    // 转换格式为 stats.contributorInfo 需要的格式
                    this.mergeStats({
                        contributorCount: data.contributors.length,
                        contributorInfo: data.contributors.map((c, index) => ({
                            rank: index + 1,
                            user_id: c.user_id,
                            user: c.nickname || c.user_id,
                            score: c.contribution_value,
                            avatar: c.user_avatar,
                            user_level: c.user_level || 0,
                            fans_club_level: c.fans_club_level || 0
                        }))
                    });
                }
            } catch (error) {
                console.error('加载场次贡献榜失败:', error);
            }
        },
        initSocket() {
            if (this.socket) {
                this.socket.disconnect();
            }

            this.socket = io();

            this.socket.on('connect', () => {
                console.log('Socket.IO连接已建立');
                // 加入房间
                this.socket.emit('join', { live_id: this.liveId });
            });

            this.socket.on('disconnect', () => {
                console.log('Socket.IO连接已断开');
            });

            this.socket.on(`room_${this.liveId}`, (data) => {
                this.handleMessage(data);
            });

            this.socket.on(`room_${this.liveId}_stats`, (data) => {
                // 实时更新运行状态
                if (this.room) {
                    if (data.room_status) {
                        this.room.status = data.room_status;
                    }
                    if (Object.prototype.hasOwnProperty.call(data, 'room_error_message')) {
                        this.room.error_message = data.room_error_message;
                    }
                }

                this.mergeStats(data);

                // 实时更新当前场次数据 - 只有正在直播的才显示
                if (data.current_session && data.current_session.status === 'live') {
                    this.currentSession = data.current_session;
                } else if (!data.current_session || data.current_session.status !== 'live') {
                    // 直播结束或没有场次，清空当场直播显示
                    this.currentSession = null;
                }
            });
        },
        mergeStats(rawStats, options = {}) {
            if (!rawStats) return;

            const pick = (camelKey, snakeKey) => {
                if (Object.prototype.hasOwnProperty.call(rawStats, camelKey)) {
                    return rawStats[camelKey];
                }
                if (Object.prototype.hasOwnProperty.call(rawStats, snakeKey)) {
                    return rawStats[snakeKey];
                }
                return undefined;
            };

            const nextStats = {
                currentUserCount: this.stats.currentUserCount,
                totalUserCount: this.stats.totalUserCount,
                totalLikeCount: this.stats.totalLikeCount,
                totalIncome: this.stats.totalIncome,
                contributorCount: this.stats.contributorCount,
                contributorInfo: this.stats.contributorInfo || [],
                likeRankInfo: this.stats.likeRankInfo || []
            };

            [
                ['currentUserCount', 'current_user_count'],
                ['totalUserCount', 'total_user_count'],
                ['totalLikeCount', 'total_like_count'],
                ['totalIncome', 'total_income'],
                ['contributorCount', 'contributor_count']
            ].forEach(([camelKey, snakeKey]) => {
                const value = pick(camelKey, snakeKey);
                if (value !== undefined && value !== null && value !== '') {
                    nextStats[camelKey] = value;
                }
            });

            const contributorInfo = pick('contributorInfo', 'contributor_info');
            if (Array.isArray(contributorInfo)) {
                const shouldKeepExistingList = (
                    contributorInfo.length === 0 &&
                    nextStats.contributorInfo.length > 0 &&
                    (
                        options.preserveEmptyContributors ||
                        Number(nextStats.contributorCount || 0) > 0
                    )
                );

                if (!shouldKeepExistingList) {
                    nextStats.contributorInfo = contributorInfo;
                }
            }

            const likeRankInfo = pick('likeRankInfo', 'like_rank_list');
            if (Array.isArray(likeRankInfo)) {
                const shouldKeepExistingLikeList = (
                    likeRankInfo.length === 0 &&
                    nextStats.likeRankInfo.length > 0 &&
                    options.preserveEmptyContributors
                );

                if (!shouldKeepExistingLikeList) {
                    nextStats.likeRankInfo = likeRankInfo;
                }
            }

            this.stats = nextStats;
        },
        handleMessage(data) {
            // 添加到消息列表（新消息在末尾）
            this.messages.push({
                ...data,
                user_id: data.user_id || null,
                timestamp: new Date()
            });

            if (this.messages.length > this.maxRealtimeMessages) {
                this.messages.splice(0, this.messages.length - this.maxRealtimeMessages);
            }

            // 智能滚动：只有当用户在底部时才自动滚动
            this.$nextTick(() => {
                const container = document.querySelector('.messages-container');
                if (container) {
                    if (this.isAtBottom) {
                        container.scrollTop = container.scrollHeight;
                    } else {
                        this.unreadCount++;
                    }
                }
            });
        },
        handleScroll() {
            const container = document.querySelector('.messages-container');
            if (!container) return;

            const threshold = 50;  // 距离底部50px内视为在底部
            const isBottom = container.scrollHeight - container.scrollTop - container.clientHeight < threshold;

            if (isBottom !== this.isAtBottom) {
                this.isAtBottom = isBottom;
                if (isBottom) {
                    this.unreadCount = 0;  // 滚动到底部时清空未读数
                }
            }
        },
        scrollToBottom() {
            const container = document.querySelector('.messages-container');
            if (container) {
                container.scrollTop = container.scrollHeight;
                this.isAtBottom = true;
                this.unreadCount = 0;
            }
        },
        setTab(tab) {
            this.activeTab = tab;
        },
        setRankTab(tab) {
            this.activeRankTab = tab;
        },
        formatIncome(value) {
            return value ? value.toLocaleString() + ' 钻石' : '0 钻石';
        },
        formatNumber(value) {
            if (!value) return '0';
            value = Number(value);
            if (value >= 100000000) {
                return (value / 100000000).toFixed(1) + '亿';
            } else if (value >= 10000) {
                return (value / 10000).toFixed(1) + '万';
            } else {
                return value.toLocaleString();
            }
        },
        formatTime(date) {
            if (!date) return '';
            const d = new Date(date);
            return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
        },
        getStatusClass(status) {
            switch (status) {
                case 'monitoring': return 'bg-green-100 text-green-800';
                case 'stopped': return 'bg-gray-100 text-gray-800';
                case 'offline': return 'bg-yellow-100 text-yellow-800';
                case 'waiting': return 'bg-yellow-100 text-yellow-800';
                case 'error': return 'bg-red-100 text-red-800';
                case 'archived': return 'bg-blue-100 text-blue-800';
                default: return 'bg-gray-100 text-gray-800';
            }
        },
        getStatusText(status) {
            switch (status) {
                case 'monitoring': return '运行中';
                case 'stopped': return '已停止';
                case 'offline': return '等待中';
                case 'waiting': return '等待中';
                case 'error': return '错误';
                case 'archived': return '已归档';
                default: return status;
            }
        },
        getMessageClass(message) {
            if (message.type === 'gift') {
                return 'gift-message';
            } else if (message.is_gift_user) {
                return 'gift-user-message';
            }
            return 'chat-message';
        },
        getSessionStatusClass(status) {
            switch (status) {
                case 'live': return 'bg-green-100 text-green-800';
                case 'ended': return 'bg-gray-100 text-gray-800';
                default: return 'bg-gray-100 text-gray-800';
            }
        },
        getSessionStatusText(status) {
            switch (status) {
                case 'live': return '直播中';
                case 'ended': return '已结束';
                default: return status;
            }
        },
        formatDateTime(dateStr) {
            if (!dateStr) return '-';
            const d = new Date(dateStr);
            return d.toLocaleString('zh-CN', {
                year: 'numeric',
                month: '2-digit',
                day: '2-digit',
                hour: '2-digit',
                minute: '2-digit'
            });
        },
        getDuration(startTime, endTime) {
            if (!startTime) return '-';
            const start = new Date(startTime);
            const end = endTime ? new Date(endTime) : new Date();
            const diff = end - start;

            const hours = Math.floor(diff / (1000 * 60 * 60));
            const minutes = Math.floor((diff % (1000 * 60 * 60)) / (1000 * 60));

            if (hours > 0) {
                return `${hours}小时${minutes}分钟`;
            }
            return `${minutes}分钟`;
        },
        getRankClass(rank) {
            if (rank === 1) return 'rank-1';
            if (rank === 2) return 'rank-2';
            if (rank === 3) return 'rank-3';
            return 'rank-other';
        },
        resetUserMessagesData(userId = null, userName = null) {
            this.userMessagesData = {
                user: {
                    user_id: userId,
                    nickname: userName
                },
                stats: {
                    total_messages: 0,
                    chat_count: 0,
                    gift_count: 0,
                    like_count: 0,
                    total_value: 0
                },
                messages: [],
                pagination: {
                    page: 1,
                    page_size: 50,
                    total: 0,
                    total_pages: 1
                }
            };
        },
        async searchUserByName(options = {}) {
            const userName = (this.userNameSearch || '').trim();
            this.userSearchError = '';
            this.userSearchResults = [];
            this.userSearchHasSearched = false;

            if (!userName) {
                if (!options.silent) {
                    this.userSearchError = '请输入用户名';
                }
                return;
            }
            if (userName.length > 100) {
                this.userSearchError = '用户名过长';
                return;
            }

            this.userSearchLoading = true;
            const requestSeq = ++this.userSearchRequestSeq;
            try {
                const params = new URLSearchParams({
                    user_name: userName,
                    limit: 30
                });
                if (this.currentSession && this.currentSession.id) {
                    params.append('session_id', this.currentSession.id);
                }
                const response = await fetch(`/api/rooms/${encodeURIComponent(this.liveId)}/user-search?${params}`);
                const data = await response.json();
                if (!response.ok) {
                    throw new Error(data.error || '搜索失败');
                }
                if (requestSeq !== this.userSearchRequestSeq) {
                    return;
                }
                this.userSearchResults = data.users || [];
                this.userSearchHasSearched = true;
                if (this.userSearchResults.length === 0) {
                    this.userSearchError = '未找到匹配用户';
                }
            } catch (error) {
                if (requestSeq !== this.userSearchRequestSeq) {
                    return;
                }
                console.error('搜索用户失败:', error);
                this.userSearchError = error.message || '搜索失败，请稍后重试';
            } finally {
                if (requestSeq === this.userSearchRequestSeq) {
                    this.userSearchLoading = false;
                }
            }
        },
        selectSearchedUser(user) {
            this.userSearchError = '';
            this.userSearchFocused = false;
            this.userSearchResults = [];
            this.openUserMessagesModal(
                user.user_id,
                user.nickname || user.user_id,
                user.session_id || (this.currentSession ? this.currentSession.id : null)
            );
        },
        // 用户消息模态框方法
        async openUserMessagesModal(userId, userName, sessionId = null) {
            this.showUserMessagesModal = true;
            this.userMessagesLoading = true;
            this.userMessagesTab = 'all';
            this.userMessagesQuery.user_id = userId;
            this.userMessagesQuery.user_name = null;
            this.userMessagesQuery.session_id = sessionId || (this.currentSession ? this.currentSession.id : null);

            // 重置数据
            this.resetUserMessagesData(userId, userName);

            await this.loadUserMessages();
        },
        closeUserMessagesModal() {
            this.showUserMessagesModal = false;
            this.resetUserMessagesData();
            this.userMessagesQuery = {
                user_id: null,
                user_name: null,
                session_id: null
            };
        },
        async loadUserMessages(options = {}) {
            const showLoading = options.showLoading !== false;
            if (!this.userMessagesQuery.user_id && !this.userMessagesQuery.user_name) return false;

            if (showLoading) {
                this.userMessagesLoading = true;
            }
            try {
                const params = new URLSearchParams({
                    type: this.userMessagesTab,
                    page: this.userMessagesData.pagination.page,
                    limit: this.userMessagesData.pagination.page_size
                });
                if (this.userMessagesQuery.user_id) {
                    params.append('user_id', this.userMessagesQuery.user_id);
                } else {
                    params.append('user_name', this.userMessagesQuery.user_name);
                }

                if (this.userMessagesQuery.session_id) {
                    params.append('session_id', this.userMessagesQuery.session_id);
                }

                const response = await fetch(`/api/rooms/${encodeURIComponent(this.liveId)}/user-messages?${params}`);
                const data = await response.json();

                if (data.user) {
                    this.userMessagesData.user = data.user;
                }
                if (data.stats) {
                    this.userMessagesData.stats = data.stats;
                }
                if (data.messages) {
                    this.userMessagesData.messages = data.messages;
                }
                if (data.pagination) {
                    this.userMessagesData.pagination = data.pagination;
                }
                return (data.stats && data.stats.total_messages > 0) || (data.messages && data.messages.length > 0);
            } catch (error) {
                console.error('加载用户消息失败:', error);
                this.userSearchError = '搜索失败，请稍后重试';
                return false;
            } finally {
                if (showLoading) {
                    this.userMessagesLoading = false;
                }
            }
        },
        async switchUserMessagesTab(tab) {
            if (this.userMessagesTab === tab) return;
            this.userMessagesTab = tab;
            this.userMessagesData.pagination.page = 1;
            await this.loadUserMessages();
        },
        async userMessagesGoToPage(page) {
            const pagination = this.userMessagesData.pagination;
            if (page < 1 || page > pagination.total_pages) return;
            pagination.page = page;
            await this.loadUserMessages();
        },
        formatUserMessageTime(dateStr) {
            if (!dateStr) return '';
            const d = new Date(dateStr);
            const MM = String(d.getMonth() + 1).padStart(2, '0');
            const DD = String(d.getDate()).padStart(2, '0');
            const time = d.toLocaleTimeString('zh-CN', {
                hour: '2-digit',
                minute: '2-digit',
                second: '2-digit'
            });
            return `${MM}-${DD} ${time}`;
        },
        formatAgeRange(val) {
            const map = {0: '-', 1: '<18', 2: '18-23', 3: '24-30', 4: '31-40', 5: '41-50', 6: '>50'};
            return map[val] || '-';
        },
        // 历史场次相关方法
        openSessionsModal() {
            this.showSessionsModal = true;
        },
        closeSessionsModal() {
            this.showSessionsModal = false;
        },
        getSessionEndTime(session) {
            if (session.end_time) {
                return this.formatDateTime(session.end_time);
            }
            return '进行中';
        }
    }
});
