/**
 * 直播数据统计页面逻辑（精简版 - 仅统计卡片）
 */
const app = new Vue({
    el: '#app',
    mixins: [FormattersMixin],
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
        loading: true,
        hasSearched: false
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
                // 仅加载统计数据
                const statsUrl = this.selectedRoomId
                    ? `/api/rooms/${encodeURIComponent(this.selectedRoomId)}/sessions/stats?${params}`
                    : `/api/rooms/sessions/stats?${params}`;

                const statsResponse = await fetch(statsUrl);
                const statsData = await statsResponse.json();
                if (statsData.stats) {
                    this.stats = statsData.stats;
                }

                // 标记已查询
                this.hasSearched = true;
            } catch (error) {
                console.error('加载数据失败:', error);
            } finally {
                this.loading = false;
            }
        },
        loadRoomData() {
            this.hasSearched = false;
            // 加载选中房间的日期范围
            this.loadRoomDateRange();
            // 重新初始化自定义日期
            this.initCustomDates();
            // 不自动加载数据，等待用户点击查询
        },
        formatDateForInput(date) {
            const d = new Date(date);
            const year = d.getFullYear();
            const month = String(d.getMonth() + 1).padStart(2, '0');
            const day = String(d.getDate()).padStart(2, '0');
            return `${year}-${month}-${day}`;
        },
        formatAvgIncome() {
            if (!this.stats.total_sessions || this.stats.total_sessions === 0) {
                return '0 钻石';
            }
            const avg = this.stats.total_income / this.stats.total_sessions;
            return avg.toLocaleString(undefined, { maximumFractionDigits: 0 }) + ' 钻石';
        }
    }
});
