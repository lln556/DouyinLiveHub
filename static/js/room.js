/**
 * 房间详情页 - 实时数据与冷启动回填逻辑
 */
const app = new Vue({
    el: '#app',
    data: {
        liveId: null,
        live_id: null,
        room: null,
        loading: true,
        actionLoading: false,
        recentMessagesLoading: false,
        activeTab: 'all',
        activeRankTab: 'gift',
        messages: [],
        lastSession: null,
        previewSession: null,
        offlineContributors: [],
        offlineLikeRank: [],
        sessions: [],
        showSessionsModal: false,
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
        pendingRoomState: null,
        socket: null,
        roomInfoRefreshTimer: null,
        nowTickTimer: null,
        nowTick: Date.now(),
        isAtBottom: true,
        unreadCount: 0,
        userNameSearch: '',
        userSearchLoading: false,
        userSearchError: '',
        userSearchResults: [],
        userSearchHasSearched: false,
        userSearchFocused: false,
        userSearchBlurTimer: null,
        userSearchDebounceTimer: null,
        userSearchRequestSeq: 0,
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
            session_id: null
        },
        maxRealtimeMessages: 500
    },
    computed: {
        isArchived() {
            return Boolean(this.room && (this.room.is_archived || this.room.status === 'archived' || Boolean(this.room.archived_at)));
        },
        isOfflinePreview() {
            return Boolean(!this.currentSession && this.previewSession);
        },
        displayedContributors() {
            if (this.currentSession) {
                return this.stats.contributorInfo || [];
            }
            if (this.stats.contributorInfo?.length > 0) {
                return this.stats.contributorInfo;
            }
            return this.offlineContributors || [];
        },
        displayedLikeRank() {
            if (this.currentSession) {
                return this.stats.likeRankInfo || [];
            }
            if (this.stats.likeRankInfo?.length > 0) {
                return this.stats.likeRankInfo;
            }
            return this.offlineLikeRank || [];
        },
        isMonitorActive() {
            if (!this.room || this.isArchived) return false;
            return this.room.is_active === true || this.room.status === 'monitoring';
        },
        canStartMonitoring() {
            return Boolean(this.room && !this.isArchived && !this.isMonitorActive);
        },
        showLiveMetricCards() {
            return Boolean(
                this.currentSession ||
                Number(this.stats.currentUserCount || 0) > 0 ||
                Number(this.stats.totalLikeCount || 0) > 0 ||
                Number(this.stats.contributorCount || 0) > 0
            );
        },
        roomStatusNote() {
            if (!this.room?.error_message) {
                return '';
            }
            if (['offline', 'waiting', 'error'].includes(this.room.status)) {
                return this.room.error_message;
            }
            return '';
        },
        chatMessageCount() {
            let count = 0;
            for (const msg of this.messages) {
                if (msg.type === 'chat') count++;
            }
            return count;
        },
        giftMessageCount() {
            let count = 0;
            for (const msg of this.messages) {
                if (msg.type === 'gift') count++;
            }
            return count;
        },
        filteredMessages() {
            if (this.activeTab === 'chat') {
                return this.messages.filter((m) => m.type === 'chat');
            }
            if (this.activeTab === 'gift') {
                return this.messages.filter((m) => m.type === 'gift');
            }
            return this.messages;
        },
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
        userCardScopeCaption() {
            if (this.userMessagesLoading || !this.userMessagesData?.scope_summary) {
                return '';
            }
            if (this.userCardDisplayStats.showScopeBar) {
                if (this.userCardScopeMode === 'focus') {
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
                currentLiveId: this.liveId
            });
        }
    },
    watch: {
        showUserMessagesModal(val) {
            this.$nextTick(() => {
                window.HubUI.syncModalDialog(this.$refs.userMessagesModal, val, {
                    getClosedBy: () => 'any',
                    onDismiss: () => this.closeUserMessagesModal()
                });
            });
        },
        showSessionsModal(val) {
            this.$nextTick(() => {
                window.HubUI.syncModalDialog(this.$refs.sessionsModal, val, {
                    getClosedBy: () => 'any',
                    onDismiss: () => this.closeSessionsModal()
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
                this.searchUserByName({ silent: true });
            }, 350);
        }
    },
    mounted() {
        this._msgSeq = 0;
        this._scrollRafId = null;
        this._roomInfoSeq = 0;
        this._wsStatsVersion = 0;
        this._sessionSyncInFlight = false;
        this._prefilledSessionId = null;
        this._previewedSessionId = null;
        this._isOfflinePreviewMessages = false;
        this._recentMsgSeq = 0;
        this._userSearchAbortController = null;
        this._userMessagesAbortController = null;
        this._messagesResizeObserver = null;

        const metaEl = document.querySelector('meta[name="live-id"]');
        this.liveId = metaEl ? metaEl.content : '';
        this.live_id = this.liveId;

        this.loadRoomInfo();
        this.loadCurrentSession();
        this.loadSessionContributors();
        this.initSocket();

        this.roomInfoRefreshTimer = setInterval(() => {
            if (!document.hidden) {
                this.loadRoomInfo({ loadHistory: false, syncSession: true, background: true });
            }
        }, 10000);

        this.nowTickTimer = setInterval(() => {
            this.nowTick = Date.now();
        }, 30000);

        this._visibilityHandler = () => {
            if (!document.hidden) {
                this.nowTick = Date.now();
                this.loadRoomInfo({ loadHistory: false, syncSession: true, background: true });
            }
        };
        document.addEventListener('visibilitychange', this._visibilityHandler);

        this._keydownHandler = (e) => {
            if (e.key === 'Escape' && this.userSearchFocused && !this.showUserMessagesModal && !this.showSessionsModal) {
                this.userSearchFocused = false;
            }
        };
        document.addEventListener('keydown', this._keydownHandler);

        this.$nextTick(() => {
            const container = this.getMessagesContainer();
            if (container) {
                container.addEventListener('scroll', this.handleScroll, { passive: true });
                if (typeof ResizeObserver !== 'undefined') {
                    this._messagesResizeObserver = new ResizeObserver(() => {
                        if (this.isAtBottom) {
                            container.scrollTop = container.scrollHeight;
                        }
                    });
                    this._messagesResizeObserver.observe(container);
                }
            }
        });
    },
    beforeDestroy() {
        this.socket?.disconnect();
        if (this.userSearchDebounceTimer) {
            clearTimeout(this.userSearchDebounceTimer);
        }
        if (this.userSearchBlurTimer) {
            clearTimeout(this.userSearchBlurTimer);
        }
        if (this.roomInfoRefreshTimer) {
            clearInterval(this.roomInfoRefreshTimer);
        }
        if (this.nowTickTimer) {
            clearInterval(this.nowTickTimer);
        }
        if (this._scrollRafId) {
            cancelAnimationFrame(this._scrollRafId);
            this._scrollRafId = null;
        }
        this._userSearchAbortController?.abort();
        this._userMessagesAbortController?.abort();
        this._messagesResizeObserver?.disconnect();
        this._messagesResizeObserver = null;
        if (this._visibilityHandler) {
            document.removeEventListener('visibilitychange', this._visibilityHandler);
        }
        if (this._keydownHandler) {
            document.removeEventListener('keydown', this._keydownHandler);
        }
        const container = this.getMessagesContainer();
        if (container) {
            container.removeEventListener('scroll', this.handleScroll);
        }
    },
    methods: {
        getMessagesContainer() {
            return this.$refs.messagesContainer || document.querySelector('.messages-container');
        },
        onDialogClose(kind) {
            if (kind === 'user' && this.showUserMessagesModal) {
                this.closeUserMessagesModal();
            } else if (kind === 'sessions' && this.showSessionsModal) {
                this.closeSessionsModal();
            }
        },
        onUserSearchBlur() {
            if (this.userSearchBlurTimer) {
                clearTimeout(this.userSearchBlurTimer);
            }
            this.userSearchBlurTimer = window.setTimeout(() => {
                this.userSearchFocused = false;
            }, 180);
        },
        applyPendingRoomState() {
            if (!this.room || !this.pendingRoomState) return;
            if (this.pendingRoomState.room_status) {
                this.room.status = this.pendingRoomState.room_status;
                if (['monitoring', 'offline', 'waiting'].includes(this.pendingRoomState.room_status)) {
                    this.room.is_active = true;
                } else if (['stopped', 'error', 'archived'].includes(this.pendingRoomState.room_status)) {
                    this.room.is_active = false;
                }
            }
            if (Object.hasOwn(this.pendingRoomState, 'room_error_message')) {
                this.room.error_message = this.pendingRoomState.room_error_message;
            }
            this.pendingRoomState = null;
        },
        async loadRoomInfo(options = {}) {
            const reqSeq = ++this._roomInfoSeq;
            const wsVersionBefore = this._wsStatsVersion;
            const historyPromise = options.loadHistory !== false ? this.loadHistorySessions() : Promise.resolve();
            try {
                const { data } = await window.HubUI.requestJson(
                    `/api/rooms/${encodeURIComponent(this.liveId)}`,
                    options.background ? { priority: 'low' } : {}
                );
                if (reqSeq !== this._roomInfoSeq) {
                    return;
                }

                if (data.room) {
                    this.room = data.room;
                    this.applyPendingRoomState();
                    if (data.room.stats && this._wsStatsVersion === wsVersionBefore) {
                        this.mergeStats(data.room.stats, { preserveEmptyContributors: true });
                    }
                    const anchorName = data.room.anchor_name || this.liveId;
                    document.title = `${anchorName} - 直播详情`;

                    if (options.syncSession && data.room.status !== 'monitoring' && this.currentSession) {
                        await this.loadCurrentSession();
                    }
                }
            } catch (error) {
                console.error('加载房间信息失败:', error);
            } finally {
                if (reqSeq === this._roomInfoSeq) {
                    this.loading = false;
                }
            }
            await historyPromise;
        },
        async loadCurrentSession() {
            if (this._sessionSyncInFlight) return;
            this._sessionSyncInFlight = true;
            const wsVersionBefore = this._wsStatsVersion;
            const hadLive = Boolean(this.currentSession?.status === 'live');
            try {
                const { data } = await window.HubUI.requestJson(
                    `/api/rooms/${encodeURIComponent(this.liveId)}/current-session`
                );
                if (this._wsStatsVersion !== wsVersionBefore) {
                    return;
                }
                if (data.session?.status === 'live') {
                    this.clearOfflinePreviewForLive();
                    this.currentSession = data.session;
                    if (data.session.id && this._prefilledSessionId !== data.session.id) {
                        this._prefilledSessionId = data.session.id;
                        this.loadRecentSessionMessages(data.session.id, { isPreview: false });
                    }
                } else {
                    this.currentSession = null;
                    if (hadLive) {
                        await this.loadHistorySessions();
                    }
                    const fallbackSession = data.session?.id ? data.session : this.lastSession;
                    if (fallbackSession?.id) {
                        this.activateOfflinePreview(fallbackSession);
                    }
                }
            } catch (error) {
                console.error('加载当前直播场次失败:', error);
            } finally {
                this._sessionSyncInFlight = false;
            }
        },
        clearOfflinePreviewForLive() {
            if (this._isOfflinePreviewMessages) {
                this.messages = [];
                this._isOfflinePreviewMessages = false;
            }
            this.previewSession = null;
            this._previewedSessionId = null;
        },
        activateOfflinePreview(sessionObj) {
            if (!sessionObj?.id || this.currentSession) return;
            this.previewSession = sessionObj;
            if (this._previewedSessionId === sessionObj.id) return;
            this._previewedSessionId = sessionObj.id;
            this.loadRecentSessionMessages(sessionObj.id, { isPreview: true });
            this.loadOfflinePreviewRanks(sessionObj.id);
        },
        async loadOfflinePreviewRanks(sessionId) {
            if (!sessionId || this.currentSession) return;
            const contributorsReq = fetch(`/api/rooms/sessions/${encodeURIComponent(sessionId)}?type=contributors`)
                .then((r) => (r.ok ? r.json() : null));
            const likersReq = fetch(`/api/rooms/top-likers?live_id=${encodeURIComponent(this.liveId)}&limit=50`)
                .then((r) => (r.ok ? r.json() : null));
            const [contribRes, likerRes] = await Promise.allSettled([contributorsReq, likersReq]);

            if (
                contribRes.status === 'fulfilled' &&
                contribRes.value &&
                !this.currentSession &&
                this.previewSession?.id === sessionId &&
                Array.isArray(contribRes.value.contributors)
            ) {
                this.offlineContributors = contribRes.value.contributors.map((c, idx) => Object.freeze({
                    rank: idx + 1,
                    user_id: c.user_id,
                    user: c.nickname || c.user_name || c.user_id,
                    score: c.contribution_value || 0,
                    avatar: c.user_avatar || null,
                    user_level: c.user_level || 0,
                    fans_club_level: c.fans_club_level || 0
                }));
            }

            if (
                likerRes.status === 'fulfilled' &&
                likerRes.value &&
                !this.currentSession &&
                Array.isArray(likerRes.value.likers)
            ) {
                this.offlineLikeRank = likerRes.value.likers.map((l, idx) => Object.freeze({
                    rank: idx + 1,
                    user_id: l.user_id,
                    user: l.user_name || l.nickname || l.user_id,
                    like_count: l.like_count || 0,
                    avatar: l.user_avatar || null,
                    user_level: l.user_level || 0,
                    fans_club_level: l.fans_club_level || 0
                }));
            }
        },
        async loadRecentSessionMessages(sessionId, options = {}) {
            const isPreview = Boolean(options.isPreview);
            if (!sessionId) return;
            const reqSeq = ++this._recentMsgSeq;
            this.recentMessagesLoading = true;
            try {
                const params = new URLSearchParams({
                    type: 'all',
                    session_id: String(sessionId),
                    limit: '50'
                });
                const { ok, data } = await window.HubUI.requestJson(
                    `/api/rooms/${encodeURIComponent(this.liveId)}/messages?${params}`
                );
                if (!ok || reqSeq !== this._recentMsgSeq) return;
                if (isPreview && (this.currentSession || this.previewSession?.id !== sessionId)) {
                    return;
                }
                if (!isPreview && this.currentSession?.id !== sessionId) {
                    return;
                }
                const rawList = Array.isArray(data.messages) ? data.messages : [];
                if (rawList.length === 0) {
                    if (isPreview) {
                        this._isOfflinePreviewMessages = true;
                        this.messages = [];
                    }
                    return;
                }

                // 后端按倒序返回最新 50 条，翻转为时间正序后铺底到实时流之前
                const historical = rawList.toReversed().map((item) => Object.freeze({
                    type: item.type || 'chat',
                    user: item.user || item.user_name || '匿名用户',
                    user_id: item.user_id || null,
                    user_level: item.user_level || 0,
                    fans_club_level: item.fans_club_level || 0,
                    text: item.text !== undefined && item.text !== null ? item.text : item.content,
                    emoji_list: Array.isArray(item.emoji_list) ? item.emoji_list : null,
                    is_gift_user: Boolean(item.is_gift_user),
                    gift_name: item.gift_name,
                    gift_count: item.gift_count,
                    gift_price: item.gift_price,
                    total_value: item.total_value,
                    session_id: sessionId,
                    is_history: true,
                    is_preview: isPreview,
                    _id: ++this._msgSeq
                }));

                if (isPreview) {
                    this._isOfflinePreviewMessages = true;
                    this.messages = historical;
                } else {
                    const baseMessages = this._isOfflinePreviewMessages ? [] : this.messages;
                    this._isOfflinePreviewMessages = false;
                    const merged = historical.concat(baseMessages);
                    if (merged.length > this.maxRealtimeMessages) {
                        merged.splice(0, merged.length - this.maxRealtimeMessages);
                    }
                    this.messages = merged;
                }
                this.$nextTick(() => this.scrollToBottom());
            } catch (error) {
                console.error('加载近期消息失败:', error);
            } finally {
                if (reqSeq === this._recentMsgSeq) {
                    this.recentMessagesLoading = false;
                }
            }
        },
        async loadHistorySessions() {
            try {
                const { data } = await window.HubUI.requestJson(
                    `/api/rooms/${encodeURIComponent(this.liveId)}/sessions?limit=15`
                );

                if (data.sessions) {
                    this.sessions = data.sessions;
                    if (data.sessions.length === 0) {
                        this.lastSession = null;
                        return;
                    }
                    const endedSessions = data.sessions.filter((s) => s.status === 'ended');
                    this.lastSession = endedSessions.length > 0 ? endedSessions[0] : data.sessions[0];
                    if (!this.currentSession && this.lastSession?.id) {
                        this.activateOfflinePreview(this.lastSession);
                    }
                }
            } catch (error) {
                console.error('加载历史场次失败:', error);
            }
        },
        async startMonitoring() {
            if (this.actionLoading) return;
            this.actionLoading = true;
            try {
                const { ok, data } = await window.HubUI.requestJson(
                    `/api/rooms/${encodeURIComponent(this.liveId)}/start`,
                    { method: 'POST' }
                );

                if (ok) {
                    window.HubUI.toast('已启动房间监控', 'success');
                    await this.loadRoomInfo();
                } else {
                    window.HubUI.toast(data.error || '启动失败', 'error');
                }
            } catch (error) {
                window.HubUI.toast('启动失败: ' + error.message, 'error');
            } finally {
                this.actionLoading = false;
            }
        },
        async stopMonitoring() {
            if (this.actionLoading) return;
            const confirmed = await window.HubUI.confirm({
                title: '停止房间监控',
                message: '确定要停止监控当前直播间吗？未开播轮询也会随之停止。',
                confirmText: '停止监控',
                danger: true
            });
            if (!confirmed) return;

            this.actionLoading = true;
            try {
                const { ok, data } = await window.HubUI.requestJson(
                    `/api/rooms/${encodeURIComponent(this.liveId)}/stop`,
                    { method: 'POST' }
                );

                if (ok) {
                    window.HubUI.toast('已停止房间监控', 'info');
                    await Promise.all([
                        this.loadRoomInfo(),
                        this.loadCurrentSession()
                    ]);
                } else {
                    window.HubUI.toast(data.error || '停止失败', 'error');
                }
            } catch (error) {
                window.HubUI.toast('停止失败: ' + error.message, 'error');
            } finally {
                this.actionLoading = false;
            }
        },
        async restoreRoom() {
            if (this.actionLoading) return;
            const confirmed = await window.HubUI.confirm({
                title: '恢复到控制台',
                message: '确定要把此直播间恢复到首页控制台吗？恢复后可重新启动监控。',
                confirmText: '确认恢复'
            });
            if (!confirmed) return;

            this.actionLoading = true;
            try {
                const { ok, data } = await window.HubUI.requestJson(
                    `/api/rooms/${encodeURIComponent(this.liveId)}/restore`,
                    { method: 'POST' }
                );

                if (ok) {
                    window.HubUI.toast('已恢复到首页控制台', 'success');
                    await this.loadRoomInfo();
                } else {
                    window.HubUI.toast(data.error || '恢复失败', 'error');
                }
            } catch (error) {
                window.HubUI.toast('恢复失败: ' + error.message, 'error');
            } finally {
                this.actionLoading = false;
            }
        },
        async loadSessionContributors() {
            const wsVersionBefore = this._wsStatsVersion;
            try {
                const { data } = await window.HubUI.requestJson(
                    `/api/rooms/${encodeURIComponent(this.liveId)}/session-contributors?limit=100`
                );
                if (this._wsStatsVersion !== wsVersionBefore) {
                    return;
                }

                if (data.contributors?.length > 0) {
                    const patch = {
                        contributorInfo: data.contributors.map((c, index) => Object.freeze({
                            rank: index + 1,
                            user_id: c.user_id,
                            user: c.nickname || c.user_id,
                            score: c.contribution_value,
                            avatar: c.user_avatar,
                            user_level: c.user_level || 0,
                            fans_club_level: c.fans_club_level || 0
                        }))
                    };
                    if (!this.stats.contributorCount) {
                        patch.contributorCount = data.total || data.contributors.length;
                    }
                    this.mergeStats(patch);
                }
            } catch (error) {
                console.error('加载场次贡献榜失败:', error);
            }
        },
        initSocket() {
            this.socket?.disconnect();
            if (typeof io === 'undefined') {
                console.warn('Socket.IO 客户端库未加载');
                return;
            }

            this.socket = io({ transports: ['websocket', 'polling'] });

            this.socket.on('connect', () => {
                this.socket.emit('join', { live_id: this.liveId });
            });

            this.socket.on(`room_${this.liveId}`, (data) => {
                this.handleMessage(data);
            });

            this.socket.on(`room_${this.liveId}_stats`, (data) => {
                if (!data) return;
                this._wsStatsVersion += 1;

                if (this.room) {
                    if (data.room_status) {
                        this.room.status = data.room_status;
                        if (['monitoring', 'offline', 'waiting'].includes(data.room_status)) {
                            this.room.is_active = true;
                        } else if (['stopped', 'error', 'archived'].includes(data.room_status)) {
                            this.room.is_active = false;
                        }
                    }
                    if (Object.hasOwn(data, 'room_error_message')) {
                        this.room.error_message = data.room_error_message;
                    }
                } else {
                    this.pendingRoomState = {
                        room_status: data.room_status,
                        ...(Object.hasOwn(data, 'room_error_message')
                            ? { room_error_message: data.room_error_message }
                            : {})
                    };
                }

                const wasLive = Boolean(this.currentSession?.status === 'live');
                const isNextLive = Boolean(data.current_session?.status === 'live');

                this.mergeStats(data, { sessionEnded: !isNextLive });

                if (isNextLive) {
                    this.clearOfflinePreviewForLive();
                    this.currentSession = data.current_session;
                    if (data.current_session.id && this._prefilledSessionId !== data.current_session.id) {
                        this._prefilledSessionId = data.current_session.id;
                        this.loadRecentSessionMessages(data.current_session.id, { isPreview: false });
                    }
                } else {
                    this.currentSession = null;
                    if (wasLive || !this.lastSession) {
                        this.loadHistorySessions();
                    } else if (this.lastSession?.id) {
                        this.activateOfflinePreview(this.lastSession);
                    }
                }
            });
        },
        mergeStats(rawStats, options = {}) {
            if (!rawStats) return;

            const pick = (camelKey, snakeKey) => {
                if (Object.hasOwn(rawStats, camelKey)) {
                    return rawStats[camelKey];
                }
                if (Object.hasOwn(rawStats, snakeKey)) {
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

            for (const [camelKey, snakeKey] of [
                ['currentUserCount', 'current_user_count'],
                ['totalUserCount', 'total_user_count'],
                ['totalLikeCount', 'total_like_count'],
                ['totalIncome', 'total_income'],
                ['contributorCount', 'contributor_count']
            ]) {
                const value = pick(camelKey, snakeKey);
                if (value !== undefined && value !== null && value !== '') {
                    nextStats[camelKey] = value;
                }
            }

            const contributorInfo = pick('contributorInfo', 'contributor_info');
            if (Array.isArray(contributorInfo)) {
                const shouldKeepExistingList = (
                    !options.sessionEnded &&
                    contributorInfo.length === 0 &&
                    nextStats.contributorInfo.length > 0 &&
                    (
                        options.preserveEmptyContributors ||
                        Number(nextStats.contributorCount || 0) > 0
                    )
                );

                if (!shouldKeepExistingList) {
                    nextStats.contributorInfo = contributorInfo.map((item) => Object.freeze(item));
                }
            }

            const likeRankInfo = pick('likeRankInfo', 'like_rank_list');
            if (Array.isArray(likeRankInfo)) {
                const shouldKeepExistingLikeList = (
                    !options.sessionEnded &&
                    likeRankInfo.length === 0 &&
                    nextStats.likeRankInfo.length > 0 &&
                    options.preserveEmptyContributors
                );

                if (!shouldKeepExistingLikeList) {
                    nextStats.likeRankInfo = likeRankInfo.map((item) => Object.freeze(item));
                }
            }

            this.stats = nextStats;
        },
        formatMessageBody(msg) {
            if (!msg) return '';
            if (msg.type === 'chat') {
                if (msg.text !== undefined && msg.text !== null) {
                    return String(msg.text);
                }
                if (msg.content !== undefined && msg.content !== null) {
                    return String(msg.content).replace(/<[^>]*>/g, '');
                }
                return '';
            }
            if (msg.type === 'gift') {
                if (msg.gift_name) {
                    if (msg.is_combo) {
                        const totalQty = msg.total_gift_count || msg.gift_count || 0;
                        if (msg.is_combo_end) {
                            const comboVal = msg.total_combo_value !== undefined
                                ? msg.total_combo_value
                                : (msg.total_value || 0);
                            return `连击完成！赠送了 ${totalQty} 个 ${msg.gift_name} (价值${comboVal}钻石)`;
                        }
                        const deltaQty = msg.partial_count || msg.gift_count || 0;
                        return `连击中... ${msg.gift_name}x${totalQty} (本次+${deltaQty})`;
                    }
                    const qty = msg.gift_count || 1;
                    const totalVal = msg.total_value !== undefined
                        ? msg.total_value
                        : ((msg.gift_price || 0) * qty);
                    return `赠送了 ${qty} 个 ${msg.gift_name} (价值${totalVal}钻石)`;
                }
                if (msg.text !== undefined && msg.text !== null) {
                    return String(msg.text);
                }
                if (msg.content !== undefined && msg.content !== null) {
                    return String(msg.content).replace(/<[^>]*>/g, '');
                }
            }
            return '';
        },
        handleMessage(data) {
            if (!data) return;
            this.clearOfflinePreviewForLive();
            const entry = Object.freeze({
                ...data,
                _id: ++this._msgSeq,
                user_id: data.user_id || null,
                user_level: data.user_level || 0,
                fans_club_level: data.fans_club_level || 0
            });
            this.messages.push(entry);

            if (this.messages.length > this.maxRealtimeMessages) {
                this.messages.splice(0, this.messages.length - this.maxRealtimeMessages);
            }

            if (!this.isAtBottom) {
                this.unreadCount++;
                return;
            }

            if (!this._scrollRafId) {
                this._scrollRafId = window.requestAnimationFrame(() => {
                    this._scrollRafId = null;
                    const container = this.getMessagesContainer();
                    if (container && this.isAtBottom) {
                        container.scrollTop = container.scrollHeight;
                    }
                });
            }
        },
        onStreamUserClick(msg) {
            if (!msg) return;
            const userId = msg.user_id || null;
            const userName = msg.user || msg.user_name || null;
            if (!userId && !userName) return;
            const fromPreview = Boolean(msg.is_preview || (!this.currentSession && this.previewSession));
            const sessionId = msg.session_id
                || (this.currentSession?.status === 'live' ? this.currentSession.id : (this.previewSession?.id || null));
            this.openUserMessagesModal(userId, userName || userId, {
                sessionId,
                userName,
                defaultScope: sessionId ? 'focus' : 'all',
                fromPreview
            });
        },
        handleScroll() {
            const container = this.getMessagesContainer();
            if (!container) return;

            const threshold = 50;
            const isBottom = container.scrollHeight - container.scrollTop - container.clientHeight < threshold;

            if (isBottom !== this.isAtBottom) {
                this.isAtBottom = isBottom;
                if (isBottom) {
                    this.unreadCount = 0;
                }
            }
        },
        scrollToBottom() {
            const container = this.getMessagesContainer();
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
            return `${window.HubUI.formatInteger(value)} 钻石`;
        },
        formatNumber(value) {
            return window.HubUI.formatCompactNumber(value);
        },
        getBadgeClass(status) {
            switch (status) {
                case 'monitoring': return 'is-live';
                case 'offline':
                case 'waiting': return 'is-waiting';
                case 'error': return 'is-error';
                default: return 'is-stopped';
            }
        },
        getStatusText(status) {
            switch (status) {
                case 'monitoring': return '运行中';
                case 'stopped': return '已停止';
                case 'offline':
                case 'waiting': return '等待开播';
                case 'error': return '异常';
                case 'archived': return '已归档';
                default: return status;
            }
        },
        getMessageClass(message) {
            if (message.type === 'gift') {
                return 'gift-message';
            }
            if (message.is_gift_user) {
                return 'gift-user-message';
            }
            return 'chat-message';
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
        formatDateTime(dateStr) {
            return window.HubUI.formatDateTime(dateStr);
        },
        getDuration(startTime, endTime, tickNow = null) {
            if (!startTime) return '-';
            const start = new Date(startTime);
            const end = endTime ? new Date(endTime) : new Date(tickNow || Date.now());
            const diff = Math.max(0, end - start);

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
        async searchUserByName(options = {}) {
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

            this._userSearchAbortController?.abort();
            const controller = typeof AbortController !== 'undefined' ? new AbortController() : null;
            this._userSearchAbortController = controller;

            this.userSearchLoading = true;
            const requestSeq = ++this.userSearchRequestSeq;
            try {
                const params = new URLSearchParams({
                    user_name: userName,
                    limit: 30
                });
                if (this.currentSession?.id) {
                    params.append('session_id', this.currentSession.id);
                }
                const { ok, data } = await window.HubUI.requestJson(
                    `/api/rooms/${encodeURIComponent(this.liveId)}/user-search?${params}`,
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
        selectSearchedUser(user) {
            this.userSearchError = '';
            this.userSearchFocused = false;
            this.userSearchResults = [];
            if (this.currentSession?.status === 'live') {
                this.openUserMessagesModal(user.user_id, user.nickname || user.user_id, {
                    sessionId: user.session_id || this.currentSession.id,
                    defaultScope: 'focus'
                });
            } else {
                this.openUserMessagesModal(user.user_id, user.nickname || user.user_id, {
                    defaultScope: 'all'
                });
            }
        },
        async openUserMessagesModal(userId, userName, sessionOrOpts = null) {
            let explicitSessionId = null;
            let fallbackUserName = null;
            let defaultScope = null;
            let fromPreview = false;
            if (sessionOrOpts && typeof sessionOrOpts === 'object') {
                explicitSessionId = sessionOrOpts.sessionId || null;
                fallbackUserName = sessionOrOpts.userName || null;
                if (sessionOrOpts.defaultScope) {
                    defaultScope = sessionOrOpts.defaultScope;
                } else if (sessionOrOpts.allSessions) {
                    defaultScope = 'all';
                }
                fromPreview = Boolean(sessionOrOpts.fromPreview);
            } else if (sessionOrOpts) {
                explicitSessionId = sessionOrOpts;
            }

            let focusSpec = null;
            let scopeMode = 'all';

            if (this.currentSession?.status === 'live' && this.currentSession.id) {
                focusSpec = {
                    kind: 'session',
                    sessionId: explicitSessionId || this.currentSession.id,
                    statusSnapshot: 'live',
                    startTimeSnapshot: this.currentSession.start_time || null,
                    label: '本场直播',
                    shortLabel: '本场'
                };
                scopeMode = defaultScope || 'focus';
            } else if (this.previewSession?.id) {
                focusSpec = {
                    kind: 'session',
                    sessionId: explicitSessionId || this.previewSession.id,
                    statusSnapshot: 'ended',
                    startTimeSnapshot: this.previewSession.start_time || null,
                    label: '上一场',
                    shortLabel: '上一场'
                };
                scopeMode = defaultScope || 'focus';
            } else if (explicitSessionId) {
                focusSpec = {
                    kind: 'session',
                    sessionId: explicitSessionId,
                    statusSnapshot: fromPreview ? 'ended' : 'live',
                    startTimeSnapshot: null,
                    label: fromPreview ? '上一场' : '本场直播',
                    shortLabel: fromPreview ? '上一场' : '本场'
                };
                scopeMode = defaultScope || 'focus';
            }

            this.showUserMessagesModal = true;
            this.userMessagesLoading = true;
            this.userMessagesTab = 'all';
            this.userCardFocusSpec = focusSpec;
            this.userCardScopeMode = scopeMode;
            this.userMessagesQuery.user_id = userId || null;
            this.userMessagesQuery.user_name = userId ? null : (fallbackUserName || userName || null);
            this.userMessagesQuery.session_id = (scopeMode === 'focus' && focusSpec?.kind === 'session')
                ? focusSpec.sessionId
                : null;

            this.resetUserMessagesData(userId || null, userName || fallbackUserName);
            await this.loadUserMessages();
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
                session_id: null
            };
        },
        async loadUserMessages(options = {}) {
            const showLoading = options.showLoading !== false;
            if (!this.userMessagesQuery.user_id && !this.userMessagesQuery.user_name) return false;

            this._userMessagesAbortController?.abort();
            const controller = typeof AbortController !== 'undefined' ? new AbortController() : null;
            this._userMessagesAbortController = controller;

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

                if (this.userCardFocusSpec?.kind === 'session' && this.userCardFocusSpec.sessionId) {
                    params.append('anchor_session_id', String(this.userCardFocusSpec.sessionId));
                    if (this.userCardScopeMode === 'focus') {
                        params.append('session_id', String(this.userCardFocusSpec.sessionId));
                    }
                }

                const { data } = await window.HubUI.requestJson(
                    `/api/rooms/${encodeURIComponent(this.liveId)}/user-messages?${params}`,
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
                if (showLoading && this._userMessagesAbortController === controller) {
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
        formatChatSegments(msg) {
            return window.HubUI.parseChatSegments(this.formatMessageBody(msg), msg?.emoji_list);
        },
        parseChatSegments(text, emojiList) {
            return window.HubUI.parseChatSegments(text, emojiList);
        },
        onEmojiError(event) {
            window.HubUI.handleEmojiError(event);
        },
        openSessionsModal() {
            this.showSessionsModal = true;
        },
        closeSessionsModal() {
            this.showSessionsModal = false;
        }
    }
});
