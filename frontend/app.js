const { createApp, ref, computed, onMounted, onUnmounted, watch } = Vue;
const localInput = date => new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
const fmt = (value, precision = 5) => {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'boolean') return value ? 'TRUE' : 'FALSE';
  const digits = Number.isInteger(precision) ? Math.max(0, Math.min(10, precision)) : 5;
  const number = Number(value);
  if (!Number.isFinite(number)) return '—';
  return number !== 0 && Math.abs(number) < 10 ** -digits ? number.toExponential(digits)
    : number.toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits });
};
const localTime = value => value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—';
const seriesGroups = data => data?.series || (data?.items ? [{ ...data, items: data.items }] : []);

const HistoryChart = {
  props: ['items', 'unit', 'large', 'start', 'end'],
  setup(props) {
    const chart = computed(() => IAGHistory.buildSeriesChart(props.items, props.start, props.end));
    return { chart, fmt, points: segment => segment.map(point => `${point.x},${point.y}`).join(' '),
      tickTime: timestamp => new Date(timestamp).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }) };
  },
  template: `<div class="chart" :class="{large}"><div v-if="!chart" class="empty">所选范围暂无有效历史数据</div><svg v-else viewBox="0 0 600 210" role="img" :aria-label="'历史曲线 '+(unit||'')"><text x="12" y="13">{{unit}}</text><g v-for="tick in chart.ticks"><line x1="62" :y1="tick.y" x2="566" :y2="tick.y" stroke="#e8eef1" stroke-dasharray="3 4"/><text x="54" :y="tick.y+3" text-anchor="end">{{fmt(tick.value)}}</text></g><g v-for="range in chart.ranges"><line :x1="range.x" :x2="range.x" :y1="range.y1" :y2="range.y2" stroke="#6cbcae" stroke-width="2"/></g><g v-for="segment in chart.segments"><polyline :points="points(segment)" fill="none" stroke="#329c8a" stroke-width="2.2" stroke-linejoin="round"/><circle v-if="segment.length===2" :cx="segment[0].x" :cy="segment[0].y" r="2.5" fill="#329c8a"/></g><g v-for="tick in chart.times"><text :x="tick.x" y="198" text-anchor="middle">{{tickTime(tick.timestamp)}}</text></g></svg></div>`
};

createApp({
  components: { HistoryChart },
  setup() {
    const api = IAGApi.createApiClient(), historyService = IAGHistory.createHistoryService(api);
    const { json, managementHeaders } = IAGApi;
    const nav = [{ id: 'overview', title: '设备总览', icon: '▦' }, { id: 'connection', title: 'PLC连接', icon: '⌁' },
      { id: 'tags', title: '点位管理', icon: '⊞' }, { id: 'realtime', title: '实时监控', icon: '∿' },
      { id: 'history', title: '历史曲线', icon: '◷' }, { id: 'ai', title: '按需数据查询', icon: '✧' },
      { id: 'model', title: '模型设置', icon: '◇' }, { id: 'diagnostics', title: '运行诊断', icon: '⚙' }];
    const page = ref('overview'), current = ref({ items: [], mode: 'simulation', total: 0, good: 0, scan_ms: 0 });
    const config = ref({ poll_interval: 1, batch_size: 100, heartbeat_seconds: 1800, retention_days: 7 });
    const connection = ref({ mode: 'simulation', endpoint: 'opc.tcp://127.0.0.1:4840', username: '', security_string: '', password_env: '', clear_username: false, clear_security_string: false,
      poll_interval: 1, batch_size: 100, heartbeat_seconds: 1800, retention_days: 7 });
    const tags = ref([]), search = ref(''), tagPage = ref(1), error = ref(''), message = ref(''), clock = ref('');
    const activity = ref(0), busy = computed(() => activity.value > 0), managerPin = ref(''), tagETag = ref(null), configETag = ref(null), connectionDraftETag = ref(null);
    const overviewDevice = ref(''), keyTagIds = ref([]), overviewTagId = ref(''), overviewHistory = ref({ series: [] }), overviewError = ref('');
    const historyCatalog = ref([]), historyData = ref({ items: [] }), historySeries = ref({ series: [] }), historySelection = ref(null);
    const historyForm = ref({ device: '', variableKey: '', start: localInput(new Date(Date.now() - 86400000)), end: localInput(new Date()), source: 'simulation' });
    const historyLoading = ref(false), historyError = ref('');
    const aiForm = ref({ device: '', variable: '', question: '' }), aiResult = ref(null), aiStatus = ref(null);
    const modelDefaults = { provider: 'local_rules', base_url: 'http://127.0.0.1:11434', model: '', timeout_seconds: 60, max_output_tokens: 1024, temperature: 0.2 };
    const modelConfig = ref({ ...modelDefaults, api_key_set: false }), modelLoaded = ref(false);
    const modelForm = ref({ ...modelDefaults, api_key: '', clear_api_key: false }), modelETag = ref(null), modelDraftETag = ref(null);
    const modelDraftInitialized = ref(false), modelTest = ref(null);
    const modelProviders = { local_rules: '本地规则统计', ollama: 'Ollama模型', openai_compatible: 'OpenAI兼容模型' };
    const modelLabel = computed(() => !modelLoaded.value ? '等待读取模型配置'
      : `${modelProviders[modelConfig.value.provider] || '未知服务'}${modelConfig.value.model && modelConfig.value.provider !== 'local_rules' ? ' · ' + modelConfig.value.model : ''}`);
    const usesModel = computed(() => modelLoaded.value && modelConfig.value.provider !== 'local_rules');
    const remoteModel = computed(() => modelConfig.value.deployment === 'remote_server');
    const editing = ref(null), editingOriginal = ref(null), editingETag = ref(null), modalError = ref(''), importPreview = ref(null);
    const writeTag = ref(null), writePin = ref(''), writeValue = ref(''), writeProposal = ref(null), writeText = ref('');
    const connectionTest = ref(null), validation = ref(null), diagnostics = ref(null), readiness = ref(null);
    const shutdownRequested = ref(false);
    const editFields = [{ key: 'name', label: '变量名称', required: true },
      { key: 'address', label: 'PLC原始地址（用于识别）', required: true, identity: true }, { key: 'device', label: '设备', required: true, identity: true }, { key: 'unit', label: '单位' },
      { key: 'threshold', label: '变化保存阈值（绝对差值）', type: 'number', min: 0, required: true },
      { key: 'history_interval_seconds', label: '定时保存间隔（秒，留空继承全局）', type: 'number', min: 0.001, placeholder: '留空使用全局默认值' },
      { key: 'precision', label: '显示小数位数（默认5位）', type: 'number', min: 0, max: 10, step: 1, required: true },
      { key: 'ai_description', label: 'AI地址注释' }, { key: 'node_id', label: 'OPC UA NodeId（实际读取地址）', identity: true }];
    const pageTitle = computed(() => nav.find(item => item.id === page.value)?.title);
    const descriptions = { overview: '按设备选择关键参数，查看完整时间范围的趋势。', connection: '在这里修改通信地址、轮询读取周期、批次和全局历史策略。',
      tags: '管理变量定义、读取权限与历史保存策略。', realtime: '查看最新设备数据、通讯质量与数据时间。',
      history: '按连接和变量版本追溯历史，保留退役点位记录。', ai: '只在提交问题后查询所选设备数据。',
      model: '选择本地规则、本机模型或兼容API，测试后保存。', diagnostics: '检查采集、历史存储、队列和磁盘状态。' };
    const pageDescription = computed(() => page.value === 'ai' ? `${descriptions.ai} 当前：${modelLabel.value}。` : descriptions[page.value]);
    const devices = computed(() => [...new Set(tags.value.map(tag => tag.device))]);
    const overviewTags = computed(() => tags.value.filter(tag => tag.device === overviewDevice.value));
    const overviewSensor = computed(() => overviewTags.value.find(tag => tag.id === Number(overviewTagId.value)));
    const keySensors = computed(() => current.value.items.filter(tag => tag.device === overviewDevice.value && keyTagIds.value.includes(tag.id)));
    const overviewSeriesGroups = computed(() => seriesGroups(overviewHistory.value));
    const historySeriesGroups = computed(() => seriesGroups(historySeries.value));
    const historyDevices = computed(() => [...new Set(historyCatalog.value.map(row => row.device))]);
    const historyVariables = computed(() => historyCatalog.value.filter(row => row.device === historyForm.value.device));
    const variableLabel = row => `${row.name} · ${row.unit || '-'} · ${row.active ? '活动' : '历史/退役'} · 连接 ${row.connection_id ?? '-'} / 版本 ${row.tag_revision ?? '-'}`;
    const matches = tag => `${tag.name} ${tag.address} ${tag.device} ${tag.id}`.toLowerCase().includes(search.value.toLowerCase());
    const filteredTags = computed(() => tags.value.filter(matches)), tagPages = computed(() => Math.max(1, Math.ceil(filteredTags.value.length / 50)));
    const pagedTags = computed(() => filteredTags.value.slice((tagPage.value - 1) * 50, tagPage.value * 50));
    const tagRowNumber = index => (tagPage.value - 1) * 50 + index + 1;
    const filteredCurrent = computed(() => current.value.items.filter(matches));
    const currentPages = computed(() => Math.max(1, Math.ceil(filteredCurrent.value.length / 50)));
    const pagedCurrent = computed(() => filteredCurrent.value.slice((tagPage.value - 1) * 50, tagPage.value * 50));
    const canWrite = computed(() => IAGRealtime.canWrite(current.value));
    const showManagement = computed(() => ['tags', 'connection', 'diagnostics', 'model', 'ai'].includes(page.value));
    watch(search, () => { tagPage.value = 1; });
    watch(() => historyForm.value.device, reconcileHistoryVariable);
    watch(overviewDevice, reconcileOverview);
    watch(overviewTagId, () => { overviewHistory.value = { series: [] }; if (page.value === 'overview') loadOverview(); });
    watch(canWrite, allowed => { if (!allowed) closeWrite(); });
    watch(() => aiForm.value.device, () => { aiForm.value.variable = ''; });

    async function action(fn, modal = false) {
      activity.value++; if (modal) modalError.value = ''; else error.value = '';
      try { return await fn(); }
      catch (failure) { if (failure.name !== 'AbortError') { if (modal) modalError.value = failure.message; else error.value = failure.message; } }
      finally { activity.value--; }
    }
    function reconcileOverview() {
      const ids = overviewTags.value.map(tag => tag.id);
      keyTagIds.value = keyTagIds.value.filter(id => ids.includes(id));
      if (!keyTagIds.value.length) keyTagIds.value = ids.slice(0, 3);
      if (!ids.includes(Number(overviewTagId.value))) overviewTagId.value = ids[0] ?? '';
    }
    function reconcileHistoryVariable() {
      if (!historyVariables.value.some(row => IAGHistory.variableKey(row) === historyForm.value.variableKey)) {
        historyForm.value.variableKey = historyVariables.value[0] ? IAGHistory.variableKey(historyVariables.value[0]) : '';
      }
    }
    async function loadTags() {
      const result = await api.requestResult('/api/tags', { channel: 'tags' });
      tags.value = result.data; tagETag.value = result.etag;
      tagPage.value = Math.min(tagPage.value, tagPages.value);
      if (!devices.value.includes(overviewDevice.value)) overviewDevice.value = devices.value[0] || '';
      if (!devices.value.includes(aiForm.value.device)) aiForm.value.device = devices.value[0] || '';
      reconcileOverview();
    }
    async function loadConfig() {
      const result = await api.requestResult('/api/config', { channel: 'config' });
      config.value = result.data; configETag.value = result.etag;
    }
    async function loadModelConfig(resetDraft = false) {
      try {
        const result = await api.requestResult('/api/ai/config', { channel: 'model-config' });
        modelConfig.value = result.data; modelETag.value = result.etag; modelLoaded.value = true;
        if (resetDraft) {
          modelForm.value = { ...modelDefaults, ...result.data, api_key: '', clear_api_key: false };
          modelDraftETag.value = result.etag; modelDraftInitialized.value = true; modelTest.value = null;
        }
      } catch (failure) { if (failure.name !== 'AbortError') modelLoaded.value = false; throw failure; }
    }
    async function refresh() {
      await action(async () => {
        const results = await Promise.allSettled([loadTags(), loadConfig()]);
        if (['model', 'ai'].includes(page.value)) await loadModelConfig(false);
        const failed = results.find(result => result.status === 'rejected' && result.reason.name !== 'AbortError');
        if (page.value === 'overview') await loadOverview();
        if (failed) throw failed.reason;
      });
    }
    async function loadOverview() {
      const sensor = overviewSensor.value;
      if (!sensor || !current.value.connection_id) { api.cancel('overview-series'); overviewHistory.value = { series: [] }; return; }
      try {
        const end = new Date(), start = new Date(end.getTime() - 86400000);
        const snapshotTag = current.value.items.find(tag => tag.id === sensor.id) || sensor;
        overviewHistory.value = await historyService.overview({ tag_id: sensor.id, device: sensor.device, source: current.value.mode,
          connection_id: current.value.connection_id, tag_revision: snapshotTag.revision,
          start: start.toISOString(), end: end.toISOString() });
        overviewError.value = '';
      } catch (failure) { if (failure.name !== 'AbortError') { overviewError.value = `趋势查询失败：${failure.message}`; overviewHistory.value = { series: [] }; } }
    }
    function candidateConnection() {
      const candidate = { mode: connection.value.mode, endpoint: connection.value.endpoint };
      for (const field of ['username', 'security_string', 'password_env']) if (connection.value[field]) candidate[field] = connection.value[field];
      if (connection.value.clear_username) candidate.username = '';
      if (connection.value.clear_security_string) candidate.security_string = '';
      for (const field of ['poll_interval', 'batch_size', 'heartbeat_seconds', 'retention_days']) candidate[field] = Number(connection.value[field]);
      return candidate;
    }
    async function navigate(id) {
      if (page.value === 'model' && id !== 'model') modelForm.value.api_key = '';
      page.value = id; search.value = ''; tagPage.value = 1; error.value = ''; message.value = '';
      if (id === 'connection') {
        connection.value = { mode: config.value.mode || current.value.mode, endpoint: config.value.endpoint || current.value.endpoint,
          username: '', security_string: '', password_env: '', clear_username: false, clear_security_string: false, poll_interval: config.value.poll_interval ?? 1, batch_size: config.value.batch_size ?? 100,
          heartbeat_seconds: config.value.heartbeat_seconds ?? 1800, retention_days: config.value.retention_days ?? 7 };
        connectionDraftETag.value = configETag.value;
      }
      if (id === 'history') {
        historyForm.value.source = current.value.mode; historyForm.value.end = localInput(new Date());
        await refreshHistoryCatalog(); if (historyForm.value.variableKey) await submitHistory();
      }
      if (id === 'overview') await loadOverview();
      if (id === 'diagnostics') await loadDiagnostics();
      if (id === 'model') await action(() => loadModelConfig(!modelDraftInitialized.value));
      if (id === 'ai') await action(() => loadModelConfig(false));
    }
    function selectModelProvider() {
      modelTest.value = null;
      if (modelForm.value.provider === 'ollama') modelForm.value.base_url = 'http://127.0.0.1:11434';
      else if (modelForm.value.provider === 'openai_compatible') modelForm.value.base_url = 'http://127.0.0.1:1234/v1';
      else modelForm.value.base_url = 'http://127.0.0.1:11434';
      modelForm.value.api_key = ''; modelForm.value.clear_api_key = false;
    }
    function candidateModel() {
      return { provider: modelForm.value.provider, base_url: modelForm.value.base_url, model: modelForm.value.model,
        timeout_seconds: Number(modelForm.value.timeout_seconds), max_output_tokens: Number(modelForm.value.max_output_tokens),
        temperature: Number(modelForm.value.temperature), api_key: modelForm.value.api_key, clear_api_key: modelForm.value.clear_api_key };
    }
    async function reloadModel() { await action(() => loadModelConfig(true)); }
    async function saveModel() {
      await action(async () => {
        try {
          await api.request('/api/ai/config', json('POST', candidateModel(), managementHeaders(managerPin.value, modelDraftETag.value)));
          await loadModelConfig(true); aiResult.value = null; message.value = '模型设置已保存。只有提交问题时才会调用模型。';
        } catch (failure) {
          if (failure.status === 412) failure.message += ' 模型草稿已保留；可放弃草稿并载入已保存配置后重新修改。';
          throw failure;
        }
      });
    }
    async function testModel() {
      await action(async () => {
        modelTest.value = null;
        modelTest.value = await api.request('/api/ai/test', { ...json('POST', candidateModel(), managementHeaders(managerPin.value)),
          timeout: (Number(modelForm.value.timeout_seconds) || 60) * 1000 + 10000 });
      });
    }
    async function saveConnection() {
      await action(async () => {
        const result = await api.request('/api/connection', json('POST', candidateConnection(), managementHeaders(managerPin.value, connectionDraftETag.value)));
        message.value = result.message; closeWrite(); await loadConfig(); connectionDraftETag.value = configETag.value;
      });
    }
    async function reloadConnection() { await action(async () => { await loadConfig(); await navigate('connection'); connectionTest.value = null; }); }
    async function testConnection() {
      await action(async () => { connectionTest.value = await api.request('/api/connection/test', { ...json('POST', candidateConnection(), managementHeaders(managerPin.value)), timeout: 20000 }); });
    }
    async function validateTags() {
      await action(async () => { validation.value = await api.request('/api/tags/validate', { ...json('POST', { items: tags.value, ...(page.value === 'connection' ? { connection: candidateConnection() } : {}) }, managementHeaders(managerPin.value)), timeout: 20000 }); });
    }
    async function importTags(event) {
      const file = event.target.files[0]; event.target.value = ''; if (!file) return;
      await action(async () => {
        const form = new FormData(); form.append('file', file);
        const result = await api.requestResult('/api/tags/import?dry_run=true', { method: 'POST', body: form, headers: managementHeaders(managerPin.value) });
        importPreview.value = { ...result.data, file, etag: result.etag }; modalError.value = '';
      });
    }
    async function applyImport() {
      await action(async () => {
        const preview = importPreview.value, form = new FormData(); form.append('file', preview.file);
        const result = await api.request('/api/tags/import', { method: 'POST', body: form, headers: managementHeaders(managerPin.value, preview.etag) });
        importPreview.value = null; await loadTags(); message.value = `已导入 ${result.count} 个点位。`;
      }, true);
    }
    async function editTag(tag) {
      editingOriginal.value = tag?.id || null; editingETag.value = tagETag.value;
      editing.value = tag ? { history_interval_seconds: null, precision: 5, record_changes: true, ...tag } : { id: null, address: '', name: '',
        device: devices.value[0] || '', type: 'FLOAT', unit: '-', permission: 'READ', ai_description: '', save: true, threshold: 0, node_id: '',
        history_interval_seconds: null, precision: 5, record_changes: false };
      modalError.value = '';
      if (!tag) await allocateTagId();
    }
    async function copyTag() {
      const draft = { ...editing.value, id: null };
      delete draft.revision;
      editingOriginal.value = null; editingETag.value = tagETag.value; editing.value = draft; modalError.value = '';
      await allocateTagId();
    }
    async function allocateTagId() {
      await action(async () => {
        if (!editing.value || editingOriginal.value) return;
        const draft = editing.value, etag = editingETag.value;
        try {
          if (!etag) throw new Error('尚未读取点位表版本，请载入最新版本后重新添加。');
          const result = await api.requestResult('/api/tags/next-id', { channel: 'tag-next-id' });
          if (result.etag !== etag) throw new Error('点位表已被其他页面修改，请载入最新版本后重新添加。');
          if (!Number.isSafeInteger(result.data.next_id) || result.data.next_id < 1) throw new Error('后台未返回有效的新点位ID，请重试。');
          draft.id = result.data.next_id;
        } catch (failure) {
          if (failure.name === 'AbortError') throw failure;
          throw new Error(`获取新点位ID失败：${failure.message} 草稿已保留。`);
        }
      }, true);
    }
    async function reloadEditing() {
      await action(async () => { await loadTags(); const tag = tags.value.find(item => item.id === editingOriginal.value);
        if (editingOriginal.value && !tag) throw new Error('该点位已被删除，请关闭草稿后重新添加。');
        await editTag(tag); }, true);
    }
    async function saveTag() {
      await action(async () => {
        if (!Number.isSafeInteger(Number(editing.value?.id)) || Number(editing.value?.id) < 1) {
          throw new Error('尚未获取新点位ID，请点击“重新获取ID”后再保存。草稿已保留。');
        }
        const tag = { ...editing.value, id: Number(editing.value.id), threshold: Number(editing.value.threshold), precision: Number(editing.value.precision),
          history_interval_seconds: editing.value.history_interval_seconds === '' || editing.value.history_interval_seconds == null
            ? null : Number(editing.value.history_interval_seconds) };
        const next = editingOriginal.value ? tags.value.map(item => item.id === editingOriginal.value ? tag : item) : [...tags.value, tag];
        await api.request('/api/tags', json('PUT', next, managementHeaders(managerPin.value, editingETag.value)));
        await loadTags(); editing.value = null; message.value = '点位已保存';
      }, true);
    }
    async function removeTag(tag) {
      const etag = tagETag.value, next = tags.value.filter(item => item.id !== tag.id);
      await action(async () => { if (!confirm(`删除点位「${tag.name}」？历史仍保留。`)) return;
        await api.request('/api/tags', json('PUT', next, managementHeaders(managerPin.value, etag)));
        await loadTags(); message.value = '点位已删除'; });
    }
    async function refreshHistoryCatalog() {
      historyError.value = '';
      try {
        const result = await historyService.catalog(historyForm.value.source); historyCatalog.value = result.items || [];
        if (!historyDevices.value.includes(historyForm.value.device)) historyForm.value.device = historyDevices.value[0] || '';
        reconcileHistoryVariable();
      } catch (failure) { if (failure.name !== 'AbortError') historyError.value = failure.message; }
    }
    let historyAction = 0;
    async function submitHistory() {
      const sequence = ++historyAction; historyLoading.value = true; historyError.value = '';
      try {
        const selected = IAGHistory.makeSelection(historyForm.value, historyCatalog.value);
        const result = await historyService.submit(selected);
        if (sequence === historyAction) { historySelection.value = result.selection; historyData.value = result.detail; historySeries.value = result.series; }
      } catch (failure) { if (sequence === historyAction && failure.name !== 'AbortError') historyError.value = failure.message; }
      finally { if (sequence === historyAction) historyLoading.value = false; }
    }
    async function pageHistory(offset) {
      const sequence = ++historyAction; historyLoading.value = true; historyError.value = '';
      try { const result = await historyService.page(offset); if (sequence === historyAction) { historyData.value = result.detail; historySelection.value = result.selection; } }
      catch (failure) { if (sequence === historyAction && failure.name !== 'AbortError') historyError.value = failure.message; }
      finally { if (sequence === historyAction) historyLoading.value = false; }
    }
    async function askAI() {
      await action(async () => {
        aiResult.value = null;
        if (!modelLoaded.value) throw new Error('尚未载入模型配置，请刷新页面后再提交查询。');
        const headers = usesModel.value ? managementHeaders(managerPin.value, modelETag.value)
          : { ...(managerPin.value.trim() ? managementHeaders(managerPin.value) : {}), ...(modelETag.value ? { 'If-Match': modelETag.value } : {}) };
        aiResult.value = await api.request('/api/ai/query', { ...json('POST', { ...aiForm.value, variable: aiForm.value.variable || null }, headers),
          channel: 'ai-query', timeout: usesModel.value ? (Number(modelConfig.value.timeout_seconds) || 60) * 1000 + 10000 : 15000 });
      });
    }
    async function getStatus() { await action(async () => { aiStatus.value = await api.request('/api/ai/status?' + new URLSearchParams({ device: aiForm.value.device }), { channel: 'ai-status' }); }); }
    async function loadDiagnostics() {
      await action(async () => {
        const results = await Promise.allSettled([
          api.request('/api/diagnostics', { channel: 'diagnostics', headers: managerPin.value ? managementHeaders(managerPin.value) : {} }).then(data => { diagnostics.value = data; }),
          api.request('/api/ready', { channel: 'readiness', acceptedStatuses: [503] }).then(data => { readiness.value = data; })
        ]);
        const failed = results.find(result => result.status === 'rejected' && result.reason.name !== 'AbortError');
        if (failed) throw failed.reason;
      });
    }
    function openWrite(tag) {
      if (!canWrite.value || tag.permission !== 'WRITE') return;
      writeTag.value = tag; writeValue.value = tag.type === 'BOOL' ? 'true' : String(tag.value ?? 0);
      writePin.value = ''; writeProposal.value = null; writeText.value = ''; modalError.value = '';
    }
    function closeWrite() { writeTag.value = null; writePin.value = ''; writeProposal.value = null; writeText.value = ''; }
    async function proposeWrite() {
      await action(async () => { if (!canWrite.value) throw new Error('当前模式禁止写入。');
        const value = writeTag.value.type === 'BOOL' ? writeValue.value === 'true' : Number(writeValue.value);
        writeProposal.value = await api.request('/api/operator/write-request', json('POST', { tag_id: writeTag.value.id, value }, managementHeaders(writePin.value))); }, true);
    }
    async function confirmWrite() {
      await action(async () => { if (!canWrite.value) throw new Error('当前模式禁止写入。');
        const result = await api.request('/api/operator/write-confirm', json('POST', { request_id: writeProposal.value.request_id, confirmation: writeText.value }, managementHeaders(writePin.value)));
        message.value = result.message; closeWrite(); }, true);
    }
    const poller = IAGRealtime.createRealtimePoller(api, {
      onData(data) {
        const changed = current.value.connection_id !== data.connection_id || current.value.mode !== data.mode;
        current.value = data;
        if (changed) { closeWrite(); overviewHistory.value = { series: [] }; if (page.value === 'overview') loadOverview(); }
      },
      onError(failure) { current.value = IAGRealtime.staleSnapshot(current.value, `无法获取实时数据：${failure.message}`); closeWrite(); }
    });
    let overviewTimer, clockTimer;
    async function shutdownGateway() {
      if (!managerPin.value) {
        error.value = '退出需要本机管理口令。请在PLC连接页填写，或运行发行包中的“停止软件.cmd”。';
        return;
      }
      if (!confirm('停止采集并退出软件？已保存的数据会保留。')) return;
      await action(async () => {
        const result = await api.request('/api/shutdown', json('POST', {}, managementHeaders(managerPin.value)));
        shutdownRequested.value = true;
        poller.stop(); clearInterval(overviewTimer); clearInterval(clockTimer); api.cancelAll();
        current.value = { ...current.value, connected: false, state: 'stopping', error: '' };
        message.value = result.message + ' 重新使用时请双击 IndustrialAIGateway.exe。';
        managerPin.value = '';
      });
    }
    onMounted(async () => {
      poller.start(); clock.value = localTime(new Date());
      clockTimer = setInterval(() => { clock.value = localTime(new Date()); }, 1000);
      overviewTimer = setInterval(() => { if (page.value === 'overview') loadOverview(); }, 30000);
      await refresh();
    });
    onUnmounted(() => { poller.stop(); clearInterval(overviewTimer); clearInterval(clockTimer); api.cancelAll(); managerPin.value = ''; modelForm.value.api_key = ''; });
    return { nav, page, current, config, connection, tags, search, tagPage, error, message, busy, clock, managerPin, showManagement,
      overviewDevice, overviewTags, keyTagIds, overviewTagId, overviewSensor, overviewHistory, overviewError, overviewSeriesGroups,
      historyData, historySeries, historySeriesGroups, historySelection, historyForm, historyDevices, historyVariables, historyLoading, historyError,
      aiForm, aiResult, aiStatus, editing, editingOriginal, modalError, importPreview, writeTag, writePin, writeValue, writeProposal, writeText,
      modelConfig, modelForm, modelLoaded, modelLabel, usesModel, remoteModel, modelTest, modelDraftInitialized,
      connectionTest, validation, diagnostics, readiness, editFields, pageTitle, pageDescription, devices, keySensors, filteredTags, pagedTags,
      tagPages, tagRowNumber, pagedCurrent, currentPages, canWrite, fmt, localTime, variableKey: IAGHistory.variableKey, variableLabel,
      navigate, refresh, saveConnection, reloadConnection, testConnection, validateTags, importTags, applyImport, editTag, copyTag, allocateTagId, reloadEditing, saveTag, removeTag,
      shutdownGateway, shutdownRequested,
      selectModelProvider, reloadModel, saveModel, testModel,
      refreshHistoryCatalog, submitHistory, pageHistory, askAI, getStatus, loadDiagnostics, openWrite, closeWrite, proposeWrite, confirmWrite };
  }
}).mount('#app');
