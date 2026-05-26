/**
 * 直播数据统计页面逻辑
 */
const app = new Vue({
    el: '#app',
    data: {
        rooms: [],
        selectedRoomId: '',
        timeRange: '7days',
        customStartDate: '',
        customEndDate: '',
        selectedMonth: '',
        selectedYear: '',
        minDate: '',  // 可选的最早日期
        maxDate: '',  // 可选的最晚日期
        stats: {},
        sessions: [],
        contributors: [],  // 贡献榜
        contributorPagination: {  // 贡献榜分页
            page: 1,
            page_size: 20,
            total: 0,
            total_pages: 1
        },
        // 累积点赞榜
        activeRankTab: 'gift',  // 'gift' | 'like'
        likers: [],
        likersLoading: false,
        loading: true,
        hasSearched: false,
        userNameSearch: '',
        userSearchLoading: false,
        userSearchError: '',
        userSearchResults: [],
        userSearchHasSearched: false,
        userSearchFocused: false,
        userSearchDebounceTimer: null,
        userSearchRequestSeq: 0,
        // 场次详情相关
        showSessionModal: false,
        sessionDetail: {},
        sessionDetailLoading: false,
        sessionDetailTab: 'chats',
        sessionDetailChats: [],
        sessionDetailGifts: [],
        sessionDetailContributors: [],
        // 场次详情分页
        sessionDetailPagination: {
            chats: { page: 1, page_size: 50, total: 0, total_pages: 1 },
            gifts: { page: 1, page_size: 50, total: 0, total_pages: 1 }
        },
        sessionDetailCounts: { chat_count: 0, gift_count: 0 },
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
            live_id: null,
            session_id: null,
            start_date: null,
            end_date: null
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
                this.searchStatsUserByName({ silent: true });
            }, 350);
        },
        selectedRoomId() {
            if (this.activeRankTab === 'like') {
                this.loadTopLikers();
            }
        }
    },
    async mounted() {
        await Promise.all([
            this.loadRooms(),
            this.loadDateRange()
        ]);
        this.initCustomDates();
        this.initPeriodDefaults();
        // 不在 mounted 时自动加载数据，等待用户点击查询
    },
    beforeDestroy() {
        if (this.userSearchDebounceTimer) {
            clearTimeout(this.userSearchDebounceTimer);
        }
    },
    methods: {
        async loadRooms() {
            try {
                const response = await fetch('/api/rooms?include_archived=1');
                const data = await response.json();
                if (data.rooms) {
                    this.rooms = data.rooms;
                }
            } catch (error) {
                console.error('加载房间列表失败:', error);
            }
        },
        setRankTab(tab) {
            if (this.activeRankTab === tab) return;
            this.activeRankTab = tab;
            if (tab === 'like') {
                this.loadTopLikers();
            }
        },
        async loadTopLikers() {
            this.likersLoading = true;
            try {
                const params = new URLSearchParams();
                if (this.selectedRoomId) {
                    params.set('live_id', this.selectedRoomId);
                }
                params.set('limit', '100');
                const resp = await fetch('/api/rooms/top-likers?' + params.toString());
                const data = await resp.json();
                this.likers = data.likers || [];
            } catch (e) {
                console.error('加载累积点赞榜失败:', e);
                this.likers = [];
            } finally {
                this.likersLoading = false;
            }
        },
        openLikerMessages(liker) {
            if (typeof this.openUserMessagesModal === 'function') {
                this.openUserMessagesModal(liker.user_id, liker.user_name || liker.user_id, {
                    live_id: liker.live_id
                });
            }
        },
        async loadDateRange() {
            // 加载所有房间的日期范围
            try {
                const response = await fetch('/api/rooms/date-range');
                const data = await response.json();
                if (data.min_date) {
                    this.minDate = data.min_date;
                }
                if (data.max_date) {
                    this.maxDate = data.max_date;
                }
            } catch (error) {
                console.error('加载日期范围失败:', error);
            }
        },
        initCustomDates() {
            // 初始化自定义日期为最近7天，但要在允许的范围内
            const today = new Date();
            let weekAgo = new Date(today);
            weekAgo.setDate(weekAgo.getDate() - 7);

            // 如果有日期限制，调整到允许的范围内
            if (this.minDate) {
                const minDt = new Date(this.minDate);
                if (weekAgo < minDt) {
                    weekAgo = new Date(minDt);
                }
            }

            this.customEndDate = this.formatDateForInput(today);
            this.customStartDate = this.formatDateForInput(weekAgo);
        },
        initPeriodDefaults() {
            const today = new Date();
            const month = String(today.getMonth() + 1).padStart(2, '0');
            this.selectedMonth = `${today.getFullYear()}-${month}`;
            this.selectedYear = String(today.getFullYear());
        },
        onTimeRangeChange() {
            this.hasSearched = false;
            this.sessions = [];
            this.contributors = [];
            this.userSearchResults = [];
            this.userSearchHasSearched = false;
            this.userSearchError = '';
            if (this.timeRange === 'month' && !this.selectedMonth) {
                this.initPeriodDefaults();
            }
            if (this.timeRange === 'year' && !this.selectedYear) {
                this.initPeriodDefaults();
            }
        },
        getDateRange() {
            const today = new Date();
            let startDate, endDate;

            switch (this.timeRange) {
                case '7days':
                    startDate = new Date(today);
                    startDate.setDate(startDate.getDate() - 7);
                    startDate.setHours(0, 0, 0, 0);
                    endDate = new Date();
                    break;
                case '30days':
                    startDate = new Date(today);
                    startDate.setDate(startDate.getDate() - 30);
                    startDate.setHours(0, 0, 0, 0);
                    endDate = new Date();
                    break;
                case 'month':
                    if (!this.selectedMonth) {
                        return null;
                    }
                    {
                        const [year, month] = this.selectedMonth.split('-').map(Number);
                        startDate = new Date(year, month - 1, 1);
                        endDate = new Date(year, month, 0, 23, 59, 59);
                    }
                    break;
                case 'year':
                    if (!this.selectedYear) {
                        return null;
                    }
                    {
                        const year = Number(this.selectedYear);
                        startDate = new Date(year, 0, 1);
                        endDate = new Date(year, 11, 31, 23, 59, 59);
                    }
                    break;
                case 'total':
                    startDate = this.minDate ? new Date(this.minDate + 'T00:00:00') : new Date(1970, 0, 1);
                    endDate = this.maxDate ? new Date(this.maxDate + 'T23:59:59') : new Date();
                    break;
                case 'custom':
                    if (!this.customStartDate || !this.customEndDate) {
                        return null;
                    }
                    startDate = new Date(this.customStartDate + 'T00:00:00');
                    endDate = new Date(this.customEndDate + 'T23:59:59');
                    break;
                default:
                    startDate = new Date(today);
                    startDate.setDate(startDate.getDate() - 7);
                    startDate.setHours(0, 0, 0, 0);
                    endDate = new Date();
            }

            // 使用本地时区的日期字符串，避免时区转换问题
            const formatDate = (date) => {
                const year = date.getFullYear();
                const month = String(date.getMonth() + 1).padStart(2, '0');
                const day = String(date.getDate()).padStart(2, '0');
                return `${year}-${month}-${day}`;
            };

            return {
                start: formatDate(startDate),
                end: formatDate(endDate)
            };
        },
        async loadData() {
            const dateRange = this.getDateRange();
            if (!dateRange) {
                alert('请选择完整的统计时间');
                return;
            }

            this.loading = true;
            const params = new URLSearchParams({
                start_date: dateRange.start.split('T')[0],
                end_date: dateRange.end.split('T')[0]
            });

            try {
                // 加载统计数据
                const statsUrl = this.selectedRoomId
                    ? `/api/rooms/${encodeURIComponent(this.selectedRoomId)}/sessions/stats?${params}`
                    : `/api/rooms/sessions/stats?${params}`;

                const statsResponse = await fetch(statsUrl);
                const statsData = await statsResponse.json();
                if (statsData.stats) {
                    this.stats = statsData.stats;
                }

                // 加载贡献榜数据（第一页）
                await this.loadContributors(1);

                // 如果选择了房间，加载场次列表
                if (this.selectedRoomId) {
                    const sessionsUrl = `/api/rooms/${encodeURIComponent(this.selectedRoomId)}/sessions?${params}`;
                    const sessionsResponse = await fetch(sessionsUrl);
                    const sessionsData = await sessionsResponse.json();
                    if (sessionsData.sessions) {
                        this.sessions = sessionsData.sessions;
                    }
                } else {
                    this.sessions = [];
                }

                // 标记已查询
                this.hasSearched = true;
            } catch (error) {
                console.error('加载数据失败:', error);
            } finally {
                this.loading = false;
            }
        },
        async loadContributors(page = 1) {
            const dateRange = this.getDateRange();
            if (!dateRange) {
                return;
            }

            const params = new URLSearchParams({
                start_date: dateRange.start.split('T')[0],
                end_date: dateRange.end.split('T')[0],
                page: page,
                page_size: this.contributorPagination.page_size
            });

            if (this.selectedRoomId) {
                params.append('live_id', this.selectedRoomId);
            }
            if (this.timeRange === 'total') {
                params.append('source', 'summary');
            }

            try {
                const response = await fetch(`/api/rooms/contributors-by-date?${params}`);
                const data = await response.json();

                if (data.contributors) {
                    this.contributors = data.contributors;
                }
                if (data.page) {
                    this.contributorPagination.page = data.page;
                }
                if (data.page_size) {
                    this.contributorPagination.page_size = data.page_size;
                }
                if (data.total) {
                    this.contributorPagination.total = data.total;
                }
                if (data.total_pages) {
                    this.contributorPagination.total_pages = data.total_pages;
                }
            } catch (error) {
                console.error('加载贡献榜失败:', error);
            }
        },
        goToContributorPage(page) {
            if (page < 1 || page > this.contributorPagination.total_pages) return;
            this.loadContributors(page);
        },
        getContributorPageNumbers() {
            const current = this.contributorPagination.page;
            const total = this.contributorPagination.total_pages;
            const pages = [];

            if (total <= 7) {
                for (let i = 1; i <= total; i++) {
                    pages.push(i);
                }
            } else {
                if (current <= 4) {
                    for (let i = 1; i <= 5; i++) pages.push(i);
                    pages.push('...');
                    pages.push(total);
                } else if (current >= total - 3) {
                    pages.push(1);
                    pages.push('...');
                    for (let i = total - 4; i <= total; i++) pages.push(i);
                } else {
                    pages.push(1);
                    pages.push('...');
                    for (let i = current - 1; i <= current + 1; i++) pages.push(i);
                    pages.push('...');
                    pages.push(total);
                }
            }
            return pages;
        },
        loadRoomData() {
            this.sessions = [];
            this.contributors = [];
            this.contributorPagination = { page: 1, page_size: 20, total: 0, total_pages: 1 };
            this.hasSearched = false;
            this.userSearchResults = [];
            this.userSearchHasSearched = false;
            this.userSearchError = '';
            // 加载选中房间的日期范围
            this.loadRoomDateRange();
            // 重新初始化自定义日期
            this.initCustomDates();
            // 不自动加载数据，等待用户点击查询
        },
        async searchStatsUserByName(options = {}) {
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

            const dateRange = this.getDateRange();
            if (!dateRange) {
                this.userSearchError = '请选择完整的统计时间';
                return;
            }

            this.userSearchLoading = true;
            const requestSeq = ++this.userSearchRequestSeq;
            try {
                const params = new URLSearchParams({
                    user_name: userName,
                    start_date: dateRange.start,
                    end_date: dateRange.end,
                    limit: 50
                });
                const url = this.selectedRoomId
                    ? `/api/rooms/${encodeURIComponent(this.selectedRoomId)}/user-search?${params}`
                    : `/api/rooms/user-search?${params}`;
                const response = await fetch(url);
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
        selectStatsSearchedUser(user) {
            const dateRange = this.getDateRange();
            this.userSearchError = '';
            this.userSearchFocused = false;
            this.userSearchResults = [];
            this.openUserMessagesModal(user.user_id, user.nickname || user.user_id, {
                live_id: user.live_id || this.selectedRoomId || null,
                start_date: dateRange ? dateRange.start : null,
                end_date: dateRange ? dateRange.end : null
            });
        },
        async loadRoomDateRange() {
            // 加载选中房间的日期范围
            if (this.selectedRoomId) {
                try {
                    const response = await fetch(`/api/rooms/date-range?live_id=${encodeURIComponent(this.selectedRoomId)}`);
                    const data = await response.json();
                    if (data.min_date) {
                        this.minDate = data.min_date;
                    }
                    if (data.max_date) {
                        this.maxDate = data.max_date;
                    }
                } catch (error) {
                    console.error('加载房间日期范围失败:', error);
                }
            } else {
                // 加载所有房间的日期范围
                await this.loadDateRange();
            }
        },
        async viewSessionDetail(session) {
            this.showSessionModal = true;
            this.sessionDetail = session;
            this.sessionDetailLoading = true;
            this.sessionDetailTab = 'chats';
            // 重置分页
            this.sessionDetailPagination = {
                chats: { page: 1, page_size: 50, total: 0, total_pages: 1 },
                gifts: { page: 1, page_size: 50, total: 0, total_pages: 1 }
            };

            try {
                // 先获取基本信息和消息总数
                const response = await fetch(`/api/rooms/sessions/${session.id}?type=chat&page=1&limit=50`);
                const data = await response.json();

                if (data.session) {
                    this.sessionDetail = data.session;
                }
                if (data.counts) {
                    this.sessionDetailCounts = data.counts;
                }
                if (data.chats) {
                    this.sessionDetailChats = data.chats;
                }
                if (data.pagination) {
                    this.sessionDetailPagination.chats = data.pagination;
                }

                // 同时获取贡献榜
                const contribResponse = await fetch(`/api/rooms/sessions/${session.id}?type=contributors`);
                const contribData = await contribResponse.json();
                if (contribData.contributors) {
                    this.sessionDetailContributors = contribData.contributors;
                }
            } catch (error) {
                console.error('加载场次详情失败:', error);
            } finally {
                this.sessionDetailLoading = false;
            }
        },
        async loadSessionDetailTab(tab) {
            this.sessionDetailTab = tab;

            if (tab === 'chats' && this.sessionDetailChats.length === 0) {
                await this.loadSessionDetailData('chat');
            } else if (tab === 'gifts' && this.sessionDetailGifts.length === 0) {
                await this.loadSessionDetailData('gift');
            }
        },
        async loadSessionDetailData(type) {
            const pagination = type === 'chat' ? this.sessionDetailPagination.chats : this.sessionDetailPagination.gifts;
            try {
                const response = await fetch(`/api/rooms/sessions/${this.sessionDetail.id}?type=${type}&page=${pagination.page}&limit=${pagination.page_size}`);
                const data = await response.json();

                if (type === 'chat' && data.chats) {
                    this.sessionDetailChats = data.chats;
                    if (data.pagination) {
                        this.sessionDetailPagination.chats = data.pagination;
                    }
                } else if (type === 'gift' && data.gifts) {
                    this.sessionDetailGifts = data.gifts;
                    if (data.pagination) {
                        this.sessionDetailPagination.gifts = data.pagination;
                    }
                }
            } catch (error) {
                console.error(`加载${type === 'chat' ? '弹幕' : '礼物'}记录失败:`, error);
            }
        },
        async goToSessionPage(type, page) {
            const pagination = type === 'chat' ? this.sessionDetailPagination.chats : this.sessionDetailPagination.gifts;
            if (page < 1 || page > pagination.total_pages) return;
            pagination.page = page;
            await this.loadSessionDetailData(type);
        },
        getSessionPageNumbers(type) {
            const pagination = type === 'chat' ? this.sessionDetailPagination.chats : this.sessionDetailPagination.gifts;
            const current = pagination.page;
            const total = pagination.total_pages;
            const pages = [];

            if (total <= 7) {
                for (let i = 1; i <= total; i++) {
                    pages.push(i);
                }
            } else {
                if (current <= 4) {
                    for (let i = 1; i <= 5; i++) pages.push(i);
                    pages.push('...');
                    pages.push(total);
                } else if (current >= total - 3) {
                    pages.push(1);
                    pages.push('...');
                    for (let i = total - 4; i <= total; i++) pages.push(i);
                } else {
                    pages.push(1);
                    pages.push('...');
                    for (let i = current - 1; i <= current + 1; i++) pages.push(i);
                    pages.push('...');
                    pages.push(total);
                }
            }
            return pages;
        },
        closeSessionModal() {
            this.showSessionModal = false;
            this.sessionDetail = {};
            this.sessionDetailChats = [];
            this.sessionDetailGifts = [];
            this.sessionDetailContributors = [];
            this.sessionDetailPagination = {
                chats: { page: 1, page_size: 50, total: 0, total_pages: 1 },
                gifts: { page: 1, page_size: 50, total: 0, total_pages: 1 }
            };
            this.sessionDetailCounts = { chat_count: 0, gift_count: 0 };
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
        formatAvgIncome() {
            if (!this.stats.total_sessions || this.stats.total_sessions === 0) {
                return '0 钻石';
            }
            const avg = this.stats.total_income / this.stats.total_sessions;
            return avg.toLocaleString(undefined, { maximumFractionDigits: 0 }) + ' 钻石';
        },
        formatDuration(seconds) {
            if (!seconds || seconds === 0) return '0分钟';

            const hours = Math.floor(seconds / 3600);
            const minutes = Math.floor((seconds % 3600) / 60);

            if (hours > 0) {
                return `${hours}小时${minutes}分钟`;
            }
            return `${minutes}分钟`;
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
        formatDateForInput(date) {
            const d = new Date(date);
            const year = d.getFullYear();
            const month = String(d.getMonth() + 1).padStart(2, '0');
            const day = String(d.getDate()).padStart(2, '0');
            return `${year}-${month}-${day}`;
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
        // 用户消息模态框方法
        async openUserMessagesModal(userId, userName, options = {}) {
            this.showUserMessagesModal = !options.defer_show;
            this.userMessagesLoading = !options.defer_show;
            this.userMessagesTab = 'all';

            // 设置查询参数
            this.userMessagesQuery = {
                user_id: userId,
                user_name: options.user_name || null,
                live_id: options.live_id || this.selectedRoomId,
                session_id: options.session_id || null,
                start_date: options.start_date || null,
                end_date: options.end_date || null
            };

            // 重置数据
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

            const found = await this.loadUserMessages({ showLoading: !options.defer_show });
            if (options.defer_show && found) {
                this.showUserMessagesModal = true;
            }
            return found;
        },
        closeUserMessagesModal() {
            this.showUserMessagesModal = false;
            this.userMessagesData = {
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
            };
            this.userMessagesQuery = {
                user_id: null,
                user_name: null,
                live_id: null,
                session_id: null,
                start_date: null,
                end_date: null
            };
        },
        async loadUserMessages(options = {}) {
            if (!this.userMessagesQuery.user_id && !this.userMessagesQuery.user_name) return false;

            const showLoading = options.showLoading !== false;
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
                } else if (this.userMessagesQuery.start_date && this.userMessagesQuery.end_date) {
                    params.append('start_date', this.userMessagesQuery.start_date);
                    params.append('end_date', this.userMessagesQuery.end_date);
                }

                const url = this.userMessagesQuery.live_id
                    ? `/api/rooms/${this.userMessagesQuery.live_id}/user-messages?${params}`
                    : `/api/rooms/user-messages?${params}`;
                const response = await fetch(url);
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
        }
    }
});
