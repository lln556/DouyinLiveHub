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
        todayDate: '',  // 今日日期上限
        loadRequestSeq: 0,
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
        chartMetric: 'income',
        chartMetrics: [
            { key: 'income', field: 'income', label: '收入', stroke: '#d97706', fill: 'rgba(217, 119, 6, 0.12)', bg: '#fffbeb', activeText: '#b45309' },
            { key: 'gift_count', field: 'gift_count', label: '礼物', stroke: '#059669', fill: 'rgba(5, 150, 105, 0.10)', bg: '#ecfdf5', activeText: '#065f46' },
            { key: 'like_count', field: 'like_count', label: '点赞', stroke: '#e11d48', fill: 'rgba(225, 29, 72, 0.10)', bg: '#fff1f2', activeText: '#be123c' },
            { key: 'peak_viewer', field: 'peak_viewer', label: '峰值观看', stroke: '#18181b', fill: 'rgba(24, 24, 27, 0.08)', bg: '#f3f4f6', activeText: '#111827' }
        ],
        chartRenderError: '',
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
        userSearchBlurTimer: null,
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
        showUserMessagesModal: false,
        userMessagesLoading: false,
        userMessagesTab: 'all',
        userCardScopeMode: 'all',
        userCardFocusSpec: null,
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
            sessions: {},
            scope_summary: null,
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
            anchor_name: null,
            session_id: null,
            start_date: null,
            end_date: null
        }
    },
    computed: {
        userCardFocusIsLive() {
            return Boolean(
                this.userCardFocusSpec?.kind === 'session' &&
                this.userCardFocusSpec.statusSnapshot === 'live'
            );
        },
        userCardDisplayStats() {
            return window.HubUI.resolveUserCardDisplayStats(
                this.userMessagesData,
                this.userCardScopeMode,
                this.userCardFocusSpec
            );
        },
        userCardRoomLabel() {
            const summaryRoom = this.userMessagesData?.scope_summary?.room;
            const anchorName = summaryRoom?.anchor_name
                || this.userMessagesData?.user?.anchor_name
                || this.userMessagesQuery?.anchor_name
                || '';
            const liveId = summaryRoom?.live_id || this.userMessagesQuery?.live_id || '';
            if (anchorName) return `主播: ${anchorName}`;
            if (liveId) return `房间: ${liveId}`;
            return '';
        },
        userCardScopeCaption() {
            if (this.userMessagesLoading || !this.userMessagesData?.scope_summary) {
                return '';
            }
            if (this.userCardDisplayStats.showScopeBar) {
                if (this.userCardScopeMode === 'focus') {
                    if (this.userCardFocusSpec?.kind === 'date_range') {
                        const start = this.userCardFocusSpec.startDate || '';
                        const end = this.userCardFocusSpec.endDate || '';
                        return start && end ? `${start} ~ ${end}` : '当前周期';
                    }
                    const startText = window.HubUI.formatShortDateTime(this.userCardFocusSpec?.startTimeSnapshot);
                    if (this.userCardFocusIsLive) {
                        return startText ? `${startText} 开播` : '仅看本场直播';
                    }
                    return startText ? `${startText} 场次` : '仅看该场记录';
                }
                return '按场次分组显示';
            }
            return '全部记录';
        },
        userMessageTimeline() {
            const multiSession = !(
                this.userCardScopeMode === 'focus' &&
                this.userCardFocusSpec?.kind === 'session'
            );
            return window.HubUI.buildUserMessageTimeline(this.userMessagesData.messages, {
                multiSession,
                sessions: this.userMessagesData.sessions,
                focusSpec: this.userCardFocusSpec,
                currentLiveId: this.userMessagesQuery?.live_id || null
            });
        }
    },
    watch: {
        showSessionModal(val) {
            this.$nextTick(() => {
                window.HubUI.syncModalDialog(this.$refs.sessionModal, val, {
                    getClosedBy: () => 'any',
                    onDismiss: () => this.closeSessionModal()
                });
            });
        },
        showUserMessagesModal(val) {
            this.$nextTick(() => {
                window.HubUI.syncModalDialog(this.$refs.userMessagesModal, val, {
                    getClosedBy: () => 'any',
                    onDismiss: () => this.closeUserMessagesModal()
                });
            });
        },
        userNameSearch(value) {
            if (this.userSearchDebounceTimer) {
                clearTimeout(this.userSearchDebounceTimer);
            }

            const raw = value === null || value === undefined ? '' : String(value);
            this.userSearchError = '';

            if (!raw) {
                this.userSearchRequestSeq++;
                this._userSearchAbortController?.abort();
                this._userSearchAbortController = null;
                this.userSearchResults = [];
                this.userSearchHasSearched = false;
                this.userSearchLoading = false;
                return;
            }

            this.userSearchDebounceTimer = setTimeout(() => {
                this.searchStatsUserByName({ silent: true });
            }, 350);
        },
        chartMetric() {
            this.$nextTick(() => this.renderTrendChart());
        }
    },
    async mounted() {
        this._loadDataAbortController = null;
        this._userSearchAbortController = null;
        this._userMessagesAbortController = null;
        this._trendResizeObserver = null;
        this._observedChartEl = null;

        this.todayDate = this.formatDateForInput(new Date());
        const urlParams = new URLSearchParams(window.location.search || '');
        const paramLiveId = (urlParams.get('live_id') || '').trim();
        const paramRange = (urlParams.get('range') || '').trim();
        const paramSessionId = (urlParams.get('session_id') || '').trim();

        if (paramLiveId) {
            this.selectedRoomId = paramLiveId;
        }
        if (['7days', '30days', 'month', 'year', 'total', 'custom'].includes(paramRange)) {
            this.timeRange = paramRange;
        }
        this.initPeriodDefaults();

        if (typeof ResizeObserver === 'undefined') {
            this._trendChartResizeHandler = () => this.resizeTrendChart();
            window.addEventListener('resize', this._trendChartResizeHandler);
        }

        this._keydownHandler = (event) => {
            if (event.key === 'Escape' && this.userSearchFocused && !this.showUserMessagesModal && !this.showSessionModal) {
                this.userSearchFocused = false;
            }
        };
        document.addEventListener('keydown', this._keydownHandler);

        const rangePromise = (paramLiveId ? this.loadRoomDateRange() : this.loadDateRange()).then(() => {
            this.initCustomDates();
        });
        if (['7days', '30days', 'month', 'year'].includes(this.timeRange)) {
            await Promise.all([this.loadRooms(), rangePromise, this.loadData()]);
        } else {
            await Promise.all([this.loadRooms(), rangePromise]);
            await this.loadData();
        }
        const parsedSessionId = Number(paramSessionId);
        if (Number.isInteger(parsedSessionId) && parsedSessionId > 0) {
            await this.viewSessionDetail({ id: parsedSessionId });
        }
    },
    beforeDestroy() {
        if (this.userSearchDebounceTimer) {
            clearTimeout(this.userSearchDebounceTimer);
        }
        if (this.userSearchBlurTimer) {
            clearTimeout(this.userSearchBlurTimer);
        }
        if (this._resizeRafId) {
            cancelAnimationFrame(this._resizeRafId);
            this._resizeRafId = null;
        }
        this._loadDataAbortController?.abort();
        this._userSearchAbortController?.abort();
        this._userMessagesAbortController?.abort();
        this._trendResizeObserver?.disconnect();
        this._trendResizeObserver = null;
        this._observedChartEl = null;
        if (this._trendChartResizeHandler) {
            window.removeEventListener('resize', this._trendChartResizeHandler);
        }
        if (this._keydownHandler) {
            document.removeEventListener('keydown', this._keydownHandler);
        }
        if (this._trendChart) {
            this._trendChart.dispose();
            this._trendChart = null;
        }
    },
    methods: {
        onDialogClose(kind) {
            if (kind === 'user' && this.showUserMessagesModal) {
                this.closeUserMessagesModal();
            } else if (kind === 'session' && this.showSessionModal) {
                this.closeSessionModal();
            }
        },
        syncTrendChartObserver() {
            const target = this.$refs.trendChart;
            if (!target || typeof ResizeObserver === 'undefined') return;
            if (this._observedChartEl === target && this._trendResizeObserver) return;
            this._trendResizeObserver?.disconnect();
            this._trendResizeObserver = new ResizeObserver(() => this.resizeTrendChart());
            this._trendResizeObserver.observe(target);
            this._observedChartEl = target;
        },
        annotateRelatedRooms(rooms) {
            const counts = {};
            for (const room of rooms) {
                if (room.anchor_id) {
                    counts[room.anchor_id] = (counts[room.anchor_id] || 0) + 1;
                }
            }
            return rooms.map((room) => ({
                ...room,
                has_related: Boolean(room.anchor_id && counts[room.anchor_id] > 1)
            }));
        },
        mergedRelatedRooms() {
            const ids = this.stats?.related_live_ids || [];
            if (!this.selectedRoomId || ids.length < 2) {
                return [];
            }
            return ids.map((id) => {
                const room = this.rooms.find((item) => item.live_id === id) || {};
                return {
                    live_id: id,
                    anchor_name: room.anchor_name,
                    is_archived: Boolean(room.is_archived)
                };
            });
        },
        async loadRooms() {
            try {
                const { data } = await window.HubUI.requestJson('/api/rooms?include_archived=1');
                if (data.rooms) {
                    this.rooms = this.annotateRelatedRooms(data.rooms);
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
                const { data } = await window.HubUI.requestJson('/api/rooms/top-likers?' + params.toString());
                this.likers = data.likers || [];
            } catch (e) {
                console.error('加载累积点赞榜失败:', e);
                this.likers = [];
            } finally {
                this.likersLoading = false;
            }
        },
        getTimeRangeShortLabel() {
            switch (this.timeRange) {
                case '7days': return '近7天';
                case '30days': return '近30天';
                case 'month': return this.selectedMonth || '所选月份';
                case 'year': return this.selectedYear ? `${this.selectedYear}年` : '所选年份';
                case 'custom': return '所选周期';
                default: return '当前周期';
            }
        },
        openContributorMessages(contributor) {
            if (!contributor) return;
            const liveId = contributor.live_id || this.selectedRoomId || null;
            if (this.timeRange !== 'total') {
                const dateRange = this.getDateRange();
                this.openUserMessagesModal(contributor.user_id, contributor.nickname || contributor.user_id, {
                    live_id: liveId,
                    anchor_name: contributor.anchor_name || null,
                    start_date: dateRange ? dateRange.start : null,
                    end_date: dateRange ? dateRange.end : null,
                    date_range_label: this.getTimeRangeShortLabel(),
                    defaultScope: dateRange ? 'focus' : 'all'
                });
            } else {
                this.openUserMessagesModal(contributor.user_id, contributor.nickname || contributor.user_id, {
                    live_id: liveId,
                    anchor_name: contributor.anchor_name || null,
                    defaultScope: 'all'
                });
            }
        },
        openSessionUserMessages(userId, userName) {
            const detail = this.sessionDetail || {};
            const sessionId = detail.id || null;
            this.openUserMessagesModal(userId, userName || userId, {
                live_id: detail.live_id || this.selectedRoomId || null,
                anchor_name: detail.anchor_name || null,
                session_id: sessionId,
                session_status: detail.status || 'ended',
                session_start_time: detail.start_time || null,
                defaultScope: sessionId ? 'focus' : 'all'
            });
        },
        openLikerMessages(liker) {
            if (typeof this.openUserMessagesModal === 'function') {
                this.openUserMessagesModal(liker.user_id, liker.user_name || liker.user_id, {
                    live_id: liker.live_id || this.selectedRoomId || null,
                    anchor_name: liker.anchor_name || null,
                    defaultScope: 'all'
                });
            }
        },
        selectChartMetric(metricKey) {
            this.chartMetric = metricKey;
        },
        currentChartMetricConfig() {
            return this.chartMetrics.find((metric) => metric.key === this.chartMetric) || this.chartMetrics[0];
        },
        trendRows() {
            return Array.isArray(this.stats.trend) ? this.stats.trend : [];
        },
        trendValue(row) {
            const metric = this.currentChartMetricConfig();
            return Number(row[metric.field] || 0);
        },
        chartHasSignal() {
            return this.trendRows().some((row) => this.trendValue(row) > 0);
        },
        normalizeChartMetric() {
            if (this.chartHasSignal()) return;
            const metricWithSignal = this.chartMetrics.find((metric) =>
                this.trendRows().some((row) => Number(row[metric.field] || 0) > 0)
            );
            if (metricWithSignal) {
                this.chartMetric = metricWithSignal.key;
            }
        },
        trendTotalValue() {
            const rows = this.trendRows();
            if (this.chartMetric === 'peak_viewer') {
                return rows.reduce((max, row) => Math.max(max, this.trendValue(row)), 0);
            }
            return rows.reduce((sum, row) => sum + this.trendValue(row), 0);
        },
        trendPeakValue() {
            return this.trendRows().reduce((max, row) => Math.max(max, this.trendValue(row)), 0);
        },
        trendAverageValue() {
            const rows = this.trendRows();
            if (rows.length === 0) return 0;
            const total = rows.reduce((sum, row) => sum + this.trendValue(row), 0);
            return total / rows.length;
        },
        formatChartValue(value) {
            if (this.chartMetric === 'income') {
                return this.formatInteger(value) + ' 钻石';
            }
            return this.formatNumber(value);
        },
        renderTrendChart() {
            if (!this.$refs.trendChart || !this.hasSearched) {
                return;
            }
            if (!window.echarts) {
                const scriptEl = document.querySelector('script[data-echarts-script]');
                if (scriptEl && scriptEl.dataset.failed !== '1') {
                    if (!scriptEl._hubBoundChart) {
                        scriptEl._hubBoundChart = true;
                        scriptEl.addEventListener('load', () => {
                            if (!this._isDestroyed) {
                                this.renderTrendChart();
                            }
                        }, { once: true });
                        scriptEl.addEventListener('error', () => {
                            scriptEl.dataset.failed = '1';
                            if (!this._isDestroyed) {
                                this.chartRenderError = '图表组件加载失败';
                            }
                        }, { once: true });
                    }
                    return;
                }
                this.chartRenderError = '图表组件加载失败';
                return;
            }

            this.chartRenderError = '';
            if (this._trendChart && this._trendChart.getDom() !== this.$refs.trendChart) {
                this._trendChart.dispose();
                this._trendChart = null;
            }
            if (!this._trendChart) {
                this._trendChart = window.echarts.init(this.$refs.trendChart, null, {
                    renderer: 'canvas'
                });
            }
            this.syncTrendChartObserver();
            this._trendChart.setOption(this.buildTrendChartOption(), true);
            this.resizeTrendChart();
        },
        resizeTrendChart() {
            if (!this._trendChart || this._resizeRafId) {
                return;
            }
            this._resizeRafId = requestAnimationFrame(() => {
                this._resizeRafId = null;
                this._trendChart?.resize();
            });
        },
        buildTrendChartOption() {
            const rows = this.trendRows();
            const metric = this.currentChartMetricConfig();
            const values = rows.map((row) => this.trendValue(row));
            const hasRows = rows.length > 0;
            const hasSignal = values.some((value) => value > 0);
            const color = hasSignal ? metric.stroke : '#94a3b8';
            const seriesData = hasRows ? values : [];
            const graphicText = !hasRows
                ? '暂无趋势数据'
                : (hasSignal ? '' : '当前指标暂无非零数据');

            return {
                animationDuration: 500,
                color: [color],
                grid: {
                    left: 14,
                    right: 18,
                    top: 18,
                    bottom: 18,
                    containLabel: true
                },
                tooltip: {
                    trigger: 'axis',
                    confine: true,
                    backgroundColor: 'rgba(17, 24, 39, 0.92)',
                    borderWidth: 0,
                    padding: [8, 10],
                    textStyle: {
                        color: '#fff',
                        fontSize: 12
                    },
                    axisPointer: {
                        type: 'line',
                        lineStyle: {
                            color: color,
                            width: 1,
                            type: 'dashed'
                        }
                    },
                    formatter: (params) => {
                        const point = params?.[0];
                        if (!point) return '';
                        return `${point.axisValue}<br>${metric.label}: ${this.formatChartValue(point.value)}`;
                    }
                },
                xAxis: {
                    type: 'category',
                    boundaryGap: true,
                    data: rows.map((row) => row.date),
                    axisTick: { show: false },
                    axisLine: { lineStyle: { color: '#e5e7eb' } },
                    axisLabel: {
                        color: '#64748b',
                        fontSize: 11,
                        margin: 12
                    }
                },
                yAxis: {
                    type: 'value',
                    min: 0,
                    splitNumber: 3,
                    axisLabel: {
                        color: '#94a3b8',
                        fontSize: 11,
                        formatter: (value) => this.formatNumber(value)
                    },
                    splitLine: {
                        lineStyle: {
                            color: '#edf2f7'
                        }
                    }
                },
                series: [{
                    name: metric.label,
                    type: 'bar',
                    data: seriesData,
                    barMaxWidth: 42,
                    barMinHeight: hasRows && !hasSignal ? 2 : 0,
                    itemStyle: {
                        color: hasSignal
                            ? new window.echarts.graphic.LinearGradient(0, 0, 0, 1, [
                                { offset: 0, color: color },
                                { offset: 1, color: metric.fill }
                            ])
                            : '#cbd5e1',
                        borderRadius: [6, 6, 0, 0]
                    },
                    emphasis: {
                        focus: 'series'
                    }
                }],
                graphic: graphicText ? [{
                    type: 'text',
                    left: 'center',
                    top: 'middle',
                    silent: true,
                    style: {
                        text: graphicText,
                        fill: '#64748b',
                        fontSize: 13,
                        fontWeight: 500,
                        backgroundColor: 'rgba(255, 255, 255, 0.82)',
                        borderColor: '#e5e7eb',
                        borderWidth: 1,
                        borderRadius: 8,
                        padding: [10, 14]
                    }
                }] : []
            };
        },
        async loadDateRange() {
            // 加载所有房间的日期范围
            try {
                const { data } = await window.HubUI.requestJson('/api/rooms/date-range');
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

            this.todayDate = this.formatDateForInput(today);
            this.customEndDate = this.todayDate;
            this.customStartDate = this.formatDateForInput(weekAgo);
        },
        initPeriodDefaults() {
            const today = new Date();
            const month = String(today.getMonth() + 1).padStart(2, '0');
            this.selectedMonth = `${today.getFullYear()}-${month}`;
            this.selectedYear = String(today.getFullYear());
        },
        onTimeRangeChange() {
            this.userSearchResults = [];
            this.userSearchHasSearched = false;
            this.userSearchError = '';
            if (this.timeRange === 'month' && !this.selectedMonth) {
                this.initPeriodDefaults();
            }
            if (this.timeRange === 'year' && !this.selectedYear) {
                this.initPeriodDefaults();
            }
            this.autoLoadSelectedRoomStats();
        },
        canAutoLoadSelectedRoomStats() {
            return Boolean(this.getDateRange());
        },
        async autoLoadSelectedRoomStats() {
            if (!this.canAutoLoadSelectedRoomStats()) {
                return;
            }
            await this.loadData();
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
                    if (this.customStartDate > this.customEndDate) {
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
            if (this.timeRange === 'custom' && this.customStartDate && this.customEndDate && this.customStartDate > this.customEndDate) {
                this.loading = false;
                window.HubUI.toast('开始日期不能晚于结束日期', 'warning');
                return;
            }
            const dateRange = this.getDateRange();
            if (!dateRange) {
                this.loading = false;
                window.HubUI.toast('请选择完整的统计时间', 'warning');
                return;
            }

            this._loadDataAbortController?.abort();
            const controller = typeof AbortController !== 'undefined' ? new AbortController() : null;
            this._loadDataAbortController = controller;
            const fetchOpts = controller ? { signal: controller.signal } : {};

            const requestSeq = ++this.loadRequestSeq;
            this.loading = true;
            const params = new URLSearchParams({
                start_date: dateRange.start.split('T')[0],
                end_date: dateRange.end.split('T')[0]
            });

            try {
                // 并发加载统计数据、贡献榜首页和（选中房间时的）场次列表
                const statsUrl = this.selectedRoomId
                    ? `/api/rooms/${encodeURIComponent(this.selectedRoomId)}/sessions/stats?${params}`
                    : `/api/rooms/sessions/stats?${params}`;
                const sessionsUrl = this.selectedRoomId
                    ? `/api/rooms/${encodeURIComponent(this.selectedRoomId)}/sessions?${params}`
                    : null;

                const statsPromise = fetch(statsUrl, fetchOpts).then((r) => r.json());
                const contributorsPromise = this.loadContributors(1, { requestSeq, signal: controller?.signal });
                const sessionsPromise = sessionsUrl
                    ? fetch(sessionsUrl, fetchOpts).then((r) => r.json())
                    : Promise.resolve(null);

                const [statsData, , sessionsData] = await Promise.all([
                    statsPromise,
                    contributorsPromise,
                    sessionsPromise
                ]);
                if (requestSeq !== this.loadRequestSeq) {
                    return;
                }
                if (statsData?.stats) {
                    this.stats = statsData.stats;
                    this.normalizeChartMetric();
                }

                if (sessionsUrl) {
                    if (sessionsData?.sessions) {
                        this.sessions = sessionsData.sessions;
                    }
                } else {
                    this.sessions = [];
                }

                // 标记已查询
                this.hasSearched = true;
                this.$nextTick(() => this.renderTrendChart());
            } catch (error) {
                if (error?.name !== 'AbortError' && requestSeq === this.loadRequestSeq) {
                    console.error('加载数据失败:', error);
                }
            } finally {
                if (requestSeq === this.loadRequestSeq) {
                    this.loading = false;
                }
            }
        },
        async loadContributors(page = 1, options = {}) {
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
                const { data } = await window.HubUI.requestJson(
                    `/api/rooms/contributors-by-date?${params}`,
                    options.signal ? { signal: options.signal } : {}
                );
                if (options.requestSeq && options.requestSeq !== this.loadRequestSeq) {
                    return;
                }

                if (data.contributors) {
                    this.contributors = data.contributors;
                }
                if (data.page) {
                    this.contributorPagination.page = data.page;
                }
                if (data.page_size) {
                    this.contributorPagination.page_size = data.page_size;
                }
                if (data.total !== undefined) {
                    this.contributorPagination.total = data.total;
                }
                if (data.total_pages) {
                    this.contributorPagination.total_pages = data.total_pages;
                }
            } catch (error) {
                if (error?.name !== 'AbortError') {
                    console.error('加载贡献榜失败:', error);
                }
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
        async loadRoomData() {
            this.contributorPagination = { page: 1, page_size: 20, total: 0, total_pages: 1 };
            this.userSearchResults = [];
            this.userSearchHasSearched = false;
            this.userSearchError = '';
            // 加载选中房间的日期范围
            await this.loadRoomDateRange();
            // 重新初始化自定义日期
            this.initCustomDates();
            await Promise.all([
                this.autoLoadSelectedRoomStats(),
                ...(this.activeRankTab === 'like' ? [this.loadTopLikers()] : [])
            ]);
        },
        onUserSearchBlur() {
            if (this.userSearchBlurTimer) {
                clearTimeout(this.userSearchBlurTimer);
            }
            this.userSearchBlurTimer = setTimeout(() => {
                this.userSearchFocused = false;
                this.userSearchBlurTimer = null;
            }, 180);
        },
        async searchStatsUserByName(options = {}) {
            const rawInput = this.userNameSearch === null || this.userNameSearch === undefined
                ? ''
                : String(this.userNameSearch);
            const blankSearch = window.HubUI.isBlankSearchInput(rawInput);
            const userName = blankSearch ? rawInput : rawInput.trim();
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

            const isTotalRange = this.timeRange === 'total';
            const dateRange = isTotalRange ? null : this.getDateRange();
            if (!isTotalRange && !dateRange) {
                this.userSearchError = '请选择完整的统计时间';
                return;
            }

            this._userSearchAbortController?.abort();
            const controller = typeof AbortController !== 'undefined' ? new AbortController() : null;
            this._userSearchAbortController = controller;

            this.userSearchLoading = true;
            const requestSeq = ++this.userSearchRequestSeq;
            try {
                const params = new URLSearchParams({
                    user_name: userName,
                    limit: 50
                });
                if (dateRange) {
                    params.append('start_date', dateRange.start);
                    params.append('end_date', dateRange.end);
                }
                const url = this.selectedRoomId
                    ? `/api/rooms/${encodeURIComponent(this.selectedRoomId)}/user-search?${params}`
                    : `/api/rooms/user-search?${params}`;
                const { ok, data } = await window.HubUI.requestJson(
                    url,
                    controller ? { signal: controller.signal } : {}
                );
                if (!ok) {
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
                if (error?.name === 'AbortError' || requestSeq !== this.userSearchRequestSeq) {
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
            if (this.userSearchBlurTimer) {
                clearTimeout(this.userSearchBlurTimer);
                this.userSearchBlurTimer = null;
            }
            const isTotalRange = this.timeRange === 'total';
            const dateRange = isTotalRange ? null : this.getDateRange();
            this.userSearchError = '';
            this.userSearchFocused = false;
            this.userSearchResults = [];
            this.openUserMessagesModal(user.user_id, user.nickname || user.user_id, {
                live_id: user.live_id || this.selectedRoomId || null,
                anchor_name: user.anchor_name || null,
                start_date: dateRange ? dateRange.start : null,
                end_date: dateRange ? dateRange.end : null,
                date_range_label: dateRange ? this.getTimeRangeShortLabel() : null,
                defaultScope: dateRange ? 'focus' : 'all'
            });
        },
        async loadRoomDateRange() {
            // 加载选中房间的日期范围
            if (this.selectedRoomId) {
                try {
                    const { data } = await window.HubUI.requestJson(
                        `/api/rooms/date-range?live_id=${encodeURIComponent(this.selectedRoomId)}`
                    );
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
                const [chatRes, contribRes] = await Promise.all([
                    window.HubUI.requestJson(`/api/rooms/sessions/${session.id}?type=chat&page=1&limit=50`),
                    window.HubUI.requestJson(`/api/rooms/sessions/${session.id}?type=contributors`)
                ]);

                const data = chatRes.data;
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

                const contribData = contribRes.data;
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
                const { data } = await window.HubUI.requestJson(
                    `/api/rooms/sessions/${this.sessionDetail.id}?type=${type}&page=${pagination.page}&limit=${pagination.page_size}`
                );

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
        formatInteger(value) {
            return window.HubUI.formatInteger(value);
        },
        formatIncome(value) {
            return this.formatInteger(value) + ' 钻石';
        },
        formatNumber(value) {
            return window.HubUI.formatCompactNumber(value);
        },
        formatAvgIncome() {
            const avg = Number(this.stats.avg_income_per_active_session || 0);
            return window.HubUI.formatInteger(avg) + ' 钻石';
        },
        formatAvgLikes() {
            const avg = Number(this.stats.avg_likes_per_active_session || 0);
            return window.HubUI.formatInteger(avg) + ' 次';
        },
        formatDurationParts(seconds) {
            if (!seconds || seconds <= 0) {
                return { main: '0', unit: '分钟' };
            }

            const hours = Math.floor(seconds / 3600);
            const minutes = Math.floor((seconds % 3600) / 60);
            if (hours > 0) {
                return {
                    main: `${hours}:${String(minutes).padStart(2, '0')}`,
                    unit: '小时:分钟'
                };
            }
            return {
                main: String(minutes),
                unit: '分钟'
            };
        },
        formatDateTime(dateStr) {
            return window.HubUI.formatDateTime(dateStr);
        },
        formatDateForInput(date) {
            const d = new Date(date);
            const year = d.getFullYear();
            const month = String(d.getMonth() + 1).padStart(2, '0');
            const day = String(d.getDate()).padStart(2, '0');
            return `${year}-${month}-${day}`;
        },
        getSessionStatusClass(status) {
            if (status === 'live') {
                return 'bg-green-100 text-green-800';
            }
            return 'bg-gray-100 text-gray-800';
        },
        getSessionStatusText(status) {
            switch (status) {
                case 'live': return '直播中';
                case 'ended': return '已结束';
                default: return status;
            }
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
                sessions: {},
                scope_summary: null,
                pagination: {
                    page: 1,
                    page_size: 50,
                    total: 0,
                    total_pages: 1
                }
            };
        },
        async openUserMessagesModal(userId, userName, options = {}) {
            let focusSpec = null;
            let scopeMode = 'all';

            if (options.session_id) {
                const isLive = options.session_status === 'live';
                focusSpec = {
                    kind: 'session',
                    sessionId: options.session_id,
                    statusSnapshot: isLive ? 'live' : 'ended',
                    startTimeSnapshot: options.session_start_time || null,
                    label: isLive ? '本场直播' : `该场 #${options.session_id}`,
                    shortLabel: isLive ? '本场' : `#${options.session_id}`
                };
                scopeMode = options.defaultScope || 'focus';
            } else if (options.start_date || options.end_date) {
                focusSpec = {
                    kind: 'date_range',
                    startDate: options.start_date || null,
                    endDate: options.end_date || null,
                    label: options.date_range_label || this.getTimeRangeShortLabel(),
                    shortLabel: '周期内'
                };
                scopeMode = options.defaultScope || 'focus';
            }

            this.showUserMessagesModal = true;
            this.userMessagesLoading = true;
            this.userMessagesTab = 'all';
            this.userCardFocusSpec = focusSpec;
            this.userCardScopeMode = scopeMode;

            this.userMessagesQuery = {
                user_id: userId,
                user_name: options.user_name || null,
                live_id: options.live_id || this.selectedRoomId || null,
                anchor_name: options.anchor_name || null,
                session_id: (scopeMode === 'focus' && focusSpec?.kind === 'session') ? focusSpec.sessionId : null,
                start_date: (scopeMode === 'focus' && focusSpec?.kind === 'date_range') ? focusSpec.startDate : null,
                end_date: (scopeMode === 'focus' && focusSpec?.kind === 'date_range') ? focusSpec.endDate : null
            };

            this.resetUserMessagesData(userId, userName);

            return await this.loadUserMessages();
        },
        closeUserMessagesModal() {
            this._userMessagesAbortController?.abort();
            this._userMessagesAbortController = null;
            this.showUserMessagesModal = false;
            this.userCardScopeMode = 'all';
            this.userCardFocusSpec = null;
            this.resetUserMessagesData();
            this.userMessagesQuery = {
                user_id: null,
                user_name: null,
                live_id: null,
                anchor_name: null,
                session_id: null,
                start_date: null,
                end_date: null
            };
        },
        async loadUserMessages() {
            if (!this.userMessagesQuery.user_id && !this.userMessagesQuery.user_name) return false;

            this._userMessagesAbortController?.abort();
            const controller = typeof AbortController !== 'undefined' ? new AbortController() : null;
            this._userMessagesAbortController = controller;

            this.userMessagesLoading = true;
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

                if (this.userCardFocusSpec?.kind === 'session' && this.userCardFocusSpec.sessionId) {
                    params.append('anchor_session_id', String(this.userCardFocusSpec.sessionId));
                    if (this.userCardScopeMode === 'focus') {
                        params.append('session_id', String(this.userCardFocusSpec.sessionId));
                    }
                } else if (this.userCardFocusSpec?.kind === 'date_range') {
                    if (this.userCardFocusSpec.startDate) {
                        params.append('anchor_start_date', this.userCardFocusSpec.startDate);
                        if (this.userCardScopeMode === 'focus') {
                            params.append('start_date', this.userCardFocusSpec.startDate);
                        }
                    }
                    if (this.userCardFocusSpec.endDate) {
                        params.append('anchor_end_date', this.userCardFocusSpec.endDate);
                        if (this.userCardScopeMode === 'focus') {
                            params.append('end_date', this.userCardFocusSpec.endDate);
                        }
                    }
                }

                const url = this.userMessagesQuery.live_id
                    ? `/api/rooms/${encodeURIComponent(this.userMessagesQuery.live_id)}/user-messages?${params}`
                    : `/api/rooms/user-messages?${params}`;
                const { data } = await window.HubUI.requestJson(
                    url,
                    controller ? { signal: controller.signal } : {}
                );

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
                this.userMessagesData.sessions = (data.sessions && typeof data.sessions === 'object')
                    ? data.sessions
                    : {};
                this.userMessagesData.scope_summary = data.scope_summary || null;

                if (!this.userCardFocusSpec && data.scope_summary?.anchor_session?.status === 'live') {
                    const anchor = data.scope_summary.anchor_session;
                    this.userCardFocusSpec = {
                        kind: 'session',
                        sessionId: anchor.id,
                        statusSnapshot: 'live',
                        startTimeSnapshot: anchor.start_time || null,
                        label: '本场直播',
                        shortLabel: '本场'
                    };
                }
                return (data.stats?.total_messages > 0) || (data.messages?.length > 0);
            } catch (error) {
                if (error?.name === 'AbortError') {
                    return false;
                }
                console.error('加载用户消息失败:', error);
                this.userSearchError = '搜索失败，请稍后重试';
                return false;
            } finally {
                if (this._userMessagesAbortController === controller) {
                    this.userMessagesLoading = false;
                }
            }
        },
        async switchUserMessagesScope(nextScope) {
            if (this.userCardScopeMode === nextScope) return;
            if (nextScope === 'focus' && !this.userCardFocusSpec) return;
            this.userCardScopeMode = nextScope;
            this.userMessagesQuery.session_id = (nextScope === 'focus' && this.userCardFocusSpec?.kind === 'session')
                ? this.userCardFocusSpec.sessionId
                : null;
            this.userMessagesQuery.start_date = (nextScope === 'focus' && this.userCardFocusSpec?.kind === 'date_range')
                ? this.userCardFocusSpec.startDate
                : null;
            this.userMessagesQuery.end_date = (nextScope === 'focus' && this.userCardFocusSpec?.kind === 'date_range')
                ? this.userCardFocusSpec.endDate
                : null;
            this.userMessagesData.pagination.page = 1;
            await this.loadUserMessages();
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
        formatDiamondValue(value) {
            return window.HubUI.formatDiamondValue(value);
        },
        formatUserMessageTime(dateStr) {
            return window.HubUI.formatShortMessageTime(dateStr);
        },
        formatAgeRange(val) {
            const map = { 0: '-', 1: '<18', 2: '18-23', 3: '24-30', 4: '31-40', 5: '41-50', 6: '>50' };
            return map[val] || '-';
        },
        isBlankNickname(name) {
            return window.HubUI.isBlankNickname(name);
        },
        formatDisplayName(name, fallback = '[空白昵称]') {
            return window.HubUI.formatDisplayName(name, fallback);
        },
        formatAvatarInitial(name, fallbackId = 'U') {
            return window.HubUI.formatAvatarInitial(name, fallbackId);
        },
        parseChatSegments(text, emojiList) {
            return window.HubUI.parseChatSegments(text, emojiList);
        },
        onEmojiError(event) {
            window.HubUI.handleEmojiError(event);
        }
    }
});
