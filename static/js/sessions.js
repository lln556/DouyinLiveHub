/**
 * 场次记录页面逻辑
 */
const app = new Vue({
    el: '#app',
    mixins: [FormattersMixin, UserMessagesModalMixin],
    data: {
        rooms: [],
        selectedRoomId: '',
        timeRange: '7days',
        customStartDate: '',
        customEndDate: '',
        selectedMonth: '',
        selectedYear: '',
        minDate: '',
        maxDate: '',

        // 场次列表
        sessions: [],
        stats: {},

        // 场次详情
        showSessionModal: false,
        sessionDetail: {},
        sessionDetailLoading: false,
        sessionDetailTab: 'chats',
        sessionDetailChats: [],
        sessionDetailGifts: [],
        sessionDetailContributors: [],
        sessionDetailPagination: {
            chats: { page: 1, page_size: 50, total: 0, total_pages: 1 },
            gifts: { page: 1, page_size: 50, total: 0, total_pages: 1 }
        },
        sessionDetailCounts: { chat_count: 0, gift_count: 0 },

        loading: false,
        hasSearched: false
    },
    async mounted() {
        await Promise.all([
            this.loadRooms(),
            this.loadDateRange()
        ]);
        this.initCustomDates();
        this.initPeriodDefaults();
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
        async loadDateRange() {
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
        async loadRoomDateRange() {
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
                await this.loadDateRange();
            }
        },
        initCustomDates() {
            const today = new Date();
            let weekAgo = new Date(today);
            weekAgo.setDate(weekAgo.getDate() - 7);

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
                    if (!this.selectedMonth) return null;
                    {
                        const [year, month] = this.selectedMonth.split('-').map(Number);
                        startDate = new Date(year, month - 1, 1);
                        endDate = new Date(year, month, 0, 23, 59, 59);
                    }
                    break;
                case 'year':
                    if (!this.selectedYear) return null;
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
                    if (!this.customStartDate || !this.customEndDate) return null;
                    startDate = new Date(this.customStartDate + 'T00:00:00');
                    endDate = new Date(this.customEndDate + 'T23:59:59');
                    break;
                default:
                    startDate = new Date(today);
                    startDate.setDate(startDate.getDate() - 7);
                    startDate.setHours(0, 0, 0, 0);
                    endDate = new Date();
            }

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
                const sessionsUrl = this.selectedRoomId
                    ? `/api/rooms/${encodeURIComponent(this.selectedRoomId)}/sessions?${params}`
                    : `/api/rooms/sessions/all?${params}`;

                const sessionsResponse = await fetch(sessionsUrl);
                const sessionsData = await sessionsResponse.json();
                if (sessionsData.sessions) {
                    this.sessions = sessionsData.sessions;
                }

                this.hasSearched = true;
            } catch (error) {
                console.error('加载数据失败:', error);
            } finally {
                this.loading = false;
            }
        },
        loadRoomData() {
            this.sessions = [];
            this.hasSearched = false;
            this.loadRoomDateRange();
            this.initCustomDates();
        },
        async viewSessionDetail(session) {
            this.showSessionModal = true;
            this.sessionDetail = session;
            this.sessionDetailLoading = true;
            this.sessionDetailTab = 'chats';
            this.sessionDetailPagination = {
                chats: { page: 1, page_size: 50, total: 0, total_pages: 1 },
                gifts: { page: 1, page_size: 50, total: 0, total_pages: 1 }
            };

            try {
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
        }
    }
});
