/**
 * 贡献榜页面逻辑
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

        // 贡献榜
        activeRankTab: 'gift',
        contributors: [],
        contributorPagination: {
            page: 1,
            page_size: 20,
            total: 0,
            total_pages: 1
        },

        // 点赞榜
        likers: [],
        likersLoading: false,

        // 用户搜索
        userNameSearch: '',
        userSearchLoading: false,
        userSearchError: '',
        userSearchResults: [],
        userSearchHasSearched: false,
        userSearchFocused: false,
        userSearchDebounceTimer: null,
        userSearchRequestSeq: 0,

        loading: false,
        hasSearched: false
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
            } else if (this.hasSearched) {
                this.contributors = [];
                this.hasSearched = false;
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
        async loadData() {
            const dateRange = this.getDateRange();
            if (!dateRange) {
                alert('请选择完整的统计时间');
                return;
            }

            this.loading = true;
            await this.loadContributors(1);
            this.hasSearched = true;
            this.loading = false;
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
            this.contributors = [];
            this.contributorPagination = { page: 1, page_size: 20, total: 0, total_pages: 1 };
            this.hasSearched = false;
            this.userSearchResults = [];
            this.userSearchHasSearched = false;
            this.userSearchError = '';
            this.loadRoomDateRange();
            this.initCustomDates();
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
        }
    }
});
