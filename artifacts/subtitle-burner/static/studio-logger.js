/**
 * @fileoverview Comprehensive 5-layer frontend telemetry engine for Subtitle Assistant Studio.
 * Exposes a global StudioLogger object for UI action tracking, media diagnostics, clip sequence logging,
 * network request tracking, and error recovery auditing.
 */
(function() {
    'use strict';

    const MAX_ACTION_ENTRIES = 500;
    const globalLogBuffer = [];
    
    let isFetchLoggingEnabled = false;
    let originalFetch = null;
    let seekStartTime = 0;
    let seekStartPos = 0;

    /**
     * Helper to format the current time as HH:MM:SS.mmm
     * @returns {string} Formatted time string
     */
    function getFormattedTime() {
        const now = new Date();
        const pad = (n, len=2) => n.toString().padStart(len, '0');
        return `${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}.${pad(now.getMilliseconds(), 3)}`;
    }

    /**
     * Appends a log to the internal buffers and outputs it to the console with colors.
     * @param {string} layer - The layer name (ACTION, MEDIA, CLIP-SEQ, NET, ERROR).
     * @param {string} message - The formatted message body.
     * @param {string} color - The CSS color for console output.
     */
    function _log(layer, message, color) {
        const time = getFormattedTime();
        let logString = `[${layer}] ${time} | ${message}`;
        
        // Exclude the time prefix for specific formats if requested, but let's stick to standard for consistency,
        // or adapt slightly based on prompt requirement. The prompt asks for:
        // ACTION: [ACTION] 17:24:05.123 | btn:reframeSwapDiarBtn | clicked
        // MEDIA: [MEDIA] ⏩ Seek 25.00s → 65.00s | latency: 4.2ms | buffered: 0-240s
        // CLIP-SEQ: [CLIP-SEQ] Segment B trimmed: end 80.0s → 78.5s (-1.5s) | total: 3 segments
        // NET: [NET] POST /render | 200 OK | 145ms | payload: 2.1KB
        // ERROR: [ERROR] render_job failed: FFmpeg exit 1 | retry: 0/3 | shown: "Reframe failed"
        
        if (layer === 'ACTION') {
            logString = `[${layer}] ${time} | ${message}`;
            window._studioActionLog.push(logString);
            if (window._studioActionLog.length > MAX_ACTION_ENTRIES) {
                window._studioActionLog.shift();
            }
        } else if (layer === 'MEDIA') {
            logString = `[${layer}] ${message}`;
        } else if (layer === 'CLIP-SEQ') {
            logString = `[${layer}] ${message}`;
        } else if (layer === 'NET') {
            logString = `[${layer}] ${message}`;
        } else if (layer === 'ERROR') {
            logString = `[${layer}] ${message}`;
        }

        globalLogBuffer.push(logString);
        console.debug(`%c[${layer}]`, `color: ${color}; font-weight: bold;`, layer === 'ACTION' ? `${time} | ${message}` : message);
    }

    const StudioLogger = {
        
        /**
         * Layer 1 — UI Action & State Journal
         * @param {HTMLElement|string} element - The target element or its identifier.
         * @param {string} action - The action performed (e.g., clicked, toggled).
         * @param {string} [detail] - Additional details.
         */
        action: function(element, action, detail = '') {
            const elName = typeof element === 'string' ? element : (element.id || element.className || element.tagName.toLowerCase());
            let msg = `btn:${elName} | ${action}`;
            if (detail) msg += ` | ${detail}`;
            _log('ACTION', msg, 'blue');
        },

        /**
         * Layer 2 — Media Playhead & Seek Diagnostics
         * @param {string} event - The media event.
         * @param {string} detail - Additional details.
         */
        media: function(event, detail) {
            _log('MEDIA', `${event} | ${detail}`, 'green');
        },

        /**
         * Hooks into a video element to measure seek latency and track states.
         * @param {HTMLVideoElement} videoEl - The video element to monitor.
         */
        attachMediaElement: function(videoEl) {
            if (!videoEl) return;
            
            videoEl.addEventListener('seeking', () => {
                seekStartTime = performance.now();
                seekStartPos = videoEl.currentTime;
            });
            
            videoEl.addEventListener('seeked', () => {
                const latency = (performance.now() - seekStartTime).toFixed(1);
                const endPos = videoEl.currentTime;
                
                let bufferedRanges = [];
                for (let i = 0; i < videoEl.buffered.length; i++) {
                    bufferedRanges.push(`${videoEl.buffered.start(i).toFixed(0)}-${videoEl.buffered.end(i).toFixed(0)}s`);
                }
                const bufStr = bufferedRanges.join(', ') || 'none';
                
                StudioLogger.media(`⏩ Seek ${seekStartPos.toFixed(2)}s → ${endPos.toFixed(2)}s`, `latency: ${latency}ms | buffered: ${bufStr}`);
            });
            
            videoEl.addEventListener('stalled', () => StudioLogger.media('⚠️ stalled', `currentTime: ${videoEl.currentTime}`));
            videoEl.addEventListener('waiting', () => StudioLogger.media('⏳ waiting', `currentTime: ${videoEl.currentTime}`));
            videoEl.addEventListener('error', (e) => StudioLogger.media('❌ error', `code: ${videoEl.error ? videoEl.error.code : 'unknown'}`));
        },

        /**
         * Layer 3 — Clip Sequence & Timeline State Logger
         * @param {string} action - The sequence action (e.g., Segment trimmed).
         * @param {string} detail - Additional details.
         */
        clip: function(action, detail) {
            _log('CLIP-SEQ', `${action} | ${detail}`, 'purple');
        },

        /**
         * Layer 4 — Network / Fetch Event Logger
         * @param {string} method - HTTP method.
         * @param {string} url - Request URL.
         * @param {number} status - HTTP status code.
         * @param {number} latencyMs - Time taken in milliseconds.
         * @param {string} payloadSize - Formatted payload size (e.g., "2.1KB").
         */
        net: function(method, url, status, latencyMs, payloadSize) {
            _log('NET', `${method} ${url} | ${status} | ${latencyMs.toFixed(0)}ms | payload: ${payloadSize}`, 'orange');
        },

        /**
         * Enables intercepting the global fetch function to auto-log network requests.
         */
        enableFetchLogging: function() {
            if (isFetchLoggingEnabled || !window.fetch) return;
            isFetchLoggingEnabled = true;
            originalFetch = window.fetch;
            
            window.fetch = async function(...args) {
                const startTime = performance.now();
                const reqUrl = typeof args[0] === 'string' ? args[0] : (args[0] ? args[0].url : 'unknown');
                const reqMethod = (args[1] && args[1].method) ? args[1].method.toUpperCase() : 'GET';
                
                let reqSize = '0B';
                if (args[1] && args[1].body) {
                    const bytes = new Blob([args[1].body]).size;
                    reqSize = bytes > 1024 ? (bytes / 1024).toFixed(1) + 'KB' : bytes + 'B';
                }

                try {
                    const response = await originalFetch.apply(this, args);
                    const latency = performance.now() - startTime;
                    const statusStr = `${response.status} ${response.statusText}`;
                    StudioLogger.net(reqMethod, reqUrl, statusStr, latency, reqSize);
                    return response;
                } catch (error) {
                    const latency = performance.now() - startTime;
                    StudioLogger.net(reqMethod, reqUrl, `ERROR: ${error.message}`, latency, reqSize);
                    throw error;
                }
            };
        },

        /**
         * Layer 5 — Error Recovery & Auto-Retry Audit Trail
         * @param {string} context - Where or what failed.
         * @param {Error|string} error - The error object or message.
         * @param {string} retryCount - Retry progress (e.g., "0/3").
         * @param {string} userMessage - What was shown to the user.
         */
        error: function(context, error, retryCount, userMessage) {
            const errMsg = error instanceof Error ? error.message : error;
            _log('ERROR', `${context} failed: ${errMsg} | retry: ${retryCount} | shown: "${userMessage}"`, 'red');
        },

        /**
         * Initializes auto-listeners for UI actions and sets up the circular buffer.
         */
        init: function() {
            window._studioActionLog = [];
            
            // Auto-attach listeners on DOMContentLoaded
            window.addEventListener('DOMContentLoaded', () => {
                const buttons = document.querySelectorAll('.btn');
                buttons.forEach(btn => {
                    btn.addEventListener('click', (e) => {
                        StudioLogger.action(e.currentTarget, 'clicked');
                    });
                });

                const selects = document.querySelectorAll('select');
                selects.forEach(select => {
                    select.addEventListener('change', (e) => {
                        StudioLogger.action(e.currentTarget, 'changed', `val:${e.currentTarget.value}`);
                    });
                });

                const checkboxes = document.querySelectorAll('input[type="checkbox"]');
                checkboxes.forEach(chk => {
                    chk.addEventListener('change', (e) => {
                        StudioLogger.action(e.currentTarget, 'toggled', `checked:${e.currentTarget.checked}`);
                    });
                });
                
                StudioLogger.action('StudioLogger', 'init', 'Auto-listeners attached');
            });
        },

        /**
         * Dumps all logs as a formatted string.
         * @returns {string} All collected logs separated by newlines.
         */
        dump: function() {
            return globalLogBuffer.join('\\n');
        },

        /**
         * Triggers a download of the full log as a text file.
         */
        downloadLog: function() {
            const logContent = this.dump();
            const blob = new Blob([logContent], { type: 'text/plain;charset=utf-8' });
            const url = URL.createObjectURL(blob);
            
            const a = document.createElement('a');
            a.href = url;
            a.download = `studio-log-${new Date().toISOString().replace(/:/g, '-')}.txt`;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            
            URL.revokeObjectURL(url);
        }
    };

    // Attach to global scope
    window.StudioLogger = StudioLogger;

})();
