// ==UserScript==
// @name         LMArena Proxy Injector V2
// @namespace    https://github.com/zhongruichen/lmarena-proxy
// @version      2.0.0
// @description  Enhanced version with improvements from lmarena-fd, better model extraction, and API compatibility
// @author       zhongruichen (enhanced)
// @match        https://*.lmarena.ai/*
// @icon         https://www.google.com/s2/favicons?sz=64&domain=lmarena.ai
// @grant        none
// @run-at       document-start
// @all-frames   true
// @updateURL    https://raw.githubusercontent.com/zhongruichen/lmarena-proxy/main/lmarena_injector_v2.user.js
// @downloadURL  https://raw.githubusercontent.com/zhongruichen/lmarena-proxy/main/lmarena_injector_v2.user.js
// ==/UserScript==

(function () {
    'use strict';

    // ==================== CONFIGURATION ====================
    const CONFIG = {
        SERVER_URL: "ws://localhost:9080/ws",
        
        // API版本配置（可动态切换）
        API_VERSION: 'auto', // 'auto' | 'nextjs-api' | 'api'
        
        // API端点配置
        API_ENDPOINTS: {
            'nextjs-api': {
                signup: '/nextjs-api/sign-up',
                stream: '/nextjs-api/stream/create-evaluation',
                base: 'https://lmarena.ai'
            },
            'api': {
                signup: '/api/sign-up', 
                stream: '/api/stream/create-evaluation',
                base: 'https://lmarena.ai'
            }
        },
        
        // Next.js Action IDs (从lmarena-fd更新)
        NEXT_ACTIONS: {
            signup: '40b0280bb60c443da043f8c177fb444d955700cdef',
            fileSignUrl: '702e4f657b526fa31431e485b8f44c79a52c991e69',
            fileNotify: '602dda4cf961b081ade826d193bd753fd5e5d5e7bb',
            verify: '40fc0d1f8f1f9cade86c05d74881beb408c1fbf3c4'
        },
        
        // 其他配置
        REQUIRED_COOKIE: "arena-auth-prod-v1",
        TURNSTILE_SITEKEY: '0x4AAAAAAA65vWDmG-O_lPtT',
        MAX_MODEL_SEARCH_DEPTH: 15, // 从lmarena-fd借鉴
        
        // 调试模式
        DEBUG: false
    };

    // ==================== STATE MANAGEMENT ====================
    let socket;
    let isRefreshing = false;
    let pendingRequests = [];
    let modelRegistrySent = false;
    let latestTurnstileToken = null;
    let currentAPIVersion = null; // 动态存储当前使用的API版本
    const activeFetchControllers = new Map();

    // ==================== LOGGING UTILITIES ====================
    const log = {
        info: (...args) => console.log('[Injector]', ...args),
        warn: (...args) => console.warn('[Injector]', ...args),
        error: (...args) => console.error('[Injector]', ...args),
        debug: (...args) => CONFIG.DEBUG && console.log('[DEBUG]', ...args),
        success: (msg) => console.log('%c' + msg, 'color: #28a745; font-weight: bold;')
    };

    // ==================== API VERSION DETECTION ====================
    async function detectAPIVersion() {
        if (CONFIG.API_VERSION !== 'auto') {
            currentAPIVersion = CONFIG.API_VERSION;
            log.info(`Using configured API version: ${currentAPIVersion}`);
            return currentAPIVersion;
        }

        log.info('Auto-detecting API version...');
        
        // 优先尝试新版本
        for (const version of ['nextjs-api', 'api']) {
            try {
                const endpoint = CONFIG.API_ENDPOINTS[version];
                const testUrl = `${endpoint.base}${endpoint.signup}`;
                
                const response = await fetch(testUrl, {
                    method: 'OPTIONS',
                    mode: 'cors'
                });
                
                if (response.ok || response.status === 405) { // 405 也表示端点存在
                    currentAPIVersion = version;
                    log.success(`✅ Detected API version: ${version}`);
                    return version;
                }
            } catch (e) {
                log.debug(`Failed to test ${version}: ${e.message}`);
            }
        }
        
        // 默认使用新版本
        currentAPIVersion = 'nextjs-api';
        log.warn(`Failed to detect API version, defaulting to: ${currentAPIVersion}`);
        return currentAPIVersion;
    }

    function getAPIEndpoint(type) {
        if (!currentAPIVersion) {
            currentAPIVersion = 'nextjs-api'; // 默认值
        }
        const endpoints = CONFIG.API_ENDPOINTS[currentAPIVersion];
        return `${endpoints.base}${endpoints[type]}`;
    }

    // ==================== ENHANCED MODEL EXTRACTION (从lmarena-fd借鉴) ====================
    function extractModelRegistry() {
        log.info('🔍 Extracting model registry using enhanced algorithm...');

        try {
            const scripts = document.querySelectorAll('script');
            let modelData = null;

            for (const script of scripts) {
                const content = script.textContent || script.innerHTML;
                
                // 方法1: 使用 self.__next_f.push 模式（从lmarena-fd）
                const regex = /self\.__next_f\.push\(\[(\d+),"([^"]+?):(.*?)"\]\)/g;
                let match;
                
                while ((match = regex.exec(content)) !== null) {
                    const moduleNumber = match[1];
                    const moduleId = match[2];
                    const moduleDataStr = match[3];
                    
                    if (moduleDataStr.includes('initialModels') || moduleDataStr.includes('initialState')) {
                        log.debug(`Found potential model data in module ${moduleNumber}`);
                        
                        try {
                            // 反转义数据
                            const unescapedData = moduleDataStr
                                .replace(/\\"/g, '"')
                                .replace(/\\\\/g, '\\')
                                .replace(/\\n/g, '\n')
                                .replace(/\\r/g, '\r')
                                .replace(/\\t/g, '\t');
                            
                            const parsedData = JSON.parse(unescapedData);
                            
                            // 深度递归搜索（最大深度15层）
                            modelData = findModelsRecursively(parsedData);
                            if (modelData && modelData.length > 0) {
                                log.success(`✅ Found ${modelData.length} models using __next_f method`);
                                break;
                            }
                        } catch (parseError) {
                            log.debug(`Parse error in module ${moduleNumber}: ${parseError.message}`);
                            
                            // 备用方法：尝试提取JSON数组
                            try {
                                const bracketMatch = moduleDataStr.match(/(\[.*\])/);
                                if (bracketMatch) {
                                    const bracketData = bracketMatch[1]
                                        .replace(/\\"/g, '"')
                                        .replace(/\\\\/g, '\\');
                                    const parsedBracketData = JSON.parse(bracketData);
                                    
                                    modelData = findModelsRecursively(parsedBracketData);
                                    if (modelData && modelData.length > 0) {
                                        log.success(`✅ Found ${modelData.length} models using bracket extraction`);
                                        break;
                                    }
                                }
                            } catch (altError) {
                                // 静默失败，继续尝试其他方法
                            }
                        }
                    }
                }
                
                if (modelData) break;
            }

            // 方法2: 如果上述方法失败，尝试搜索initialState（备用）
            if (!modelData || modelData.length === 0) {
                log.info('Trying fallback extraction method...');
                for (const script of scripts) {
                    const content = script.textContent || script.innerHTML;
                    if (content.includes('initialState') || content.includes('initialModels')) {
                        try {
                            // 尝试提取大的JSON块
                            const jsonMatch = content.match(/\{[\s\S]*"initialState"[\s\S]*\}/);
                            if (jsonMatch) {
                                const jsonStr = jsonMatch[0];
                                const parsed = JSON.parse(jsonStr);
                                modelData = findModelsRecursively(parsed);
                                if (modelData && modelData.length > 0) {
                                    log.success(`✅ Found ${modelData.length} models using fallback method`);
                                    break;
                                }
                            }
                        } catch (e) {
                            // 继续尝试其他脚本
                        }
                    }
                }
            }

            if (!modelData || modelData.length === 0) {
                log.warn('⚠️ Model extraction failed - no models found');
                return null;
            }

            // 构建模型注册表
            const registry = {};
            modelData.forEach(model => {
                if (!model || typeof model !== 'object' || !model.publicName) return;
                if (registry[model.publicName]) return; // 避免重复

                // 确定模型类型
                let type = 'chat';
                if (model.capabilities && model.capabilities.outputCapabilities) {
                    if (model.capabilities.outputCapabilities.image) type = 'image';
                    else if (model.capabilities.outputCapabilities.video) type = 'video';
                }

                registry[model.publicName] = { 
                    type: type, 
                    ...model 
                };
            });

            log.success(`✅ Extracted ${Object.keys(registry).length} unique models`);
            return registry;

        } catch (error) {
            log.error('❌ Error extracting model registry:', error);
            return null;
        }
    }

    // 递归搜索模型数据（支持深度15层）
    function findModelsRecursively(obj, depth = 0) {
        if (depth > CONFIG.MAX_MODEL_SEARCH_DEPTH) return null;
        if (!obj || typeof obj !== 'object') return null;
        
        // 检查多种可能的属性名
        const modelKeys = ['initialModels', 'initialState', 'models', 'modelList'];
        for (const key of modelKeys) {
            if (obj[key] && Array.isArray(obj[key])) {
                const models = obj[key];
                // 验证是否是有效的模型数组
                if (models.length > 0 && models[0] && 
                    (models[0].publicName || models[0].name || models[0].id)) {
                    log.debug(`Found models at depth ${depth} with key "${key}"`);
                    return models;
                }
            }
        }
        
        // 在数组中递归搜索
        if (Array.isArray(obj)) {
            for (const item of obj) {
                const result = findModelsRecursively(item, depth + 1);
                if (result) return result;
            }
        }
        
        // 在对象属性中递归搜索
        for (const key in obj) {
            if (obj.hasOwnProperty(key)) {
                const result = findModelsRecursively(obj[key], depth + 1);
                if (result) return result;
            }
        }
        
        return null;
    }

    // ==================== HUMAN-LIKE INTERACTION ====================
    function simulateHumanClick() {
        const viewportWidth = window.innerWidth;
        const viewportHeight = window.innerHeight;

        const centerX = viewportWidth / 2;
        const centerY = viewportHeight / 2;
        const randomOffsetX = (Math.random() - 0.5) * (viewportWidth * 0.1);
        const randomOffsetY = (Math.random() - 0.5) * (viewportHeight * 0.1);

        const clickX = Math.round(centerX + randomOffsetX);
        const clickY = Math.round(centerY + randomOffsetY);

        const finalX = Math.max(10, Math.min(viewportWidth - 10, clickX));
        const finalY = Math.max(10, Math.min(viewportHeight - 10, clickY));

        log.debug(`🖱️ Simulating human-like click at (${finalX}, ${finalY})`);

        const target = document.elementFromPoint(finalX, finalY) || document.body;

        const mouseDown = new MouseEvent('mousedown', {
            bubbles: true,
            cancelable: true,
            clientX: finalX,
            clientY: finalY,
            button: 0
        });

        const mouseUp = new MouseEvent('mouseup', {
            bubbles: true,
            cancelable: true,
            clientX: finalX,
            clientY: finalY,
            button: 0
        });

        const click = new MouseEvent('click', {
            bubbles: true,
            cancelable: true,
            clientX: finalX,
            clientY: finalY,
            button: 0
        });

        target.dispatchEvent(mouseDown);
        setTimeout(() => {
            target.dispatchEvent(mouseUp);
            setTimeout(() => {
                target.dispatchEvent(click);
            }, Math.random() * 20 + 10);
        }, Math.random() * 50 + 50);
    }

    // ==================== TURNSTILE TOKEN CAPTURE ====================
    log.info('Setting up Turnstile token capture...');

    const originalCreateElement = document.createElement;
    document.createElement = function(...args) {
        const element = originalCreateElement.apply(this, args);

        if (element.tagName === 'SCRIPT') {
            const originalSetAttribute = element.setAttribute;
            element.setAttribute = function(name, value) {
                originalSetAttribute.call(this, name, value);

                if (name === 'src' && value && value.includes('challenges.cloudflare.com/turnstile')) {
                    log.info('Turnstile SCRIPT tag found! Adding load listener.');

                    element.addEventListener('load', function() {
                        log.info('Turnstile script has loaded. Now safe to hook turnstile.render().');
                        if (window.turnstile) {
                            hookTurnstileRender(window.turnstile);
                        }
                    });

                    document.createElement = originalCreateElement;
                }
            };
        }
        return element;
    };

    function hookTurnstileRender(turnstile) {
        const originalRender = turnstile.render;
        turnstile.render = function(container, params) {
            log.info('Intercepted turnstile.render() call.');
            const originalCallback = params.callback;
            params.callback = (token) => {
                handleTurnstileToken(token);
                if (originalCallback) return originalCallback(token);
            };
            return originalRender(container, params);
        };
    }

    function handleTurnstileToken(token) {
        latestTurnstileToken = token;
        log.success(`✅ Cloudflare Turnstile Token Captured: ${token.substring(0, 20)}...`);
    }

    window.onloadTurnstileCallback = function() {
        log.info('🎯 Turnstile onload callback triggered');
        if (window.turnstile) {
            log.info('🔧 Turnstile object available, setting up hooks...');
            hookTurnstileRender(window.turnstile);
            setTimeout(() => {
                createHiddenTurnstileWidget();
            }, 1000);
        } else {
            log.warn('⚠️ Turnstile object not available in onload callback');
        }
    };

    function createHiddenTurnstileWidget() {
        try {
            log.info('🎯 Creating hidden Turnstile widget to generate token...');

            const container = document.createElement('div');
            container.id = 'hidden-turnstile-widget';
            container.style.position = 'absolute';
            container.style.left = '-9999px';
            container.style.top = '-9999px';
            container.style.width = '300px';
            container.style.height = '65px';
            container.style.visibility = 'hidden';
            container.style.opacity = '0';
            container.style.pointerEvents = 'none';

            document.body.appendChild(container);

            if (window.turnstile && window.turnstile.render) {
                const widgetId = window.turnstile.render(container, {
                    sitekey: CONFIG.TURNSTILE_SITEKEY,
                    callback: function(token) {
                        log.success('🎉 Hidden Turnstile widget generated token!');
                        handleTurnstileToken(token);
                    },
                    'error-callback': function(error) {
                        log.warn('⚠️ Hidden Turnstile widget error:', error);
                    },
                    'expired-callback': function() {
                        log.info('⏰ Hidden Turnstile token expired, creating new widget...');
                        const oldContainer = document.getElementById('hidden-turnstile-widget');
                        if (oldContainer) {
                            oldContainer.remove();
                        }
                        setTimeout(createHiddenTurnstileWidget, 1000);
                    },
                    theme: 'light',
                    size: 'normal'
                });

                log.success('✅ Hidden Turnstile widget created with ID:', widgetId);
            } else {
                log.error('❌ Turnstile render function not available');
            }
        } catch (error) {
            log.error('❌ Error creating hidden Turnstile widget:', error);
        }
    }

    // ==================== AUTHENTICATION ====================
    async function initializeTurnstileIfNeeded() {
        log.info("🔧 Initializing Turnstile API if needed...");

        try {
            const script = document.createElement('script');
            script.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?onload=onloadTurnstileCallback&render=explicit';
            script.async = true;
            script.defer = true;

            script.onload = () => {
                log.success("✅ Turnstile API script loaded successfully");
            };
            script.onerror = (error) => {
                log.warn("⚠️ Failed to load Turnstile API script:", error);
            };

            document.head.appendChild(script);
            log.info("✅ Turnstile API script injection initiated");

            setTimeout(() => {
                simulateHumanClick();
            }, 3000 + Math.random() * 1000);
        } catch (error) {
            log.warn("⚠️ Failed to initialize Turnstile API:", error.message);
            throw error;
        }
    }

    async function ensureAuthenticationReady(requestId) {
        log.info(`🔐 Ensuring authentication is ready for request ${requestId}...`);

        if (!checkAuthCookie()) {
            log.info(`⚠️ Missing auth cookie for request ${requestId}, initiating auth flow...`);

            let authData = getStoredAuthData();

            if (!authData) {
                if (checkAuthCookie()) {
                    log.success(`✅ Auth cookie became available during auth check for request ${requestId}`);
                    return;
                }

                let turnstileToken = latestTurnstileToken;

                if (!turnstileToken) {
                    log.info(`⏳ No Turnstile token available yet for request ${requestId}, initializing...`);

                    await initializeTurnstileIfNeeded();

                    log.info(`⏳ Waiting for Turnstile token for request ${requestId}...`);
                    turnstileToken = await waitForTurnstileToken();

                    if (turnstileToken === 'auth_cookie_available') {
                        log.success(`✅ Auth cookie became available during wait for request ${requestId}`);
                        return;
                    }

                    if (!turnstileToken) {
                        throw new Error("Authentication required: Turnstile token not generated within timeout.");
                    }
                }

                log.info(`🔑 Have Turnstile token for request ${requestId}, performing authentication...`);
                authData = await performAuthentication(turnstileToken);
            }

            log.success(`✅ Authentication complete for request ${requestId}`);
        } else {
            log.success(`✅ Auth cookie already present for request ${requestId}`);
        }
    }

    function getCookie(name) {
        const value = `; ${document.cookie}`;
        const parts = value.split(`; ${name}=`);
        if (parts.length === 2) return parts.pop().split(';').shift();
        return null;
    }

    function checkAuthCookie() {
        const authCookie = getCookie(CONFIG.REQUIRED_COOKIE);
        if (authCookie) {
            log.debug(`✅ Found required cookie: ${CONFIG.REQUIRED_COOKIE}`);
            return true;
        } else {
            log.debug(`❌ Missing required cookie: ${CONFIG.REQUIRED_COOKIE}`);
            return false;
        }
    }

    async function waitForTurnstileToken(maxWaitTime = 60000) {
        log.info(`⏳ Waiting for Turnstile token to be generated...`);

        const checkInterval = 1000;
        let waitTime = 0;

        while (waitTime < maxWaitTime) {
            if (checkAuthCookie()) {
                log.success(`✅ Auth cookie became available after ${waitTime}ms`);
                return 'auth_cookie_available';
            }

            if (latestTurnstileToken) {
                log.success(`✅ Turnstile token available after ${waitTime}ms`);
                return latestTurnstileToken;
            }

            log.debug(`⏳ Still waiting for Turnstile token or auth cookie... (${waitTime}ms elapsed)`);
            await new Promise(resolve => setTimeout(resolve, checkInterval));
            waitTime += checkInterval;
        }

        log.warn(`⚠️ Turnstile token wait timeout after ${maxWaitTime}ms`);
        return null;
    }

    async function performAuthentication(turnstileToken) {
        log.info(`🔐 Starting authentication process with Turnstile token...`);

        // 确保API版本已检测
        if (!currentAPIVersion) {
            await detectAPIVersion();
        }

        try {
            // Step 1: Get JWT token from sign-up endpoint
            const signupUrl = getAPIEndpoint('signup');
            log.info(`Using signup endpoint: ${signupUrl}`);
            
            const authResponse = await fetch(signupUrl, {
                method: 'POST',
                headers: {
                    'Content-Type': 'text/plain;charset=UTF-8',
                    'Accept': '*/*',
                },
                body: JSON.stringify({
                    turnstile_token: turnstileToken
                })
            });

            if (!authResponse.ok) {
                throw new Error(`Authentication request failed with status ${authResponse.status}`);
            }

            const authData = await authResponse.json();
            log.success(`✅ Step 1: Received JWT token from sign-up.`);

            // Step 2: Create and set the auth cookie
            const cookieValue = `base64-${btoa(JSON.stringify(authData))}`;
            document.cookie = `${CONFIG.REQUIRED_COOKIE}=${cookieValue}; path=/; domain=.lmarena.ai; secure; samesite=lax`;
            log.success(`✅ Step 2: Set auth cookie in browser.`);

            // Step 3: Make verification request to complete authentication
            const verifyResponse = await fetch('https://lmarena.ai/', {
                method: 'POST',
                headers: {
                    'Content-Type': 'text/plain;charset=UTF-8',
                    'Accept': 'text/x-component',
                    'next-action': CONFIG.NEXT_ACTIONS.verify,
                    'next-router-state-tree': '%5B%22%22%2C%7B%22children%22%3A%5B%5B%22locale%22%2C%22en%22%2C%22d%22%5D%2C%7B%22children%22%3A%5B%22(app)%22%2C%7B%22children%22%3A%5B%22(with-sidebar)%22%2C%7B%22children%22%3A%5B%22__PAGE__%22%2C%7B%7D%2C%22%2F%22%2C%22refresh%22%5D%7D%5D%7D%2Cnull%2Cnull%2Ctrue%5D%7D%5D%7D%5D',
                    'x-deployment-id': 'dpl_55LQ8Z7ygwa1s99pJVeED1iU2jvW',
                },
                body: JSON.stringify([])
            });

            if (!verifyResponse.ok) {
                throw new Error(`Authentication verification failed with status ${verifyResponse.status}`);
            }

            const verifyText = await verifyResponse.text();
            log.success(`✅ Step 3: Authentication verification completed.`);

            // Store the authentication data
            localStorage.setItem('lmarena_auth_data', JSON.stringify(authData));
            localStorage.setItem('lmarena_auth_timestamp', Date.now().toString());

            log.success(`💾 Stored authentication data. Token expires at: ${new Date(authData.expires_at * 1000).toISOString()}`);
            log.success(`🎉 Complete authentication flow finished successfully!`);

            return authData;

        } catch (error) {
            log.error(`❌ Authentication failed:`, error);
            throw error;
        }
    }

    function getStoredAuthData() {
        const authData = localStorage.getItem('lmarena_auth_data');
        const timestamp = localStorage.getItem('lmarena_auth_timestamp');

        if (authData && timestamp) {
            try {
                const parsedAuthData = JSON.parse(authData);
                const currentTime = Math.floor(Date.now() / 1000);

                if (parsedAuthData.expires_at && (parsedAuthData.expires_at - 300) > currentTime) {
                    const remainingTime = parsedAuthData.expires_at - currentTime;
                    log.info(`Using stored auth data (expires in ${Math.round(remainingTime/60)} minutes)`);
                    return parsedAuthData;
                } else {
                    log.info(`Stored auth data expired, removing...`);
                    localStorage.removeItem('lmarena_auth_data');
                    localStorage.removeItem('lmarena_auth_timestamp');
                }
            } catch (error) {
                log.error(`Error parsing stored auth data:`, error);
                localStorage.removeItem('lmarena_auth_data');
                localStorage.removeItem('lmarena_auth_timestamp');
            }
        }
        return null;
    }

    // ==================== CLOUDFLARE HANDLING ====================
    function isCurrentPageCloudflareChallenge() {
        try {
            if (document.title.includes('Just a moment') ||
                document.title.includes('Checking your browser') ||
                document.title.includes('Please wait')) {
                log.debug("🛡️ CF challenge detected in page title");
                return true;
            }

            const cfIndicators = [
                'cf-browser-verification',
                'cf-challenge-running',
                'cf-wrapper',
                'cf-error-details',
                'cloudflare-static'
            ];

            for (const indicator of cfIndicators) {
                if (document.getElementById(indicator) ||
                    document.querySelector(`[class*="${indicator}"]`) ||
                    document.querySelector(`[id*="${indicator}"]`)) {
                    log.debug(`🛡️ CF challenge detected: found element with ${indicator}`);
                    return true;
                }
            }

            const bodyText = document.body ? document.body.textContent || document.body.innerText : '';
            if (bodyText.includes('Checking your browser before accessing') ||
                bodyText.includes('DDoS protection by Cloudflare') ||
                bodyText.includes('Enable JavaScript and cookies to continue') ||
                bodyText.includes('Please complete the security check') ||
                bodyText.includes('Verifying you are human')) {
                log.debug("🛡️ CF challenge detected in page text content");
                return true;
            }

            const scripts = document.querySelectorAll('script');
            for (const script of scripts) {
                const scriptContent = script.textContent || script.innerHTML;
                if (scriptContent.includes('__cf_chl_jschl_tk__') ||
                    scriptContent.includes('window._cf_chl_opt') ||
                    scriptContent.includes('cf_challenge_response')) {
                    log.debug("🛡️ CF challenge detected in script content");
                    return true;
                }
            }

            const normalPageIndicators = [
                'nav', 'header', 'main',
                '[data-testid]', '[class*="chat"]', '[class*="model"]',
                'input[type="text"]', 'textarea'
            ];

            let normalElementsFound = 0;
            for (const selector of normalPageIndicators) {
                if (document.querySelector(selector)) {
                    normalElementsFound++;
                }
            }

            if (normalElementsFound >= 3) {
                log.debug(`✅ Normal page detected: found ${normalElementsFound} normal elements`);
                return false;
            }

            const totalElements = document.querySelectorAll('*').length;
            if (totalElements < 50) {
                log.debug(`🛡️ Possible CF challenge: page has only ${totalElements} elements`);
                return true;
            }

            log.debug("✅ No CF challenge indicators found in current page");
            return false;

        } catch (error) {
            log.warn(`⚠️ Error checking current page for CF challenge: ${error.message}`);
            return false;
        }
    }

    function isCloudflareChallenge(responseText) {
        return responseText.includes('Checking your browser before accessing') ||
               responseText.includes('DDoS protection by Cloudflare') ||
               responseText.includes('cf-browser-verification') ||
               responseText.includes('cf-challenge-running') ||
               responseText.includes('__cf_chl_jschl_tk__') ||
               responseText.includes('cloudflare-static') ||
               responseText.includes('<title>Just a moment...</title>') ||
               responseText.includes('Enable JavaScript and cookies to continue') ||
               responseText.includes('window._cf_chl_opt') ||
               (responseText.includes('cloudflare') && responseText.includes('challenge'));
    }

    async function waitForCloudflareAuth() {
        log.info("⏳ Waiting for Cloudflare authentication to complete...");

        const maxWaitTime = 45000;
        const checkInterval = 500;
        let waitTime = 0;

        while (waitTime < maxWaitTime) {
            try {
                if (!isCurrentPageCloudflareChallenge()) {
                    log.success(`✅ Cloudflare authentication completed after ${waitTime}ms`);
                    return true;
                }

                log.debug(`⏳ Still waiting for CF auth... (${waitTime}ms elapsed)`);

            } catch (error) {
                log.debug(`⏳ CF auth check failed, continuing to wait... (${waitTime}ms elapsed) - ${error.message}`);
            }

            await new Promise(resolve => setTimeout(resolve, checkInterval));
            waitTime += checkInterval;
        }

        log.warn(`⚠️ CF authentication wait timeout after ${maxWaitTime}ms`);
        return false;
    }

    async function handleCloudflareRefresh() {
        if (isRefreshing) {
            log.info("🔄 Already refreshing, skipping duplicate refresh request");
            return;
        }

        isRefreshing = true;
        log.info("🔄 Cloudflare challenge detected! Refreshing page to get new token...");

        try {
            const storedRequests = localStorage.getItem('lmarena_pending_requests');
            if (storedRequests) {
                const requests = JSON.parse(storedRequests);
                log.info(`💾 Found ${requests.length} pending requests, refreshing page...`);
            }

            window.location.reload();

        } catch (error) {
            log.error("❌ Error during CF refresh:", error);
            isRefreshing = false;
        }
    }

    async function handleRateLimitRefresh() {
        if (isRefreshing) {
            log.info("🔄 Already refreshing, skipping duplicate rate limit refresh request");
            return;
        }

        isRefreshing = true;
        log.info("🚫 Rate limit (429) detected! Deleting auth cookie and refreshing to create new identity...");

        try {
            log.info(`🗑️ Deleting ALL cookies to ensure fresh identity...`);

            const cookies = document.cookie.split(";");
            for (let cookie of cookies) {
                const eqPos = cookie.indexOf("=");
                const name = eqPos > -1 ? cookie.substr(0, eqPos).trim() : cookie.trim();
                if (name) {
                    document.cookie = `${name}=; path=/; expires=Thu, 01 Jan 1970 00:00:00 GMT`;
                    document.cookie = `${name}=; path=/; domain=lmarena.ai; expires=Thu, 01 Jan 1970 00:00:00 GMT`;
                    document.cookie = `${name}=; path=/; domain=.lmarena.ai; expires=Thu, 01 Jan 1970 00:00:00 GMT`;
                }
            }
            log.info(`🗑️ Deleted ${cookies.length} cookies`);

            localStorage.removeItem('lmarena_auth_data');
            localStorage.removeItem('lmarena_auth_timestamp');
            log.info(`🗑️ Cleared stored auth data`);

            const storedRequests = localStorage.getItem('lmarena_pending_requests');
            if (storedRequests) {
                const requests = JSON.parse(storedRequests);
                log.info(`💾 Found ${requests.length} pending requests, refreshing page...`);
            }

            window.location.reload();

        } catch (error) {
            log.error("❌ Error during rate limit refresh:", error);
            isRefreshing = false;
        }
    }

    // ==================== FILE UPLOAD HANDLING ====================
    function base64ToBlob(base64, contentType) {
        const byteCharacters = atob(base64);
        const byteNumbers = new Array(byteCharacters.length);
        for (let i = 0; i < byteCharacters.length; i++) {
            byteNumbers[i] = byteCharacters.charCodeAt(i);
        }
        const byteArray = new Uint8Array(byteNumbers);
        return new Blob([byteArray], { type: contentType });
    }

    async function handleUploadAndChat(requestId, payload, filesToUpload) {
        const abortController = new AbortController();
        activeFetchControllers.set(requestId, abortController);

        try {
            log.info(`🚀 Starting upload and chat for request ${requestId}`);

            await ensureAuthenticationReady(requestId);

            const attachments = [];
            for (const file of filesToUpload) {
                log.info(`Processing file: ${file.fileName}`);

                // Step 1: Get Signed URL
                log.info(`Step 1: Getting signed URL for ${file.fileName}`);
                const signUrlResponse = await fetch('https://lmarena.ai/?mode=direct', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'text/plain;charset=UTF-8',
                        'Accept': 'text/x-component',
                        'next-action': CONFIG.NEXT_ACTIONS.fileSignUrl,
                        'next-router-state-tree': '%5B%22%22%2C%7B%22children%22%3A%5B%5B%22locale%22%2C%22en%22%2C%22d%22%5D%2C%7B%22children%22%3A%5B%22(app)%22%2C%7B%22children%22%3A%5B%22(with-sidebar)%22%2C%7B%22children%22%3A%5B%22__PAGE__%3F%7B%5C%22mode%5C%22%3A%5C%22direct%5C%22%7D%22%2C%7B%7D%2C%22%2F%3Fmode%3Ddirect%22%2C%22refresh%22%5D%7D%2Cnull%2Cnull%5D%7D%2Cnull%2Cnull%2Ctrue%5D%7D%2Cnull%2Cnull%5D%7D%2Cnull%2Cnull%5D',
                        'origin': 'https://lmarena.ai',
                        'referer': 'https://lmarena.ai/'
                    },
                    body: JSON.stringify([file.fileName, file.contentType]),
                    signal: abortController.signal
                });

                const signUrlText = await signUrlResponse.text();
                log.debug("Received for signed URL:", signUrlText);

                let signUrlData = null;

                let match = signUrlText.match(/\d+:({.*})/);
                if (match && match.length >= 2) {
                    log.debug(`Found data with '${match[0].split(':')[0]}:' prefix`);
                    signUrlData = JSON.parse(match[1]);
                } else {
                    try {
                        signUrlData = JSON.parse(signUrlText);
                        log.debug("Parsed entire response as JSON");
                    } catch (e) {
                        const jsonMatches = signUrlText.match(/{[^}]*"uploadUrl"[^}]*}/g);
                        if (jsonMatches && jsonMatches.length > 0) {
                            signUrlData = JSON.parse(jsonMatches[0]);
                            log.debug("Found JSON object containing uploadUrl");
                        } else {
                            throw new Error(`Could not parse signed URL response. Response: ${signUrlText}`);
                        }
                    }
                }

                if (!signUrlData || !signUrlData.data || !signUrlData.data.uploadUrl) {
                    throw new Error('Signed URL data is incomplete or invalid after parsing.');
                }
                const { uploadUrl, key } = signUrlData.data;
                log.info(`Got signed URL. Key: ${key}`);

                // Step 2: Upload file to storage
                log.info(`Step 2: Uploading file to cloud storage...`);
                const blob = base64ToBlob(file.data, file.contentType);
                const uploadResponse = await fetch(uploadUrl, {
                    method: 'PUT',
                    headers: { 'Content-Type': file.contentType },
                    body: blob,
                    signal: abortController.signal
                });
                if (!uploadResponse.ok) throw new Error(`File upload failed with status ${uploadResponse.status}`);
                log.success(`File uploaded successfully.`);

                // Step 3: Notify LMArena of upload
                log.info(`Step 3: Notifying LMArena of upload completion...`);
                const notifyResponse = await fetch('https://lmarena.ai/?mode=direct', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'text/plain;charset=UTF-8',
                        'Accept': 'text/x-component',
                        'next-action': CONFIG.NEXT_ACTIONS.fileNotify,
                        'next-router-state-tree': '%5B%22%22%2C%7B%22children%22%3A%5B%5B%22locale%22%2C%22en%22%2C%22d%22%5D%2C%7B%22children%22%3A%5B%22(app)%22%2C%7B%22children%22%3A%5B%22(with-sidebar)%22%2C%7B%22children%22%3A%5B%22__PAGE__%3F%7B%5C%22mode%5C%22%3A%5C%22direct%5C%22%7D%22%2C%7B%7D%2C%22%2F%3Fmode%3Ddirect%22%2C%22refresh%22%5D%7D%2Cnull%2Cnull%5D%7D%2Cnull%2Cnull%2Ctrue%5D%7D%2Cnull%2Cnull%5D%7D%2Cnull%2Cnull%5D',
                        'origin': 'https://lmarena.ai',
                        'referer': 'https://lmarena.ai/'
                    },
                    body: JSON.stringify([key]),
                    signal: abortController.signal
                });

                const notifyText = await notifyResponse.text();
                log.debug(`Notification sent. Response:`, notifyText);

                const finalUrlDataLine = notifyText.split('\n').find(line => line.startsWith('1:'));
                if (!finalUrlDataLine) throw new Error('Could not find final URL data in notification response.');

                const finalUrlData = JSON.parse(finalUrlDataLine.substring(2));
                const finalUrl = finalUrlData.data.url;
                if (!finalUrl) throw new Error('Final URL not found in notification response data.');

                log.info(`Extracted final GetObject URL: ${finalUrl}`);

                attachments.push({
                    name: key,
                    contentType: file.contentType,
                    url: finalUrl
                });
            }

            // Step 4: Modify payload with attachments and send final request
            log.info('All files uploaded. Modifying final payload...');
            const userMessage = payload.messages.find(m => m.role === 'user');
            if (userMessage) {
                userMessage.experimental_attachments = attachments;
            } else {
                throw new Error("Could not find user message in payload to attach files to.");
            }

            log.info('Payload modified. Initiating final chat stream.');
            await executeFetchAndStreamBack(requestId, payload);

        } catch (error) {
            if (error.name === 'AbortError') {
                log.info(`Upload process aborted for request ${requestId}`);
            } else {
                log.error(`Error during file upload process for request ${requestId}:`, error);
                
                if (error.message && error.message.includes('429')) {
                    log.info(`🚫 Rate limit detected during upload`);

                    const existingRequests = JSON.parse(localStorage.getItem('lmarena_pending_requests') || '[]');
                    const alreadyStored = existingRequests.some(req => req.requestId === requestId);

                    if (!alreadyStored) {
                        existingRequests.push({
                            requestId,
                            payload,
                            files_to_upload: filesToUpload
                        });
                        localStorage.setItem('lmarena_pending_requests', JSON.stringify(existingRequests));
                        log.info(`💾 Stored upload request ${requestId} with ${filesToUpload.length} files for retry`);
                    }

                    handleRateLimitRefresh();
                    return;
                }
                sendToServer(requestId, JSON.stringify({ error: `File upload failed: ${error.message}` }));
                sendToServer(requestId, "[DONE]");
            }
        } finally {
            activeFetchControllers.delete(requestId);
        }
    }

    // ==================== FETCH AND STREAM ====================
    async function executeFetchAndStreamBack(requestId, payload) {
        const abortController = new AbortController();
        activeFetchControllers.set(requestId, abortController);

        try {
            log.info(`🚀 Starting fetch for request ${requestId}`);

            await ensureAuthenticationReady(requestId);
            
            // 确保API版本已检测
            if (!currentAPIVersion) {
                await detectAPIVersion();
            }

            const streamUrl = getAPIEndpoint('stream');
            log.info(`Using stream endpoint: ${streamUrl}`);

            const response = await fetch(streamUrl, {
                method: 'POST',
                headers: {
                    'Content-Type': 'text/plain;charset=UTF-8',
                    'Accept': '*/*',
                },
                body: JSON.stringify(payload),
                signal: abortController.signal
            });

            if (response.status === 429) {
                log.info(`🚫 Rate limit (429) detected for request ${requestId}`);

                const existingRequests = JSON.parse(localStorage.getItem('lmarena_pending_requests') || '[]');
                const alreadyStored = existingRequests.some(req => req.requestId === requestId);

                if (!alreadyStored) {
                    existingRequests.push({
                        requestId,
                        payload,
                        files_to_upload: []
                    });
                    localStorage.setItem('lmarena_pending_requests', JSON.stringify(existingRequests));
                    log.info(`💾 Stored request ${requestId} for retry after rate limit refresh`);
                } else {
                    log.info(`⚠️ Request ${requestId} already stored, skipping duplicate`);
                }

                handleRateLimitRefresh();
                return;
            }

            if (!response.ok || response.headers.get('content-type')?.includes('text/html')) {
                const responseText = await response.text();

                if (isCloudflareChallenge(responseText)) {
                    log.info(`🛡️ Cloudflare challenge detected for request ${requestId} (Status: ${response.status})`);

                    const existingRequests = JSON.parse(localStorage.getItem('lmarena_pending_requests') || '[]');
                    const alreadyStored = existingRequests.some(req => req.requestId === requestId);

                    if (!alreadyStored) {
                        existingRequests.push({ requestId, payload });
                        localStorage.setItem('lmarena_pending_requests', JSON.stringify(existingRequests));
                        log.info(`💾 Stored request ${requestId} for retry after CF refresh`);
                    } else {
                        log.info(`⚠️ Request ${requestId} already stored, skipping duplicate`);
                    }

                    handleCloudflareRefresh();
                    return;
                }

                throw new Error(`Fetch failed with status ${response.status}: ${responseText}`);
            }

            if (!response.body) {
                throw new Error(`No response body received for request ${requestId}`);
            }

            log.info(`📡 Starting to stream response for request ${requestId}`);
            const reader = response.body.getReader();
            const decoder = new TextDecoder();

            while (true) {
                if (abortController.signal.aborted) {
                    log.info(`Stream aborted for request ${requestId}, cancelling reader`);
                    await reader.cancel();
                    break;
                }

                const { value, done } = await reader.read();
                if (done) {
                    log.success(`✅ Stream finished for request ${requestId}.`);
                    sendToServer(requestId, "[DONE]");
                    break;
                }

                const chunk = decoder.decode(value);

                if (chunk.includes('<html') || chunk.includes('<!DOCTYPE')) {
                    if (isCloudflareChallenge(chunk)) {
                        log.info(`🛡️ Cloudflare challenge detected in stream for request ${requestId}`);

                        const existingRequests = JSON.parse(localStorage.getItem('lmarena_pending_requests') || '[]');
                        const alreadyStored = existingRequests.some(req => req.requestId === requestId);

                        if (!alreadyStored) {
                            existingRequests.push({ requestId, payload });
                            localStorage.setItem('lmarena_pending_requests', JSON.stringify(existingRequests));
                            log.info(`💾 Stored request ${requestId} for retry after CF refresh (detected in stream)`);
                        }

                        handleCloudflareRefresh();
                        return;
                    }
                }

                if (abortController.signal.aborted) {
                    log.info(`Stream aborted for request ${requestId}, stopping data transmission`);
                    await reader.cancel();
                    break;
                }

                const lines = chunk.split('\n').filter(line => line.trim() !== '');
                for (const line of lines) {
                    if (abortController.signal.aborted) {
                        log.info(`Aborting mid-chunk for request ${requestId}`);
                        await reader.cancel();
                        return;
                    }
                    sendToServer(requestId, line);
                }
            }

        } catch (error) {
            if (error.name === 'AbortError') {
                log.info(`Fetch aborted for request ${requestId}`);
            } else {
                log.error(`❌ Error during fetch for request ${requestId}:`, error);
                sendToServer(requestId, JSON.stringify({ error: error.message }));
                sendToServer(requestId, "[DONE]");
            }
        } finally {
            activeFetchControllers.delete(requestId);
        }
    }

    // ==================== PENDING REQUESTS PROCESSING ====================
    async function processPendingRequests() {
        const storedRequests = localStorage.getItem('lmarena_pending_requests');
        if (!storedRequests) {
            log.info("📭 No pending requests found, skipping processing");
            return;
        }

        try {
            const requests = JSON.parse(storedRequests);
            if (!requests || requests.length === 0) {
                log.info("📭 No pending requests in storage, cleaning up");
                localStorage.removeItem('lmarena_pending_requests');
                return;
            }

            log.info(`🔄 Found ${requests.length} pending requests after refresh`);

            log.info("⏳ Waiting for Cloudflare authentication to complete before processing requests...");
            const authComplete = await waitForCloudflareAuth();

            if (!authComplete) {
                log.error("❌ CF authentication timeout - requests may fail");
                log.info("🔍 CF auth failed, but NOT triggering refresh - only refreshing on actual 429/CF challenge");
                return;
            }

            log.success("✅ Cloudflare authentication completed, proceeding with requests");

            await new Promise(resolve => setTimeout(resolve, 2000));

            log.info("⏳ Waiting for either Turnstile token or auth cookie to become available...");
            const maxWaitTime = 60000;
            const checkInterval = 1000;
            let waitTime = 0;
            let authReady = false;
            let turnstileInitialized = false;

            while (waitTime < maxWaitTime && !authReady) {
                if (checkAuthCookie()) {
                    log.success("🎉 Auth cookie found after refresh, ready to proceed!");
                    authReady = true;
                    break;
                }

                if (latestTurnstileToken) {
                    log.success("✅ Turnstile token available, ready to proceed!");
                    authReady = true;
                    break;
                }

                if (!turnstileInitialized && waitTime > 2000) {
                    if (checkAuthCookie()) {
                        log.success("🎉 Auth cookie became available before Turnstile init, skipping!");
                        authReady = true;
                        break;
                    }

                    log.info("🔧 No auth cookie or Turnstile token found, initializing Turnstile API...");
                    try {
                        await initializeTurnstileIfNeeded();
                        turnstileInitialized = true;

                        if (checkAuthCookie()) {
                            log.success("🎉 Auth cookie became available during Turnstile init!");
                            authReady = true;
                            break;
                        }
                    } catch (error) {
                        log.warn("⚠️ Failed to initialize Turnstile API:", error.message);
                        turnstileInitialized = true;
                    }
                }

                log.debug(`⏳ Still waiting for auth cookie or Turnstile token... (${waitTime}ms elapsed)`);
                await new Promise(resolve => setTimeout(resolve, checkInterval));
                waitTime += checkInterval;
            }

            if (!authReady) {
                log.warn("⚠️ Neither auth cookie nor Turnstile token became available within timeout, proceeding anyway");
            }

            await new Promise(resolve => setTimeout(resolve, 1000));

            localStorage.removeItem('lmarena_pending_requests');

            for (let i = 0; i < requests.length; i++) {
                const request = requests[i];
                const { requestId, payload, files_to_upload } = request;
                log.info(`🔄 Retrying request ${requestId} after refresh (${i + 1}/${requests.length})`);

                if (i > 0) {
                    await new Promise(resolve => setTimeout(resolve, 500));
                }

                try {
                    if (files_to_upload && files_to_upload.length > 0) {
                        log.info(`🔄 Retrying upload request ${requestId} with ${files_to_upload.length} file(s)`);
                        await handleUploadAndChat(requestId, payload, files_to_upload);
                    } else {
                        log.info(`🔄 Retrying regular request ${requestId}`);
                        await executeFetchAndStreamBack(requestId, payload);
                    }
                    log.success(`✅ Successfully retried request ${requestId}`);
                } catch (error) {
                    log.error(`❌ Failed to retry request ${requestId}:`, error);
                    if (socket && socket.readyState === WebSocket.OPEN) {
                        socket.send(JSON.stringify({
                            request_id: requestId,
                            data: JSON.stringify({ error: `Retry failed: ${error.message}` })
                        }));
                        socket.send(JSON.stringify({
                            request_id: requestId,
                            data: "[DONE]"
                        }));
                    }
                }
            }

            log.success(`🎉 Completed processing ${requests.length} pending requests`);

        } catch (error) {
            log.error("❌ Error processing pending requests:", error);
            localStorage.removeItem('lmarena_pending_requests');
        }
    }

    // ==================== WEBSOCKET CONNECTION ====================
    function connect() {
        log.info(`Connecting to server at ${CONFIG.SERVER_URL}...`);
        socket = new WebSocket(CONFIG.SERVER_URL);

        socket.onopen = async () => {
            log.success("✅ Connection established with local server.");

            // 检测API版本
            await detectAPIVersion();

            sendReconnectionHandshake();
            processPendingRequests();

            if (!modelRegistrySent) {
                setTimeout(() => {
                    sendModelRegistry();
                }, 2000);
            }
        };

        socket.onmessage = async (event) => {
            try {
                const message = JSON.parse(event.data);

                if (message.type === 'ping') {
                    log.debug('💓 Received ping, sending pong...');
                    socket.send(JSON.stringify({
                        type: 'pong',
                        timestamp: message.timestamp
                    }));
                    return;
                }

                if (message.type === 'refresh_models') {
                    log.info('🔄 Received model refresh request');
                    sendModelRegistry();
                    return;
                }

                if (message.type === 'model_registry_ack') {
                    log.success(`✅ Model registry updated with ${message.count} models`);
                    modelRegistrySent = true;
                    return;
                }

                if (message.type === 'reconnection_ack') {
                    log.info(`🤝 Reconnection acknowledged: ${message.message}`);
                    if (message.pending_request_ids && message.pending_request_ids.length > 0) {
                        log.info(`📋 Server has ${message.pending_request_ids.length} pending requests waiting`);
                    }
                    return;
                }

                if (message.type === 'restoration_ack') {
                    log.info(`🔄 Request restoration acknowledged: ${message.message}`);
                    log.success(`✅ ${message.restored_count} request channels restored`);
                    return;
                }

                if (message.type === 'abort_request') {
                    const requestId = message.request_id;
                    log.info(`🛑 Received abort request for ${requestId}`);

                    const controller = activeFetchControllers.get(requestId);
                    if (controller) {
                        controller.abort();
                        activeFetchControllers.delete(requestId);
                        log.success(`✅ Aborted fetch request ${requestId}`);
                    } else {
                        log.warn(`⚠️ No active fetch found for request ${requestId}`);
                    }
                    return;
                }

                const { request_id, payload, files_to_upload } = message;

                if (!request_id || !payload) {
                    log.error("Invalid message from server:", message);
                    return;
                }

                if (files_to_upload && files_to_upload.length > 0) {
                    log.info(`⬆️ Received request with ${files_to_upload.length} file(s). Starting upload process.`);
                    await handleUploadAndChat(request_id, payload, files_to_upload);
                } else {
                    log.info(`⬇️ Received standard text request ${request_id}. Firing fetch.`);
                    await executeFetchAndStreamBack(request_id, payload);
                }

            } catch (error) {
                log.error("Error processing message from server:", error);
            }
        };

        socket.onclose = () => {
            log.warn("🔌 Connection to local server closed. Retrying in 5 seconds...");
            modelRegistrySent = false;

            if (activeFetchControllers.size > 0) {
                log.info(`🛑 Aborting ${activeFetchControllers.size} active fetch requests due to WebSocket disconnect`);
                for (const [requestId, controller] of activeFetchControllers) {
                    controller.abort();
                    log.info(`✅ Aborted fetch request ${requestId}`);
                }
                activeFetchControllers.clear();
            }

            setTimeout(connect, 5000);
        };

        socket.onerror = (error) => {
            log.error("❌ WebSocket error:", error);
            socket.close();
        };
    }

    function sendToServer(requestId, data) {
        const controller = activeFetchControllers.get(requestId);
        if (controller && controller.signal.aborted) {
            log.debug(`Not sending data for aborted request ${requestId}`);
            return;
        }

        if (socket && socket.readyState === WebSocket.OPEN) {
            const message = {
                request_id: requestId,
                data: data
            };
            socket.send(JSON.stringify(message));
        } else {
            log.error("Cannot send data, socket is not open.");
        }
    }

    function sendReconnectionHandshake() {
        if (!socket || socket.readyState !== WebSocket.OPEN) {
            log.warn('⚠️ WebSocket not ready, cannot send reconnection handshake');
            return;
        }

        const storedRequests = localStorage.getItem('lmarena_pending_requests');
        let pendingRequestIds = [];

        if (storedRequests) {
            try {
                const requests = JSON.parse(storedRequests);
                pendingRequestIds = requests.map(req => req.requestId);
                log.info(`🤝 Sending reconnection handshake with ${pendingRequestIds.length} pending requests`);
            } catch (error) {
                log.error("Error parsing stored requests for handshake:", error);
            }
        }

        const handshakeMessage = {
            type: 'reconnection_handshake',
            pending_request_ids: pendingRequestIds,
            timestamp: Date.now()
        };

        socket.send(JSON.stringify(handshakeMessage));
        log.info(`📤 Sent reconnection handshake`);
    }

    function sendModelRegistry() {
        if (!socket || socket.readyState !== WebSocket.OPEN) {
            log.warn('⚠️ WebSocket not ready, cannot send model registry');
            return;
        }

        const models = extractModelRegistry();

        if (models && Object.keys(models).length > 0) {
            const message = {
                type: 'model_registry',
                models: models
            };

            socket.send(JSON.stringify(message));
            log.success(`📤 Sent model registry with ${Object.keys(models).length} models`);
        } else {
            log.warn('⚠️ No models extracted, not sending registry');
        }
    }

    // ==================== INITIALIZATION ====================
    log.success('🚀 LMArena Proxy Injector V2 Starting...');
    connect();

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', () => {
            setTimeout(sendModelRegistry, 3000);
        });
    } else {
        setTimeout(sendModelRegistry, 3000);
    }

    log.success('✅ LMArena Proxy Injector V2 initialized successfully!');

})();
