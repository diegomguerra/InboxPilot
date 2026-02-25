const HF = {
    state: 'IDLE',
    phase: 'idle',
    activeTurn: 0,
    requestLock: false,
    stream: null,
    mediaRecorder: null,
    audioChunks: [],
    audioPlayer: null,
    audioCtx: null,
    analyser: null,
    analyserSource: null,
    silenceInterval: null,
    silenceStart: null,
    silenceTimer: null,
    selectedVoice: localStorage.getItem('handsfree_voice_uri') || 'nova',
    ttsSpeed: parseFloat(localStorage.getItem('handsfree_tts_rate')) || 1.0,
    mode: localStorage.getItem('handsfree_mode') || 'manual',
    normalizeLangs: localStorage.getItem('handsfree_normalize_langs') !== 'false',
    hfSource: localStorage.getItem('handsfree_source') || 'dashboard',
    contextId: null,
    lastResponse: null,
    lastTranscript: '',
    lastIntent: null,
    lastError: null,
    debugEnabled: false,
    speakGeneration: 0,
    speakAbort: null,
    interruptMonitorInterval: null,
    interruptSource: null,
    interruptAnalyser: null,
    proposedActions: [],
    pendingConfirmation: null,
    timings: {},
    processingTimeout: null,
    alwaysOnRecognition: null,
    alwaysOnActive: false,
    antiFeedbackLocked: false,
};

const HF_SILENCE_MS = 1800;
const HF_SOUND_THRESHOLD = 12;
const HF_MIN_BLOB_SIZE = 1500;
const HF_MAX_RECORD_MS = 30000;
const HF_MAX_RECORD_MANUAL_MS = 60000;
const HF_PROCESSING_TIMEOUT_MS = 45000;
const HF_SILENCE_CHECK_MS = 50;
const HF_TIMESLICE_MS = 100;
const HF_AUTO_LISTEN_DELAY_MS = 600;
const HF_HOTWORD = 'rordens';
const HF_HOTWORD_VARIANTS = ['rordens', 'rórdens', 'ordem', 'ordens', 'borders', 'orders', 'horders', 'jordens'];
const HF_WEBSPECH_RESTART_DELAY_MS = 300;
const HF_ANTI_FEEDBACK_DELAY_MS = 800;

const HF_TRANSITIONS = {
    IDLE:       ['LISTENING'],
    LISTENING:  ['PROCESSING', 'IDLE', 'ERROR'],
    PROCESSING: ['SPEAKING', 'IDLE', 'ERROR'],
    SPEAKING:   ['IDLE', 'LISTENING', 'ERROR'],
    ERROR:      ['IDLE'],
};

const HF_POST_CORRECTIONS = {
    'não lido': 'não lido',
    'nao lido': 'não lido',
    'não lidos': 'não lidos',
    'nao lidos': 'não lidos',
    'marcar como lido': 'marcar como lido',
    'marca como lido': 'marcar como lido',
    'apagar': 'apagar',
    'deletar': 'deletar',
    'arquivar': 'arquivar',
    'responder': 'responder',
    'sugestão': 'sugestão',
    'sugerir': 'sugerir',
    'g-mail': 'Gmail',
    'g mail': 'Gmail',
    'ge mail': 'Gmail',
    'ji mail': 'Gmail',
    'ji-mail': 'Gmail',
    'apple': 'Apple',
    'inbox': 'Inbox',
    'in box': 'Inbox',
    'triagem': 'triagem',
    'triage': 'triagem',
    'e-mail': 'e-mail',
    'email': 'e-mail',
    'Email': 'e-mail',
    'resumo': 'resumo',
    'fila': 'fila',
    'executar': 'executar',
    'despachar': 'despachar',
    'cancelar': 'cancelar',
    'confirmar': 'confirmar',
    'atualizar': 'atualizar',
    'listar': 'listar',
    'ler': 'ler',
    'leia': 'leia',
    'ajuda': 'ajuda',
    'parar': 'parar',
    'próximo': 'próximo',
    'proximo': 'próximo',
    'repetir': 'repetir',
    'rordens': 'Rordens',
    'rórdens': 'Rordens',
    'orders': 'Rordens',
    'horders': 'Rordens',
    'borders': 'Rordens',
};

function hfPostCorrect(text) {
    let result = text;
    for (const [wrong, right] of Object.entries(HF_POST_CORRECTIONS)) {
        const regex = new RegExp('\\b' + wrong.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\b', 'gi');
        result = result.replace(regex, right);
    }
    return result;
}

function hfTimestamp() { return Date.now(); }

function hfTransition(newState, reason) {
    const allowed = HF_TRANSITIONS[HF.state];
    if (!allowed || !allowed.includes(newState)) {
        console.warn(`[HF] Blocked transition ${HF.state} -> ${newState} (${reason})`);
        return false;
    }
    const prev = HF.state;
    HF.state = newState;
    console.log(`[HF] ${prev} -> ${newState} (${reason})`);
    hfOnExit(prev);
    hfOnEnter(newState);
    hfUpdateUI();
    hfUpdateDebug();
    return true;
}

function hfForceState(newState, reason) {
    const prev = HF.state;
    HF.state = newState;
    console.log(`[HF] FORCE ${prev} -> ${newState} (${reason})`);
    hfOnExit(prev);
    hfOnEnter(newState);
    hfUpdateUI();
    hfUpdateDebug();
}

function hfSetPhase(phase) {
    HF.phase = phase;
    hfUpdateDebug();
}

function hfOnEnter(state) {
    HF.timings['t_' + state.toLowerCase() + '_start'] = hfTimestamp();
    if (state === 'LISTENING') { hfPlayEarcon('listen'); hfSetPhase('listening'); }
    if (state === 'PROCESSING') {
        hfPlayEarcon('process');
        hfClearProcessingTimeout();
        HF.processingTimeout = setTimeout(() => {
            if (HF.state === 'PROCESSING') {
                console.warn('[HF] Processing timeout');
                HF.requestLock = false;
                hfForceState('IDLE', 'processing timeout');
                hfSetStatus('Demorou demais. Tente novamente.', '');
                hfSetPhase('error');
                hfAddMessage('system', 'Tive um problema, tente novamente.');
                if (HF.mode === 'auto') {
                    setTimeout(() => { if (HF.state === 'IDLE') hfStartListening(); }, HF_AUTO_LISTEN_DELAY_MS);
                }
            }
        }, HF_PROCESSING_TIMEOUT_MS);
    }
    if (state === 'SPEAKING') { hfPlayEarcon('speak'); hfSetPhase('speaking'); }
}

function hfOnExit(state) {
    HF.timings['t_' + state.toLowerCase() + '_end'] = hfTimestamp();
    if (state === 'PROCESSING') hfClearProcessingTimeout();
}

function hfClearProcessingTimeout() {
    if (HF.processingTimeout) { clearTimeout(HF.processingTimeout); HF.processingTimeout = null; }
}

function hfPlayEarcon(type) {
    try {
        const ctx = new (window.AudioContext || window.webkitAudioContext)();
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.connect(gain);
        gain.connect(ctx.destination);
        gain.gain.value = 0.08;

        if (type === 'listen') {
            osc.frequency.value = 880;
            osc.type = 'sine';
            osc.start();
            osc.stop(ctx.currentTime + 0.12);
        } else if (type === 'process') {
            osc.frequency.value = 660;
            osc.type = 'sine';
            osc.start();
            osc.stop(ctx.currentTime + 0.08);
        } else if (type === 'speak') {
            osc.frequency.value = 1100;
            osc.type = 'sine';
            osc.start();
            osc.stop(ctx.currentTime + 0.1);
        } else if (type === 'error') {
            osc.frequency.value = 330;
            osc.type = 'square';
            osc.start();
            osc.stop(ctx.currentTime + 0.2);
        } else if (type === 'confirm') {
            osc.frequency.value = 523;
            osc.type = 'sine';
            osc.start();
            osc.stop(ctx.currentTime + 0.15);
        }
        setTimeout(() => ctx.close(), 500);
    } catch (e) {}
}

const HF_INTENTS = [
    { pattern: /^(?:ler|leia|abrir?|mostrar?)\s+(?:o\s+)?e-?mail\s+(\d+)/i, intent: 'READ_EMAIL', extract: m => ({ index: parseInt(m[1]) }) },
    { pattern: /^(?:email|e-mail)\s+(\d+)/i, intent: 'READ_EMAIL', extract: m => ({ index: parseInt(m[1]) }) },
    { pattern: /^(?:apagar?|deletar?|excluir?|remover?)\s+(?:o\s+)?e-?mail\s+(\d+)/i, intent: 'QUEUE_DELETE', extract: m => ({ index: parseInt(m[1]) }) },
    { pattern: /^(?:apagar?|deletar?|excluir?|remover?)\s+(?:todos?|tudo)/i, intent: 'DELETE_ALL', extract: () => ({}) },
    { pattern: /^(?:marcar?\s+(?:como\s+)?lido)\s+(?:o\s+)?e-?mail\s+(\d+)/i, intent: 'QUEUE_MARK_READ', extract: m => ({ index: parseInt(m[1]) }) },
    { pattern: /^(?:marcar?\s+(?:como\s+)?(?:não\s+lido|n[aã]o\s+lido))\s+(?:o\s+)?e-?mail\s+(\d+)/i, intent: 'QUEUE_MARK_UNREAD', extract: m => ({ index: parseInt(m[1]) }) },
    { pattern: /^(?:responder?|reply)\s+(?:o\s+)?e-?mail\s+(\d+)/i, intent: 'REQUEST_REPLY', extract: m => ({ index: parseInt(m[1]) }) },
    { pattern: /^(?:sugerir?\s+resposta|sugira\s+resposta)\s+(?:(?:para|pro|do)\s+)?(?:o\s+)?e-?mail\s+(\d+)/i, intent: 'SUGGEST_REPLY', extract: m => ({ index: parseInt(m[1]) }) },
    { pattern: /^(?:sugerir?\s+resposta|sugira\s+resposta)/i, intent: 'SUGGEST_REPLY', extract: () => ({ index: 1 }) },
    { pattern: /^(?:mostrar?\s+(?:a\s+)?fila|ver\s+fila|queue|fila)/i, intent: 'SHOW_QUEUE', extract: () => ({}) },
    { pattern: /^(?:executar?|despachar?|confirmar?\s+despacho|dispatch)/i, intent: 'DISPATCH_QUEUE', extract: () => ({}) },
    { pattern: /^(?:limpar?\s+fila|esvaziar?\s+fila)/i, intent: 'CLEAR_QUEUE', extract: () => ({}) },
    { pattern: /^(?:quantos?\s+e-?mails?|contar?\s+e-?mails?)/i, intent: 'COUNT_EMAILS', extract: () => ({}) },
    { pattern: /^(?:listar?\s+(?:(?:todos?|todas?)\s+)?(?:os?\s+)?e-?mails?|liste?\s+(?:(?:todos?|todas?)\s+)?(?:os?\s+)?e-?mails?)/i, intent: 'LIST_EMAILS', extract: () => ({}) },
    { pattern: /^(?:resumo|resumir?|summ)/i, intent: 'SUMMARY', extract: () => ({}) },
    { pattern: /^(?:triagem|triage|classificar?|priorizar?)/i, intent: 'TRIAGE', extract: () => ({}) },
    { pattern: /^(?:ajuda|help|o\s+que\s+(?:voc[eê]|vc)\s+(?:faz|pode|sabe))/i, intent: 'HELP', extract: () => ({}) },
    { pattern: /^(?:atualizar?|refresh|recarregar?)/i, intent: 'REFRESH', extract: () => ({}) },
    { pattern: /^(?:parar?|cancelar?|stop)/i, intent: 'STOP', extract: () => ({}) },
    { pattern: /^(?:n[aã]o|cancelar?|cancela|n[aã]o\s+(?:faz|fa[cç]a|quero))/i, intent: 'DENY', extract: () => ({}) },
    { pattern: /^(?:sim|ok|confirmo?|confirmar?|pode|aprovado?|prosseguir?|execute?|executar?|pode\s+sim|manda\s+ver)/i, intent: 'APPROVE', extract: () => ({}) },
    { pattern: /^(?:pr[oó]ximo|next)/i, intent: 'NEXT', extract: () => ({}) },
    { pattern: /^(?:repetir?|repeat)/i, intent: 'REPEAT', extract: () => ({}) },
];

function hfParseIntent(text) {
    const clean = text.trim();
    for (const rule of HF_INTENTS) {
        const m = clean.match(rule.pattern);
        if (m) {
            return { intent: rule.intent, params: rule.extract(m), raw: clean };
        }
    }
    return { intent: 'LLM_CHAT', params: {}, raw: clean };
}

function hfGetActiveProviders() {
    if (HF.hfSource === 'apple') return 'apple';
    if (HF.hfSource === 'gmail') return 'gmail';
    if (typeof getSelectedProviders === 'function') {
        return getSelectedProviders().join(',');
    }
    return 'apple,gmail';
}

function hfStartAlwaysOn() {
    if (HF.alwaysOnActive) return;
    if (!('webkitSpeechRecognition' in window) && !('SpeechRecognition' in window)) {
        hfAddMessage('system', 'Navegador não suporta reconhecimento contínuo. Use Chrome.');
        HF.mode = 'auto';
        localStorage.setItem('handsfree_mode', 'auto');
        document.querySelectorAll('.hf-mode-btn').forEach(b => b.classList.toggle('active', b.dataset.mode === 'auto'));
        return;
    }

    const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    const recognition = new SpeechRecognition();
    recognition.continuous = true;
    recognition.interimResults = true;
    recognition.lang = 'pt-BR';
    recognition.maxAlternatives = 3;

    recognition.onresult = (event) => {
        if (HF.antiFeedbackLocked) return;
        if (HF.state !== 'IDLE') return;

        for (let i = event.resultIndex; i < event.results.length; i++) {
            for (let j = 0; j < event.results[i].length; j++) {
                const transcript = event.results[i][j].transcript.toLowerCase().trim();
                const words = transcript.split(/\s+/);
                const lastWords = words.slice(-3).join(' ');
                
                const detected = HF_HOTWORD_VARIANTS.some(hw => lastWords.includes(hw));
                if (detected) {
                    console.log('[HF Always-On] Hotword detected:', transcript);
                    hfPlayEarcon('confirm');
                    hfSetStatus('Hotword detectado! Ouvindo comando...', 'listening');
                    hfAddMessage('system', '🎯 Hotword detectado. Diga seu comando.');
                    hfPauseAlwaysOn();
                    setTimeout(() => {
                        if (HF.state === 'IDLE' && !HF.requestLock) {
                            hfStartListening();
                        }
                    }, 200);
                    return;
                }
            }
        }
    };

    recognition.onerror = (event) => {
        console.warn('[HF Always-On] Error:', event.error);
        if (event.error === 'not-allowed') {
            hfAddMessage('system', 'Permissão de microfone negada para escuta contínua.');
            HF.alwaysOnActive = false;
            return;
        }
        if (HF.alwaysOnActive && HF.mode === 'always_on') {
            setTimeout(() => hfResumeAlwaysOn(), HF_WEBSPECH_RESTART_DELAY_MS * 3);
        }
    };

    recognition.onend = () => {
        if (HF.alwaysOnActive && HF.mode === 'always_on' && HF.state === 'IDLE' && !HF.antiFeedbackLocked) {
            setTimeout(() => hfResumeAlwaysOn(), HF_WEBSPECH_RESTART_DELAY_MS);
        }
    };

    HF.alwaysOnRecognition = recognition;
    HF.alwaysOnActive = true;
    
    try {
        recognition.start();
        console.log('[HF Always-On] Started hotword listening');
        const emailCount = hfSessionEmails().length;
        hfSetStatus(`Always On ativo (${emailCount} e-mails). Diga "Rordens"...`, '');
        hfSetPhase('idle');
    } catch(e) {
        console.error('[HF Always-On] Start error:', e);
        HF.alwaysOnActive = false;
        hfAddMessage('system', 'Erro ao iniciar Always On: ' + (e.message || 'desconhecido') + '. Tente usar Chrome.');
        hfSetStatus('Erro no Always On. Toque no mic.', '');
    }
}

function hfStopAlwaysOn() {
    HF.alwaysOnActive = false;
    if (HF.alwaysOnRecognition) {
        try { HF.alwaysOnRecognition.abort(); } catch(e) {}
        HF.alwaysOnRecognition = null;
    }
}

function hfPauseAlwaysOn() {
    if (HF.alwaysOnRecognition) {
        try { HF.alwaysOnRecognition.abort(); } catch(e) {}
    }
}

function hfResumeAlwaysOn() {
    if (!HF.alwaysOnActive || HF.mode !== 'always_on') return;
    if (HF.state !== 'IDLE') return;
    if (HF.antiFeedbackLocked) return;
    
    if (HF.alwaysOnRecognition) {
        try {
            HF.alwaysOnRecognition.start();
            const emailCount = hfSessionEmails().length;
            hfSetStatus(`Always On ativo (${emailCount} e-mails). Diga "Rordens"...`, '');
            hfSetPhase('idle');
        } catch(e) {
            const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
            if (SpeechRecognition) {
                HF.alwaysOnRecognition = null;
                hfStartAlwaysOn();
            }
        }
    } else {
        hfStartAlwaysOn();
    }
}

function hfAntiFeedbackLock() {
    HF.antiFeedbackLocked = true;
    hfPauseAlwaysOn();
}

function hfAntiFeedbackUnlock() {
    HF.antiFeedbackLocked = false;
    if (HF.mode === 'always_on' && HF.state === 'IDLE') {
        setTimeout(() => hfResumeAlwaysOn(), HF_ANTI_FEEDBACK_DELAY_MS);
    }
}

function hfSessionEmails() {
    return (window.hfSession && window.hfSession.emails) ? window.hfSession.emails : (window.emails || []);
}

function hfGetEmail(index) {
    const list = hfSessionEmails();
    if (list.length === 0) return null;
    if (index < 1 || index > list.length) return null;
    return list[index - 1];
}

function hfInit() {
    const handsFreeBtn = document.getElementById('handsFreeBtn');
    if (handsFreeBtn) handsFreeBtn.addEventListener('click', hfToggle);
    const closeBtn = document.getElementById('hfCloseBtn');
    if (closeBtn) closeBtn.addEventListener('click', hfClose);
    const micBtn = document.getElementById('hfMicBtn');
    if (micBtn) micBtn.addEventListener('click', hfMicAction);

    const stopBtn = document.getElementById('hfStopBtn');
    if (stopBtn) stopBtn.addEventListener('click', hfStopAction);

    const repeatBtn = document.getElementById('hfRepeatBtn');
    if (repeatBtn) repeatBtn.addEventListener('click', hfRepeatAction);

    const debugToggle = document.getElementById('hfDebugToggle');
    if (debugToggle) debugToggle.addEventListener('change', (e) => {
        HF.debugEnabled = e.target.checked;
        const panel = document.getElementById('hfDebugPanel');
        if (panel) panel.classList.toggle('hidden', !HF.debugEnabled);
        hfUpdateDebug();
    });

    document.querySelectorAll('.hf-mode-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const prevMode = HF.mode;
            document.querySelectorAll('.hf-mode-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            HF.mode = btn.dataset.mode;
            localStorage.setItem('handsfree_mode', HF.mode);
            
            const overlay = document.getElementById('hfOverlay');
            const isOpen = overlay && !overlay.classList.contains('hidden');
            
            if (prevMode === 'always_on' && HF.mode !== 'always_on') {
                hfStopAlwaysOn();
            }

            if (isOpen && HF.state === 'IDLE') {
                const emailCount = hfSessionEmails().length;
                if (HF.mode === 'always_on') {
                    hfStartAlwaysOn();
                    hfSetStatus(`${emailCount} e-mails. Diga "Rordens" para comandar.`, '');
                    hfAddMessage('system', 'Always On ativado. Diga "Rordens" seguido do comando.');
                } else if (HF.mode === 'auto') {
                    hfSetStatus(`${emailCount} e-mails. Ouvindo...`, 'listening');
                    hfAddMessage('system', 'Modo automático ativado. Pode falar.');
                    setTimeout(() => {
                        if (HF.state === 'IDLE' && !HF.requestLock) hfStartListening();
                    }, 400);
                } else {
                    hfSetStatus(`${emailCount} e-mails. Toque no microfone para falar.`, '');
                }
            }
        });
    });

    const savedMode = HF.mode;
    document.querySelectorAll('.hf-mode-btn').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.mode === savedMode);
    });

    const voiceSelect = document.getElementById('hfVoiceSelect');
    if (voiceSelect) {
        voiceSelect.innerHTML = `
            <option value="nova">Nova (feminina)</option>
            <option value="alloy">Alloy (neutra)</option>
            <option value="ash">Ash (expressiva)</option>
            <option value="coral">Coral (calorosa)</option>
            <option value="echo">Echo (masculina)</option>
            <option value="fable">Fable (narrativa)</option>
            <option value="onyx">Onyx (grave)</option>
            <option value="shimmer">Shimmer (suave)</option>
            <option value="sage">Sage (calma)</option>
        `;
        voiceSelect.value = HF.selectedVoice;
        voiceSelect.addEventListener('change', () => {
            HF.selectedVoice = voiceSelect.value;
            localStorage.setItem('handsfree_voice_uri', voiceSelect.value);
        });
    }

    const speedSlider = document.getElementById('hfSpeedSlider');
    const speedValue = document.getElementById('hfSpeedValue');
    if (speedSlider) {
        speedSlider.value = HF.ttsSpeed;
        if (speedValue) speedValue.textContent = HF.ttsSpeed.toFixed(2) + 'x';
        speedSlider.addEventListener('input', () => {
            HF.ttsSpeed = parseFloat(speedSlider.value);
            localStorage.setItem('handsfree_tts_rate', HF.ttsSpeed.toString());
            if (speedValue) speedValue.textContent = HF.ttsSpeed.toFixed(2) + 'x';
        });
    }

    const normalizeToggle = document.getElementById('hfNormalizeToggle');
    if (normalizeToggle) {
        normalizeToggle.checked = HF.normalizeLangs;
        normalizeToggle.addEventListener('change', (e) => {
            HF.normalizeLangs = e.target.checked;
            localStorage.setItem('handsfree_normalize_langs', HF.normalizeLangs.toString());
        });
    }

    const sourceSelect = document.getElementById('hfSourceSelect');
    if (sourceSelect) {
        sourceSelect.value = HF.hfSource;
        sourceSelect.addEventListener('change', () => {
            HF.hfSource = sourceSelect.value;
            localStorage.setItem('handsfree_source', HF.hfSource);
        });
    }

    const testVoiceBtn = document.getElementById('hfTestVoiceBtn');
    if (testVoiceBtn) testVoiceBtn.addEventListener('click', hfTestVoice);

    const editSendBtn = document.getElementById('hfEditSendBtn');
    if (editSendBtn) editSendBtn.addEventListener('click', hfSendEditedText);

    const editInput = document.getElementById('hfEditInput');
    if (editInput) editInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); hfSendEditedText(); }
    });
}

async function hfTestVoice() {
    if (HF.state !== 'IDLE') return;
    hfForceState('SPEAKING', 'test voice');
    await hfSpeak('Olá! Pronto para revisar seus e-mails.');
}

async function hfSendEditedText() {
    const input = document.getElementById('hfEditInput');
    if (!input) return;
    const text = input.value.trim();
    if (!text) return;
    input.value = '';
    hfAddMessage('user', text);
    if (HF.state !== 'IDLE') return;
    if (!hfTransition('PROCESSING', 'text input')) {
        hfForceState('PROCESSING', 'text input force');
    }
    HF.requestLock = true;
    HF.lastTranscript = text;
    const turn = ++HF.activeTurn;
    HF.timings.t_turn_start = hfTimestamp();
    hfSetStatus('Processando...', 'processing');
    hfSetPhase('thinking');
    await hfProcessIntent(text, turn);
}

function hfToggle() {
    const overlay = document.getElementById('hfOverlay');
    if (!overlay) return;
    if (HF.state !== 'IDLE' || !overlay.classList.contains('hidden')) {
        hfClose();
    } else {
        hfOpen();
    }
}

async function hfOpen() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        if (typeof showToast === 'function') showToast('Seu navegador não suporta captura de áudio.');
        return;
    }

    HF.state = 'IDLE';
    HF.phase = 'idle';
    HF.requestLock = false;
    HF.activeTurn = 0;
    HF.proposedActions = [];
    HF.pendingConfirmation = null;
    HF.lastResponse = null;
    HF.lastTranscript = '';
    HF.lastIntent = null;
    HF.lastError = null;
    HF.timings = {};

    const overlay = document.getElementById('hfOverlay');
    if (overlay) overlay.classList.remove('hidden');
    const hfBtn = document.getElementById('handsFreeBtn');
    if (hfBtn) hfBtn.classList.add('active');
    hfSetStatus('Preparando...', 'processing');
    hfUpdateUI();

    try {
        HF.stream = await navigator.mediaDevices.getUserMedia({
            audio: { sampleRate: 16000, channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true }
        });
        hfMuteMic();
    } catch (e) {
        hfAddMessage('system', 'Permissão de microfone negada.');
        hfForceState('ERROR', 'mic denied on open');
        setTimeout(() => { if (HF.state === 'ERROR') hfForceState('IDLE', 'auto recover'); }, 3000);
        return;
    }

    try {
        await apiCall('/llm/chat/reset', {
            method: 'POST',
            body: JSON.stringify({ session_id: ensureSession() })
        });
    } catch (e) {}

    const sessionEmails = Array.isArray(window.emails) ? [...window.emails] : [];
    const sessionFilter = typeof getHomeFilterState === 'function' ? getHomeFilterState() : {};
    window.hfSession = { emails: sessionEmails, filter: sessionFilter, startedAt: Date.now() };
    console.log("[DIAG] HF session created:", window.hfSession.emails.length, window.hfSession.filter);

    let emailCount = window.hfSession.emails.length;

    if (emailCount === 0) {
        hfSetStatus('Verificando snapshot...', 'processing');
        try {
            const sid = ensureSession();
            const providers = hfGetActiveProviders();
            const ctx = await apiCall(`/handsfree/context?session_id=${encodeURIComponent(sid)}&providers=${encodeURIComponent(providers)}`);
            if (ctx.ok && ctx.count > 0) {
                emailCount = ctx.count;
                HF.contextId = ctx.context_id || null;
            }
        } catch (e) {}
    }

    if (emailCount === 0) {
        hfSetStatus('Carregando e-mails...', 'processing');
        try {
            const freshEmails = typeof fetchEmailsIsolated === 'function'
                ? await fetchEmailsIsolated()
                : [];
            if (freshEmails.length > 0) {
                window.hfSession.emails = freshEmails;
                window.hfSession.filter = typeof getHomeFilterState === 'function' ? getHomeFilterState() : {};
                emailCount = freshEmails.length;
            }
        } catch (e) {}
    }

    if (emailCount > 0) {
        if (HF.mode === 'always_on') {
            hfSetStatus(`${emailCount} e-mails. Diga "Rordens" para comandar.`, '');
        } else if (HF.mode === 'auto') {
            hfSetStatus(`${emailCount} e-mails. Ouvindo...`, 'listening');
        } else {
            hfSetStatus(`${emailCount} e-mails carregados. Toque no microfone para falar.`, '');
        }
    } else {
        hfSetStatus('Nenhum e-mail. Clique "Atualizar" primeiro.', '');
    }

    if (HF.mode === 'always_on') {
        hfAddMessage('system', 'Always On ativado. Diga "Rordens" seguido do comando.');
    } else if (HF.mode === 'auto') {
        hfAddMessage('system', 'Modo automático. Pode falar seu comando.');
    } else {
        hfAddMessage('system', 'Hands Free pronto. Toque no microfone e diga um comando.');
    }
    hfSetPhase('idle');
    hfUpdateUI();
    hfUpdateDebug();

    if (HF.mode === 'always_on') {
        hfStartAlwaysOn();
    } else if (HF.mode === 'auto' && emailCount > 0) {
        setTimeout(() => {
            if (HF.state === 'IDLE' && !HF.requestLock) {
                hfStartListening();
            }
        }, 500);
    }
}

function hfClose() {
    hfStopAllAudio();
    hfStopAlwaysOn();
    hfCleanupStream();
    hfClearProcessingTimeout();
    HF.state = 'IDLE';
    HF.phase = 'idle';
    HF.requestLock = false;
    HF.proposedActions = [];
    HF.pendingConfirmation = null;

    const overlay = document.getElementById('hfOverlay');
    if (overlay) overlay.classList.add('hidden');
    const hfBtn = document.getElementById('handsFreeBtn');
    if (hfBtn) hfBtn.classList.remove('active');
    hfUpdateUI();
}

function hfStopAllAudio() {
    hfStopVoiceInterrupt();
    hfStopSilenceDetection();
    hfStopSpeaking();
    hfStopListeningRaw();
}

function hfCleanupStream() {
    if (HF.analyserSource) {
        try { HF.analyserSource.disconnect(); } catch(e) {}
        HF.analyserSource = null;
    }
    if (HF.audioCtx) {
        try { HF.audioCtx.close(); } catch(e) {}
        HF.audioCtx = null;
        HF.analyser = null;
    }
    if (HF.stream) {
        HF.stream.getTracks().forEach(t => t.stop());
        HF.stream = null;
    }
    HF.mediaRecorder = null;
    HF.audioChunks = [];
}

function hfMicAction() {
    if (HF.state === 'SPEAKING') {
        hfStopSpeaking();
        hfForceState('IDLE', 'barge-in');
        return;
    }
    if (HF.state === 'LISTENING') {
        hfForceStopAndProcess();
        return;
    }
    if (HF.state === 'PROCESSING') {
        hfSetStatus('Processando... aguarde', 'processing');
        return;
    }
    if (HF.state === 'IDLE') {
        hfStartListening();
    }
    if (HF.state === 'ERROR') {
        hfForceState('IDLE', 'user retry');
    }
}

function hfStopAction() {
    if (HF.state === 'SPEAKING') {
        hfStopSpeaking();
        hfForceState('IDLE', 'user stop');
    } else if (HF.state === 'LISTENING') {
        hfStopListeningRaw();
        hfStopSilenceDetection();
        hfForceState('IDLE', 'user stop');
    } else if (HF.state === 'PROCESSING') {
        hfSetStatus('Processando... não é possível cancelar', 'processing');
    }
    HF.pendingConfirmation = null;
}

function hfRepeatAction() {
    if (HF.lastResponse && HF.state === 'IDLE') {
        hfSpeak(HF.lastResponse);
    }
}

function hfSetStatus(text, cssState) {
    const el = document.getElementById('hfStatus');
    if (el) {
        el.textContent = text;
        el.className = 'hf-status' + (cssState ? ' ' + cssState : '');
    }
    const micBtn = document.getElementById('hfMicBtn');
    if (micBtn) micBtn.className = 'hf-mic-btn' + (cssState ? ' ' + cssState : '');

    const pulse = document.getElementById('hfPulse');
    if (pulse) {
        if (cssState === 'listening') pulse.classList.add('active');
        else pulse.classList.remove('active');
    }
}

function hfAddMessage(role, text) {
    const container = document.getElementById('hfTranscript');
    if (!container) return;
    const welcome = container.querySelector('.hf-welcome');
    if (welcome) welcome.remove();

    const div = document.createElement('div');
    div.className = `hf-msg ${role}`;
    div.textContent = text;
    container.appendChild(div);
    container.scrollTop = container.scrollHeight;
}

function hfAddConfirmCard(message, onApprove, onDeny) {
    const container = document.getElementById('hfTranscript');
    if (!container) return;
    const div = document.createElement('div');
    div.className = 'hf-msg confirm-card';
    div.innerHTML = `
        <div style="margin-bottom:8px;">${hfEscape(message)}</div>
        <button class="hf-confirm-yes" data-role="approve">Confirmar</button>
        <button class="hf-confirm-no" data-role="deny">Cancelar</button>
    `;
    const yesBtn = div.querySelector('[data-role="approve"]');
    const noBtn = div.querySelector('[data-role="deny"]');
    yesBtn.addEventListener('click', () => {
        div.innerHTML = '<span style="color:#00c853;">Confirmado</span>';
        onApprove();
    });
    noBtn.addEventListener('click', () => {
        div.innerHTML = '<span style="color:#e53935;">Cancelado</span>';
        onDeny();
    });
    container.appendChild(div);
    container.scrollTop = container.scrollHeight;
}

function hfAddActionCard(actions) {
    const container = document.getElementById('hfTranscript');
    if (!container) return;
    const actionLabels = { send: 'ENVIAR', delete: 'DELETAR', mark_read: 'MARCAR LIDO', mark_unread: 'MARCAR NÃO LIDO', skip: 'IGNORAR' };

    actions.forEach((action, idx) => {
        const emailInfo = hfSessionEmails().find(e => e.key === action.key);
        const subject = emailInfo ? emailInfo.subject : action.key;
        const globalIdx = HF.proposedActions.length - actions.length + idx;
        const div = document.createElement('div');
        div.className = 'hf-msg action';
        div.innerHTML = `
            <strong>${actionLabels[action.action] || action.action}</strong>: ${hfEscape(subject)}
            ${action.body ? `<br><small>${hfEscape(action.body.substring(0, 80))}...</small>` : ''}
            <br>
            <button onclick="hfApproveOneAction(this, ${globalIdx})">Executar</button>
            <button onclick="this.closest('.hf-msg').remove()">Cancelar</button>
        `;
        container.appendChild(div);
    });
    container.scrollTop = container.scrollHeight;
}

function hfEscape(str) {
    const d = document.createElement('div');
    d.textContent = str || '';
    return d.innerHTML;
}

async function hfApproveOneAction(btn, idx) {
    const action = HF.proposedActions[idx];
    if (!action) return;
    btn.disabled = true;
    const parent = btn.parentElement;
    if (parent) parent.innerHTML = '<span style="color:#f0ad4e;">Executando...</span>';
    try {
        const data = await apiCall('/llm/dispatch', {
            method: 'POST',
            body: JSON.stringify({
                session_id: ensureSession(),
                actions: [{ key: action.key, action: action.action, body: action.body || null }],
                mode: 'execute',
                confirm_delete: true,
            })
        });
        if (data.ok && data.results && data.results[0]?.status === 'ok') {
            if (parent) parent.innerHTML = '<span style="color:#00c853;">Executado</span>';
            if (typeof removeExecutedEmails === 'function') removeExecutedEmails(data.results);
        } else {
            if (parent) parent.innerHTML = `<span style="color:#e53935;">Falha: ${data.results?.[0]?.message || 'erro'}</span>`;
        }
    } catch (err) {
        if (parent) parent.innerHTML = `<span style="color:#e53935;">Erro: ${err.message}</span>`;
    }
}

function hfUpdateUI() {
    const stopBtn = document.getElementById('hfStopBtn');
    const repeatBtn = document.getElementById('hfRepeatBtn');

    if (stopBtn) stopBtn.style.display = (HF.state === 'LISTENING' || HF.state === 'SPEAKING') ? '' : 'none';
    if (repeatBtn) repeatBtn.style.display = (HF.state === 'IDLE' && HF.lastResponse) ? '' : 'none';

    const micBtn = document.getElementById('hfMicBtn');
    if (micBtn) {
        if (HF.state === 'IDLE') micBtn.title = 'Toque para falar';
        else if (HF.state === 'LISTENING') micBtn.title = 'Toque para enviar';
        else if (HF.state === 'SPEAKING') micBtn.title = 'Toque para interromper';
        else micBtn.title = 'Aguarde...';
    }
}

function hfCalcLatencies() {
    const t = HF.timings;
    const latencies = {};
    if (t.t_turn_start && t.t_stt_end) latencies.stt = t.t_stt_end - t.t_turn_start;
    if (t.t_stt_end && t.t_intent_end) latencies.intent = t.t_intent_end - t.t_stt_end;
    if (t.t_intent_end && t.t_tts_start) latencies.llm = t.t_tts_start - t.t_intent_end;
    if (t.t_tts_start && t.t_tts_end) latencies.tts = t.t_tts_end - t.t_tts_start;
    if (t.t_tts_end && t.t_audio_play_start) latencies.audio_start = t.t_audio_play_start - t.t_tts_end;
    if (t.t_turn_start && t.t_audio_play_start) latencies.total = t.t_audio_play_start - t.t_turn_start;
    if (t.t_turn_start && t.t_audio_play_end) latencies.full_turn = t.t_audio_play_end - t.t_turn_start;
    if (t.t_record_start && t.t_record_end) latencies.record = t.t_record_end - t.t_record_start;
    return latencies;
}

function hfUpdateDebug() {
    const panel = document.getElementById('hfDebugContent');
    if (!panel || !HF.debugEnabled) return;

    const latencies = hfCalcLatencies();

    const lines = [
        `state: ${HF.state}`,
        `phase: ${HF.phase}`,
        `activeTurn: ${HF.activeTurn}`,
        `requestLock: ${HF.requestLock}`,
        `mode: ${HF.mode}`,
        `alwaysOn: ${HF.alwaysOnActive}`,
        `antiFeedback: ${HF.antiFeedbackLocked}`,
        `ttsSpeed: ${HF.ttsSpeed}`,
        `normalizeLangs: ${HF.normalizeLangs}`,
        `voice: ${HF.selectedVoice}`,
        `lastTranscript: ${HF.lastTranscript}`,
        `lastIntent: ${HF.lastIntent ? JSON.stringify(HF.lastIntent) : 'null'}`,
        `lastError: ${HF.lastError || 'null'}`,
        `proposedActions: ${HF.proposedActions.length}`,
        `pendingConfirmation: ${HF.pendingConfirmation ? 'yes' : 'no'}`,
        `emails: ${hfSessionEmails().length}`,
        `stream: ${HF.stream ? 'active' : 'null'}`,
        `speakGen: ${HF.speakGeneration}`,
        '--- LATÊNCIAS ---',
    ];

    if (Object.keys(latencies).length > 0) {
        for (const [k, v] of Object.entries(latencies)) {
            lines.push(`${k}: ${v}ms`);
        }
    } else {
        lines.push('(aguardando turno completo)');
    }

    lines.push('--- TIMESTAMPS ---');
    for (const [k, v] of Object.entries(HF.timings)) {
        lines.push(`${k}: ${v}`);
    }
    panel.textContent = lines.join('\n');
}

function hfMuteMic() {
    if (HF.stream) HF.stream.getAudioTracks().forEach(t => { t.enabled = false; });
}

function hfUnmuteMic() {
    if (HF.stream) HF.stream.getAudioTracks().forEach(t => { t.enabled = true; });
}

async function hfStartListening() {
    if (HF.state !== 'IDLE') return;
    if (HF.requestLock) {
        hfSetStatus('Aguarde...', '');
        return;
    }

    if (!hfTransition('LISTENING', 'mic action')) return;

    const turn = ++HF.activeTurn;
    HF.timings = {};
    HF.timings.t_turn_start = hfTimestamp();

    try {
        if (!HF.stream || HF.stream.getAudioTracks().every(t => t.readyState === 'ended')) {
            HF.stream = await navigator.mediaDevices.getUserMedia({
                audio: { sampleRate: 16000, channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true }
            });
        }
        hfUnmuteMic();
    } catch (e) {
        hfAddMessage('system', 'Permissão de microfone negada.');
        hfForceState('ERROR', 'mic denied');
        HF.lastError = 'Mic denied';
        setTimeout(() => { if (HF.state === 'ERROR') hfForceState('IDLE', 'auto recover'); }, 3000);
        return;
    }

    HF.audioChunks = [];
    HF.timings.t_record_start = hfTimestamp();
    hfSetStatus('Ouvindo...', 'listening');
    hfSetPhase('listening');

    let mimeType = 'audio/webm';
    if (typeof MediaRecorder !== 'undefined') {
        if (MediaRecorder.isTypeSupported('audio/webm;codecs=opus')) mimeType = 'audio/webm;codecs=opus';
        else if (MediaRecorder.isTypeSupported('audio/mp4')) mimeType = 'audio/mp4';
        else if (MediaRecorder.isTypeSupported('audio/ogg')) mimeType = 'audio/ogg';
    }
    const fileExt = mimeType.includes('mp4') ? 'mp4' : mimeType.includes('ogg') ? 'ogg' : 'webm';

    HF.mediaRecorder = new MediaRecorder(HF.stream, { mimeType });

    HF.mediaRecorder.ondataavailable = (e) => {
        if (e.data.size > 0) HF.audioChunks.push(e.data);
    };

    HF.mediaRecorder.onstop = async () => {
        if (turn !== HF.activeTurn) { console.log(`[HF] stale turn ${turn}, ignoring`); return; }

        HF.timings.t_record_end = hfTimestamp();
        hfStopSilenceDetection();
        hfMuteMic();

        if (HF.audioChunks.length === 0 || HF.state !== 'LISTENING') {
            if (HF.state === 'LISTENING') hfForceState('IDLE', 'no audio');
            return;
        }

        const audioBlob = new Blob(HF.audioChunks, { type: mimeType });
        HF.audioChunks = [];

        if (audioBlob.size < HF_MIN_BLOB_SIZE) {
            if (HF.state === 'LISTENING') hfForceState('IDLE', 'audio too small');
            hfSetStatus('Não detectei fala. Toque para tentar.', '');
            return;
        }

        if (!hfTransition('PROCESSING', 'audio captured')) return;
        HF.requestLock = true;
        hfSetStatus('Transcrevendo...', 'processing');
        hfSetPhase('transcribing');

        try {
            HF.timings.t_stt_start = hfTimestamp();
            const formData = new FormData();
            formData.append('audio', audioBlob, `audio.${fileExt}`);
            formData.append('language', 'pt');

            const headers = {};
            if (typeof apiKey !== 'undefined' && apiKey) headers['X-API-Key'] = apiKey;
            const resp = await fetch('/voice/transcribe', { method: 'POST', headers, body: formData });
            const data = await resp.json();
            HF.timings.t_stt_end = hfTimestamp();

            if (turn !== HF.activeTurn) { HF.requestLock = false; return; }

            if (data.ok && data.text && data.text.trim().length > 1) {
                let transcript = data.text.trim();
                transcript = hfPostCorrect(transcript);
                HF.lastTranscript = transcript;
                hfAddMessage('user', transcript);
                if (typeof addChatBubble === 'function') addChatBubble('user', transcript);
                hfUpdateDebug();
                hfSetPhase('thinking');
                HF.timings.t_intent_start = hfTimestamp();
                await hfProcessIntent(transcript, turn);
            } else {
                HF.requestLock = false;
                hfForceState('IDLE', 'empty transcript');
                hfSetStatus('Não entendi. Toque no microfone ou digite abaixo.', '');
                if (HF.mode === 'auto') {
                    setTimeout(() => { if (HF.state === 'IDLE' && !HF.requestLock) hfStartListening(); }, HF_AUTO_LISTEN_DELAY_MS * 2);
                }
            }
        } catch (err) {
            HF.requestLock = false;
            HF.lastError = err.message;
            hfForceState('ERROR', 'transcribe error');
            hfSetStatus('Erro: ' + err.message, '');
            setTimeout(() => { if (HF.state === 'ERROR') hfForceState('IDLE', 'auto recover'); }, 3000);
        }
    };

    HF.mediaRecorder.start(HF_TIMESLICE_MS);
    hfStartSilenceDetection();

    const maxMs = HF.mode === 'auto' ? HF_MAX_RECORD_MS : HF_MAX_RECORD_MANUAL_MS;
    HF.silenceTimer = setTimeout(() => {
        if (HF.state === 'LISTENING' && HF.mediaRecorder && HF.mediaRecorder.state === 'recording') {
            hfStopSilenceDetection();
            HF.mediaRecorder.stop();
        }
    }, maxMs);
}

function hfStopListeningRaw() {
    if (HF.silenceTimer) { clearTimeout(HF.silenceTimer); HF.silenceTimer = null; }
    if (HF.mediaRecorder && HF.mediaRecorder.state === 'recording') {
        try { HF.mediaRecorder.stop(); } catch(e) {}
    }
    hfMuteMic();
}

function hfForceStopAndProcess() {
    hfStopSilenceDetection();
    if (HF.silenceTimer) { clearTimeout(HF.silenceTimer); HF.silenceTimer = null; }
    if (HF.mediaRecorder && HF.mediaRecorder.state === 'recording') {
        HF.mediaRecorder.stop();
    }
}

function hfStartSilenceDetection() {
    hfStopSilenceDetection();
    if (!HF.stream) return;
    try {
        if (!HF.audioCtx || HF.audioCtx.state === 'closed') {
            HF.audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        }
        if (HF.analyserSource) {
            try { HF.analyserSource.disconnect(); } catch(e) {}
        }
        HF.analyserSource = HF.audioCtx.createMediaStreamSource(HF.stream);
        HF.analyser = HF.audioCtx.createAnalyser();
        HF.analyser.fftSize = 512;
        HF.analyserSource.connect(HF.analyser);
    } catch(e) { return; }

    const dataArray = new Uint8Array(HF.analyser.frequencyBinCount);
    HF.silenceStart = null;
    let soundSamples = 0;
    const MIN_SOUND_SAMPLES = 6;

    HF.silenceInterval = setInterval(() => {
        if (HF.state !== 'LISTENING' || !HF.mediaRecorder || HF.mediaRecorder.state !== 'recording') {
            hfStopSilenceDetection();
            return;
        }
        if (!HF.analyser) { hfStopSilenceDetection(); return; }
        HF.analyser.getByteFrequencyData(dataArray);
        const avg = dataArray.reduce((a, b) => a + b, 0) / dataArray.length;

        if (avg > HF_SOUND_THRESHOLD) {
            soundSamples++;
            HF.silenceStart = null;
        } else if (soundSamples >= MIN_SOUND_SAMPLES) {
            if (!HF.silenceStart) HF.silenceStart = Date.now();
            else if (Date.now() - HF.silenceStart > HF_SILENCE_MS) {
                hfStopSilenceDetection();
                if (HF.mediaRecorder && HF.mediaRecorder.state === 'recording') {
                    HF.mediaRecorder.stop();
                }
            }
        }
    }, HF_SILENCE_CHECK_MS);
}

function hfStopSilenceDetection() {
    if (HF.silenceInterval) { clearInterval(HF.silenceInterval); HF.silenceInterval = null; }
    HF.silenceStart = null;
    if (HF.analyserSource) {
        try { HF.analyserSource.disconnect(); } catch(e) {}
        HF.analyserSource = null;
    }
}

async function hfProcessIntent(text, turn) {
    const parsed = hfParseIntent(text);
    HF.lastIntent = parsed;
    HF.timings.t_intent_end = hfTimestamp();
    hfUpdateDebug();

    const emails = hfSessionEmails();
    console.log("[DIAG] HF reading from session count:", emails.length);

    if (HF.pendingConfirmation) {
        const confirmation = HF.pendingConfirmation;
        HF.pendingConfirmation = null;

        if (parsed.intent === 'APPROVE' || parsed.intent === 'DISPATCH_QUEUE') {
            if (confirmation.doubleConfirm && !confirmation.firstConfirmDone) {
                confirmation.firstConfirmDone = true;
                HF.pendingConfirmation = confirmation;
                const deleteCount = confirmation.actions.filter(a => a.action === 'delete').length;
                const msg = `Atenção: ${deleteCount} emails serão deletados. Diga "confirmar" novamente para prosseguir.`;
                hfAddMessage('system', msg);
                HF.requestLock = false;
                await hfSpeakAndContinue(msg);
                return;
            }
            hfPlayEarcon('confirm');
            hfSetPhase('executing');
            hfSetStatus('Executando ações...', 'processing');
            const result = await hfDispatch(confirmation.actions);
            HF.proposedActions = HF.proposedActions.filter(a => !confirmation.actions.includes(a));
            HF.requestLock = false;
            await hfSpeakAndContinue(result);
            return;
        } else if (parsed.intent === 'DENY' || parsed.intent === 'STOP') {
            const msg = 'Cancelado. Ações não executadas.';
            hfAddMessage('system', msg);
            HF.requestLock = false;
            await hfSpeakAndContinue(msg);
            return;
        } else {
            const msg = 'Confirmação cancelada. Processando novo comando.';
            hfAddMessage('system', msg);
        }
    }

    try {
        switch (parsed.intent) {
            case 'READ_EMAIL': {
                const email = hfGetEmail(parsed.params.index);
                if (!email) {
                    const msg = emails.length === 0
                        ? 'Nenhum e-mail carregado. Atualize a lista primeiro.'
                        : `E-mail ${parsed.params.index} não encontrado. Temos ${emails.length} e-mails.`;
                    hfAddMessage('system', msg);
                    HF.requestLock = false;
                    await hfSpeakAndContinue(msg);
                    return;
                }
                const snippet = email.snippet || email.body_preview || '';
                const msg = `E-mail ${parsed.params.index}: De ${email.from || 'desconhecido'}. Assunto: ${email.subject || 'sem assunto'}. ${snippet.substring(0, 200)}`;
                hfAddMessage('assistant', msg);
                HF.requestLock = false;
                await hfSpeakAndContinue(msg);
                return;
            }
            case 'QUEUE_DELETE': {
                const email = hfGetEmail(parsed.params.index);
                if (!email) {
                    const msg = `E-mail ${parsed.params.index} não encontrado.`;
                    hfAddMessage('system', msg);
                    HF.requestLock = false;
                    await hfSpeakAndContinue(msg);
                    return;
                }
                HF.proposedActions.push({ key: email.key, action: 'delete', body: null });
                const msg = `Deletar "${email.subject}" adicionado à fila. ${HF.proposedActions.length} ação(ões) pendente(s). Diga "executar" para confirmar.`;
                hfAddMessage('system', msg);
                hfAddActionCard([{ key: email.key, action: 'delete', body: null }]);
                HF.requestLock = false;
                await hfSpeakAndContinue(msg);
                return;
            }
            case 'DELETE_ALL': {
                if (emails.length === 0) {
                    const msg = 'Nenhum e-mail carregado para apagar.';
                    hfAddMessage('system', msg);
                    HF.requestLock = false;
                    await hfSpeakAndContinue(msg);
                    return;
                }
                const deleteActions = emails.map(e => ({ key: e.key, action: 'delete', body: null }));
                HF.proposedActions = HF.proposedActions.concat(deleteActions);

                const msg = `Você quer apagar ${emails.length} e-mails. Confirmar?`;
                hfAddMessage('system', msg);
                HF.pendingConfirmation = { type: 'delete_all', actions: deleteActions };
                hfAddConfirmCard(
                    `Apagar ${emails.length} e-mails?`,
                    async () => {
                        HF.pendingConfirmation = null;
                        hfSetPhase('executing');
                        hfSetStatus('Apagando...', 'processing');
                        if (HF.state === 'IDLE') {
                            hfForceState('PROCESSING', 'confirm click');
                            HF.requestLock = true;
                        }
                        const result = await hfDispatch(deleteActions);
                        HF.proposedActions = HF.proposedActions.filter(a => !deleteActions.includes(a));
                        HF.requestLock = false;
                        await hfSpeakAndContinue(result);
                    },
                    () => {
                        HF.pendingConfirmation = null;
                        HF.proposedActions = HF.proposedActions.filter(a => !deleteActions.includes(a));
                        hfAddMessage('system', 'Cancelado.');
                    }
                );
                HF.requestLock = false;
                await hfSpeakAndContinue(msg);
                return;
            }
            case 'QUEUE_MARK_READ': {
                const email = hfGetEmail(parsed.params.index);
                if (!email) {
                    const msg = `E-mail ${parsed.params.index} não encontrado.`;
                    hfAddMessage('system', msg);
                    HF.requestLock = false;
                    await hfSpeakAndContinue(msg);
                    return;
                }
                HF.proposedActions.push({ key: email.key, action: 'mark_read', body: null });
                const msg = `Marcar "${email.subject}" como lido adicionado à fila. Diga "executar" para confirmar.`;
                hfAddMessage('system', msg);
                HF.requestLock = false;
                await hfSpeakAndContinue(msg);
                return;
            }
            case 'QUEUE_MARK_UNREAD': {
                const email = hfGetEmail(parsed.params.index);
                if (!email) {
                    const msg = `E-mail ${parsed.params.index} não encontrado.`;
                    hfAddMessage('system', msg);
                    HF.requestLock = false;
                    await hfSpeakAndContinue(msg);
                    return;
                }
                HF.proposedActions.push({ key: email.key, action: 'mark_unread', body: null });
                const msg = `Marcar "${email.subject}" como não lido adicionado à fila. Diga "executar" para confirmar.`;
                hfAddMessage('system', msg);
                HF.requestLock = false;
                await hfSpeakAndContinue(msg);
                return;
            }
            case 'REQUEST_REPLY':
            case 'SUGGEST_REPLY': {
                const email = hfGetEmail(parsed.params.index);
                if (!email) {
                    const msg = `E-mail ${parsed.params.index} não encontrado.`;
                    hfAddMessage('system', msg);
                    HF.requestLock = false;
                    await hfSpeakAndContinue(msg);
                    return;
                }
                hfSetStatus('Gerando sugestão de resposta...', 'processing');
                hfSetPhase('thinking');
                const data = await apiCall('/llm/suggest-reply', {
                    method: 'POST',
                    body: JSON.stringify({ session_id: ensureSession(), key: email.key, tone: 'neutral' })
                });
                if (turn !== HF.activeTurn) { HF.requestLock = false; return; }

                let reply;
                if (data.queued && data.job_id) {
                    hfSetStatus('Na fila...', 'processing');
                    const jobResult = await hfPollJob(data.job_id);
                    reply = jobResult?.result?.reply || jobResult?.result?.suggestion;
                } else {
                    reply = data.reply || data.suggestion;
                }

                if (reply) {
                    HF.proposedActions.push({ key: email.key, action: 'send', body: reply });
                    const msg = `Resposta sugerida para "${email.subject}": ${reply.substring(0, 150)}... Adicionada à fila. Diga "executar" para enviar.`;
                    hfAddMessage('assistant', msg);
                    hfAddActionCard([{ key: email.key, action: 'send', body: reply }]);
                    HF.requestLock = false;
                    await hfSpeakAndContinue(msg);
                } else {
                    const msg = 'Não consegui gerar uma resposta. Tente novamente.';
                    hfAddMessage('system', msg);
                    HF.requestLock = false;
                    await hfSpeakAndContinue(msg);
                }
                return;
            }
            case 'SHOW_QUEUE': {
                if (HF.proposedActions.length === 0) {
                    const msg = 'Fila vazia. Nenhuma ação pendente.';
                    hfAddMessage('system', msg);
                    HF.requestLock = false;
                    await hfSpeakAndContinue(msg);
                } else {
                    const items = HF.proposedActions.map((a, i) => {
                        const e = hfSessionEmails().find(e => e.key === a.key);
                        return `${i + 1}. ${a.action} - ${e ? e.subject : a.key}`;
                    });
                    const msg = `Fila com ${HF.proposedActions.length} ação(ões): ${items.join('. ')}`;
                    hfAddMessage('system', msg);
                    HF.requestLock = false;
                    await hfSpeakAndContinue(msg);
                }
                return;
            }
            case 'DISPATCH_QUEUE':
            case 'APPROVE': {
                if (HF.proposedActions.length === 0) {
                    const msg = 'Fila vazia. Nada para executar.';
                    hfAddMessage('system', msg);
                    HF.requestLock = false;
                    await hfSpeakAndContinue(msg);
                    return;
                }
                const hasDeletes = HF.proposedActions.some(a => a.action === 'delete');
                if (hasDeletes) {
                    const deleteCount = HF.proposedActions.filter(a => a.action === 'delete').length;
                    const msg = `A fila tem ${deleteCount} exclusão(ões). Confirmar execução?`;
                    hfAddMessage('system', msg);
                    HF.pendingConfirmation = { type: 'dispatch', actions: [...HF.proposedActions] };
                    hfAddConfirmCard(
                        msg,
                        async () => {
                            HF.pendingConfirmation = null;
                            hfSetPhase('executing');
                            hfSetStatus('Executando fila...', 'processing');
                            if (HF.state === 'IDLE') {
                                hfForceState('PROCESSING', 'confirm click');
                                HF.requestLock = true;
                            }
                            const result = await hfDispatch(HF.proposedActions);
                            HF.proposedActions = [];
                            HF.requestLock = false;
                            await hfSpeakAndContinue(result);
                        },
                        () => {
                            HF.pendingConfirmation = null;
                            hfAddMessage('system', 'Cancelado.');
                        }
                    );
                    HF.requestLock = false;
                    await hfSpeakAndContinue(msg);
                } else {
                    hfSetPhase('executing');
                    hfSetStatus('Executando fila...', 'processing');
                    const result = await hfDispatch(HF.proposedActions);
                    HF.proposedActions = [];
                    HF.requestLock = false;
                    await hfSpeakAndContinue(result);
                }
                return;
            }
            case 'DENY': {
                HF.pendingConfirmation = null;
                const msg = 'Ok, cancelado.';
                hfAddMessage('system', msg);
                HF.requestLock = false;
                await hfSpeakAndContinue(msg);
                return;
            }
            case 'CLEAR_QUEUE': {
                HF.proposedActions = [];
                HF.pendingConfirmation = null;
                const msg = 'Fila limpa.';
                hfAddMessage('system', msg);
                HF.requestLock = false;
                await hfSpeakAndContinue(msg);
                return;
            }
            case 'COUNT_EMAILS': {
                const msg = emails.length > 0
                    ? `Você tem ${emails.length} e-mails carregados.`
                    : 'Nenhum e-mail carregado. Atualize a lista.';
                hfAddMessage('system', msg);
                HF.requestLock = false;
                await hfSpeakAndContinue(msg);
                return;
            }
            case 'LIST_EMAILS': {
                if (emails.length === 0) {
                    const msg = 'Nenhum e-mail carregado. Atualize a lista.';
                    hfAddMessage('system', msg);
                    HF.requestLock = false;
                    await hfSpeakAndContinue(msg);
                    return;
                }
                const listItems = emails.map((e, i) => {
                    const from = (e.from || '?').split('<')[0].trim().split('@')[0];
                    const subj = (e.subject || 'sem assunto').substring(0, 60);
                    return `${i + 1}. ${from}: ${subj}`;
                });
                const msg = `${emails.length} e-mails. ${listItems.join('. ')}`;
                hfAddMessage('assistant', msg);
                HF.requestLock = false;
                await hfSpeakAndContinue(msg);
                return;
            }
            case 'HELP': {
                const msg = 'Comandos: "ler e-mail 1", "apagar e-mail 2", "apagar todos", "marcar como lido e-mail 3", "marcar como não lido e-mail 3", "sugerir resposta e-mail 1", "listar e-mails", "mostrar fila", "executar", "limpar fila", "quantos e-mails", "resumo", "triagem", "atualizar". Você também pode digitar no campo abaixo.';
                hfAddMessage('system', msg);
                HF.requestLock = false;
                await hfSpeakAndContinue(msg);
                return;
            }
            case 'REFRESH': {
                hfSetStatus('Atualizando...', 'processing');
                hfSetPhase('executing');
                try {
                    const freshEmails = typeof fetchEmailsIsolated === 'function'
                        ? await fetchEmailsIsolated()
                        : [];
                    if (freshEmails.length > 0) {
                        window.hfSession.emails = freshEmails;
                        window.hfSession.filter = typeof getHomeFilterState === 'function' ? getHomeFilterState() : {};
                    }
                    const msg = `Lista atualizada. ${window.hfSession.emails.length} e-mails.`;
                    hfAddMessage('system', msg);
                    HF.requestLock = false;
                    await hfSpeakAndContinue(msg);
                } catch (e) {
                    HF.requestLock = false;
                    hfForceState('IDLE', 'refresh error');
                    hfSetStatus('Erro ao atualizar.', '');
                }
                return;
            }
            case 'STOP': {
                HF.requestLock = false;
                HF.pendingConfirmation = null;
                hfForceState('IDLE', 'user stop command');
                hfSetStatus('Pronto. Toque no microfone.', '');
                hfSetPhase('idle');
                return;
            }
            case 'REPEAT': {
                if (HF.lastResponse) {
                    HF.requestLock = false;
                    await hfSpeakAndContinue(HF.lastResponse);
                } else {
                    const msg = 'Nada para repetir.';
                    hfAddMessage('system', msg);
                    HF.requestLock = false;
                    hfForceState('IDLE', 'nothing to repeat');
                    hfSetStatus('Pronto.', '');
                }
                return;
            }
            case 'NEXT': {
                HF.requestLock = false;
                hfForceState('IDLE', 'next');
                hfSetStatus('Pronto. Diga o próximo comando.', '');
                hfSetPhase('idle');
                if (HF.mode === 'auto') {
                    setTimeout(() => { if (HF.state === 'IDLE') hfStartListening(); }, HF_AUTO_LISTEN_DELAY_MS);
                }
                return;
            }
            case 'SUMMARY': {
                if (emails.length === 0) {
                    const msg = 'Nenhum e-mail para resumir. Atualize a lista.';
                    hfAddMessage('system', msg);
                    HF.requestLock = false;
                    await hfSpeakAndContinue(msg);
                    return;
                }
                const summaryParts = emails.slice(0, 5).map((e, i) =>
                    `${i + 1}. De ${e.from || '?'}: ${e.subject || 'sem assunto'}`
                );
                const msg = `Resumo dos primeiros ${Math.min(5, emails.length)} e-mails: ${summaryParts.join('. ')}${emails.length > 5 ? `. E mais ${emails.length - 5}.` : ''}`;
                hfAddMessage('assistant', msg);
                HF.requestLock = false;
                await hfSpeakAndContinue(msg);
                return;
            }
            case 'TRIAGE': {
                if (emails.length === 0) {
                    const msg = 'Nenhum e-mail para triagem.';
                    hfAddMessage('system', msg);
                    HF.requestLock = false;
                    await hfSpeakAndContinue(msg);
                    return;
                }
                hfSetStatus('Triagem em andamento...', 'processing');
                hfSetPhase('thinking');
                const keys = emails.map(e => e.key);
                const data = await apiCall('/llm/triage', {
                    method: 'POST',
                    body: JSON.stringify({ session_id: ensureSession(), keys, language: 'pt' })
                });
                if (turn !== HF.activeTurn) { HF.requestLock = false; return; }

                let items;
                if (data.queued && data.job_id) {
                    hfSetStatus('Na fila...', 'processing');
                    const jobResult = await hfPollJob(data.job_id);
                    items = jobResult?.result?.items;
                } else if (data.ok) {
                    items = data.items;
                }

                if (items) {
                    if (typeof window.triageResults !== 'undefined') window.triageResults = items;
                    const high = items.filter(i => i.priority === 'high').length;
                    const med = items.filter(i => i.priority === 'med').length;
                    const low = items.filter(i => i.priority === 'low').length;
                    const msg = `Triagem concluída: ${items.length} e-mails. ${high} urgentes, ${med} médios, ${low} baixos.`;
                    hfAddMessage('system', msg);
                    HF.requestLock = false;
                    await hfSpeakAndContinue(msg);
                } else {
                    const msg = 'Falha na triagem.';
                    hfAddMessage('system', msg);
                    HF.requestLock = false;
                    await hfSpeakAndContinue(msg);
                }
                return;
            }
            case 'LLM_CHAT':
            default: {
                const approvals = ['sim', 'ok', 'aprovado', 'confirmo', 'confirmar', 'executar', 'execute', 'prosseguir', 'pode sim', 'pode fazer', 'pode enviar', 'pode mandar'];
                const lowerText = text.toLowerCase().trim();
                const isApproval = approvals.some(a => lowerText.includes(a)) && lowerText.split(/\s+/).length <= 4;

                if (isApproval && HF.proposedActions.length > 0) {
                    hfSetPhase('executing');
                    hfSetStatus('Executando ações...', 'processing');
                    const result = await hfDispatch(HF.proposedActions);
                    HF.proposedActions = [];
                    HF.requestLock = false;
                    await hfSpeakAndContinue(result);
                    return;
                }

                hfSetStatus('Consultando assistente...', 'processing');
                hfSetPhase('thinking');
                const visibleKeys = emails.map(e => e.key);
                const activeProviders = hfGetActiveProviders();
                const chatData = await apiCall('/llm/chat', {
                    method: 'POST',
                    body: JSON.stringify({
                        session_id: ensureSession(),
                        message: text,
                        visible_keys: visibleKeys,
                        providers: activeProviders,
                    })
                });
                if (turn !== HF.activeTurn) { HF.requestLock = false; return; }

                let answer, proposedActions;
                if (chatData.queued && chatData.job_id) {
                    hfSetStatus('Na fila...', 'processing');
                    const jobResult = await hfPollJob(chatData.job_id);
                    if (jobResult?.ok && jobResult?.result) {
                        answer = jobResult.result.answer || 'Processado.';
                        proposedActions = jobResult.result.proposed_actions || [];
                    } else {
                        const errMsg = jobResult?.error_code === 'rate_limited'
                            ? 'Assistente ocupado. Tente em 30 segundos.'
                            : (jobResult?.message || 'Falha ao processar.');
                        hfAddMessage('assistant', errMsg);
                        HF.requestLock = false;
                        await hfSpeakAndContinue(errMsg);
                        return;
                    }
                } else if (chatData.ok) {
                    answer = chatData.answer;
                    proposedActions = chatData.proposed_actions || [];
                } else {
                    const errMsg = chatData.error_code === 'rate_limited'
                        ? 'Assistente ocupado. Tente em 30 segundos.'
                        : (chatData.answer || chatData.message || 'Erro no processamento.');
                    hfAddMessage('assistant', errMsg);
                    HF.requestLock = false;
                    await hfSpeakAndContinue(errMsg);
                    return;
                }

                hfAddMessage('assistant', answer);
                if (typeof addChatBubble === 'function') addChatBubble('assistant', answer);

                if (proposedActions && proposedActions.length > 0) {
                    HF.proposedActions = HF.proposedActions.concat(proposedActions);
                    window._proposedActions = HF.proposedActions;
                    window._hfProposedActions = HF.proposedActions;
                    if (typeof renderProposedActions === 'function') renderProposedActions(proposedActions);
                    hfAddActionCard(proposedActions);

                    const deleteCount = proposedActions.filter(a => a.action === 'delete').length;
                    if (deleteCount > 0) {
                        HF.pendingConfirmation = { type: 'llm_proposed', actions: proposedActions };
                        if (deleteCount >= 5) {
                            HF.pendingConfirmation.doubleConfirm = true;
                        }
                    }

                    HF.requestLock = false;
                    hfSetPhase('proposing');
                    await hfSpeakAndContinue(answer + `. ${proposedActions.length} ações propostas. Diga "executar" para confirmar ou "cancelar".`);
                } else {
                    HF.requestLock = false;
                    await hfSpeakAndContinue(answer);
                }
                return;
            }
        }
    } catch (err) {
        HF.requestLock = false;
        HF.lastError = err.message;
        hfUpdateDebug();

        if (err.message && (err.message.includes('429') || err.message.includes('rate'))) {
            const msg = 'IA indisponível no momento. Tente novamente em 30 segundos.';
            hfAddMessage('system', msg);
            await hfSpeakAndContinue(msg);
        } else {
            hfAddMessage('system', 'Erro: ' + err.message);
            hfForceState('IDLE', 'process error');
            hfSetStatus('Erro. Toque no microfone.', '');
            hfSetPhase('error');
        }
    }
}

async function hfSpeakAndContinue(text, onDone) {
    if (!text) return;
    try {
        if (HF.mode === 'always_on') hfAntiFeedbackLock();
        await hfSpeak(text);
    } catch (err) {
        console.error("TTS error:", err);
    } finally {
        if (HF.mode === 'always_on') {
            hfAntiFeedbackUnlock();
        } else if (HF.mode === 'auto' && HF.state === 'IDLE') {
            setTimeout(() => {
                if (HF.state === 'IDLE' && !HF.requestLock) {
                    hfStartListening();
                }
            }, HF_AUTO_LISTEN_DELAY_MS);
        }
        if (typeof onDone === "function") {
            onDone();
        }
    }
}

async function hfPollJob(jobId) {
    if (typeof pollJob === 'function') return await pollJob(jobId);
    const maxAttempts = 30;
    for (let i = 0; i < maxAttempts; i++) {
        await new Promise(r => setTimeout(r, 2000));
        try {
            const data = await apiCall(`/llm/job/${jobId}`);
            if (data.status === 'done') return data;
            if (data.status === 'error') return data;
        } catch (e) {}
    }
    return { ok: false, message: 'Timeout polling job' };
}

async function hfDispatch(actions) {
    try {
        const dispatchActions = actions.map(a => ({ key: a.key, action: a.action, body: a.body || null }));

        const data = await apiCall('/llm/dispatch', {
            method: 'POST',
            body: JSON.stringify({
                session_id: ensureSession(),
                actions: dispatchActions,
                mode: 'execute',
                confirm_delete: true,
                context_id: HF.contextId || undefined,
            })
        });

        if (data.ok && data.results) {
            let okCount = 0, failCount = 0;
            const providerStats = {};
            const actionLabels = { send: 'enviado', delete: 'deletado', mark_read: 'marcado lido', mark_unread: 'marcado não lido', skip: 'ignorado' };
            const providerLabels = { apple: 'Apple', gmail: 'Gmail', microsoft: 'Outlook' };
            const failDetails = [];

            for (const r of data.results) {
                const prov = r?.provider || (r?.key ? r.key.split(':')[0] : 'outro');
                if (!providerStats[prov]) providerStats[prov] = { ok: 0, fail: 0, actions: {} };
                if (r && (r.status === 'ok' || r.status === 'skipped')) {
                    okCount++;
                    providerStats[prov].ok++;
                    const al = actionLabels[r.action] || r.action;
                    providerStats[prov].actions[al] = (providerStats[prov].actions[al] || 0) + 1;
                } else {
                    failCount++;
                    providerStats[prov].fail++;
                    failDetails.push(`${providerLabels[prov] || prov}: ${r?.message || 'erro'}`);
                }
            }

            if (typeof removeExecutedEmails === 'function') removeExecutedEmails(data.results);

            const parts = [];
            for (const [prov, stats] of Object.entries(providerStats)) {
                const label = providerLabels[prov] || prov;
                const actionParts = Object.entries(stats.actions).map(([a, c]) => `${c} ${a}${c > 1 ? 's' : ''}`);
                if (stats.fail > 0) actionParts.push(`${stats.fail} falha${stats.fail > 1 ? 's' : ''}`);
                parts.push(`${label}: ${actionParts.join(', ')}`);
            }
            const summary = parts.length > 0 ? parts.join('. ') : `${okCount} ações executadas.`;

            hfSetPhase('done');

            if (failCount > 0) {
                const failInfo = failDetails.length > 0 ? ' Erros: ' + failDetails.join('; ') : '';
                const msg = `${okCount} executadas, ${failCount} falharam. ${summary}${failInfo}`;
                hfAddMessage('system', msg);
                return msg;
            }
            hfAddMessage('system', summary);
            return summary;
        }
        return `Erro: ${data.message || 'erro desconhecido'}`;
    } catch (err) {
        if (!navigator.onLine) {
            return 'Sem conexão. Não é possível executar agora. Reconecte e tente novamente.';
        }
        return `Erro ao executar: ${err.message}`;
    }
}

async function hfSpeak(text) {
    if (!text) {
        if (HF.state === 'PROCESSING') hfForceState('IDLE', 'nothing to speak');
        hfSetPhase('idle');
        return;
    }

    HF.lastResponse = text;

    if (HF.state !== 'PROCESSING' && HF.state !== 'IDLE') {
        return;
    }

    if (HF.state === 'PROCESSING') {
        HF.requestLock = false;
        if (!hfTransition('SPEAKING', 'speak start')) {
            hfForceState('IDLE', 'speak transition failed');
            hfSetPhase('idle');
            return;
        }
    } else {
        hfForceState('SPEAKING', 'repeat');
    }

    const myGen = ++HF.speakGeneration;
    hfSetStatus('Gerando áudio...', 'speaking');
    hfSetPhase('speaking');

    const cleanText = text
        .replace(/[{}[\]"]/g, '')
        .replace(/\n+/g, '. ')
        .replace(/\.{2,}/g, '.')
        .replace(/\s{2,}/g, ' ')
        .trim()
        .substring(0, 4096);

    if (!cleanText) {
        hfForceState('IDLE', 'empty text');
        hfSetPhase('idle');
        return;
    }

    const voice = HF.selectedVoice || 'nova';
    const speed = HF.ttsSpeed || 1.0;

    let instructions = '__none__';
    if (HF.normalizeLangs) {
        instructions = 'Speak in a calm, clear, and natural tone like a professional assistant. ' +
            'Maintain the same pace and energy whether speaking Portuguese or English. ' +
            'Do not speed up or change intonation when switching languages. ' +
            'Keep a steady, warm rhythm throughout. ' +
            'Pronounce Portuguese words with a Brazilian accent. Be concise and articulate.';
    }

    try {
        HF.timings.t_tts_start = hfTimestamp();
        const ttsHeaders = { 'Content-Type': 'application/json' };
        if (typeof apiKey !== 'undefined' && apiKey) ttsHeaders['X-API-Key'] = apiKey;
        const resp = await fetch('/voice/tts', {
            method: 'POST',
            headers: ttsHeaders,
            body: JSON.stringify({ text: cleanText, voice, speed, instructions }),
        });

        if (!resp.ok) throw new Error(`TTS falhou (${resp.status})`);
        if (myGen !== HF.speakGeneration || HF.state !== 'SPEAKING') return;

        const audioBlob = await resp.blob();
        HF.timings.t_tts_end = hfTimestamp();
        if (myGen !== HF.speakGeneration || HF.state !== 'SPEAKING') return;

        const audioUrl = URL.createObjectURL(audioBlob);
        const player = new Audio(audioUrl);
        HF.audioPlayer = player;

        hfSetStatus('Falando... (toque no mic para interromper)', 'speaking');

        hfMuteMic();

        HF.timings.t_audio_play_start = hfTimestamp();

        await new Promise((resolve) => {
            HF.speakAbort = resolve;
            player.onended = () => {
                HF.timings.t_audio_play_end = hfTimestamp();
                URL.revokeObjectURL(audioUrl);
                resolve();
            };
            player.onerror = () => { URL.revokeObjectURL(audioUrl); resolve(); };
            player.play().catch(() => resolve());
        });

        HF.speakAbort = null;
        HF.audioPlayer = null;

        if (HF.state === 'SPEAKING') {
            hfForceState('IDLE', 'speak done');
            hfSetStatus('Pronto. Toque no microfone para falar.', '');
            hfSetPhase('idle');
        }

        hfUpdateDebug();

    } catch (err) {
        console.error('[HF TTS] Error:', err);
        hfMuteMic();
        HF.speakAbort = null;
        if (HF.audioPlayer) {
            try { HF.audioPlayer.pause(); } catch(e) {}
            HF.audioPlayer = null;
        }
        HF.lastError = err.message;
        hfForceState('IDLE', 'tts error');
        hfSetStatus('Erro no áudio. Toque no microfone.', '');
        hfSetPhase('error');
    }
}

function hfStopSpeaking() {
    hfStopVoiceInterrupt();
    hfMuteMic();
    if (HF.speakAbort) {
        HF.speakAbort();
        HF.speakAbort = null;
    }
    if (HF.audioPlayer) {
        try {
            HF.audioPlayer.onended = null;
            HF.audioPlayer.onerror = null;
            HF.audioPlayer.pause();
            if (HF.audioPlayer.src && HF.audioPlayer.src.startsWith('blob:')) {
                URL.revokeObjectURL(HF.audioPlayer.src);
            }
        } catch(e) {}
        HF.audioPlayer = null;
    }
}

function hfStartVoiceInterrupt() {
    hfStopVoiceInterrupt();
    if (!HF.stream) return;
    try {
        if (!HF.audioCtx || HF.audioCtx.state === 'closed') {
            HF.audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        }
        HF.interruptSource = HF.audioCtx.createMediaStreamSource(HF.stream);
        HF.interruptAnalyser = HF.audioCtx.createAnalyser();
        HF.interruptAnalyser.fftSize = 512;
        HF.interruptSource.connect(HF.interruptAnalyser);
    } catch(e) { return; }

    const dataArray = new Uint8Array(HF.interruptAnalyser.frequencyBinCount);
    let consecutiveHits = 0;

    HF.interruptMonitorInterval = setInterval(() => {
        if (HF.state !== 'SPEAKING' || !HF.interruptAnalyser) {
            hfStopVoiceInterrupt();
            return;
        }
        HF.interruptAnalyser.getByteFrequencyData(dataArray);
        const avg = dataArray.reduce((a, b) => a + b, 0) / dataArray.length;

        if (avg > HF_SOUND_THRESHOLD * 2) {
            consecutiveHits++;
            if (consecutiveHits >= 3) {
                console.log('[HF] Voice interrupt detected');
                hfStopSpeaking();
                hfForceState('IDLE', 'voice interrupt');
                hfSetStatus('Interrompido. Toque no mic para falar.', '');
            }
        } else {
            consecutiveHits = 0;
        }
    }, 100);
}

function hfStopVoiceInterrupt() {
    if (HF.interruptMonitorInterval) { clearInterval(HF.interruptMonitorInterval); HF.interruptMonitorInterval = null; }
    if (HF.interruptSource) {
        try { HF.interruptSource.disconnect(); } catch(e) {}
        HF.interruptSource = null;
    }
    HF.interruptAnalyser = null;
}

window.hfApproveOneAction = hfApproveOneAction;
window.hfDispatch = hfDispatch;

document.addEventListener('DOMContentLoaded', () => {
    hfInit();
});
